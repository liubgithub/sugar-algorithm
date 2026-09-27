"""Adapter entry point for the local yield estimation algorithm.

Wraps the legacy script (preserved verbatim in legacy_script.py) and exposes
the unified `run(params, job_dir)` signature expected by the platform.

The legacy computation logic (Ridge + StandardScaler + SimpleImputer, county
history folding, chunked Rasterio reads, GeoTIFF output) is untouched. This
file only:

  - builds an argparse.Namespace from the platform's params dict;
  - calls build_local_model_result + write_window_tif_outputs;
  - redirects outputs into the per-job working directory;
  - returns the unified result structure.
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.algorithms.base import ProgressCallback, make_card, make_result
from app.algorithms.yield_local import legacy_script

# 把 legacy_script 的目标行 DataFrame 序列化成平台可消费的 list[dict]。
# 列与 legacy_script.run_folded_formula_workflow 中 target[...]= ... 一致；
# 字段命名与前端"县级预测结果"表格列直接对应。
_COUNTY_PREDICTION_COLUMNS = [
    "地名",
    "crop_year",
    "observed_month_count",
    "predicted_yield",
    "raw_formula_pred",
    "error",
    "final_actual_yield",
    "final_predicted_yield",
    "final_raw_formula_pred",
    "final_error",
    "current_area",
    "area_for_weight",
    "county_yield_hist_mean",
    "county_yield_hist_last",
    "county_yield_hist_count",
]


def _safe_records(df) -> List[str]:
    """Serialize selected columns of a DataFrame to JSON-safe dicts.

    Mirrors app.storage.job_store._json_safe so NaN/Inf become null, numpy
    scalars become plain Python types — frontend gets a clean list.
    """
    import math

    def _coerce(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, float):
            return v if math.isfinite(v) else None
        if isinstance(v, (str, int, bool)):
            return v
        # pandas may yield numpy scalars / Timestamp; fall back to str cast.
        try:
            import numpy as np
            if isinstance(v, np.floating):
                f = float(v)
                return f if math.isfinite(f) else None
            if isinstance(v, np.integer):
                return int(v)
            if isinstance(v, np.bool_):
                return bool(v)
        except ImportError:
            pass
        return str(v)

    out: List[Dict[str, Any]] = []
    cols = [c for c in _COUNTY_PREDICTION_COLUMNS if c in df.columns]
    for _, row in df[cols].iterrows():
        out.append({c: _coerce(row[c]) for c in cols})
    return out


def _scale_or_none(value: Any, scale: float) -> Optional[float]:
    """Multiply a raw-unit value by the final scale; None when not finite."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v * float(scale) if math.isfinite(v) else None


def _count_valid_pixels(tif_path: Path, nodata: Optional[float]) -> Optional[int]:
    """One-shot read of the produced GeoTIFF to count non-nodata pixels."""
    if not tif_path.is_file():
        return None
    try:
        import rasterio
        import numpy as np
    except ImportError:
        return None
    try:
        with rasterio.open(tif_path) as ds:
            arr = ds.read(1, masked=True)
        if np.ma.is_masked(arr):
            valid = int(np.count_nonzero(~arr.mask))
        else:
            valid = int(arr.size)
        return valid
    except Exception:
        return None


def _build_namespace(params: Dict[str, Any]) -> argparse.Namespace:
    """Translate the platform params dict into the legacy Namespace."""
    target_label = str(params["target_month"]).replace("-", "_")
    scale_label = f"{float(params.get('final_result_scale', legacy_script.FINAL_RESULT_SCALE)):.3f}".replace(".", "")
    train_start = int(params["train_start_year"])
    train_end = int(params["train_end_year"])
    export_prefix = params.get(
        "gee_export_prefix",
        f"GX_countyFolded_pixelYield_train{train_start}_{train_end}_"
        f"target{target_label}_final{scale_label}",
    )

    return argparse.Namespace(
        input_file=params["excel_path"],
        sheet=params.get("sheet_name", legacy_script.SHEET_NAME),
        target_month=params["target_month"],
        train_start_year=train_start,
        train_end_year=train_end,
        allow_target_in_training=bool(params.get("allow_target_in_training", False)),
        preview_rows=int(params.get("preview_rows", 20)),
        gee_sugarcane_asset=params.get(
            "gee_sugarcane_asset", legacy_script.GEE_SUGARCANE_ASSET
        ),
        gee_province_asset=params.get(
            "gee_province_asset", legacy_script.GEE_PROVINCE_ASSET
        ),
        gee_export_folder=params.get(
            "gee_export_folder", legacy_script.GEE_EXPORT_FOLDER
        ),
        gee_export_prefix=export_prefix,
        final_result_scale=float(params.get("final_result_scale", legacy_script.FINAL_RESULT_SCALE)),
        input_raster_dir=params["raster_dir"],
        output_tif=params.get("output_filename", "yield_final.tif"),
        raw_output_tif=params.get("raw_output_filename", "yield_raw.tif"),
        cane_mask_tif=params.get("cane_mask_path"),
        raster_template=params.get("raster_template", legacy_script.RASTER_NAME_TEMPLATE),
        block_size=int(params.get("block_size", 512)),
    )


def run(
    params: Dict[str, Any],
    job_dir: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    """Execute the local yield estimation and write rasters into job_dir."""
    job_dir_path = Path(job_dir)
    job_dir_path.mkdir(parents=True, exist_ok=True)

    # Surface a clear error when required params are missing instead of a
    # cryptic KeyError from the legacy script.
    missing = [k for k in ("excel_path", "raster_dir", "target_month",
                           "train_start_year", "train_end_year")
               if not params.get(k)]
    if missing:
        return make_result(
            status="failed",
            result_type="json",
            message=f"缺少必填参数：{', '.join(missing)}",
        )

    # Validate the on-disk existence of file/dir params so the user sees a
    # readable error instead of a generic exception from the legacy script.
    path_problems: list[str] = []
    for key in ("excel_path", "raster_dir"):
        p = Path(str(params[key]))
        if not p.exists():
            path_problems.append(f"{key} 路径不存在: {p}")
    cane_mask = params.get("cane_mask_path")
    if cane_mask and not Path(str(cane_mask)).exists():
        path_problems.append(f"cane_mask_path 路径不存在: {cane_mask}")
    if path_problems:
        return make_result(
            status="failed",
            result_type="json",
            message="；".join(path_problems),
        )

    ns = _build_namespace(params)

    if progress_callback:
        progress_callback(5, "读取建模表并训练模型")
    model_result = legacy_script.build_local_model_result(ns)
    folded = model_result["folded_full"]

    # 与本地脚本 main() 保持一致地打印模型参数和省级折叠核对报告，
    # 这些输出会被任务日志捕获并在前端"运行日志"面板中展示。
    print("\n" + "=" * 24 + " 模型参数 " + "=" * 24)
    print(f"目标月份: {model_result['target_dt'].strftime('%Y-%m')}")
    print(f"使用月份: Apr 到 {legacy_script.MONTH_LABELS[int(model_result['cutoff_idx'])]}")
    print(f"动态参数数: {len(folded.dynamic_params)}")
    print(f"折叠截距 raw: {folded.folded_intercept:.12f}")
    print(f"最终结果系数: {ns.final_result_scale:.12g}")
    legacy_script.print_province_fold_check(model_result)

    output_tif = job_dir_path / ns.output_tif
    raw_output_tif = job_dir_path / ns.raw_output_tif
    cane_mask_path = Path(ns.cane_mask_tif) if ns.cane_mask_tif else None

    if progress_callback:
        progress_callback(55, "分块生成 GeoTIFF")
    legacy_script.write_window_tif_outputs(
        target_month=ns.target_month,
        input_dir=Path(ns.input_raster_dir),
        template=ns.raster_template,
        folded=folded,
        final_scale=ns.final_result_scale,
        cane_mask_path=cane_mask_path,
        raw_output_tif=raw_output_tif,
        output_tif=output_tif,
        nodata=legacy_script.NODATA_VALUE,
        block_size=ns.block_size,
    )

    files = []
    for label, path in (("raw", raw_output_tif), ("final", output_tif)):
        if path.exists():
            files.append(
                {
                    "name": path.name,
                    "label": label,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                }
            )

    if progress_callback:
        progress_callback(100, "估产完成")

    # 写完 GeoTIFF 后顺手数一下有效像元，写进 metrics，给前端估产指标的"有效像元数"用。
    valid_pixel_count = _count_valid_pixels(output_tif, legacy_script.NODATA_VALUE)

    metrics = {
        "target_month": str(model_result["target_dt"].strftime("%Y-%m")),
        "target_season": int(model_result["target_season"]),
        "cutoff_idx": int(model_result["cutoff_idx"]),
        "result_scale": float(model_result["result_scale"]),
        "ridge_alpha": float(model_result["model"].named_steps["ridgecv"].alpha_),
        "train_n": int(len(model_result["train"])),
        "target_n": int(len(model_result["target"])),
        "feature_count": int(len(model_result["feature_cols"])),
        "dynamic_param_count": int(len(model_result["folded_full"].dynamic_params)),
        "folded_history_count": int(len(model_result["folded_full"].fold_table)),
        "province_county_pred_full": model_result.get("province_county_pred_full"),
        "province_county_pred_full_final": model_result.get("province_county_pred_full_final"),
        "folded_formula_full_final": model_result.get("folded_formula_full_final"),
        # 省级折叠核对的关键数值（对应 print_province_fold_check 的输出）。
        "province_actual_final": _scale_or_none(
            model_result.get("province_actual"), model_result["result_scale"]
        ),
        "province_county_pred_eval_final": model_result.get("province_county_pred_eval_final"),
        "folded_formula_eval_final": model_result.get("folded_formula_eval_final"),
        "folded_intercept_raw": float(folded.folded_intercept),
        "folded_intercept_final": float(folded.folded_intercept) * float(model_result["result_scale"]),
        "train_metrics_final": model_result.get("train_metrics_final"),
        "target_metrics_final": model_result.get("target_metrics_final"),
        # 前端"县级预测结果"表格 + "估产指标 - 有效像元数" 用。
        "county_predictions": _safe_records(model_result["target"]),
        "valid_pixel_count": valid_pixel_count,
    }

    # 统一指标卡：AlgorithmResultPanel 优先按 cards 渲染。
    train_metrics = model_result.get("train_metrics_final") or {}
    cards: List[Dict[str, Any]] = [
        make_card("target_month", "目标月份", metrics["target_month"]),
        make_card("target_season", "目标榨季", metrics["target_season"], "年"),
        make_card("train_n", "训练样本数", metrics["train_n"], "条"),
        make_card("target_n", "县级数量", metrics["target_n"], "个"),
        make_card("feature_count", "动态参数数", metrics["feature_count"], "个"),
        make_card(
            "province_county_pred_full_final",
            "全年平均预测面积",
            metrics["province_county_pred_full_final"],
            "分数",
        ),
        make_card("train_r2", "训练集 R²", train_metrics.get("R2"), ""),
        make_card("train_mae", "训练集 MAE", train_metrics.get("MAE"), "分数"),
        make_card(
            "folded_formula_full_final",
            "折叠公式省级值",
            metrics["folded_formula_full_final"],
            "分数",
        ),
        make_card(
            "province_actual_final",
            "省级实际结果",
            metrics["province_actual_final"],
            "分数",
        ),
        make_card(
            "ridge_alpha", "Ridge α", float(model_result["model"].named_steps["ridgecv"].alpha_), ""
        ),
        make_card("valid_pixel_count", "有效像元数", valid_pixel_count, "个"),
    ]
    # NaN/Inf 自动由 job_store._json_safe 后续消毒，但卡片先保留原始值。
    metrics["cards"] = cards

    return make_result(
        status="completed",
        result_type="raster",
        files=files,
        metrics=metrics,
        message=(
            f"本地持续估产完成：目标月份 {metrics['target_month']}，"
            f"输出 {len(files)} 个 GeoTIFF"
        ),
    )


__all__ = ["run"]