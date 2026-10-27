"""Download a GEE asset to a local GeoTIFF — download only, no side effects.

This is the *narrow* half of the recovery: it only ever writes

    <target>.partial      (the in-progress raster)
    <target>.tiles.json   (the resume manifest)
    <target>              (published only once every tile has arrived)

It never touches the job database, never renames or deletes existing files,
and never moves anything aside.  Run it as often as you like — a re-run
resumes from the manifest instead of re-downloading what it already has.

Usage from ``backend/``::

    python scripts/download_gee_asset.py <asset_id> <target_tif> [project] [--workers N]
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.gee_auth import gee_session  # noqa: E402
from app.services.gee_raster import download_asset_to_geotiff  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--workers"]
    workers = 4
    if "--workers" in sys.argv:
        workers = int(sys.argv[sys.argv.index("--workers") + 1])
    if len(args) < 2:
        raise SystemExit(
            "用法: python scripts/download_gee_asset.py <asset_id> <target_tif> "
            "[project] [--workers N]"
        )
    asset_id = args[0]
    target = os.path.abspath(args[1])
    project = args[2] if len(args) > 2 else os.environ.get("EE_PROJECT") or ""

    print(f"asset  : {asset_id}")
    print(f"target : {target}")
    print(f"project: {project or '(未指定)'}")
    print(f"workers: {workers}")

    def _progress(pct: int, message: str) -> None:
        print(f"[{pct:3d}%] {message}", flush=True)

    ctx = gee_session(project) if project else gee_session("")
    with ctx:
        stats = download_asset_to_geotiff(
            asset_id,
            target,
            max_workers=workers,
            progress_callback=_progress,
        )

    print("\n下载完成:")
    for key in ("width", "height", "count", "dtype", "tile_count", "size_bytes"):
        print(f"  {key:<12} {stats.get(key)}")
    print(f"  path          {stats.get('path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
