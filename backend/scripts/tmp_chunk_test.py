# -*- coding: utf-8 -*-
"""本地验证（不连 GEE）：新 future2.py 的 CSV 解析与空间分块逻辑。

检查：
  1. CSV 解析（经纬度列识别、无效行剔除、point_id=原始行号）；
  2. 分块覆盖全部样本点、无重复、无遗漏；
  3. 每块点数 ≤ CHUNK_SIZE、跨度 ≤ SPATIAL_CHUNK_MAX_DEG；
  4. BAND_COL_PATTERN 对 VH_1 / B2_12 的匹配。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "algorithms"))

from future2 import (
    BAND_COL_PATTERN, CHUNK_SIZE, SPATIAL_CHUNK_MAX_DEG,
    build_spatial_chunks, parse_sample_csv,
)

csv_path = Path(__file__).resolve().parents[1] / "data" / "table.csv"
df, lon_col, lat_col, dropped = parse_sample_csv(csv_path, "class")
print(f"解析: {len(df)} 行有效, 剔除 {dropped} 行; 列 {lon_col}/{lat_col}")

chunks = build_spatial_chunks(df, lon_col, lat_col)
print(f"分块: {len(chunks)} 块, 块大小 {sorted(len(c) for c in chunks)[:5]}...")

# 覆盖性：所有点恰好出现一次
all_ids = [i for c in chunks for i in c]
assert len(all_ids) == len(set(all_ids)), "point_id 重复！"
assert set(all_ids) == set(df["point_id"]), "分块遗漏样本点！"
print("覆盖性: 通过（无重复、无遗漏）")

# 点数与跨度约束
for i, c in enumerate(chunks):
    sub = df[df["point_id"].isin(c)]
    span_lon = sub[lon_col].max() - sub[lon_col].min()
    span_lat = sub[lat_col].max() - sub[lat_col].min()
    assert len(c) <= CHUNK_SIZE, f"块 {i} 点数 {len(c)} 超限"
    assert span_lon <= SPATIAL_CHUNK_MAX_DEG, f"块 {i} 经度跨度 {span_lon:.3f} 超限"
    assert span_lat <= SPATIAL_CHUNK_MAX_DEG, f"块 {i} 纬度跨度 {span_lat:.3f} 超限"
print(
    f"约束: 通过（每块点数≤{CHUNK_SIZE}、跨度≤{SPATIAL_CHUNK_MAX_DEG}°；"
    f"最大块 {max(len(c) for c in chunks)} 点）"
)

# 波段列名正则
assert BAND_COL_PATTERN.match("VH_1") and BAND_COL_PATTERN.match("B2_12")
assert not BAND_COL_PATTERN.match("B2_0") and not BAND_COL_PATTERN.match("VH_13")
print("BAND_COL_PATTERN: 通过（VH_1、B2_12 匹配，B2_0、VH_13 不匹配）")
print("本地验证全部通过")
