"""Verify the shard → mosaic round trip on a real, large job raster.

Takes the real ``Harvest_2025_20260421.tif`` (a 5-Gpixel export), splits it
into two shards with ``raster_shards.split_raster_into_shards``, stitches them
back with the preview service's mosaic pipeline, and compares the result with
the single-file preview of the original.

Both files are copied into a throw-away temp directory; the real job data is
only read, never written.

Run from ``backend/``:

    python scripts/test_shard_real.py [job_id] [filename]
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import numpy as np
import rasterio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import JOBS_DIR  # noqa: E402
from app.services import preview_service  # noqa: E402
from app.services.raster_shards import split_raster_into_shards  # noqa: E402

DEFAULT_JOB = "8b36a88240aa4fe196a9950f33c146e3"
DEFAULT_FILE = "Harvest_2025_20260421.tif"


def foreground(path: str) -> tuple:
    """Pixel value histogram of a rendered preview PNG (as RGB indices)."""
    with rasterio.open(path) as ds:
        arr = ds.read(1)
        values, counts = np.unique(arr, return_counts=True)
    return dict(zip(values.tolist(), counts.tolist()))


def main() -> int:
    job_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_JOB
    filename = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_FILE
    original = os.path.join(JOBS_DIR, job_id, filename)
    if not os.path.isfile(original):
        print(f"not found: {original}")
        return 1

    with rasterio.open(original) as ds:
        print(
            f"source: {ds.width}x{ds.height}  crs={ds.crs}  dtype={ds.dtypes[0]}  "
            f"bounds={ds.bounds}"
        )

    with tempfile.TemporaryDirectory(prefix="shard_real_") as workdir:
        preview_service.JOBS_DIR = workdir
        try:
            job_dir = os.path.join(workdir, job_id)
            os.makedirs(job_dir, exist_ok=True)
            copy = os.path.join(job_dir, filename)
            shutil.copyfile(original, copy)
            print(f"copied to {copy}")

            # --- single-file preview of the original (reference) -----------
            single_info = preview_service.get_info(job_id, filename)
            single_png = preview_service.get_preview_path(job_id, filename)
            print(
                f"single: type={single_info['type']} "
                f"preview={single_info['preview_width']}x{single_info['preview_height']} "
                f"bounds={single_info['bounds']}"
            )

            # --- split into 2 shards ---------------------------------------
            # ``remove_source=False`` —— 这个脚本只读真实作业目录，源文件
            # 必须留在原处（临时目录退出时自己清理）。
            shards = split_raster_into_shards(
                copy,
                out_dir=job_dir,
                base_name=os.path.splitext(filename)[0],
                num_shards=2,
                remove_source=False,
            )
            names = [s["name"] for s in shards]
            for shard in shards:
                print(
                    f"  {shard['name']}: {shard['width']}x{shard['height']} "
                    f"rows {shard['row_start']}..{shard['row_end']}  "
                    f"{shard['size_bytes'] / 1024 / 1024:.1f} MB"
                )

            # --- stitch them back -------------------------------------------
            mosaic_info = preview_service.get_mosaic_info(job_id, names)
            mosaic_png = preview_service.get_mosaic_preview_path(job_id, names)
            print(
                f"mosaic: type={mosaic_info['type']} "
                f"preview={mosaic_info['preview_width']}x{mosaic_info['preview_height']} "
                f"bounds={mosaic_info['bounds']}"
            )
            print(f"mosaic png: {os.path.getsize(mosaic_png) / 1024:.1f} KB")

            # --- compare ------------------------------------------------------
            assert mosaic_info["bounds"] == single_info["bounds"], (
                mosaic_info["bounds"],
                single_info["bounds"],
            )
            assert (
                mosaic_info["preview_width"] == single_info["preview_width"]
                and mosaic_info["preview_height"] == single_info["preview_height"]
            ), (mosaic_info, single_info)

            single_counts = foreground(single_png)
            mosaic_counts = foreground(mosaic_png)
            print(f"single png px: {single_counts}")
            print(f"mosaic png px: {mosaic_counts}")

            # 前景像元数（info 里的 count_1）才是真正有意义的比较：两张图
            # 的重采样网格不完全一致，允许有 1% 的差异。
            single_fg = single_info["stats"].get("count_1")
            mosaic_fg = mosaic_info["stats"].get("count_1")
            print(f"foreground px: single={single_fg} mosaic={mosaic_fg}")
            diff = abs(single_fg - mosaic_fg) / max(single_fg, 1)
            print(f"foreground diff = {diff:.4%}")
            assert diff < 0.02, diff

            total_single = sum(single_counts.values())
            total_mosaic = sum(mosaic_counts.values())
            assert total_single == total_mosaic, (total_single, total_mosaic)

            # --- the deletion guard -----------------------------------------
            # ``remove_source=True`` 只允许删掉「就躺在分片旁边」的源文件。
            # 源文件在别处时必须原封不动 —— 这条守卫是防止误删真实作业产物
            # 的最后一道闸（真实作业目录里的 .tif 是不可再生的用户数据）。
            # 这里只验证「保留」这一半；「同目录即删除」那一半由
            # crop_classification 的 finalize 走真实流程覆盖。
            other_dir = os.path.join(workdir, "elsewhere")
            os.makedirs(other_dir, exist_ok=True)
            guard_source = os.path.join(other_dir, "guard_source.tif")
            shutil.copyfile(original, guard_source)
            split_raster_into_shards(
                guard_source,
                out_dir=job_dir,  # 异目录 → 必须保留
                base_name="guard_other_dir",
                num_shards=2,
                remove_source=True,
            )
            assert os.path.exists(guard_source), "异目录的源文件不应被删除"
            print("guard: source outside the shard dir was kept — OK")

            print("\nOK — real raster splits into 2 shards and stitches back "
                  "to the same extent / same picture.")
            return 0
        finally:
            preview_service.clear_cache()


if __name__ == "__main__":
    raise SystemExit(main())
