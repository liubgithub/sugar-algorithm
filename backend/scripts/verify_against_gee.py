"""Cross-check a downloaded classification raster against the GEE asset.

Local checks (grid / histogram / tile coverage) can only say "this file is
self-consistent".  This script asks the *source* whether the copy is
complete: it pulls the class histogram straight from the GEE asset and
compares the class proportions with the local raster.

If a download tile had been lost, the missing area would show up as a
shifted proportion for the classes that dominate that area.

Usage from ``backend/``::

    python scripts/verify_against_gee.py <asset_id> <local_tif> [project]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import rasterio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.gee_auth import gee_session  # noqa: E402

CLASSES = (10, 20, 30, 40, 50)


def local_proportions(path: str) -> dict:
    """Class proportions of the local raster, ignoring nodata.

    Read from the coarsest overview — a uniform decimation of the whole map,
    so the proportions are unbiased and it costs seconds instead of minutes.
    """
    with rasterio.open(path) as ds:
        overviews = ds.overviews(1)
        factor = max(overviews) if overviews else 1
        h, w = ds.height // factor, ds.width // factor
        arr = ds.read(1, out_shape=(h, w), masked=False)
    values, counts = np.unique(arr, return_counts=True)
    total = sum(c for v, c in zip(values.tolist(), counts.tolist()) if v in CLASSES)
    return {c: (counts[values.tolist().index(c)] / total) for c in CLASSES if c in values.tolist()}


def gee_proportions(asset_id: str, roi_asset_id: str, scale: int = 200) -> dict:
    """Class proportions computed by GEE over the ROI polygon."""
    import ee

    img = ee.Image(asset_id)
    roi = ee.FeatureCollection(roi_asset_id).geometry()
    hist = img.reduceRegion(
        reducer=ee.Reducer.frequencyHistogram(),
        geometry=roi,
        scale=scale,
        maxPixels=1e9,
        tileScale=8,
    ).getInfo()
    counts = hist.get("classification") or hist.get("constant") or {}
    total = sum(counts.get(str(c), 0) for c in CLASSES)
    return {c: counts.get(str(c), 0) / total for c in CLASSES if total}


def main() -> int:
    asset_id = sys.argv[1]
    local_tif = sys.argv[2]
    project = sys.argv[3] if len(sys.argv) > 3 else "guangxi-try"
    roi_asset_id = "projects/guangxi-try/assets/Guangxi_shp"

    local = local_proportions(local_tif)
    print("local  :", {c: f"{p * 100:.2f}%" for c, p in sorted(local.items())})

    with gee_session(project) as ctx:
        remote = gee_proportions(asset_id, roi_asset_id)
    print("gee    :", {c: f"{p * 100:.2f}%" for c, p in sorted(remote.items())})

    print("\n逐类差异（|本地 - GEE| / GEE）:")
    worst = 0.0
    for c in CLASSES:
        lp = local.get(c, 0.0)
        rp = remote.get(c, 0.0)
        diff = abs(lp - rp) / rp if rp else (0.0 if lp == 0 else 1.0)
        worst = max(worst, diff)
        flag = "  <-- 偏差过大" if diff > 0.05 else ""
        print(f"  class {c:>3}: local {lp * 100:6.2f}%   gee {rp * 100:6.2f}%   diff {diff * 100:5.2f}%{flag}")

    if worst <= 0.05:
        print("\nOK — 本地栅格与 GEE 资产的类别占比一致（差异 < 5%），下载完整。")
        return 0
    print("\n!! 类别占比与 GEE 明显不符 —— 本地文件可能不完整。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
