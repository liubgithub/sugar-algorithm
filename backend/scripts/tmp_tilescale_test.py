# -*- coding: utf-8 -*-
"""临时实验：验证「空间分块 + 更小 tileScale」能否解决 User memory limit exceeded。

用 future.py 现有模块（结构同 future2），对第 1 块（2000 点，跨 3.9°×0.47°）
分别以 tileScale=2 / 4 提取，看哪个配置能通过 GEE 8GB 内存限制。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "algorithms"))

import ee
from future2 import (
    buildFeatureImage, build_spatial_chunks, features_from_dataframe,
    init_gee, parse_sample_csv,
)

init_gee("test1-506208", None)
df, lon_col, lat_col, _ = parse_sample_csv(Path("data/table.csv"), "class")
features = features_from_dataframe(df, lon_col, lat_col)
fc = ee.FeatureCollection(features)
aoi = fc.geometry()

print("构建 133 波段特征影像…")
final_image = buildFeatureImage(2025, aoi, 80.0)

chunks = build_spatial_chunks(df, lon_col, lat_col)
c = chunks[0]
print(f"开始提取第 1 块（{len(c)} 点）")

for tag, ts in [("A: tileScale=2", 2), ("B: tileScale=4", 4)]:
    try:
        sampled = final_image.sampleRegions(
            collection=fc.filter(ee.Filter.inList("point_id", ee.List(c))),
            properties=["class", "point_id"],
            scale=10,
            tileScale=ts,
            geometries=False,
        )
        info = sampled.getInfo()
        print(f"{tag}: 成功，返回 {len(info.get('features', []))} 点")
    except Exception as exc:
        print(f"{tag}: 失败 -> {str(exc)[:100]}")
