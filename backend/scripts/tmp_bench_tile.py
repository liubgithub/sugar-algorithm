# -*- coding: utf-8 -*-
"""实测 GEE 单瓦片（2200×2200，133 波段 ×100 棵树随机森林）下载耗时，
用于估算省尺度研究区的总运行时长。临时脚本，用完即删。"""
import shutil
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "algorithms"))

import ee
import pandas as pd
from shapely.geometry import box as shapely_box

import future2
from areaclassification import (
    build_probability_image,
    build_training_features,
    download_tile,
    parse_sample_csv,
    plan_download_grid,
    resolve_feature_bands,
)

workdir = BACKEND_DIR / "outputs" / "tmp_bench"
if workdir.exists():
    shutil.rmtree(workdir)
workdir.mkdir(parents=True)

future2.init_gee(future2.DEFAULT_PROJECT, None)

# 与 run() 相同的训练流程：全量样本 CSV（15000+ 样本）
df, lon_col, lat_col, _ = parse_sample_csv(BACKEND_DIR / "data" / "样本特征表.csv", "class")
feature_bands, _ = resolve_feature_bands(df.columns)
label_vals = pd.to_numeric(df["class"], errors="coerce")
ok = label_vals.notna() & (label_vals == label_vals.round())
train_df = df[ok].copy()
train_df["class"] = label_vals[ok].astype("int64")
train_df = train_df[~train_df[feature_bands].isna().any(axis=1)].reset_index(drop=True)
classes = sorted(train_df["class"].unique().tolist())
print(f"训练样本 {len(train_df)}，特征波段 {len(feature_bands)}，类别 {classes}", flush=True)

# 0.4°×0.2° 窗口 → 3×2 瓦片，其中 4 个满尺寸 2200×2200
ref = df.iloc[0]
pad_x, pad_y = 0.2, 0.1
minx, miny = ref[lon_col] - pad_x, ref[lat_col] - pad_y
maxx, maxy = ref[lon_col] + pad_x, ref[lat_col] + pad_y
bounds = (minx, miny, maxx, maxy)
geom = shapely_box(minx, miny, maxx, maxy)
pixel, nx, ny, tiles = plan_download_grid(bounds, 10.0, geom)
full = [t for t in tiles if t["w"] == 2200 and t["h"] == 2200]
print(f"网格 {nx}×{ny}，瓦片 {len(tiles)} 个，满尺寸瓦片 {len(full)} 个", flush=True)

t0 = time.time()
aoi = ee.Geometry.Rectangle([minx, miny, maxx, maxy])
final_image, _ = future2.buildFeatureImage(2025, aoi, 80)
train_fc = build_training_features(train_df, lon_col, lat_col, "class", feature_bands)
prob_image = build_probability_image(final_image, feature_bands, train_fc, "class", 100, len(classes))
print(f"训练 FeatureCollection 构建完成（{time.time() - t0:.0f} s），开始逐瓦片测速…", flush=True)

tile_dir = workdir / "tiles"
tile_dir.mkdir(exist_ok=True)
times = []
for i, t in enumerate(full[:2]):
    t0 = time.time()
    download_tile(prob_image, t, pixel, len(classes), tile_dir)
    dt = time.time() - t0
    times.append(dt)
    print(f"  瓦片 r{t['row']}c{t['col']}：{dt:.0f} s（含 GEE 分类计算 + 下载）", flush=True)

avg = sum(times) / len(times)
print(f"\n单瓦片平均耗时 ≈ {avg:.0f} s", flush=True)
print(f"按 {avg / 60:.1f} min/瓦片 × 约 550 块需要下载的瓦片 ÷ {8} 线程并行"
      f" ≈ {avg * 550 / 8 / 3600:.1f} 小时", flush=True)
