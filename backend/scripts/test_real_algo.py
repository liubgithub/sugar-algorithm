# -*- coding: utf-8 -*-
"""真实甘蔗估产算法验证脚本（耗时较长：分块计算约 2240 个窗口，约 1-5 分钟）。

用法（在 backend 目录下，可先不启动后端）：
    venv/Scripts/python scripts/test_real_algo.py

验证内容：
  1. 直接调用 algorithms/predicted26.py 的 run()，跑通 建模→分块栅格计算→报告 全流程；
  2. 校验输出 tif 可被 rasterio 打开、网格与参考影像一致、数值范围合理；
  3. 校验结果结构与平台约定一致（metrics/tables/rasters）。
"""
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "algorithms"))

import numpy as np
import rasterio
from predicted26 import ALGO_META, run


def check(name: str, ok: bool, extra: str = ""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name} {extra}")
    if not ok:
        sys.exit(1)


def main() -> None:
    workdir = BACKEND_DIR / "outputs" / "real_algo_test"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    print(f"算法: {ALGO_META['name']}（{ALGO_META['id']}）")
    print(f"参数: {[(p['name'], p['type'], p['required']) for p in ALGO_META['params']]}")

    result = run(
        inputs={
            "excel": BACKEND_DIR / "data" / "GX_County_Sugarcane_Monthly_2020_2025_with_Yield_Area.xlsx",
            "raster_sample": BACKEND_DIR / "data" / "月度影像_2026" / "2026_04_NDVI.tif",
            "cane_mask": BACKEND_DIR / "data" / "classification_April_1.tif",
            "target_month": "2026-06",
        },
        workdir=workdir,
    )

    # 1. 结果结构符合平台约定
    check("metrics 非空", len(result["metrics"]) >= 5, f"({len(result['metrics'])} 个)")
    check("tables 三个", len(result["tables"]) == 3)
    check("rasters 两个", len(result["rasters"]) == 2)
    county_rows = result["tables"][0]["rows"]
    check("县级表有 111 行", len(county_rows) == 111, f"({len(county_rows)} 行)")

    # 2. 输出 tif 可打开、网格与参考影像一致
    final_path = Path(result["rasters"][0]["tif"])
    check("final tif 存在", final_path.is_file(), str(final_path))
    with rasterio.open(final_path) as src:
        check("final tif 网格一致", (src.width, src.height) == (28240, 20358),
              f"({src.width}x{src.height})")
        nodata = src.nodata
        # 甘蔗掩膜只保留约 1.66% 的像元，先降采样定位有效像元再抽查数值范围
        scale = 28
        small = src.read(1, out_shape=(src.height // scale, src.width // scale))
        ys, xs = np.where(small != nodata)
        check("final 存在有效像元", len(ys) > 0, f"({len(ys)} 个降采样有效像元)")
        if len(ys):
            r0 = max(0, ys[len(ys) // 2] * scale - 64)
            r1 = min(src.height, r0 + 128)
            c0 = max(0, xs[len(xs) // 2] * scale - 64)
            c1 = min(src.width, c0 + 128)
            arr = src.read(1, window=((r0, r1), (c0, c1)))
            valid = arr[arr != nodata]
            check("final 数值范围合理(0-10)", bool(len(valid)) and valid.min() >= 0 and valid.max() <= 10,
                  f"(有效 {len(valid)} 个, {valid.min():.3f} ~ {valid.max():.3f})")

    # 3. 指标合理性（最终单位为 t/亩）
    prov = next(m["value"] for m in result["metrics"] if m["name"] == "全省平均预测单产")
    check("省级预测单产合理(3-8)", 3 <= prov <= 8, f"({prov:.4f} t/亩)")

    print(f"\nmessage: {result['message']}")
    print("全部用例通过。")


if __name__ == "__main__":
    main()
