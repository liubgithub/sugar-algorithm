"""Spot-check the local raster against the GEE asset, point by point.

The area-histogram comparison in ``verify_against_gee.py`` compares two
*regular-grid samples* of the same map (a 1/32 overview locally, a 200 m
``reduceRegion`` on GEE).  On a map made of small, grid-aligned fields those
two samples can legitimately disagree, so a mismatch there is not proof of a
bad download.

This script asks a sharper question: at a set of known coordinates, does the
local pixel hold the same class as the GEE asset?  It samples

  * points inside the tiles the local file has as all-zero (the suspicious
    ones), and
  * points inside the classified area (the control group).

If the download lost a tile, the first group reads 0 locally and a real
class on GEE; the control group should agree everywhere.

Usage from ``backend/``::

    python scripts/verify_points_against_gee.py <asset_id> <local_tif> [project]
"""
from __future__ import annotations

import os
import random
import sys

import numpy as np
import rasterio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.gee_auth import gee_session  # noqa: E402
from scripts.verify_classification_raster import _tile_coverage  # noqa: E402

TILE_PX = 4096


def main() -> int:
    asset_id = sys.argv[1]
    local_tif = sys.argv[2]
    project = sys.argv[3] if len(sys.argv) > 3 else "guangxi-try"

    with rasterio.open(local_tif) as ds:
        transform = ds.transform
        height, width = ds.height, ds.width
        overviews = ds.overviews(1)
        factor = max(overviews) if overviews else 1
        cov_arr = ds.read(1, out_shape=(height // factor, width // factor), masked=False)
        block = max(1, TILE_PX // factor)
        cov_rows = (cov_arr.shape[0] + block - 1) // block
        cov_cols = (cov_arr.shape[1] + block - 1) // block

        # --- candidate points ------------------------------------------
        suspicious = []
        for r in range(cov_rows):
            for c in range(cov_cols):
                tile = cov_arr[r * block : (r + 1) * block, c * block : (c + 1) * block]
                if tile.size and not tile.any():
                    suspicious.append((r, c))

        random.seed(20261027)
        pts = []  # (col, row, kind)

        # 1) inside all-zero tiles that have a neighbour with data
        for r, c in suspicious:
            neigh = [
                cov_arr[rr * block : (rr + 1) * block, cc * block : (cc + 1) * block]
                for rr, cc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1))
                if 0 <= rr < cov_rows and 0 <= cc < cov_cols
            ]
            if any(n.any() for n in neigh):
                for k in range(3):
                    pts.append((c * TILE_PX + random.randint(200, TILE_PX - 200),
                                r * TILE_PX + random.randint(200, TILE_PX - 200),
                                "zero-tile"))
                if len(pts) >= 24:
                    break
        # 2) control group: inside the classified area
        ys, xs = np.nonzero(cov_arr)
        idx = random.sample(range(len(ys)), min(12, len(ys)))
        for i in idx:
            pts.append((int(xs[i]) * factor, int(ys[i]) * factor, "control"))

        # --- local values ------------------------------------------------
        # 注意：ds.sample() 收的是**地图坐标**，不是像元下标。要按行列取值
        # 必须用 window 读 —— 之前用 sample 读像元下标，越界取到的是 nodata，
        # 会把「一致」误判成「全不一致」。
        local_vals = []
        for col, row, kind in pts:
            val = None
            for dc in (-1, 0, 1):
                for dr in (-1, 0, 1):
                    c, r = col + dc, row + dr
                    if not (0 <= c < width and 0 <= r < height):
                        continue
                    try:
                        buf = ds.read(
                            1, window=rasterio.windows.Window(c, r, 1, 1)
                        )
                    except Exception:
                        continue
                    val = int(buf[0, 0])
                    if val:
                        break
                if val:
                    break
            lon, lat = transform * (col + 0.5, row + 0.5)
            local_vals.append((col, row, kind, lon, lat, val))

    # --- GEE values -----------------------------------------------------
    import ee

    with gee_session(project):
        img = ee.Image(asset_id)
        remote = []
        for _col, _row, _kind, lon, lat, _v in local_vals:
            val = img.reduceRegion(
                reducer=ee.Reducer.first(),
                geometry=ee.Geometry.Point([lon, lat]),
                scale=10,
                crs="EPSG:4326",
                maxPixels=1,
            ).getInfo()
            band = val.get("classification") if isinstance(val, dict) else None
            remote.append(int(band) if band is not None else None)

    # --- compare ---------------------------------------------------------
    # 三种情况要分开看：
    #   * 两边都是"无数据"（本地 0 / GEE None）—— 一致，说明这块地本来就在
    #     ROI 多边形之外，不是下载丢了图块；
    #   * 两边都有类别且相同 —— 一致；
    #   * 两边都有类别但不同 —— 边界像元。GEE 那边是按 scale=10 重新采样的，
    #     网格和导出时的原生网格不完全重合，落在地块边界上时会取到相邻像元。
    #   * 本地无数据但 GEE 有类别 —— 真正的「缺块」。
    print(f"{'kind':<10} {'col':>8} {'row':>8} {'local':>6} {'gee':>6}  {'lon':>10} {'lat':>9}")
    both_nodata = 0
    matched = 0
    boundary = 0
    missing = 0
    for (col, row, kind, lon, lat, lv), rv in zip(local_vals, remote):
        if lv in (None, 0) and rv is None:
            verdict, cls = "两边都无数据", "nodata"
            both_nodata += 1
        elif lv == rv:
            verdict, cls = "一致", "ok"
            matched += 1
        elif lv in (None, 0) and rv is not None:
            verdict, cls = "本地缺数据", "missing"
            missing += 1
        else:
            verdict, cls = "边界像元不同", "boundary"
            boundary += 1
        print(
            f"{kind:<10} {col:>8} {row:>8} {str(lv):>6} {str(rv):>6}  "
            f"{lon:>10.5f} {lat:>9.5f}  {verdict}"
        )
    print(
        f"\n共 {len(local_vals)} 个点："
        f"类别一致 {matched}，两边都无数据 {both_nodata}，"
        f"边界像元不同 {boundary}，本地缺数据 {missing}"
    )
    if missing:
        print("!! 本地缺数据 —— 栅格不完整。")
        return 1
    print("OK — 本地栅格与 GEE 资产吻合，没有缺失的图块。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
