"""End-to-end check: split a raster into shards, then stitch them back.

Exercises the exact chain the result page uses:

    raster_shards.split_raster_into_shards   ->  N GeoTIFFs
    preview_service.get_mosaic_info          ->  one stitched map (info)
    preview_service.get_mosaic_preview_path  ->  one stitched map (PNG)

Everything happens inside a throw-away temp directory — the preview
service's ``JOBS_DIR`` is redirected there for the duration of the test, so
no real job data is touched.

Run from ``backend/``:

    python scripts/test_shard_mosaic.py
"""
from __future__ import annotations

import os
import sys
import tempfile

import numpy as np
import rasterio
from rasterio.transform import from_origin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import preview_service  # noqa: E402
from app.services.raster_shards import split_raster_into_shards  # noqa: E402


def build_source(path: str) -> tuple:
    """A categorical-looking raster: a few classes over a known extent."""
    width, height = 400, 240
    left, top = 108.0, 24.0
    res = 0.001
    data = np.zeros((1, height, width), dtype=np.uint8)
    data[0, : height // 2, :] = 10                    # 甘蔗 (top half)
    data[0, height // 2 :, : width // 3] = 20         # 水稻
    data[0, height // 2 :, width // 3 :] = 50         # 背景
    transform = from_origin(left, top, res, res)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
        nodata=0,
    ) as dst:
        dst.write(data)
    return transform, data, width, height


def counts(arr) -> dict:
    values, nums = np.unique(arr, return_counts=True)
    return dict(zip(values.tolist(), nums.tolist()))


def main() -> int:
    return _run()


def _run() -> int:
    with tempfile.TemporaryDirectory(prefix="shard_test_") as workdir:
        try:
            return _run_in(workdir)
        finally:
            # preview_service keeps open rasterio handles in an lru_cache; on
            # Windows an open file cannot be deleted, so drop them before the
            # temp dir is cleaned up.
            preview_service.clear_cache()


def _run_in(workdir: str) -> int:
    if True:
        # Redirect the preview service at the temp dir — no real job data
        # is read or written.
        preview_service.JOBS_DIR = workdir
        job_id = "test_job"
        job_dir = os.path.join(workdir, job_id)
        os.makedirs(job_dir, exist_ok=True)

        src = os.path.join(job_dir, "demo.tif")
        transform, data, width, height = build_source(src)
        print(f"source: {width}x{height}  transform={transform}")

        # --- split into 2 shards -------------------------------------------
        shards = split_raster_into_shards(
            src, out_dir=job_dir, base_name="demo", num_shards=2, remove_source=True
        )
        names = [s["name"] for s in shards]
        print(f"shards: {names}")
        assert len(shards) == 2, shards
        assert not os.path.exists(src), "source should be removed"

        # each shard is a valid raster with its own transform
        for shard in shards:
            with rasterio.open(shard["path"]) as ds:
                assert ds.width == width, (ds.width, width)
                print(
                    f"  {shard['name']}: {ds.width}x{ds.height} "
                    f"rows {shard['row_start']}..{shard['row_end']} "
                    f"origin=({ds.transform.c}, {ds.transform.f})"
                )

        # --- stitch them back through the preview service ------------------
        info = preview_service.get_mosaic_info(job_id, names)
        print(
            f"mosaic info: type={info['type']}  "
            f"preview={info['preview_width']}x{info['preview_height']}  "
            f"mosaic={info['mosaic']}  count={info['mosaic_count']}  "
            f"bounds={info['bounds']}"
        )
        assert info["mosaic"] is True
        assert info["mosaic_count"] == 2
        src_counts = counts(data[0])
        mosaic_counts = info["stats"].get("class_counts") or {}
        print(f"source class counts : {src_counts}")
        print(f"mosaic class counts : {mosaic_counts}")
        # 每个类别的像元数必须一致 —— 说明两个分片真的各取到了自己的那一段
        for cls, n in src_counts.items():
            assert mosaic_counts.get(str(cls), mosaic_counts.get(cls)) == n, (
                cls,
                n,
                mosaic_counts,
            )

        png = preview_service.get_mosaic_preview_path(job_id, names)
        print(f"mosaic png: {png}  ({os.path.getsize(png)} bytes)")
        assert os.path.isfile(png)

        with rasterio.open(png) as ds:
            mosaic = ds.read(1)
        print(f"mosaic raster: {mosaic.shape}")

        src_bounds = rasterio.transform.array_bounds(height, width, transform)
        mosaic_bounds = info["bounds"]
        print(f"source bounds  : {src_bounds}")
        print(f"mosaic bounds  : {mosaic_bounds}")
        # info bounds = [[minx, miny], [maxx, maxy]]
        assert abs(mosaic_bounds[0][0] - src_bounds[0]) < 1e-6, mosaic_bounds
        assert abs(mosaic_bounds[0][1] - src_bounds[1]) < 1e-6, mosaic_bounds
        assert abs(mosaic_bounds[1][0] - src_bounds[2]) < 1e-6, mosaic_bounds
        assert abs(mosaic_bounds[1][1] - src_bounds[3]) < 1e-6, mosaic_bounds

        print("\nOK — shards split and stitched back into the original extent.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
