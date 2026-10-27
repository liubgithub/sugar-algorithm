"""Ground truth for a finished GEE classification asset.

Answers one question: does the asset itself contain classified pixels over
the whole ROI, or only over part of it?  That distinguishes an *incomplete
download* (GEE has the data, the local GeoTIFF does not) from a *correct
download of a mostly-nodata asset* (the algorithm classified little).

Usage::

    python scripts/probe_classification_asset.py <job_id>
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import DB_PATH  # noqa: E402
from app.services.gee_auth import initialize_ee, project_for_job  # noqa: E402


def load_job(job_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"job not found: {job_id}")
    job = dict(row)
    job["result"] = json.loads(job["result"] or "{}")
    job["params"] = json.loads(job["params"] or "{}")
    return job


def main() -> int:
    import ee

    job_id = sys.argv[1]
    job = load_job(job_id)
    metrics = (job.get("result") or {}).get("metrics") or {}
    asset_id = str(metrics.get("asset_id") or "").strip()
    roi_ids = metrics.get("roi_asset_ids") or []
    if not asset_id:
        raise SystemExit("job result has no asset_id")

    project = project_for_job(job)
    initialize_ee(project)

    info = ee.data.getAsset(asset_id)
    band = info["bands"][0]
    grid = band.get("grid", {})
    print("=== asset ===")
    print("asset   :", asset_id)
    print("bands   :", [(b.get("id"), b.get("dataType")) for b in info["bands"]])
    print("grid crs:", grid.get("crsCode"))
    print("dims    :", grid.get("dimensions"))
    print("affine  :", grid.get("affineTransform"))

    roi = ee.FeatureCollection(roi_ids[0]) if roi_ids else None
    region = roi.geometry() if roi is not None else ee.Image(asset_id).geometry()
    print("\n=== roi bounds (WGS84) ===")
    print(region.bounds().getInfo())

    img = ee.Image(asset_id)
    print("\n=== class histogram over ROI (scale=30, EPSG:4326) ===")
    hist = img.reduceRegion(
        ee.Reducer.frequencyHistogram(),
        region,
        30,
        "EPSG:4326",
        maxPixels=1e13,
    ).getInfo()
    print(json.dumps(hist, indent=2, ensure_ascii=False))

    print("\n=== pixel count over ROI (scale=30) ===")
    cnt = img.reduceRegion(
        ee.Reducer.count(),
        region,
        30,
        "EPSG:4326",
        maxPixels=1e13,
    ).getInfo()
    print(json.dumps(cnt, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
