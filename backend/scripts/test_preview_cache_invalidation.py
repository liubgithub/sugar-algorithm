"""Regression test: the preview cache must notice that the file changed.

The failure this guards against is very specific and very annoying:

    1. A job publishes ``<name>.tif``.
    2. The result page asks for its preview -> the service caches the
       dataset (open file handle) + writes ``.info.json`` / ``.png``.
    3. The same path is then overwritten with a *new* file (re-download of a
       GEE asset, a re-run of the algorithm, a re-published result).
    4. The result page still shows the OLD preview / OLD legend / OLD class
       counts, because the on-disk ``.info.json`` still matches the old file.

Note step 3 needs the handle from step 2 to be gone first: on Windows an
open file cannot be replaced at all (PermissionError).  That is exactly why
``preview_service`` now drops its cached handles whenever a read fails — a
broken result file must not keep its own path locked forever.

Everything happens inside a throw-away temp directory; the preview service's
``JOBS_DIR`` is redirected there, so no real job data is touched.

Run from ``backend/``::

    python scripts/test_preview_cache_invalidation.py
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


def write_raster(path: str, classes: dict) -> None:
    """Write a 40x30 categorical raster: ``{value: number of rows}``."""
    height, width = 30, 40
    data = np.zeros((height, width), dtype=np.uint8)
    row = 0
    for value, count in classes.items():
        data[row : row + count, :] = value
        row += count
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_origin(108.0, 24.0, 0.001, 0.001),
        nodata=0,
    ) as dst:
        dst.write(data, 1)


def counts_of(info: dict) -> dict:
    """class_counts with int keys (in-memory it is ``{int: int}``)."""
    raw = info.get("stats", {}).get("class_counts") or {}
    return {int(k): v for k, v in raw.items()}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="preview_cache_test_") as workdir:
        try:
            return _run_in(workdir)
        finally:
            # preview_service keeps open rasterio handles in an lru_cache; on
            # Windows an open file cannot be deleted, so drop them first.
            preview_service.clear_cache()


def _run_in(workdir: str) -> int:
    preview_service.JOBS_DIR = workdir
    job_id = "cache_test_job"
    job_dir = os.path.join(workdir, job_id)
    os.makedirs(job_dir, exist_ok=True)
    tif = os.path.join(job_dir, "result.tif")
    name = "result.tif"

    # --- 1. first version: 10 / 20 ---------------------------------------
    write_raster(tif, {10: 10, 20: 20})
    info1 = preview_service.get_info(job_id, name)
    print("v1 class counts:", counts_of(info1))
    assert counts_of(info1).get(10) == 400, counts_of(info1)
    assert counts_of(info1).get(20) == 800, counts_of(info1)
    png1 = preview_service.get_preview_path(job_id, name)
    png1_mtime = png1.stat().st_mtime_ns
    assert png1.is_file()

    # --- 2. publish a new version onto the same path ----------------------
    #    (this is what the downloader does once every tile has arrived)
    staged = os.path.join(job_dir, "result.tif.new")
    write_raster(staged, {30: 30})
    # The handle cached by step 1 has to be gone first — on Windows an open
    # file cannot be replaced (this is why preview_service releases handles
    # when a read fails).
    preview_service.clear_cache()
    os.replace(staged, tif)
    print("new version published onto the same path")

    # --- 3. the cached info must NOT be served ----------------------------
    info2 = preview_service.get_info(job_id, name)
    print("v2 class counts:", counts_of(info2))
    assert counts_of(info2).get(10) is None, "stale info.json was served!"
    assert counts_of(info2).get(30) == 1200, counts_of(info2)

    # --- 4. the PNG must be regenerated too --------------------------------
    png2 = preview_service.get_preview_path(job_id, name)
    print("png regenerated:", png2.stat().st_mtime_ns != png1_mtime)
    assert png2.stat().st_mtime_ns != png1_mtime, "stale PNG was served!"

    # --- 5. editing the class sidecar must re-colour the legend ----------
    #    (sidecar 决定图例的类名和颜色；用户在 IDE 里改完，页面要跟着变)
    sidecar = os.path.join(job_dir, "result.tif.metadata.json")
    with open(sidecar, "w", encoding="utf-8") as fh:
        fh.write('{"class_labels": {"30": "甘蔗"}, '
                 '"class_colors": {"30": "#FF0000"}}')
    info3 = preview_service.get_info(job_id, name)
    # class_metadata 的 key 在内存里是 int（落盘成 JSON 才是 string）
    meta = {int(k): v for k, v in (info3.get("class_metadata") or {}).items()}
    print("sidecar label/color:", meta.get(30))
    assert (meta.get(30) or {}).get("label") == "甘蔗", meta
    assert (meta.get(30) or {}).get("color") == "#FF0000", meta
    assert info3["classes"].get(30) == "#FF0000", info3["classes"]

    # --- 6. a failed read must not pin the file handle ---------------------
    #    (on Windows an open handle blocks the next publish onto that path)
    preview_service.clear_cache()
    with open(tif, "wb") as fh:
        fh.write(b"not a tiff at all")
    try:
        preview_service.get_info(job_id, name)
        print("expected the broken file to fail")
        return 1
    except Exception as exc:  # noqa: BLE001
        print("broken file correctly rejected:", type(exc).__name__)
    cache = preview_service._open_dataset.cache_info()
    print("dataset cache after failure:", cache)
    assert cache.currsize == 0, "a broken file kept its handle open"

    print("\nOK — preview cache follows the file on disk, and failures release it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
