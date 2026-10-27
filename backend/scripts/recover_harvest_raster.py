"""Re-download a finished job's GEE asset so its local GeoTIFF can be restored.

Background
----------
A finished Pattern-B job keeps its result as a local GeoTIFF inside
``data/jobs/<job_id>/``.  The authoritative copy still lives in GEE
(``projects/<project>/assets/<name>``), so the local file can always be
rebuilt from the asset — byte-for-byte the same raster, because the
downloader writes on the asset's own pixel grid.

This script does exactly that:

  1. reads the job row (asset id, description, expected geometry),
  2. downloads the asset into a ``.recovering`` file next to the target,
  3. moves it into place only after the geometry checks out,
  4. re-renders the preview info and compares it with the stored one.

Run from ``backend/``::

    python scripts/recover_harvest_raster.py [job_id]
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import DATA_DIR, DB_PATH  # noqa: E402
from app.services.gee_auth import gee_session, project_for_job  # noqa: E402
from app.services.gee_raster import download_asset_to_geotiff  # noqa: E402
from app.services import preview_service  # noqa: E402

DEFAULT_JOB = "8b36a88240aa4fe196a9950f33c146e3"


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
    job_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_JOB
    job = load_job(job_id)
    result = job["result"]
    metrics = result.get("metrics") or {}

    asset_id = str(metrics.get("asset_id") or "").strip()
    description = str(metrics.get("description") or "").strip()
    if not asset_id or not description:
        raise SystemExit("job result has no asset_id / description")

    files = result.get("files") or []
    if not files:
        raise SystemExit("job result has no files")
    filename = files[0]["name"]
    target = os.path.join(preview_service.JOBS_DIR, job_id, filename)
    job_dir = os.path.dirname(target)

    if os.path.isfile(target):
        print(f"already there: {target}")
        return 0

    project = project_for_job(job)
    print(f"job      : {job_id} ({job['algorithm']})")
    print(f"asset    : {asset_id}")
    print(f"project  : {project}")
    print(f"target   : {target}")

    tmp_path = target + ".recovering"

    def _progress(pct: int, message: str) -> None:
        print(f"[{pct:3d}%] {message}", flush=True)

    with gee_session(project):
        stats = download_asset_to_geotiff(
            asset_id,
            tmp_path,
            progress_callback=_progress,
        )

    # --- verify the geometry before it takes the real name ---------------
    expected_w = int(metrics.get("raster_width") or 0)
    expected_h = int(metrics.get("raster_height") or 0)
    if expected_w and stats["width"] != expected_w:
        raise SystemExit(f"width mismatch: {stats['width']} != {expected_w}")
    if expected_h and stats["height"] != expected_h:
        raise SystemExit(f"height mismatch: {stats['height']} != {expected_h}")

    os.replace(tmp_path, target)
    size_bytes = os.path.getsize(target)
    print(f"\nrestored: {target}  {size_bytes / 1024 / 1024:.1f} MB")

    # --- re-render the preview and compare with the stored info -----------
    info = preview_service.get_info(job_id, filename)
    info_path = os.path.join(
        preview_service.JOBS_DIR, job_id, "preview", f"{filename}.info.json"
    )
    if os.path.isfile(info_path):
        with open(info_path, encoding="utf-8") as fh:
            old = json.load(fh)
        for key in ("bounds", "preview_width", "preview_height", "width", "height"):
            same = old.get(key) == info.get(key)
            print(f"  {key:<16} old={old.get(key)}  new={info.get(key)}  "
                  f"{'OK' if same else 'DIFFERS'}")
    print(f"  stats            {info.get('stats')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
