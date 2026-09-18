# -*- coding: utf-8 -*-
"""GEE 全时序特征提取算法验证脚本（小样本约 1-3 分钟，取决于 GEE 服务器速度）。

用法（在 backend 目录下，可先不启动后端）：
    venv/Scripts/python scripts/test_gee_algo.py

前置条件（任选其一）：
  1. 本机已执行 `earthengine authenticate`，且登录账号拥有 GEE 项目权限；
  2. 或把 GEE 服务账号 JSON 放到 backend/data/ 下，并在下方 inputs 传入 gee_credentials。

验证内容：
  1. 直接调用 algorithms/future2.py 的 run()，用 table.csv 前 10 行小样本跑通 GEE 全流程；
  2. 校验结果结构符合平台约定（metrics/tables）；
  3. 校验特征表包含 133 个波段列（波段_月份 命名）与标签列。
"""
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "algorithms"))

import pandas as pd

from future2 import ALGO_META, BAND_COL_PATTERN, run


def check(name: str, ok: bool, extra: str = ""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name} {extra}")
    if not ok:
        sys.exit(1)


def main() -> None:
    workdir = BACKEND_DIR / "outputs" / "gee_algo_test"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    print(f"算法: {ALGO_META['name']}（{ALGO_META['id']}）")
    print(f"参数: {[(p['name'], p['type'], p['required']) for p in ALGO_META['params']]}")

    # 从 data/table.csv 取前 10 行做小样本，避免全量 15000 点验证耗时过长
    src_csv = BACKEND_DIR / "data" / "table.csv"
    if not src_csv.is_file():
        sys.exit("未找到 data/table.csv，请先放入样本点 CSV。")
    small_csv = workdir / "samples_small.csv"
    pd.read_csv(src_csv).head(10).to_csv(small_csv, index=False)

    result = run(
        inputs={
            "sample_csv": small_csv,
            "label_property": "class",
            "year": 2025,
            # "gee_credentials": BACKEND_DIR / "data" / "你的服务账号.json",  # 按需启用
        },
        workdir=workdir,
    )

    # 1. 结果结构符合平台约定
    check("metrics 非空", len(result["metrics"]) >= 5, f"({len(result['metrics'])} 个)")
    check("tables 两个（样本特征表 + 波段有效性统计）", len(result["tables"]) == 2)
    for m in result["metrics"]:
        print(f"   指标: {m['name']} = {m['value']} {m['unit']}")

    # 2. 特征表包含标签列与 133 个波段列（命名格式：波段_月份，如 VH_1、B2_12）
    feature_cols = result["tables"][0]["columns"]
    band_cols = [c for c in feature_cols if c == "elevation" or BAND_COL_PATTERN.match(str(c))]
    check("特征表含 133 波段", len(band_cols) == 133, f"({len(band_cols)} 个)")
    check("标签列在特征表中", "class" in feature_cols)
    check("波段命名为 波段_月份 格式", "VH_1" in feature_cols and "B2_12" in feature_cols)

    # 3. 波段有效性统计表（每波段有效样本数与比例）
    validity_cols = result["tables"][1]["columns"]
    check(
        "波段有效性统计表含 波段/有效样本数/有效比例 三列",
        validity_cols == ["波段", "有效样本数", "有效比例(%)"],
        f"({validity_cols})",
    )

    # 4. 输出全年 NDVI 中值预览 GeoTIFF（前端展示 + 下载）
    check("输出 1 个预览栅格", len(result.get("rasters", [])) == 1)
    if result.get("rasters"):
        tif = Path(result["rasters"][0]["tif"])
        check("预览 tif 文件存在", tif.is_file(), f"({tif.name})")

    print(f"\nmessage: {result['message']}")
    print("全部用例通过。")


if __name__ == "__main__":
    main()
