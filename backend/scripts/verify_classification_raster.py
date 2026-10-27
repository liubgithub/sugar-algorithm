"""Sanity-check a downloaded classification raster.

Answers the question "is this file actually the full result?" without
decoding 5 G pixels: the downloader writes an overview pyramid, so the
coarsest overview is a ~5 M pixel sample of the whole map.  We also probe a
grid of real windows in the base raster to catch zero-filled holes that an
overview average could hide.

Usage from ``backend/``::

    python scripts/verify_classification_raster.py <tif> [--windows 24]
"""
from __future__ import annotations

import os
import sys

import numpy as np
import rasterio


def main() -> int:
    path = sys.argv[1]
    n_windows = 24
    if "--windows" in sys.argv:
        n_windows = int(sys.argv[sys.argv.index("--windows") + 1])
    # 下载器切图用的边长（gee_raster 的默认 max_tile_px）。逐图块覆盖度
    # 检查要按这个网格来分块，否则分不出「哪一块下载丢了」。
    tile_px = 4096
    if "--tile-px" in sys.argv:
        tile_px = int(sys.argv[sys.argv.index("--tile-px") + 1])

    with rasterio.open(path) as ds:
        print(f"file    : {path}")
        print(f"size    : {os.path.getsize(path) / 1024 / 1024:.1f} MB")
        print(f"grid    : {ds.width} x {ds.height} "
              f"({ds.width * ds.height / 1e9:.2f} G px)")
        print(f"crs     : {ds.crs}")
        print(f"dtype   : {ds.dtypes}  nodata={ds.nodata}  bands={ds.count}")
        print(f"bounds  : {ds.bounds}")
        overviews = ds.overviews(1)
        print(f"overviews: {overviews}")

        # --- class histogram from the coarsest overview -------------------
        if overviews:
            factor = max(overviews)
            h, w = ds.height // factor, ds.width // factor
            arr = ds.read(1, out_shape=(h, w))
            src = f"1/{factor} 概览（{w}x{h}）"
        else:
            # No overview: sample a coarse grid of windows instead.
            step_y = max(1, ds.height // 512)
            step_x = max(1, ds.width // 512)
            arr = ds.read(
                1, out_shape=(ds.height // step_y, ds.width // step_x)
            )
            src = "降采样读"
        values, counts = np.unique(arr, return_counts=True)
        total = counts.sum()
        print(f"\nclass histogram（{src}，{total} 个采样像元）:")
        for value, count in zip(values.tolist(), counts.tolist()):
            pct = 100.0 * count / total
            print(f"  class {value:>4} : {count:>10} ({pct:6.2f}%)")
        nonzero = float((arr != 0).mean()) * 100
        print(f"  非零像元占比: {nonzero:.2f}%")

        # --- per-tile coverage: is any *download tile* missing? -----------
        # 抽查若干个全零窗口说明不了什么 —— 广西分类图的 ROI 是省界多边形，
        # 包围盒里有接近一半是真正的 nodata，整块全零完全正常。真正要抓的
        # 是「某个 4096px 下载图块整块缺失」：它表现为一块规则矩形全零，
        # 而周围图块有数据。
        #
        # 判据用**概览**（1/32，覆盖整幅图，是从基础栅格算出来的）：某个
        # 图块对应的一块概览像元如果全零，而它四周的图块都有数据，那就是
        # 缺块。
        print(f"\n逐图块覆盖度检查（图块 = {tile_px}px）:")
        grid = _tile_coverage(ds, tile_px)
        if grid is None:
            print("  (跳过：没有概览，无法廉价地扫全图)")
        else:
            rows, cols, cov = grid
            empty = [
                (r, c) for r in range(rows) for c in range(cols) if cov[r][c] == 0.0
            ]
            print(f"  图块总数: {rows * cols}，全零图块: {len(empty)}")
            for r, c in empty[:20]:
                neigh = [
                    cov[rr][cc]
                    for rr, cc in (
                        (r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)
                    )
                    if 0 <= rr < rows and 0 <= cc < cols
                ]
                tag = "  <-- 周围有数据，疑似缺块!" if any(v > 0 for v in neigh) else "  (边缘，四邻皆空，正常)"
                print(f"    行{r} 列{c}{tag}")
            if any(
                any(
                    cov[rr][cc] > 0
                    for rr, cc in (
                        (r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)
                    )
                    if 0 <= rr < rows and 0 <= cc < cols
                )
                for r, c in empty
            ):
                print("  !! 有图块疑似缺失")
            else:
                print("  OK 没有发现缺失的图块")

        # --- probe real windows across the base raster --------------------
        print(f"\n在原始栅格上抽查 {n_windows} 个窗口:")
        step_y = max(1, ds.height // n_windows)
        step_x = max(1, ds.width // n_windows)
        empty = 0
        for row in range(n_windows):
            for col in range(n_windows):
                y = row * step_y
                x = col * step_x
                h = min(256, ds.height - y)
                w = min(256, ds.width - x)
                if h <= 0 or w <= 0:
                    continue
                window = ds.read(1, window=((y, y + h), (x, x + w)))
                if not window.any():
                    empty += 1
        checked = n_windows * n_windows
        print(f"  全零窗口: {empty}/{checked}"
              "（ROI 是省界多边形时这些是正常的 nodata，不是缺数据）")
    return 0


def _tile_coverage(ds, tile_px: int):
    """Non-zero fraction of every ``tile_px`` block, read from the coarsest
    overview.  Returns ``(rows, cols, coverage)`` or ``None`` if there is no
    overview to read cheaply."""
    overviews = ds.overviews(1)
    if not overviews:
        return None
    factor = max(overviews)
    h, w = ds.height // factor, ds.width // factor
    arr = ds.read(1, out_shape=(h, w), masked=False)
    block = max(1, tile_px // factor)
    rows = (h + block - 1) // block
    cols = (w + block - 1) // block
    coverage = [[0.0] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            tile = arr[r * block : (r + 1) * block, c * block : (c + 1) * block]
            if tile.size:
                coverage[r][c] = float((tile != 0).mean())
    return rows, cols, coverage


if __name__ == "__main__":
    raise SystemExit(main())
