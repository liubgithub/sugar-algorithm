"""Crop time-series feature extraction (GEE).

前端上传本地 CSV 样本点文件（含 longitude / latitude 两列），后端自动
注册到 GEE Asset，然后在 GEE 中构建 12 个月 Sentinel-1 / Sentinel-2
月度合成影像，按固定 min / max 归一化并堆叠为 11 × 12 = 132 个时序
特征；追加归一化的 DEM 高程，最终共 133 个特征。对样本点 reduceRegions
取首像元值，去除 geometry，并通过 Export.table.toAsset 导出到项目 GEE
Asset；``run()`` 在提交完成后立即返回，由后台 GEE 轮询线程在 ``finalize()``
里阻塞等待 Asset 就绪并通过 ``getDownloadURL`` 拉回本地 CSV 写入
``job_dir``，由前端 ResultRenderer 直接提供下载。

ROI 直接取 ``sample_points.geometry()``，不需要单独上传研究区。

参数
----
params
    gee_project_id      GEE 项目 ID（用于 ee.Initialize(project=...)）
    year                目标年份（int）
    samples_local_path  本地 CSV 文件绝对路径（与 samples_asset_id 二选一）
    samples_asset_id    已存在的 GEE 样本点 Asset id（与 samples_local_path 二选一）
    cloud_pct_max       可选 S2 云量阈值
    export_description  可选 GEE 任务描述，留空自动按 TimeSeriesFeatures_<year> 命名

执行模型：Pattern B（fire-and-forget）。``run()`` 立即返回
``status="submitted"``；``finalize()`` 在轮询线程发现 GEE COMPLETED 后被
调用，负责等 Asset 就绪 + 下载 CSV。这是与 ``harvest``（Pattern A）
的关键区别：后者在 ``run()`` 里就完成了所有工作。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import ee

from app.algorithms.base import ProgressCallback, make_card, make_result
from app.algorithms.configs import time_series_config as cfg
from app.services import ingest_service
from app.services.gee_auth import GeeAuthRequired, initialize_ee
from app.storage import job_store


# algorithm + slot —— 与 ingest_service 的缓存命名空间一致。
# 注意：必须与 ``CropFeaturesAdapter.id`` 保持一致（注册表里注册用的 id
# 即 ``crop_features``）。早期版本此处曾写成 ``"time-series-features"``，
# 导致本地缓存目录、GEE 资产注册记录里出现两份同名算法的目录名分裂，
# 已统一改回与注册表对齐的 ``"crop_features"``。
_ALGORITHM_ID = "crop_features"
_SAMPLES_SLOT = "samples"

# GEE 资产导出等待 / 下载超时（秒）。
# 默认 10 分钟覆盖小样本 + 中等分辨率 ROI 的常见情况；用户可在高级参数中
# 调小，但目前保持写死以避免误改。
_ASSET_WAIT_TIMEOUT = 600.0
_ASSET_WAIT_INTERVAL = 3.0
_DOWNLOAD_TIMEOUT = 600.0
# finalize() 阶段的等待上限（秒）。比 run() 内历史同步等待的 600s 长很多：
# 因为 finalize 在后台轮询线程里跑，不再阻塞 HTTP / worker。30 分钟能
# 覆盖 52565 EECU-seconds 量级的中型任务。
_FINALIZE_WAIT_TIMEOUT = 1800.0


def _mask_s2_clouds(image: ee.Image) -> ee.Image:
    """QA60 bitmask + scale to surface reflectance (matches user's script)."""
    qa = image.select("QA60")
    mask = qa.bitwiseAnd(1 << 10).eq(0).And(qa.bitwiseAnd(1 << 11).eq(0))
    return image.updateMask(mask).divide(10000)


def _build_normalize_fn() -> Any:
    """Return a server-side function that maps each band to [0, 1] via min/max."""
    min_img = ee.Image.constant(cfg.MIN_VALUES).rename(cfg.BAND_NAMES)
    max_img = ee.Image.constant(cfg.MAX_VALUES).rename(cfg.BAND_NAMES)

    def _normalize(img: ee.Image) -> ee.Image:
        return img.subtract(min_img).divide(max_img.subtract(min_img)).clamp(0, 1)

    return _normalize


def _monthly_image(
    m: ee.Number,
    year: int,
    roi_bounds: ee.Geometry,
    normalize: Any,
    cloud_pct_max: int,
) -> ee.Image:
    """Build one month: S2 + S1 → 11 bands → normalized, tagged with 'month'."""
    m_int = ee.Number(m)
    start = ee.Date.fromYMD(year, m_int, 1)
    end = start.advance(1, "month")

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(roi_bounds)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", cloud_pct_max))
        .map(_mask_s2_clouds)
        .median()
    )

    s1 = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(roi_bounds)
        .filterDate(start, end)
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .select(["VV", "VH"])
        .median()
    )

    combined = (
        s2.select(["B2", "B3", "B4", "B8", "B11", "B12"])
        .addBands(s2.normalizedDifference(["B8", "B4"]).rename("NDVI"))
        .addBands(
            s2.expression(
                "2.5*((B8-B4)/(B8+6*B4-7.5*B2+1))",
                {
                    "B8": s2.select("B8"),
                    "B4": s2.select("B4"),
                    "B2": s2.select("B2"),
                },
            ).rename("EVI")
        )
        .addBands(s2.normalizedDifference(["B3", "B8"]).rename("NDWI"))
        .addBands(s1)
    )
    return normalize(combined).set("month", m_int)


def _build_time_series_stack(
    year: int,
    roi_bounds: ee.Geometry,
    *,
    cloud_pct_max: int = cfg.CLOUD_PCT_MAX,
) -> ee.Image:
    """12-month normalized stack + DEM, identical to user's original script."""
    normalize = _build_normalize_fn()
    months = ee.List.sequence(1, cfg.TIME_SERIES_MONTHS)

    monthly = ee.ImageCollection(
        months.map(
            lambda m: _monthly_image(m, year, roi_bounds, normalize, cloud_pct_max)
        )
    )
    annual_median = monthly.median()

    def _fill(img: ee.Image) -> ee.Image:
        img = ee.Image(img)
        m_str = ee.Number(img.get("month")).format("%02d")

        def _rename(b: ee.String) -> ee.String:
            return (
                ee.String(cfg.MONTH_BAND_PREFIX)
                .cat(m_str)
                .cat("_")
                .cat(ee.String(b))
            )

        new_names = img.bandNames().map(_rename)
        return img.unmask(annual_median).rename(new_names)

    filled = monthly.map(_fill)

    def _stack(img: ee.Image, prev: ee.Image) -> ee.Image:
        return ee.Image(prev).addBands(img)

    dummy = ee.Image.constant(0).rename("dummy")
    stacked = ee.Image(filled.iterate(_stack, dummy))
    valid = stacked.bandNames().remove("dummy")
    stacked = stacked.select(valid).unmask(cfg.STACK_NODATA)

    dem = (
        ee.Image("NASA/NASADEM_HGT/001")
        .resample("bilinear")
        .reproject(crs="EPSG:4326", scale=10)
        .clip(roi_bounds)
    )
    elevation_norm = (
        dem.select("elevation")
        .subtract(cfg.DEM_MIN)
        .divide(cfg.DEM_MAX - cfg.DEM_MIN)
        .clamp(0, 1)
        .rename(cfg.DEM_BAND_NAME)
    )
    return stacked.addBands(elevation_norm)


def _add_coordinates(feature: ee.Feature) -> ee.Feature:
    geom = feature.geometry()
    coords = geom.coordinates()
    return feature.set(
        {
            "longitude": coords.get(0),
            "latitude": coords.get(1),
        }
    )


def _coerce_class_to_number(feature: ee.Feature, prop: str = "class") -> ee.Feature:
    """Defensive: coerce a feature's ``prop`` (default ``class``) to ``ee.Number``.

    Why this exists
    ---------------
    ``ee.Classifier.smileRandomForest().train(classProperty=prop, ...)``
    requires the class column to be Float. Sample assets frequently come
    back from GEE with the class property typed as ``String`` (e.g. ``"10"``)
    even when the source CSV looked numeric — and worse, sometimes a
    single FeatureCollection ends up with mixed types (some rows have
    Integer ``0`` and others have String ``"10"``), at which point
    downstream ops crash with::

        Collection.reduceColumns: Cannot compare values '0' (Type:Integer)
        and '10' (Type:String).

    The fix: regardless of the property's current type, route it through
    ``ee.String(...)`` then ``ee.Number.parse(input, radix=10)``::

        - Number 0   -> ee.String("0")   -> ee.Number.parse -> 0
        - String "10" -> ee.String("10") -> ee.Number.parse -> 10
        - null / missing -> ee.Number(NaN)  -> filtered out later

    Casting via ``ee.Number(...)`` directly is unreliable here: the Python
    wrapper raises for non-Number inputs and server-side coercion is the
    only safe path. ``Number.parse`` is explicit about wanting a String,
    so we always feed it one.

    Missing property
    ----------------
    If the source FC does NOT have ``prop`` at all (e.g. an upstream CSV
    that only has longitude / latitude), ``feature.propertyNames().contains``
    returns false and we leave the feature untouched. The downstream
    ``filter(notNull)`` / ``filter(gte(..., 0))`` will then drop the
    feature, and the ``n_samples == 0`` guard in ``run()`` surfaces a
    clear Chinese error rather than silently submitting a 30-minute
    GEE task that ends in failure.
    """
    has_prop = feature.propertyNames().contains(ee.String(prop))
    raw = feature.get(prop)
    return feature.set(
        prop,
        ee.Algorithms.If(
            has_prop,
            ee.Number.parse(ee.String(raw), 10),
            raw,
        ),
    )


def _ingest_samples(
    local_path: str,
    project: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> List[str]:
    """上传本地 CSV → GEE Asset(s)，并返回 asset id 列表。

    阶段 2 工作。复用 ``ingest_service.ensure_asset_for_local``：第一次上传
    会通过 ``geemap`` 解析 CSV 并提交 ``Export.table.toAsset``；同一个本地文件
    在 file_id + project 命中缓存时直接复用，节省时间。

    返回值是列表——单个 CSV 一般是 ``[asset_id]``；超过 GEE 的 10 MB
    ``Export.table.toAsset`` 上限（典型如 30+ MB 样本特征 CSV）会被切分为
    多个 chunk asset，调用方需要 ``ee.FeatureCollection(asset_ids).flatten()``
    服务端合并后使用。
    """
    src = Path(local_path)
    if not src.is_file():
        raise FileNotFoundError(
            f"本地样本点文件不存在：{local_path}。"
            "请先在前端通过上传控件选择 CSV 文件。"
        )
    if src.suffix.lower() != ".csv":
        raise ValueError(
            f"本算法仅支持 CSV 格式样本点文件（含 longitude / latitude 两列），"
            f"当前文件后缀：{src.suffix!r}。"
        )

    if progress_callback:
        progress_callback(10, "上传样本点到 GEE Asset")

    asset_ids = ingest_service.ensure_asset_for_local(
        local_path=str(src),
        slot=_SAMPLES_SLOT,
        algorithm=_ALGORITHM_ID,
        project=project,
        kind_hint="feature_collection",
    )

    if progress_callback:
        progress_callback(30, f"样本点已注册：{len(asset_ids)} 个 Asset")
    return asset_ids


def _wait_for_asset(asset_id: str, *, timeout: float = _ASSET_WAIT_TIMEOUT) -> None:
    """Poll ``ee.data.getAsset`` until the ingested asset becomes queryable.

    与 ``harvest._wait_for_asset`` 行为一致：在 GEE 端把 Export 任务跑完后，
    ``ee.data.getAsset`` 会从「资源不存在」变为「返回 asset 字典」。抛出
    ``TimeoutError`` 表示超时，调用方决定如何向用户呈现。
    """
    deadline = time.monotonic() + timeout
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            ee.data.getAsset(asset_id)
            return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(_ASSET_WAIT_INTERVAL)
    raise TimeoutError(
        f"GEE 资产导出超时（>{int(timeout)}s）：{asset_id}; last={last_err}"
    )


def _download_table_asset_to_csv(asset_id: str, *, local_path: str) -> int:
    """Stream a FeatureCollection GEE Asset to a local CSV file.

    通过 ``FeatureCollection.getDownloadURL(filetype='csv')`` 拿到签名 URL，
    然后用 ``requests`` 把内容写入 ``local_path``。返回写入字节数。

    与 ``harvest`` 的 ``_download_asset_to_local``（使用 ``geemap.ee_export_image``
    + GeoTIFF 路径）不同：FeatureCollection 的下载走 ``getDownloadURL`` + CSV
    分支，不需要 scale / region 等栅格参数。
    """
    import requests  # 局部导入，避免 requests 缺失时阻塞算法加载。

    fc = ee.FeatureCollection(asset_id)
    # ee 不同版本 getDownloadURL 返回值不同：早期是字符串，新版是 dict
    # { "url": "...", "size": ..., "attemptsRemaining": ... }。两种都接受。
    payload = fc.getDownloadURL(
        filetype="csv",
        filename=os.path.splitext(os.path.basename(local_path))[0],
    )
    if isinstance(payload, dict):
        url = payload.get("url") or payload.get("downloadURL")
    else:
        url = payload
    if not url:
        raise RuntimeError(
            f"无法从 GEE 拿到 CSV 下载链接：{asset_id}; payload={payload!r}"
        )

    response = requests.get(url, timeout=_DOWNLOAD_TIMEOUT)
    response.raise_for_status()

    out_dir = os.path.dirname(os.path.abspath(local_path))
    os.makedirs(out_dir, exist_ok=True)
    Path(local_path).write_bytes(response.content)
    return len(response.content)


def run(
    params: Dict[str, Any],
    job_dir: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    """Build the 12-month time-series feature stack and submit Export.table.toAsset.

    Pattern B: submit and return immediately with ``status="submitted"``.
    The background GEE poller will detect GEE COMPLETED and call
    :func:`finalize` to wait for the Asset, download the CSV, and
    promote the result to ``status="completed"``.

    The sample-point source can be either:
      - ``samples_local_path``: a local CSV uploaded via ``/api/uploads``
        (will be ingested into a fresh GEE Asset by ``ingest_service``).
      - ``samples_asset_id``: an existing ``projects/<p>/assets/<name>``
        FeatureCollection — skipped upload, used directly. Picked via
        the front-end "或使用 GEE 上已上传的样本点 Asset" dropdown.

    Exactly one of the two must be provided.
    """
    project = (
        params.get("gee_project_id")
        or params.get("gee_project")
        or os.environ.get("EE_PROJECT")
        or "sodium-ray-505904-i3"
    )
    try:
        initialize_ee(project)
    except GeeAuthRequired as exc:
        return make_result(status="failed", result_type="json", message=str(exc))

    if progress_callback:
        progress_callback(5, "初始化 GEE")

    samples_local_path = params.get("samples_local_path")
    samples_asset_id = params.get("samples_asset_id")
    if samples_asset_id and samples_local_path:
        return make_result(
            status="failed",
            result_type="json",
            message=(
                "samples_local_path 与 samples_asset_id 只能选其一："
                "要么上传本地 CSV，要么从 GEE 历史样本点中选一个。"
            ),
        )
    if not samples_asset_id and not samples_local_path:
        return make_result(
            status="failed",
            result_type="json",
            message=(
                "必须提供本地 CSV 文件 或 GEE 样本点 Asset（samples_local_path / "
                "samples_asset_id）。"
            ),
        )

    if samples_asset_id:
        # 复用 GEE 上已存在的样本点 Asset。优先做一次可达性校验，
        # 避免用户粘贴错 ID 后白等 30 分钟才发现任务失败。
        try:
            ee.data.getAsset(samples_asset_id)
        except GeeAuthRequired:
            raise
        except Exception as exc:  # noqa: BLE001
            return make_result(
                status="failed",
                result_type="json",
                message=(
                    f"无法访问 GEE 样本点 Asset：{samples_asset_id}。"
                    f"请确认 Asset 存在、属于当前项目 ({project})，且您有读取权限。"
                    f"原始错误：{exc}"
                ),
            )
        sample_asset_ids = [samples_asset_id]
        if progress_callback:
            progress_callback(15, f"复用样本点 Asset：{samples_asset_id}")
    else:
        # 阶段 2：本地 CSV → GEE Asset(s)
        try:
            sample_asset_ids = _ingest_samples(
                samples_local_path, project, progress_callback=progress_callback
            )
        except FileNotFoundError as exc:
            return make_result(status="failed", result_type="json", message=str(exc))
        except ValueError as exc:
            return make_result(status="failed", result_type="json", message=str(exc))
        except RuntimeError as exc:
            return make_result(status="failed", result_type="json", message=str(exc))
        except Exception as exc:  # noqa: BLE001
            return make_result(
                status="failed",
                result_type="json",
                message=f"样本点上传到 GEE 失败：{exc}",
            )
        # 自动把刚生成的 Asset ID 持久化到本系统的「已保存 GEE Assets」
        # 表里，让用户后续能直接勾选复用，无需再上传 CSV。
        # chunked 上传时只保存第一个 chunk 作为主入口。
        # 这是一次 best-effort 写入；保存失败不应阻塞算法本身。
        primary_sample_id = sample_asset_ids[0] if sample_asset_ids else None
        if primary_sample_id:
            source_name = (
                Path(samples_local_path).name if samples_local_path else ""
            )
            if len(sample_asset_ids) > 1:
                source_name = (
                    f"{source_name}（已切分为 {len(sample_asset_ids)} 个 GEE Asset）"
                )
            try:
                job_store.save_gee_asset(
                    asset_id=primary_sample_id,
                    algorithm_id=_ALGORITHM_ID,
                    source_filename=source_name,
                )
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[crop_features] save_gee_asset failed (asset={primary_sample_id}): {exc}",
                    file=sys.stderr,
                )

    year = int(params["year"])
    cloud_pct_max = int(params.get("cloud_pct_max", cfg.CLOUD_PCT_MAX))
    description = (
        params.get("export_description") or f"TimeSeriesFeatures_{year}"
    )

    sample_points = ee.FeatureCollection(sample_asset_ids).flatten()
    # ROI 直接来自样本点几何范围（无需单独上传研究区）。
    roi_bounds = sample_points.geometry()

    # 过滤掉几何为空的样本行（含缺失经纬度、或上传为纯文本字符串的脏行），
    # 否则 reduceRegions 会抛 "Unable to export features with null geometry"，
    # 错误原因会在 GEE 端转成 FAILED，前端仅看到 "等待中" → 最终失败。
    # 用 .filterBounds(roi_bounds) 是因为：空几何的特征与任何区域都不相交，
    # 会被自然剔除；有几何的则全部保留。
    sample_points = sample_points.filterBounds(roi_bounds)

    # 上传 CSV 中的 class 列如果是文本（例如 "10"、"20"），GEE 上传后会
    # 保留为 String；下游 crop_classification 训练时 ee.Classifier 会抛
    # "Property 'class' ... Expected type: Float. Actual type: String"。
    # 在 reduceRegions 之前统一把 class 转成 ee.Number，从源头保证类型：
    # 已是 Number 的值原样透传，String 数字自动 parse，非法值变成 NaN
    # 后被下面的 gte 过滤掉。源 FC 不含 class 列时不动，不影响现有输出。
    sample_points = sample_points.map(lambda f: _coerce_class_to_number(f, "class"))
    sample_points = sample_points.filter(ee.Filter.gte("class", 0))

    n_samples = sample_points.size().getInfo()
    if progress_callback:
        progress_callback(40, f"样本点有效行数：{n_samples}")
    if not n_samples:
        return make_result(
            status="failed",
            result_type="json",
            message=(
                "GEE 样本点 Asset 中所有行均无有效几何（lon/latitude 缺失或全为空）。"
                "请检查上传的 CSV：longitude / latitude 列是否存在，"
                "是否存在空值 / 经度超出 [-180, 180] / 纬度超出 [-90, 90] 的行。"
                f"原始 Asset：{sample_asset_ids}"
            ),
        )

    if progress_callback:
        progress_callback(45, f"构建 {cfg.TIME_SERIES_MONTHS} 个月时序特征")

    final_image = _build_time_series_stack(
        year, roi_bounds, cloud_pct_max=cloud_pct_max
    )

    if progress_callback:
        progress_callback(75, "提取样本点时序特征 (reduceRegions)")

    sample_with_coords = sample_points.map(_add_coordinates)

    extracted = final_image.reduceRegions(
        collection=sample_with_coords,
        reducer=ee.Reducer.first(),
        scale=10,
        tileScale=16,
    )

    # 保留原始 Point geometry：reduceRegions 输入是 Point，输出 Feature
    # 的 geometry 应当与输入一致（Point 几何）。下游 Export.table.toAsset
    # 会保留 geometry 列；最终下载 CSV 时 geemap/getDownloadURL 会把
    # geometry 序列化成 WKT "POINT(...)" 列，与 longitude/latitude 属性
    # 列共存。**绝不在此调用 _drop_geometry** —— 那会让所有 Feature
    # geometry=null，Export.table.toAsset 服务端会拒绝并抛
    # "Unable to export features with null geometry. (Error code: 3)"。

    if progress_callback:
        progress_callback(90, "提交 GEE Export.table.toAsset")

    # description 加上时间戳，避免同一年份重复提交时 GEE Asset 冲突；
    # 留空自动生成时使用用户提供的 export_description。
    unique_description = f"{description}_{time.strftime('%Y%m%d%H%M%S')}"
    asset_id = f"projects/{project}/assets/{unique_description}"

    task = ee.batch.Export.table.toAsset(
        collection=extracted,
        description=unique_description,
        assetId=asset_id,
    )
    task.start()
    # task.id is available after start(); fall back to status()['id'] if absent.
    gee_task_id = getattr(task, "id", None) or task.status().get("id")

    if progress_callback:
        progress_callback(95, f"已提交 GEE：{asset_id}；等待后台 finalize")

    # ---- Pattern B: return immediately. finalize() picks up from here. ----
    return make_result(
        status="submitted",
        result_type="gee_task",
        gee_task_id=gee_task_id,
        progress=10,
        metrics={
            "year": year,
            "feature_count": cfg.TOTAL_FEATURE_COUNT,
            "bands_per_month": cfg.FEATURE_BAND_COUNT,
            "months": cfg.TIME_SERIES_MONTHS,
            "sample_asset_id": sample_asset_ids[0] if sample_asset_ids else None,
            "sample_asset_ids": list(sample_asset_ids),
            "asset_id": asset_id,
            "description": description,
            "local_csv_name": f"{description}.csv",
            "cards": [
                make_card("year", "目标年份", year, "年"),
                make_card(
                    "feature_count",
                    "特征数量",
                    cfg.TOTAL_FEATURE_COUNT,
                    "个",
                ),
                make_card("bands_per_month", "每月波段数", cfg.FEATURE_BAND_COUNT, ""),
                make_card("months", "月份数", cfg.TIME_SERIES_MONTHS, ""),
            ],
        },
        message=(
            f"GEE 时序特征导出任务已提交（task_id={gee_task_id}）；"
            f"年份={year}, 特征数={cfg.TOTAL_FEATURE_COUNT}, "
            f"样本点 Asset={sample_asset_ids}, 输出 Asset={asset_id}。"
            f"等待 GEE 端处理完成后由后台自动下载 CSV。"
        ),
    )


def finalize(
    job: Dict[str, Any],
    job_dir: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> Optional[Dict[str, Any]]:
    """Background finalizer invoked by the GEE poller.

    Steps:
      1. Read ``asset_id`` and ``description`` from ``job.result.metrics``.
         Return ``None`` defensively if either is missing (e.g. the job
         was created before this finalize hook existed).
      2. Wait until the GEE Asset is queryable, up to
         ``_FINALIZE_WAIT_TIMEOUT`` (30 min by default).
      3. Download the Asset as CSV into ``job_dir/<description>.csv``.
      4. Build and return the new ``make_result`` payload. The caller
         (job_service) is responsible for writing it to the job store.

    Raises on any failure so the poller can mark the job as ``failed``
    and surface the error message in the UI.
    """
    result = (job or {}).get("result") or {}
    metrics = result.get("metrics") or {}
    asset_id = metrics.get("asset_id")
    description = metrics.get("description")
    local_csv_name = metrics.get("local_csv_name") or (
        f"{description}.csv" if description else "TimeSeriesFeatures.csv"
    )
    if not asset_id or not description:
        # Defensive: nothing to wait on, nothing to download.
        return None

    # ``progress_callback`` is wired through for symmetry with ``run``;
    # in practice the worker thread has exited by the time finalize runs,
    # so callers should also write transient messages to the DB via the
    # poller's own update_job calls.
    if progress_callback:
        progress_callback(50, f"等待 GEE Asset 就绪：{asset_id}")

    # Block here is OK — finalize runs in the GEE poller thread, not an
    # HTTP handler. _FINALIZE_WAIT_TIMEOUT (30 min) gives even large
    # tasks room to finish without making the operator wait for a UI
    # timeout.
    _wait_for_asset(asset_id, timeout=_FINALIZE_WAIT_TIMEOUT)

    if progress_callback:
        progress_callback(90, "下载时序特征 CSV 到本地")

    local_csv_path = os.path.abspath(os.path.join(job_dir, local_csv_name))
    size_bytes = _download_table_asset_to_csv(asset_id, local_path=local_csv_path)

    if progress_callback:
        progress_callback(100, "GEE 时序特征导出完成")

    return make_result(
        status="completed",
        result_type="csv",
        files=[
            {
                "name": local_csv_name,
                "label": "时序特征 (CSV)",
                "path": local_csv_path,
                "size_bytes": size_bytes,
            }
        ],
        metrics={
            **metrics,
            "output_csv": local_csv_name,
            "cards": [
                make_card("year", "目标年份", metrics.get("year", ""), "年"),
                make_card(
                    "feature_count",
                    "特征数量",
                    metrics.get("feature_count", cfg.TOTAL_FEATURE_COUNT),
                    "个",
                ),
                make_card(
                    "bands_per_month",
                    "每月波段数",
                    metrics.get("bands_per_month", cfg.FEATURE_BAND_COUNT),
                    "",
                ),
                make_card(
                    "months", "月份数", metrics.get("months", cfg.TIME_SERIES_MONTHS), ""
                ),
            ],
        },
        message=(
            f"GEE 时序特征导出完成（asset={asset_id}）；"
            f"年份={metrics.get('year', '')}, 特征数={metrics.get('feature_count', '')}, "
            f"样本点 Asset={metrics.get('sample_asset_id', '')}, "
            f"输出 {local_csv_name} ({size_bytes} B)"
        ),
    )


__all__ = ["run", "finalize"]