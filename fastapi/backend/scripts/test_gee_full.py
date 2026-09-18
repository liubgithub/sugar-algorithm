# -*- coding: utf-8 -*-
"""GEE 全时序特征提取算法——全量 15000 点实测（约 10-30 分钟，取决于 GEE 速度）。

用法（在 backend 目录下）：
    python scripts/test_gee_full.py

与 test_gee_algo.py（10 点小样本）的区别：直接用 backend/data/table.csv
全量 15000 点走完整平台流程，验证「空间分块 + 块级 AOI 构图 + 8 线程并行」
架构在生产规模下能否在 GEE 交互式内存限制（8GB）内完成。
"""
import shutil
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "algorithms"))

import pandas as pd

from future2 import ALGO_META, BAND_COL_PATTERN, run


def main() -> None:
    workdir = BACKEND_DIR / "outputs" / "gee_algo_full_test"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    src_csv = BACKEND_DIR / "data" / "table.csv"
    if not src_csv.is_file():
        sys.exit("未找到 data/table.csv。")
    n_src = len(pd.read_csv(src_csv))
    print(f"算法: {ALGO_META['id']}；样本源表 {n_src} 行", flush=True)

    t0 = time.time()
    result = run(
        inputs={
            "sample_csv": str(src_csv),
            "label_property": "class",
            "year": 2025,
            "cloud_pct": 80,
            "sample_scale": 10,
        },
        workdir=workdir,
    )
    elapsed = (time.time() - t0) / 60

    print(f"\n总耗时: {elapsed:.1f} 分钟")
    print(f"message: {result['message']}")
    for m in result["metrics"]:
        print(f"  指标: {m['name']} = {m['value']} {m['unit']}")

    # 平台契约校验
    feature_cols = result["tables"][0]["columns"]
    band_cols = [c for c in feature_cols if c == "elevation" or BAND_COL_PATTERN.match(str(c))]
    assert len(band_cols) == 133, f"波段列数 {len(band_cols)} != 133"
    assert "VH_1" in feature_cols and "B2_12" in feature_cols
    assert "class" in feature_cols
    assert len(result["tables"]) == 2
    assert len(result["rasters"]) == 1

    # 结果行数与样本数一致
    out_csv = Path(result["tables"][0]["file"])
    n_out = len(pd.read_csv(out_csv))
    assert n_out == n_src, f"结果表 {n_out} 行 != 样本 {n_src} 行"
    print(f"结果表: {n_out} 行 × {len(feature_cols)} 列，行数与样本一致")

    print("\n全量 15000 点实测通过。")


if __name__ == "__main__":
    main()
