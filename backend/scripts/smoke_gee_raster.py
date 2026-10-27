"""Smoke test for the tiled GEE raster downloader.

Downloads a couple of tiles of the real harvest asset into a scratch
GeoTIFF and verifies the tile alignment, so the full download can be
trusted before it is started on a multi-gigabyte asset.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import rasterio
from rasterio.windows import Window

from app.services.gee_auth import initialize_ee
from app.services import gee_raster

ASSET_ID = "projects/guangxi-try/assets/Harvest_2025_20260421"
SCRATCH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "_scratch_tile_test.tif")
)


def main() -> int:
    project = "guangxi-try"
    initialize_ee(project)

    grid = gee_raster.read_asset_grid(ASSET_ID)
    print("asset grid:")
    print(f"  crs        = {grid.crs}")
    print(f"  size       = {grid.width} x {grid.height} "
          f"({grid.width * grid.height / 1e9:.2f} G px)")
    print(f"  transform  = {grid.transform}")
    print(f"  bytes/px   = {grid.bytes_per_px}")

    tile_px = grid.max_tile_px(gee_raster._DEFAULT_TILE_PX)
    tiles = gee_raster.plan_tiles(grid, tile_px)
    n_cols = (grid.width + tile_px - 1) // tile_px
    n_rows = (grid.height + tile_px - 1) // tile_px
    print(f"  tile size  = {tile_px} px  ({n_cols} x {n_rows} = {len(tiles)} tiles)")

    # --- download 3 tiles that are known to carry data --------------------
    sample = [t for t in tiles if t.col in (10, 20) and t.row in (7, 14)][:3]
    if not sample:
        sample = tiles[:3]
    print(f"\nsample tiles: {[t.key for t in sample]}")

    out_transform = grid.transform
    profile = {
        "driver": "GTiff",
        "width": grid.width,
        "height": grid.height,
        "count": 1,
        "dtype": "uint8",
        "crs": grid.crs,
        "transform": out_transform,
        "nodata": 0,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
        "BIGTIFF": "IF_SAFER",
    }

    import ee

    img = ee.Image(ASSET_ID)
    if os.path.isfile(SCRATCH):
        os.remove(SCRATCH)

    with rasterio.open(SCRATCH, "w", **profile) as dst:
        for tile in sample:
            t0 = time.monotonic()
            url = img.getDownloadURL(
                {
                    "name": f"tile_{tile.col}_{tile.row}",
                    "filePerBand": False,
                    "crs": grid.crs,
                    "crs_transform": gee_raster.tile_crs_transform(grid, tile),
                    "dimensions": [tile.width, tile.height],
                }
            )
            payload = gee_raster._http_get(url)
            arr, tile_transform, _nodata = gee_raster._read_tile(payload)
            gee_raster._check_scales_match(out_transform, tile_transform, tile.key)
            col_off, row_off = gee_raster._window_for_tile(out_transform, tile_transform)
            dst.write(arr[0] if arr.ndim == 3 else arr, 1,
                      window=Window(col_off, row_off, arr.shape[-1], arr.shape[-2]))
            print(
                f"  tile {tile.key}: {arr.shape} dtype={arr.dtype} "
                f"nonzero={int((arr != 0).sum())} "
                f"window=({col_off},{row_off}) "
                f"crs_transform={tile_transform[:6]} "
                f"{time.monotonic() - t0:.1f}s"
            )

    with rasterio.open(SCRATCH) as ds:
        print(f"\nscratch raster: {ds.width} x {ds.height} {ds.crs} nodata={ds.nodata}")
        print(f"  transform = {ds.transform}")
        for tile in sample:
            arr = ds.read(
                1,
                window=Window(tile.col_off, tile.row_off, tile.width, tile.height),
            )
            print(f"  re-read tile {tile.key}: nonzero={int((arr != 0).sum())}")

    print(f"\nOK -> {SCRATCH} ({os.path.getsize(SCRATCH)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
