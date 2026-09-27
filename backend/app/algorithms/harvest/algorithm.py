"""Sugarcane SAR harvest detection (GEE) - runnable wrapper.

Simplified user flow (see adapter.py for the schema):

  params:
    gee_project_id            GEE project id (for ee.Initialize(project=...))
    year                      target season year (int)
    sugarcane_mask_local_path local 1-band TIFF (1 = sugarcane, 0 = other)
    sugarcane_mask_asset      optional, GEE Asset path of the sugarcane mask;
                              use this when the local mask is too large to
                              construct an ee.Image from directly

Date windows are derived automatically from `year`:

    base_start     = f"{year}-10-01"
    base_end       = f"{year}-11-30"
    current_start  = f"{year}-12-01"
    current_end    = f"{year + 1}-04-21"

ROI source
----------
There is no separate research-area input. The sugarcane mask's own
extent serves as the SAR analysis region — `filterBounds()` of the S1
collection and `region=` of the export are both derived from the mask.
This matches the original algorithm's intent (analyse only where
sugarcane grows) and removes one redundant user input.

Mask handling
-------------
Two paths are supported:

  * Local TIFF (preferred for typical city / county scale masks):
    the file is read with rasterio into a uint8 numpy array; an
    `ee.Image` is constructed via `ee.Image(arr).reproject(crs="EPSG:4326", ...)`
    using the file's bounds / shape / transform.

  * GEE Asset (escape hatch for very large masks that would be costly
    to ship as a numpy array): the user provides `sugarcane_mask_asset`
    and we just call `ee.Image(asset_id)`.

Delivery
---------
Submit `Export.image.toAsset` (not `toDrive`), wait for the asset to
become queryable, then download it into the job's working directory
via `geemap.ee_export_image`, so the platform's ResultRenderer can
preview / download the produced GeoTIFF directly.

The original Sentinel-1 logic — focal_median smoothing, linear-scale
backscatter conversion, RVI / NDPI / VH-texture, base vs current
aggregation (median / min), and the SAR-based harvest mask — is kept
verbatim. RVI thresholds / scale / CRS are hard-coded defaults that
match the prior hard-coded values.

Progress messages are short, user-facing Chinese strings — never raw
Python / GEE stack traces (per spec).
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

import ee

from app.algorithms.base import ProgressCallback, make_card, make_result
from app.services.gee_auth import GeeAuthRequired, initialize_ee


# 固定算法参数（用户不动时行为与原硬编码完全一致）。
_DEFAULT_RVI_ABSOLUTE_LOW = 0.6
_DEFAULT_RVI_DROP_MIN = 0.2
_DEFAULT_SCALE = 10
_DEFAULT_CRS = "EPSG:4326"
_DEFAULT_MAX_PIXELS = int(1e13)

# 本地甘蔗掩膜 TIFF 的分块读取参数。
# 单次 ee.Image(arr).reproject(...) 把整张 numpy 数组作为「常量像素瓦片」
# 提交到 GEE，过大的 payload 会被服务端拒绝。改为按瓦片分块提交再 mosaic：
#
#   * _LOCAL_MASK_TILE_PIXELS — 每个源像素瓦片边长（像素数）。
#     4000x4000 uint8 ≈ 16 MB 单瓦片，落在 GEE 单次请求安全区内。
#   * _LOCAL_MASK_MAX_TILES — 最多允许的总瓦片数；超出后必须改用
#     GEE Asset（sugarcane_mask_asset 高级参数），提示信息里也写明。
_LOCAL_MASK_TILE_PIXELS = 4000
_LOCAL_MASK_MAX_TILES = 5000
# SAR 计算 ROI 的外扩 buffer（米）。
# `_calculate_sar_metrics` 用了 30 m 圆形核（focal_median /
# reduceNeighborhood），若 ROI 紧贴甘蔗像素，最外圈 1-2 个像素
# （~30 m）会受边界 padding（默认按 0 处理）影响。
# 外扩 2 km 足以让 30 m 核始终在真实 SAR 数据上滑动，对实际甘蔗像素
# 的检测结果零影响；同时相比「整个掩膜 bbox」ROI 可大幅缩小 SAR
# 分析区域和导出体积。
_ROI_BUFFER_M = 2000


def _initialize_ee(project: Optional[str]) -> None:
    """委托给 app.services.gee_auth —— 项目不匹配时抛 GeeAuthRequired。"""
    initialize_ee(project)


def _calculate_sar_metrics(image: ee.Image) -> ee.Image:
    """Original Sentinel-1 metric function: RVI / NDPI / VH texture.

    RVI and NDPI use linear-scale backscatter; texture is computed on the
    raw dB VH band inside a 30 m circular kernel.
    """
    smoothed = image.focal_median(30, "circle", "meters")

    linear_vv = ee.Image(10).pow(smoothed.select("VV").divide(10))
    linear_vh = ee.Image(10).pow(smoothed.select("VH").divide(10))

    rvi = linear_vh.multiply(4).divide(linear_vv.add(linear_vh)).rename("RVI")
    ndpi = linear_vv.subtract(linear_vh).divide(linear_vv.add(linear_vh)).rename("NDPI")

    vh_texture = image.select("VH").reduceNeighborhood(
        reducer=ee.Reducer.variance(),
        kernel=ee.Kernel.circle(30, "meters"),
    ).rename("Texture")

    return ee.Image.cat([rvi, ndpi, vh_texture]).copyProperties(image, ["system:time_start"])


def _wait_for_task(
    task,
    *,
    timeout: float = 7200.0,
    poll: float = 10.0,
    progress_callback=None,
) -> None:
    """Poll `task.status()` until the export task is COMPLETED.

    Replaces the old `_wait_for_asset` (which polled `ee.data.getAsset`
    and returned the instant the asset placeholder existed — even when
    the underlying export was still RUNNING, which caused premature
    timeouts for large-area SAR exports such as a 850 km × 560 km ROI
    at 10 m resolution).

    Args:
        task: an `ee.batch.Task` returned by `task.start()`.
        timeout: max seconds to wait before raising TimeoutError.
        poll: seconds between status polls.
        progress_callback: optional callable(percent, message) for live
            progress updates while waiting.

    Raises:
        RuntimeError: on FAILED / CANCELLED states.
        TimeoutError: when state is still not COMPLETED after `timeout`.
    """
    import time

    deadline = time.monotonic() + timeout
    last_status: Optional[Dict[str, Any]] = None
    start = time.monotonic()
    while time.monotonic() < deadline:
        status = task.status()
        last_status = status
        state = status.get("state", "UNKNOWN")
        if state == "COMPLETED":
            return
        if state in ("FAILED", "CANCELLED"):
            raise RuntimeError(
                f"GEE 任务失败：state={state}, "
                f"id={status.get('id')}, info={status}"
            )
        if progress_callback:
            elapsed = int(time.monotonic() - start)
            progress_callback(
                92,
                f"正在等待 GEE 任务完成（state={state}，已运行 {elapsed}s）",
            )
        time.sleep(poll)
    last_id = (last_status or {}).get("id")
    last_state = (last_status or {}).get("state", "UNKNOWN")
    raise TimeoutError(
        f"GEE 任务超时（>{int(timeout)}s），"
        f"id={last_id}, last state={last_state}"
    )


def _download_asset_to_local(
    asset_id: str,
    *,
    local_path: str,
    scale: int,
    region: ee.Geometry,
) -> None:
    """Stream a GEE Asset Image to a local GeoTIFF.

    Uses `geemap.ee_export_image`, which transparently handles both the
    `getDownloadURL` path (small images) and the chunked tiling path
    (large images). The destination directory is created if missing.
    """
    import geemap  # type: ignore

    out_dir = os.path.dirname(os.path.abspath(local_path))
    os.makedirs(out_dir, exist_ok=True)

    geemap.ee_export_image(
        ee_object=ee.Image(asset_id),
        filename=local_path,
        scale=scale,
        region=region,
        file_per_band=False,
    )


def _local_tif_to_ee_image(
    local_path: str,
    *,
    tile_pixels: int = _LOCAL_MASK_TILE_PIXELS,
    max_tiles: int = _LOCAL_MASK_MAX_TILES,
    progress_callback: Optional[ProgressCallback] = None,
    progress_start: float = 0.0,
    progress_end: float = 100.0,
) -> Tuple[ee.Image, ee.Geometry]:
    """Read a 1-band local GeoTIFF into a tiled ee.Image with WGS84 georeferencing.

    Large local masks (>> 50M 像素) cannot be shipped to GEE as a single
    `ee.Image(arr).reproject(...)` payload — the request payload would
    exceed GEE's per-call limits. Instead, the TIFF is split into
    rectangular tiles of `tile_pixels` source pixels per side; each tile
    is converted to its own `ee.Image` (with proper WGS84 reprojection)
    and the tiles are combined via `ee.ImageCollection(...).mosaic()`.

    Returns (image, roi) where:
      * image — the mosaic of all non-empty tiles in EPSG:4326.
        * Tiles whose pixels are all zero (no sugarcane) are skipped —
          they contribute no signal to `is_sugarcane.eq(1)` and would
          just waste GEE payload. The mosaic's footprint therefore
          covers only the regions that actually contain sugarcane.
        * Tiles with at least one non-zero pixel preserve their exact
          uint8 values via `ee.Image(arr).reproject(...)`.
      * roi — an `ee.Geometry.Rectangle` matching the BOUNDING BOX OF
        NON-ZERO PIXELS in WGS84 (with a `_ROI_BUFFER_M` meter
        buffer), NOT the full mask extent. We deliberately shrink the
        ROI to where sugarcane actually is, which dramatically reduces
        the SAR analysis area, the export size, and the GEE server-side
        processing time (the dominant cost for large masks). The buffer
        ensures that the 30 m neighborhood operations
        (`focal_median`, `reduceNeighborhood`) inside
        `_calculate_sar_metrics` don't see boundary padding (= 0) on
        any real sugarcane pixel. Non-sugarcane pixels within the
        (smaller) ROI are still masked out by `updateMask(is_sugarcane)`
        later in the algorithm.

    Limits:
      * Only 1-band TIFFs are accepted.
      * The TIFF must split into at most `max_tiles` source tiles;
        beyond that, callers should fall back to `sugarcane_mask_asset`
        (GEE Asset uploaded via GEE Code Editor).

    Args:
      local_path: path to the local 1-band GeoTIFF.
      tile_pixels: edge length (in source pixels) of each tile.
      max_tiles: maximum allowed tile count; raises ValueError when exceeded.
      progress_callback: optional callback for chunked-reading progress.
      progress_start, progress_end: percent range mapped to the chunked-read
        loop (so the caller's overall bar keeps moving on large uploads).

    Raises:
      FileNotFoundError, ValueError, rasterio.errors.RasterioIOError.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds

    src = rasterio.open(local_path)
    try:
        if src.count != 1:
            raise ValueError(
                f"甘蔗分类掩膜必须是单波段 TIFF（当前 {src.count} 个波段）："
                f"{local_path}"
            )
        width = src.width
        height = src.height
        src_crs = src.crs
        src_transform = src.transform
        src_bounds = src.bounds  # (left, bottom, right, top) in source CRS
    finally:
        src.close()

    # Plan the tile grid.
    # The ROI is built LATER (after the loop) from the bbox of non-zero
    # pixels — see below. That keeps the SAR analysis / export area
    # small even for sparse masks.
    n_tile_rows = (height + tile_pixels - 1) // tile_pixels
    n_tile_cols = (width + tile_pixels - 1) // tile_pixels
    total_tiles = n_tile_rows * n_tile_cols

    if total_tiles > max_tiles:
        raise ValueError(
            f"本地甘蔗掩膜过大：{height}×{width} 像素，按 {tile_pixels}×{tile_pixels} "
            f"分块需切为 {n_tile_rows}×{n_tile_cols} = {total_tiles} 个瓦片，"
            f"超过 {max_tiles} 上限。"
            f"请在 GEE Code Editor 上传为 Asset 后，"
            f"在高级参数中填 Asset 路径（sugarcane_mask_asset）。"
        )

    tile_imgs: list[ee.Image] = []
    # Track the bbox (in source pixel coords) of non-zero tiles so we
    # can shrink the SAR analysis ROI to just where sugarcane is.
    nonzero_min_col = width
    nonzero_max_col = -1
    nonzero_min_row = height
    nonzero_max_row = -1
    tiles_with_data = 0
    total_bytes_read = 0

    src = rasterio.open(local_path)
    try:
        report_every = max(1, total_tiles // 20)  # ~20 progress ticks max
        for row_idx in range(n_tile_rows):
            for col_idx in range(n_tile_cols):
                row_off = row_idx * tile_pixels
                col_off = col_idx * tile_pixels
                win_h = min(tile_pixels, height - row_off)
                win_w = min(tile_pixels, width - col_off)
                if win_h <= 0 or win_w <= 0:
                    continue

                # Source-CRS bounds of this tile (left, bottom, right, top).
                src_left, src_top = src_transform * (col_off, row_off)
                src_right, src_bottom = src_transform * (
                    col_off + win_w, row_off + win_h
                )

                # Re-project the tile bounds into EPSG:4326.
                tile_wgs84 = transform_bounds(
                    src_crs, "EPSG:4326",
                    src_left, src_bottom, src_right, src_top,
                )
                t_min_lon, t_min_lat, t_max_lon, t_max_lat = tile_wgs84

                # Earth Engine crsTransform convention:
                #   [sx, 0, tx, 0, sy, ty]
                # with sx > 0 (east is positive) and sy < 0 (south is
                # positive for increasing row index). (tx, ty) is the
                # upper-left corner in CRS units.
                t_scale_x = (t_max_lon - t_min_lon) / win_w
                t_scale_y = -(t_max_lat - t_min_lat) / win_h
                t_tx = t_min_lon
                t_ty = t_max_lat

                # Read just this tile's pixels.
                tile_arr = src.read(
                    1,
                    window=((row_off, row_off + win_h),
                            (col_off, col_off + win_w)),
                )
                total_bytes_read += tile_arr.nbytes

                # Optimization: skip tiles that contain NO sugarcane pixels
                # (value != 1). The user's TIFF may have 0 (non-sugarcane),
                # 1 (sugarcane), AND 255 (nodata) — `np.any(tile_arr)` alone
                # would only skip all-zero tiles, sending every all-255
                # (nodata) tile to GEE as well, which balloons the upload
                # to ~26 GB JSON for a 5 GB mask. Checking `== 1` matches
                # the documented semantic ("1 = 甘蔗") and skips any tile
                # that has no sugarcane pixels at all.
                if not np.any(tile_arr == 1):
                    continue

                # Track the bbox of non-zero pixels (in source pixel
                # coords) so we can build a tight ROI after the loop.
                nonzero_min_col = min(nonzero_min_col, col_off)
                nonzero_max_col = max(nonzero_max_col, col_off + win_w - 1)
                nonzero_min_row = min(nonzero_min_row, row_off)
                nonzero_max_row = max(nonzero_max_row, row_off + win_h - 1)

                # ee.Image(arr) only accepts lists / tuples / scalars, not
                # raw numpy ndarrays — convert explicitly. tolist() of a
                # 4000x4000 uint8 takes ~0.2 s; for the user's 5B-px mask
                # that's ~50 s total across ~313 tiles.
                tile_img = ee.Image(tile_arr.astype(np.uint8).tolist()).reproject(
                    crs="EPSG:4326",
                    crsTransform=[
                        t_scale_x, 0, t_tx,
                        0, t_scale_y, t_ty,
                    ],
                )
                tile_imgs.append(tile_img)
                tiles_with_data += 1

                if progress_callback:
                        idx = row_idx * n_tile_cols + col_idx + 1
                        if (
                            idx == total_tiles
                            or idx % report_every == 0
                        ):
                            pct = progress_start + (
                                progress_end - progress_start
                            ) * idx / total_tiles
                            progress_callback(
                                pct,
                                f"正在分块读取甘蔗掩膜："
                                f"{idx}/{total_tiles} 个瓦片"
                                f"（含数据 {tiles_with_data} 个）",
                            )
    finally:
        src.close()

    if not tile_imgs:
        raise ValueError(
            f"本地甘蔗分类掩膜全部为 0（不含甘蔗像素）：{local_path}。"
            f"请检查 TIFF 内容是否正确。"
        )

    if len(tile_imgs) == 1:
        mask_img = tile_imgs[0]
    else:
        mask_img = ee.ImageCollection(tile_imgs).mosaic()

    # Build the ROI from the bounding box of non-zero pixels (NOT the
    # full mask extent), then buffer by _ROI_BUFFER_M. The buffer
    # prevents edge effects from `_calculate_sar_metrics`'s 30 m
    # neighborhood operations on boundary sugarcane pixels.
    src_left, src_top = src_transform * (nonzero_min_col, nonzero_min_row)
    src_right, src_bottom = src_transform * (
        nonzero_max_col + 1, nonzero_max_row + 1
    )
    nz_wgs84 = transform_bounds(
        src_crs, "EPSG:4326", src_left, src_bottom, src_right, src_top
    )
    nz_min_lon, nz_min_lat, nz_max_lon, nz_max_lat = nz_wgs84
    roi = ee.Geometry.Rectangle([
        nz_min_lon, nz_min_lat, nz_max_lon, nz_max_lat,
    ]).buffer(_ROI_BUFFER_M)

    if progress_callback:
        progress_callback(
            progress_end,
            f"甘蔗掩膜分块读取完成：{tiles_with_data}/{total_tiles} 个瓦片含数据，"
            f"读取 {total_bytes_read / (1024 * 1024):.1f} MB",
        )

    return mask_img, roi


def run(
    params: Dict[str, Any],
    job_dir: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    """Build the GEE asset graph, export to Asset, download to local job_dir.

    On success returns `make_result(status="completed", result_type="raster",
    files=[<local .tif>], ...)` so the platform's ResultRenderer can
    preview / download the GeoTIFF directly.
    On a hard failure (missing params, GEE auth mismatch, mask read
    failure, export failure, download failure) returns
    `make_result(status="failed", ...)` with a user-facing Chinese
    message — never the raw Python / GEE stack trace.
    """
    project = (
        params.get("gee_project_id")
        or params.get("gee_project")
        or os.environ.get("EE_PROJECT")
        or "sodium-ray-505904-i3"
    )

    try:
        _initialize_ee(project)
    except GeeAuthRequired as exc:
        return make_result(status="failed", result_type="json", message=str(exc))

    if progress_callback:
        progress_callback(5, "正在连接 GEE")

    # ---------- 1. 参数解析 ----------
    if "year" not in params or params["year"] in ("", None):
        return make_result(
            status="failed",
            result_type="json",
            message="缺少必填参数：年份（year）。",
        )
    try:
        year = int(params["year"])
    except (TypeError, ValueError):
        return make_result(
            status="failed",
            result_type="json",
            message=f"年份必须是整数：{params['year']!r}",
        )

    base_start = f"{year}-10-01"
    base_end = f"{year}-11-30"
    current_start = f"{year}-12-01"
    current_end = f"{year + 1}-04-21"

    # 默认参数（用户不动时与原硬编码完全一致）。
    rvi_absolute_low = float(
        params.get("rvi_absolute_low", _DEFAULT_RVI_ABSOLUTE_LOW)
    )
    rvi_drop_min = float(params.get("rvi_drop_min", _DEFAULT_RVI_DROP_MIN))
    scale = int(params.get("scale", _DEFAULT_SCALE))
    crs = params.get("crs", _DEFAULT_CRS)
    max_pixels = int(params.get("max_pixels", _DEFAULT_MAX_PIXELS))

    description = params.get(
        "export_description", f"Harvest_{year}_{current_end.replace('-', '')}"
    )

    # ---------- 2. 甘蔗掩膜解析 ----------
    # 优先使用 GEE Asset 路径（适合超大地块）；否则把本地 TIFF 自动
    # 构造为带 WGS84 投影的 ee.Image，并把掩膜范围同时当作 SAR 分析
    # 的研究区。
    sugarcane_mask_asset = (params.get("sugarcane_mask_asset") or "").strip()
    sugarcane_mask_local_path = params.get("sugarcane_mask_local_path")

    if not sugarcane_mask_asset and not sugarcane_mask_local_path:
        return make_result(
            status="failed",
            result_type="json",
            message=(
                "缺少甘蔗分类掩膜：请上传本地甘蔗分类掩膜 TIFF "
                "（基本参数区『甘蔗分类掩膜 TIFF』），"
                "或在高级参数中直接填写 GEE Asset 路径。"
            ),
        )

    if progress_callback:
        progress_callback(12, "正在读取甘蔗分类掩膜")

    if sugarcane_mask_asset:
        try:
            sugarcane_mask_img = ee.Image(sugarcane_mask_asset)
            # ROI = 掩膜的几何范围；.geometry() 对栅格也是 bounding rectangle。
            roi = sugarcane_mask_img.geometry()
        except Exception as exc:  # noqa: BLE001
            return make_result(
                status="failed",
                result_type="json",
                message=f"无法读取甘蔗掩膜 GEE Asset（{sugarcane_mask_asset}）：{exc}",
            )
        mask_source = "gee_asset"
        mask_source_label = sugarcane_mask_asset
    else:
        try:
            sugarcane_mask_img, roi = _local_tif_to_ee_image(
                str(sugarcane_mask_local_path),
                progress_callback=progress_callback,
                progress_start=15.0,
                progress_end=30.0,
            )
        except FileNotFoundError:
            return make_result(
                status="failed",
                result_type="json",
                message=(
                    f"本地甘蔗分类掩膜文件不存在：{sugarcane_mask_local_path}。"
                    "请重新上传。"
                ),
            )
        except ValueError as exc:
            return make_result(
                status="failed",
                result_type="json", message=f"本地甘蔗分类掩膜读取失败：{exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return make_result(
                status="failed",
                result_type="json",
                message=f"本地甘蔗分类掩膜读取失败：{exc}",
            )
        mask_source = "local_tif"
        mask_source_label = str(sugarcane_mask_local_path)

    is_sugarcane = sugarcane_mask_img.eq(1)

    # ---------- 3. SAR 收割检测 ----------
    if progress_callback:
        progress_callback(35, "正在构建 SAR 影像集合")

    s1_col = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(roi)
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .filter(ee.Filter.eq("instrumentMode", "IW"))
    )

    base_metrics = s1_col.filterDate(base_start, base_end).map(_calculate_sar_metrics).median()
    current_metrics = s1_col.filterDate(current_start, current_end).map(_calculate_sar_metrics).min()

    if progress_callback:
        progress_callback(60, "正在计算基期 / 监测期指标")

    rvi_drop = base_metrics.select("RVI").subtract(current_metrics.select("RVI"))
    is_harvested_sar = (
        current_metrics.select("RVI").lt(rvi_absolute_low)
        .And(rvi_drop.gt(rvi_drop_min))
    )

    final_harvest_mask = (
        is_harvested_sar.updateMask(is_sugarcane).rename("Harvest_Status_SAR")
    )

    # ---------- 4. 提交 Export.image.toAsset ----------
    if progress_callback:
        progress_callback(85, "正在提交 GEE 导出任务")

    task = ee.batch.Export.image.toAsset(
        image=final_harvest_mask,
        description=description,
        assetId=f"projects/{project}/assets/{description}",
        scale=scale,
        crs=crs,
        maxPixels=max_pixels,
        region=roi,
    )
    task.start()
    task_id = getattr(task, "id", None) or task.status().get("id")
    asset_id = f"projects/{project}/assets/{description}"

    # ---------- 5. 等待 GEE 任务完成 ----------
    if progress_callback:
        progress_callback(92, "正在等待 GEE 任务完成（state=STARTING）")

    try:
        _wait_for_task(
            task,
            timeout=7200.0,
            poll=10.0,
            progress_callback=progress_callback,
        )
    except RuntimeError as exc:
        return make_result(
            status="failed",
            result_type="json",
            message=f"GEE 任务失败（task_id={task_id}）：{exc}",
        )
    except TimeoutError as exc:
        return make_result(
            status="failed",
            result_type="json",
            message=(
                f"GEE 任务超时（task_id={task_id}，已等待 >2h）。"
                f"请去 GEE Code Editor → Tasks 标签查看该任务当前进度；"
                f"若已 COMPLETED，可直接去 Assets 手动下载 {asset_id}。"
                f"详情：{exc}"
            ),
        )

    if progress_callback:
        progress_callback(95, "正在下载结果到本地")

    local_tif_path = os.path.abspath(os.path.join(job_dir, f"{description}.tif"))
    try:
        _download_asset_to_local(
            asset_id,
            local_path=local_tif_path,
            scale=scale,
            region=roi,
        )
    except Exception as exc:  # noqa: BLE001
        return make_result(
            status="failed",
            result_type="json",
            message=(
                f"下载 GEE 资产到本地失败（task_id={task_id}, asset={asset_id}）："
                f"{exc}。可在 GEE Code Editor 的 Assets 标签页手动下载 {asset_id}。"
            ),
        )

    if not os.path.isfile(local_tif_path):
        return make_result(
            status="failed",
            result_type="json",
            message=(
                f"GEE 资产下载完成但未找到本地文件：{local_tif_path}。"
                f"请检查 geemap.ee_export_image 输出目录配置（task_id={task_id}）。"
            ),
        )

    size_bytes = os.path.getsize(local_tif_path)

    # ---------- 6. 返回结果 ----------
    if progress_callback:
        progress_callback(100, "完成")

    return make_result(
        status="completed",
        result_type="raster",
        files=[
            {
                "name": f"{description}.tif",
                "label": "harvest_mask",
                "path": local_tif_path,
                "size_bytes": size_bytes,
            }
        ],
        metrics={
            "year": year,
            "base_window": f"{base_start} → {base_end}",
            "current_window": f"{current_start} → {current_end}",
            "gee_task_id": task_id,
            "asset_id": asset_id,
            "mask_source": mask_source,
            "mask_source_label": mask_source_label,
            "rvi_absolute_low": rvi_absolute_low,
            "rvi_drop_min": rvi_drop_min,
            "scale": scale,
            "crs": crs,
            "cards": [
                make_card("task_status", "任务状态", "完成", ""),
                make_card("year", "目标年份", year, "年"),
                make_card(
                    "base_window", "基期窗口", f"{base_start} → {base_end}", ""
                ),
                make_card(
                    "current_window",
                    "监测期窗口",
                    f"{current_start} → {current_end}",
                    "",
                ),
                make_card("rvi_low", "RVI 绝对下限", rvi_absolute_low, ""),
                make_card("rvi_drop", "RVI 下降阈值", rvi_drop_min, ""),
                make_card("scale", "导出分辨率", scale, "m"),
                make_card("mask_source", "掩膜来源", mask_source, ""),
                make_card("gee_task_id", "GEE 任务 ID", task_id, ""),
                make_card("asset_id", "GEE 资产", asset_id, ""),
                make_card(
                    "output_tif",
                    "输出文件",
                    f"{description}.tif",
                    "",
                ),
            ],
        },
        message=(
            f"GEE 收割监测完成（task_id={task_id}，asset={asset_id}，"
            f"年份={year}，输出 {description}.tif {size_bytes} B）"
        ),
    )


__all__ = ["run"]