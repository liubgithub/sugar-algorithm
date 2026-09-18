# -*- coding: utf-8 -*-
"""GEE 随机森林区域分类算法（areaclassification）验证脚本（小范围，约 3-8 分钟）。

用法（在 backend 目录下，可先不启动后端）：
    venv/Scripts/python scripts/test_area_algo.py

前置条件（任选其一）：
  1. 本机已执行 `earthengine authenticate`，且登录账号拥有 GEE 项目权限；
  2. 或把 GEE 服务账号 JSON 放到 backend/data/ 下，并在下方 inputs 传入 gee_credentials。

验证内容：
  1. 用 data/样本特征表.csv 在首点附近取一个小研究区（≥100 个样本、≥2 个类别），
     把 data/shp.zip 裁剪成对应的小边界，直接调用 algorithms/areaclassification.py 的
     run() 跑通「训练 → 分类 → 瓦片下载 → 合并」全流程；
  2. 校验结果结构符合平台约定（metrics/tables/rasters）；
  3. 校验概率图 5 波段 Byte、分类图 nodata、类别统计表与编码 10/20/30/40/50；
  4. 压力测试：同样的小研究区 + 全量 15000 样本训练，验证 GEE 请求载荷上限下
     可行性（只下载小块区域，耗时不随训练样本量显著增加）。
"""
import shutil
import sys
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "algorithms"))

import numpy as np
import pandas as pd
import rasterio

from areaclassification import ALGO_META, CLASS_NODATA, extract_zip_safe, run


def check(name: str, ok: bool, extra: str = ""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name} {extra}")
    if not ok:
        sys.exit(1)


def make_small_inputs(workdir: Path):
    """从全量数据裁剪一个小研究区：首点附近自适应扩窗，直到 ≥100 个样本且 ≥2 个类别。"""
    src_csv = BACKEND_DIR / "data" / "样本特征表.csv"
    src_zip = BACKEND_DIR / "data" / "shp.zip"
    if not src_csv.is_file() or not src_zip.is_file():
        sys.exit("未找到 data/样本特征表.csv 或 data/shp.zip。")

    df = pd.read_csv(src_csv)
    ref = df.iloc[0]
    for pad in (0.02, 0.05, 0.1, 0.2, 0.5, 1.0):
        in_box = (
            df["Longitude"].between(ref["Longitude"] - pad, ref["Longitude"] + pad)
            & df["Latitude"].between(ref["Latitude"] - pad, ref["Latitude"] + pad)
        )
        sub = df[in_box]
        if len(sub) >= 100 and sub["class"].nunique() >= 2:
            break
    print(f"小研究区窗口 ±{pad}°，含 {len(sub)} 个样本、{sub['class'].nunique()} 个类别。")

    sub_csv = workdir / "features_small.csv"
    sub.to_csv(sub_csv, index=False)

    import geopandas as gpd
    from shapely.geometry import box as shapely_box

    unzip_dir = workdir / "_全量边界"
    extract_zip_safe(src_zip, unzip_dir)
    gdf = gpd.read_file(sorted(unzip_dir.rglob("*.shp"))[0])
    box = shapely_box(
        ref["Longitude"] - pad, ref["Latitude"] - pad,
        ref["Longitude"] + pad, ref["Latitude"] + pad,
    )
    small_gdf = gdf.clip(box)
    small_gdf = small_gdf[small_gdf.geometry.notna() & ~small_gdf.geometry.is_empty]
    if small_gdf.empty:
        sys.exit("裁剪后的边界为空，请检查 shp 与样本点位置是否一致。")

    shp_dir = workdir / "_小边界"
    shp_dir.mkdir(exist_ok=True)
    small_gdf.to_file(shp_dir / "roi_small.shp", encoding="utf-8")
    zip_path = workdir / "boundary_small.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        for f in shp_dir.iterdir():
            z.write(f, f.name)
    return sub_csv, zip_path


def verify_result(result: dict, expected_classes, tag: str) -> None:
    """校验 run() 返回结构符合平台约定，且两个 tif 内容正确。"""
    check(f"[{tag}] metrics ≥ 6 个", len(result["metrics"]) >= 6, f"({len(result['metrics'])} 个)")
    for m in result["metrics"]:
        print(f"   指标: {m['name']} = {m['value']} {m['unit']}")
    check(f"[{tag}] tables 两个（训练集类别分布 + 类别统计）", len(result["tables"]) == 2)
    check(f"[{tag}] rasters 两个（概率图 + 分类图）", len(result["rasters"]) == 2)

    prob = Path(result["rasters"][0]["tif"])
    cls = Path(result["rasters"][1]["tif"])
    check(f"[{tag}] 概率图 tif 存在", prob.is_file(), f"({prob.name})")
    check(f"[{tag}] 分类图 tif 存在", cls.is_file(), f"({cls.name})")
    check(f"[{tag}] 分类图 png 预览存在", Path(result["rasters"][1]["png"]).is_file())

    with rasterio.open(prob) as src:
        check(f"[{tag}] 概率图波段数 = 类别数", src.count == len(expected_classes), f"({src.count})")
        check(f"[{tag}] 概率图 dtype 为 Byte", src.dtypes[0] == "uint8")
        check(f"[{tag}] 概率图 CRS 为 WGS84", str(src.crs) == "EPSG:4326")
        arr = src.read()
        check(f"[{tag}] 概率值在 0-100 之间", bool(0 <= arr.min() and arr.max() <= 100),
              f"(min={arr.min()}, max={arr.max()})")

    with rasterio.open(cls) as src:
        check(f"[{tag}] 分类图 nodata = {CLASS_NODATA}", src.nodata == CLASS_NODATA)
        arr = src.read(1)
        vals = set(np.unique(arr).tolist())
        check(f"[{tag}] 分类图像元值 ⊆ 类别编码 ∪ nodata", vals <= set(expected_classes) | {CLASS_NODATA},
              f"({vals})")

    stats_rows = result["tables"][1]["rows"]
    codes_in_stats = [row[1] for row in stats_rows]
    check(f"[{tag}] 类别统计表编码 = 升序类别编码", codes_in_stats == sorted(expected_classes),
          f"({codes_in_stats})")
    print("   类别统计:", stats_rows)


def main() -> None:
    print(f"算法: {ALGO_META['name']}（{ALGO_META['id']}）")
    print(f"参数: {[(p['name'], p['type'], p['required']) for p in ALGO_META['params']]}")

    # ---------- 测试 1：小研究区全流程 ----------
    workdir = BACKEND_DIR / "outputs" / "area_algo_test"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    sub_csv, small_zip = make_small_inputs(workdir)

    expected_classes = sorted(pd.read_csv(sub_csv)["class"].unique().tolist())
    print(f"\n===== 测试 1：小研究区全流程（{len(pd.read_csv(sub_csv))} 个样本） =====")
    result = run(
        inputs={
            "feature_csv": sub_csv,
            "boundary": small_zip,
            "label_property": "class",
            "year": 2025,
            "scale": 10,
            # "gee_credentials": BACKEND_DIR / "data" / "你的服务账号.json",  # 按需启用
        },
        workdir=workdir,
    )
    verify_result(result, expected_classes, "小研究区")
    print(f"message: {result['message']}")

    # ---------- 测试 2：全量样本训练 + 小研究区下载（请求载荷压力） ----------
    workdir2 = BACKEND_DIR / "outputs" / "area_algo_full_test"
    if workdir2.exists():
        shutil.rmtree(workdir2)
    workdir2.mkdir(parents=True, exist_ok=True)
    full_csv = BACKEND_DIR / "data" / "样本特征表.csv"
    print(f"\n===== 测试 2：全量 {len(pd.read_csv(full_csv))} 样本训练 + 小研究区下载 =====")
    result2 = run(
        inputs={
            "feature_csv": full_csv,
            "boundary": small_zip,
            "label_property": "class",
            "year": 2025,
            "scale": 10,
        },
        workdir=workdir2,
    )
    train_metric = next(m for m in result2["metrics"] if m["name"] == "训练样本数")
    print(f"   训练样本数指标: {train_metric['value']}")
    verify_result(result2, expected_classes, "全量样本")
    print(f"message: {result2['message']}")
    print("\n全部用例通过。")


if __name__ == "__main__":
    main()
