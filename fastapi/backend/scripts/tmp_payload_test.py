# -*- coding: utf-8 -*-
"""定位 GEE 下载请求载荷（10 MB 上限）的构成：
   1) 纯 133 波段特征影像表达式多大；
   2) 不同训练样本量 / maxNodes / 树数量下分类器能否塞进 10 MB 载荷。
只调用 getDownloadId（不下发真实下载），每次实验几秒。临时脚本，用完即删。"""
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
    parse_sample_csv,
    plan_download_grid,
    resolve_feature_bands,
)

future2.init_gee(future2.DEFAULT_PROJECT, None)

df, lon_col, lat_col, _ = parse_sample_csv(BACKEND_DIR / "data" / "样本特征表.csv", "class")
feature_bands, _ = resolve_feature_bands(df.columns)
label_vals = pd.to_numeric(df["class"], errors="coerce")
ok = label_vals.notna() & (label_vals == label_vals.round())
train_df = df[ok].copy()
train_df["class"] = label_vals[ok].astype("int64")
train_df = train_df[~train_df[feature_bands].isna().any(axis=1)].reset_index(drop=True)
classes = sorted(train_df["class"].unique().tolist())
n_total = len(train_df)
print(f"全量训练样本 {n_total}，特征波段 {len(feature_bands)}，类别 {classes}", flush=True)

ref = df.iloc[0]
pad = 0.02
minx, miny = ref[lon_col] - pad, ref[lat_col] - pad
maxx, maxy = ref[lon_col] + pad, ref[lat_col] + pad
bounds = (minx, miny, maxx, maxy)
geom = shapely_box(minx, miny, maxx, maxy)
pixel, nx, ny, tiles = plan_download_grid(bounds, 10.0, geom)
tile = tiles[0]
print(f"测试瓦片 {tile['w']}×{tile['h']}", flush=True)

aoi = ee.Geometry.Rectangle([minx, miny, maxx, maxy])
final_image, _ = future2.buildFeatureImage(2025, aoi, 80)

params = {
    "name": "payload_probe",
    "crs": "EPSG:4326",
    "crsTransform": [pixel, 0, 0, -pixel, tile["x0"], tile["y1"]],
    "dimensions": [tile["w"], tile["h"]],
    "format": "GEO_TIFF",
}


def probe(label, image, dims=None):
    t0 = time.time()
    try:
        image.getDownloadId({**params, "dimensions": dims or params["dimensions"]})
        # getDownloadId 只创建任务不下载
        print(f"  [PASS] {label}（{time.time() - t0:.1f} s）", flush=True)
        return True
    except ee.EEException as exc:
        msg = str(exc)
        print(f"  [FAIL] {label}：{msg[:120]}（{time.time() - t0:.1f} s）", flush=True)
        return False


def make_classifier(n_samples, num_trees=100, max_nodes=None):
    sub = train_df if n_samples >= n_total else train_df.sample(n=n_samples, random_state=42)
    fc = build_training_features(sub, lon_col, lat_col, "class", feature_bands)
    rf = ee.Classifier.smileRandomForest(num_trees, maxNodes=max_nodes)
    rf = rf.setOutputMode("MULTIPROBABILITY")
    clf = rf.train(features=fc, classProperty="class", inputProperties=feature_bands)
    return clf


def classified_image(clf, n_classes):
    arr = final_image.select(feature_bands).classify(clf).select("classification")
    return ee.Image.cat([arr.arrayGet(ee.Image(i)).rename(f"prob_{i}") for i in range(n_classes)]).multiply(100).toByte()


# E1：纯特征影像（无分类器，小窗口避免碰 32 MB 响应上限）
probe("纯 133 波段特征影像（100×100 窗口）", final_image, dims=[100, 100])

# E2-E5：不同训练规模/树参数下的分类器
for n in (1000, 5000, 10000, 15000):
    clf = make_classifier(n)
    probe(f"分类影像（训练 {n} 样本 × 100 树，无 maxNodes）", classified_image(clf, len(classes)))

clf = make_classifier(15000, num_trees=100, max_nodes=500)
probe("分类影像（15000 样本 × 100 树，maxNodes=500）", classified_image(clf, len(classes)))

clf = make_classifier(15000, num_trees=50, max_nodes=300)
probe("分类影像（15000 样本 × 50 树，maxNodes=300）", classified_image(clf, len(classes)))

clf = make_classifier(15000, num_trees=100, max_nodes=100)
probe("分类影像（15000 样本 × 100 树，maxNodes=100）", classified_image(clf, len(classes)))

print("\n载荷实验结束。", flush=True)
