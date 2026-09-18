# -*- coding: utf-8 -*-
"""临时实验：定位内存超限根因——全量 FC 序列化 vs 影像 AOI 大小。

对照：10 点测试通过（FC=10 要素、AOI=10 点范围）；所有失败用例 FC=15000 要素、
影像 AOI=全域。取最密集 0.25° 网格内 250 点（空间最紧凑）做两个对照：
  F1: 小 FC(250 点客户端构造, 无过滤) + 全域 AOI 影像
  F2: 小 FC(250 点) + 局部 AOI 影像
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

# 最密集 0.25° 网格内取 250 点
cell_lon = (df[lon_col] // 0.25).astype(int)
cell_lat = (df[lat_col] // 0.25).astype(int)
cell = cell_lat * 10000 + cell_lon
dense = df[cell == cell.value_counts().idxmax()]
ids = dense["point_id"].iloc[:250].tolist()
df250 = df[df["point_id"].isin(ids)]
sub_lon, sub_lat = df250[lon_col], df250[lat_col]
print(
    f"密集 250 点范围: 经度 {sub_lon.min():.4f}~{sub_lon.max():.4f} "
    f"纬度 {sub_lat.min():.4f}~{sub_lat.max():.4f}", flush=True,
)

fc_full = ee.FeatureCollection(features_from_dataframe(df, lon_col, lat_col))
fc250 = ee.FeatureCollection(features_from_dataframe(df250, lon_col, lat_col))
aoi_full = fc_full.geometry()
aoi250 = fc250.geometry()

print("构建影像（惰性，只是构图）…", flush=True)
img_full = buildFeatureImage(2025, aoi_full, 80.0)
img_local = buildFeatureImage(2025, aoi250, 80.0)


def sample(tag, img, coll):
    try:
        out = img.sampleRegions(
            collection=coll,
            properties=["class", "point_id"],
            scale=10, tileScale=4, geometries=False,
        ).getInfo()
        print(f"{tag}: 成功，{len(out['features'])} 点", flush=True)
    except Exception as exc:
        print(f"{tag}: 失败 -> {str(exc)[:90]}", flush=True)


sample("F1 小FC(250) + 全域AOI影像", img_full, fc250)
sample("F2 小FC(250) + 局部AOI影像", img_local, fc250)
print("实验完成", flush=True)
