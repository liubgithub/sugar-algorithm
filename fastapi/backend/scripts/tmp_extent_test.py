# -*- coding: utf-8 -*-
"""临时实验：验证局部 AOI 构图下，块面积上限能到多大。

F2 已验证 0.25°×0.25°（约 7M 像素）通过。本实验测 0.5°×0.5°（2×2 单元，
约 30M 像素）窗口 + 窗口内最多 2000 点是否通过；再测 0.75°×0.75°（3×3 单元，
约 66M 像素）作上限参考。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "algorithms"))

import ee
from future2 import (
    buildFeatureImage, features_from_dataframe, init_gee, parse_sample_csv,
)

init_gee("test1-506208", None)
df, lon_col, lat_col, _ = parse_sample_csv(Path("data/table.csv"), "class")


def window_test(tag, n_cells):
    """取最密集单元为中心，扩展 n_cells×n_cells 个 0.25° 单元的窗口，取窗口内最多 2000 点。"""
    cell_lon = (df[lon_col] // 0.25).astype(int)
    cell_lat = (df[lat_col] // 0.25).astype(int)
    cell = cell_lat * 10000 + cell_lon
    center = cell.value_counts().idxmax()
    c_lat, c_lon = divmod(center, 10000)
    half = n_cells // 2
    in_window = (
        (cell_lon - c_lon).abs().le(half) & (cell_lat - c_lat).abs().le(half)
    )
    sub = df[in_window].head(2000)
    fc = ee.FeatureCollection(features_from_dataframe(sub, lon_col, lat_col))
    aoi = fc.geometry()
    img = buildFeatureImage(2025, aoi, 80.0)
    print(
        f"{tag}: {len(sub)} 点, 经度 {sub[lon_col].min():.3f}~{sub[lon_col].max():.3f} "
        f"纬度 {sub[lat_col].min():.3f}~{sub[lat_col].max():.3f}，开始提取…", flush=True,
    )
    try:
        out = img.sampleRegions(
            collection=fc, properties=["class", "point_id"],
            scale=10, tileScale=4, geometries=False,
        ).getInfo()
        print(f"{tag}: 成功，{len(out['features'])} 点", flush=True)
    except Exception as exc:
        print(f"{tag}: 失败 -> {str(exc)[:90]}", flush=True)


window_test("W2 0.5°×0.5° 窗口", 2)
window_test("W3 0.75°×0.75° 窗口", 3)
print("实验完成", flush=True)
