"""Split one GeoTIFF into several independent shard GeoTIFFs.

Why this exists
---------------
Some GEE exports come back as *several* independent GeoTIFFs that only form
a complete map once they are stitched together — the same thing happens when
the very same algorithm is run locally and GEE hands the download back in
two parts.  The platform therefore has to be able to:

  1. keep the shards as separate files (each one downloadable on its own),
  2. stitch them back into one map for the preview.

Step 2 lives in ``preview_service`` (the same mosaic pipeline the
"展示页" uses when several TIFs are uploaded).  This module covers step 1:
it takes the single GeoTIFF the tiled downloader produced and cuts it into
``num_shards`` horizontal bands.

The shards are cut along the *row* axis so that each shard keeps the full
width of the map — the same layout GEE itself uses when it splits a large
image download into parts.  Every shard is a self-contained, valid GeoTIFF:
same CRS, same pixel size, its own affine that points at its own slice of
the map, so stitching the shards back together reproduces the original
extent exactly.

Memory
------
The copy is done stripe by stripe (``_STRIPE_ROWS`` rows at a time), so the
peak memory is one stripe of the raster regardless of how large the map is.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from rasterio.transform import Affine
from rasterio.windows import Window


logger = logging.getLogger(__name__)


# Rows copied per stripe.  512 rows × a few thousand columns is a few MB —
# small enough to stay out of the way, large enough to keep the IO busy.
_STRIPE_ROWS = 512

# Overview factors built for every shard, mirroring ``gee_raster`` — the
# preview service downsamples through them instead of decoding the whole
# raster.
_OVERVIEW_FACTORS = (2, 4, 8, 16, 32)


def _shard_transform(transform: Any, row_offset: int) -> Any:
    """Affine of the shard that starts at ``row_offset`` rows from the top."""
    return transform * Affine.translation(0, row_offset)


def _row_split(total_rows: int, num_shards: int) -> List[tuple]:
    """Split ``total_rows`` into ``num_shards`` (start, stop) row ranges.

    The remainder is spread over the first shards, so the bands differ by at
    most one row.
    """
    if total_rows <= 0:
        return []
    base = total_rows // num_shards
    extra = total_rows % num_shards
    ranges: List[tuple] = []
    start = 0
    for i in range(num_shards):
        height = base + (1 if i < extra else 0)
        ranges.append((start, start + height))
        start += height
    return [r for r in ranges if r[1] > r[0]]


def split_raster_into_shards(
    src_path: str,
    out_dir: str,
    base_name: str,
    *,
    num_shards: int = 2,
    remove_source: bool = False,
) -> List[Dict[str, Any]]:
    """Cut ``src_path`` into ``num_shards`` GeoTIFFs inside ``out_dir``.

    Args:
        src_path: the GeoTIFF to split.
        out_dir: destination directory (created on demand).
        base_name: file name stem for the shards; the actual names are
            ``<base_name>_part01.tif``, ``<base_name>_part02.tif``, ...
        num_shards: how many bands to cut the raster into.  ``1`` returns
            the source unchanged (as a single-part "shard").
        remove_source: delete the source file once the shards are written.
            The shards are a complete replacement for it, so keeping both
            would only waste disk.

    Returns:
        A list of ``{"name", "path", "size_bytes", "width", "height",
        "row_start", "row_end"}`` dicts, in top-to-bottom order.

    Raises:
        ValueError: on an invalid ``num_shards``.
        FileNotFoundError: when the source raster does not exist.
    """
    import rasterio

    num_shards = int(num_shards)
    if num_shards < 1:
        raise ValueError(f"num_shards must be >= 1, got {num_shards}")
    if not os.path.isfile(src_path):
        raise FileNotFoundError(f"source raster not found: {src_path}")

    os.makedirs(out_dir, exist_ok=True)

    with rasterio.open(src_path) as src:
        ranges = _row_split(src.height, num_shards)
        if not ranges:
            raise ValueError("source raster has no rows")

        profile = src.profile.copy()
        profile.pop("compress", None)
        profile.pop("tiled", None)
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)

        shards: List[Dict[str, Any]] = []
        for index, (row_start, row_end) in enumerate(ranges, start=1):
            name = f"{base_name}_part{index:02d}.tif"
            out_path = os.path.join(out_dir, name)
            shard_height = row_end - row_start

            shard_profile = dict(profile)
            shard_profile.update(
                {
                    "width": src.width,
                    "height": shard_height,
                    "transform": _shard_transform(src.transform, row_start),
                    "tiled": True,
                    "blockxsize": 256,
                    "blockysize": 256,
                    "compress": "deflate",
                    "BIGTIFF": "IF_SAFER",
                }
            )

            with rasterio.open(out_path, "w", **shard_profile) as dst:
                for stripe_start in range(0, shard_height, _STRIPE_ROWS):
                    stripe_h = min(_STRIPE_ROWS, shard_height - stripe_start)
                    # 源窗口要带上 row_start 的偏移，否则每个分片拿到的都是
                    # 源栅格最上面那几行；目标窗口则从分片自己的第 0 行算。
                    src_window = Window(0, row_start + stripe_start, src.width, stripe_h)
                    dst_window = Window(0, stripe_start, src.width, stripe_h)
                    data = src.read(window=src_window)
                    dst.write(data, window=dst_window)

            # Overviews: let the preview service downsample without decoding
            # the whole shard.  Best-effort — GDAL falls back to a full read.
            try:
                with rasterio.open(out_path, "r+") as dst:
                    factors = [f for f in _OVERVIEW_FACTORS if shard_height // f >= 1]
                    if factors:
                        from rasterio.enums import Resampling

                        dst.build_overviews(factors, Resampling.nearest)
            except Exception:  # noqa: BLE001
                pass

            shards.append(
                {
                    "name": name,
                    "path": os.path.abspath(out_path),
                    "size_bytes": os.path.getsize(out_path),
                    "width": src.width,
                    "height": shard_height,
                    "row_start": row_start,
                    "row_end": row_end,
                }
            )

    if remove_source and len(shards) > 0:
        # 只有当源文件就躺在分片旁边时才删它 —— 也就是「下载出来的中间产物
        # 被分片取代」这种场景。源文件在别处时（常见的用法：拿一个已经
        # 归档的结果去做实验），它多半是别人的正式产物，删掉就找不回来了。
        src_dir = os.path.dirname(os.path.abspath(src_path))
        out_dir_abs = os.path.abspath(out_dir)
        if os.path.normcase(src_dir) != os.path.normcase(out_dir_abs):
            logger.warning(
                "split_raster_into_shards: 源文件不在分片目录内，跳过删除 "
                "(src=%s, out=%s)",
                src_path,
                out_dir_abs,
            )
        else:
            try:
                os.remove(src_path)
            except OSError:
                pass

    return shards


def find_shard_sidecars(shard_names: List[str], suffix: str) -> List[str]:
    """Return the sidecar paths that belong to ``shard_names``.

    A sidecar is ``<shard name><suffix>`` — e.g. ``result_part01.tif`` +
    ``.metadata.json`` → ``result_part01.tif.metadata.json``.
    """
    return [f"{name}{suffix}" for name in shard_names]


__all__ = ["find_shard_sidecars", "split_raster_into_shards"]
