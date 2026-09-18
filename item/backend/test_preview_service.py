"""Unit test for preview_service.

Generates three synthetic GeoTIFFs (continuous, binary, categorical)
under the real JOBS_DIR (config is captured at import time so we can't
easily relocate it) and asserts analyze() / pixel_value() behave correctly.
Cleans up the test artifacts at exit.
"""
import shutil
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds

from app.core.config import JOBS_DIR
from app.services import preview_service as ps


TEST_JOBS = ["test_preview_cont", "test_preview_bin", "test_preview_cat"]


def _write_geotiff(path: Path, array: np.ndarray, bounds, nodata=None) -> None:
    height, width = array.shape
    transform = from_bounds(*bounds, width, height)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype=array.dtype,
        transform=transform,
        crs="EPSG:4326",
        nodata=nodata,
    ) as dst:
        dst.write(array, 1)


def _make_job_dir(job_id: str) -> Path:
    d = Path(JOBS_DIR) / job_id
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_continuous() -> None:
    print("--- continuous ---")
    job_id = TEST_JOBS[0]
    job_dir = _make_job_dir(job_id)
    bounds = (100.0, 22.0, 100.5, 22.5)
    arr = np.linspace(0.0, 10.0, 50 * 50, dtype=np.float32).reshape(50, 50)
    tif_path = job_dir / "yield_final.tif"
    _write_geotiff(tif_path, arr, bounds, nodata=-9999.0)
    ps.clear_cache()

    info = ps.analyze(job_id, "yield_final.tif")
    assert info["type"] == "continuous", info["type"]
    assert info["width"] == 50 and info["height"] == 50
    assert abs(info["bounds"][0][0] - 100.0) < 1e-6
    assert abs(info["bounds"][1][1] - 22.5) < 1e-6
    s = info["stats"]
    assert s["min"] == 0.0
    assert s["max"] > 9.5
    assert s["nodata_count"] == 0

    png_path = ps.get_preview_path(job_id, "yield_final.tif")
    assert png_path.is_file(), png_path
    assert png_path.stat().st_size > 100

    pv = ps.pixel_value(job_id, "yield_final.tif", 100.25, 22.25)
    assert pv["type"] == "continuous"
    assert pv["value"] is not None
    print(f"  OK  type=continuous  bounds ok  stats.min={s['min']} max={s['max']:.3f}")
    print(f"  OK  PNG written ({png_path.stat().st_size} bytes)")
    print(f"  OK  pixel_value at center = {pv['value']:.4f}")


def test_binary() -> None:
    print()
    print("--- binary ---")
    job_id = TEST_JOBS[1]
    job_dir = _make_job_dir(job_id)
    bounds = (100.0, 22.0, 100.1, 22.1)
    arr = np.zeros((20, 20), dtype=np.uint8)
    arr[5:15, 5:15] = 1
    tif_path = job_dir / "harvest.tif"
    _write_geotiff(tif_path, arr, bounds)
    ps.clear_cache()

    info = ps.analyze(job_id, "harvest.tif")
    assert info["type"] == "binary", info["type"]
    assert info["stats"]["count_1"] == 100
    assert info["stats"]["count_0"] == 300
    assert info["classes"] is not None
    print(f"  OK  type=binary  count_1={info['stats']['count_1']} count_0={info['stats']['count_0']}")

    png = ps.get_preview_path(job_id, "harvest.tif")
    assert png.is_file()
    print(f"  OK  PNG written ({png.stat().st_size} bytes)")


def test_categorical() -> None:
    print()
    print("--- categorical ---")
    job_id = TEST_JOBS[2]
    job_dir = _make_job_dir(job_id)
    bounds = (100.0, 22.0, 100.1, 22.1)
    arr = np.zeros((30, 30), dtype=np.uint8)
    arr[:10, :10] = 0     # 100 pixels class 0
    arr[:10, 10:] = 1     # 200 pixels class 1
    arr[10:, :15] = 2     # 300 pixels class 2
    arr[10:, 15:] = 3     # 300 pixels class 3
    tif_path = job_dir / "classified.tif"
    _write_geotiff(tif_path, arr, bounds)
    ps.clear_cache()

    info = ps.analyze(job_id, "classified.tif")
    assert info["type"] == "categorical", info["type"]
    assert info["classes"] is not None
    assert set(info["classes"].keys()) == {0, 1, 2, 3}, info["classes"]
    cc = info["stats"]["class_counts"]
    assert cc[0] == 100
    assert cc[1] == 200
    assert cc[2] == 300
    assert cc[3] == 300
    print(f"  OK  type=categorical  classes={sorted(info['classes'].keys())}")

    pv = ps.pixel_value(job_id, "classified.tif", 100.02, 22.05)
    assert pv["type"] == "categorical"
    assert pv["value"] in (0, 1, 2, 3)
    print(f"  OK  pixel_value sample = {pv['value']}")

    png = ps.get_preview_path(job_id, "classified.tif")
    assert png.is_file()
    print(f"  OK  PNG written ({png.stat().st_size} bytes)")


def cleanup() -> None:
    for job_id in TEST_JOBS:
        d = Path(JOBS_DIR) / job_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    try:
        test_continuous()
        test_binary()
        test_categorical()
        print()
        print("ALL OK")
    finally:
        cleanup()