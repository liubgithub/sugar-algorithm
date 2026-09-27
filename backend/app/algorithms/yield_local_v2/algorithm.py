"""Adapter entry point for the v2 local yield estimation algorithm.

Math is preserved exactly in app.algorithms.yield_local_v2.legacy_script (the
user-supplied optimized version). This file only:

  - Translates the platform's params dict into an argparse.Namespace;
  - Calls legacy_script.run_model + legacy_script.predict_rasters_blockwise +
    legacy_script.save_model_tables;
  - Redirects outputs into the per-job working directory (job_dir);
  - Builds the unified result payload with cards + county_predictions +
    valid_pixel_count, mirroring v1's result structure so the same Vue
    ResultRenderer renders v1 and v2 identically.

Differences vs v1 (yield_local):
  - Fixed Ridge alpha (no RidgeCV).
  - 16 dynamic features (mean/std/min/max/trend + sum for precipitation),
    plus 4 history features folded into the provincial intercept.
  - Recent-regime flag + regime_x__ interactions for all 20 base features,
    yielding a 36-dim model feature vector.
  - Blockwise WarpedVRT reprojection for grid alignment (vs v1's windowed
    raw reads).
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.algorithms.base import ProgressCallback, make_card, make_result
from app.algorithms.yield_local_v2 import legacy_script

# Columns surfaced to the frontend "县级预测结果" table. Keep a stable key set
# so the table renders even if some optional columns are absent in a future
# legacy-script revision.
_COUNTY_PREDICTION_COLUMNS = [
    "地名",
    "crop_year",
    "actual_yield",
    "predicted_yield_raw",
    "predicted_yield_final",
    "filled_month_count",
    "area_for_weight",
    "county_yield_hist_mean",
    "county_yield_hist_last",
    "county_yield_hist_count",
    "county_area_hist_last",
]


def _safe_records(df) -> List[Dict[str, Any]]:
    """Serialize selected DataFrame columns to JSON-safe dicts.

    Mirrors v1 (yield_local.algorithm) so both algorithms produce the same
    shape for the frontend table renderer.
    """
    def _coerce(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, float):
            return v if math.isfinite(v) else None
        if isinstance(v, (str, int, bool)):
            return v
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


def _parse_cane_mask_values(raw: Any) -> List[float]:
    """Parse the comma-separated text field into a list of floats.

    Empty / missing / unparseable → default [1.0] (matches
    legacy_script.CANE_MASK_VALUES). Numeric parse errors on individual tokens
    are silently dropped so a stray character doesn't fail the whole job.
    """
    if raw is None:
        return list(legacy_script.CANE_MASK_VALUES)
    text = str(raw).strip()
    if not text:
        return list(legacy_script.CANE_MASK_VALUES)
    parts: List[float] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            parts.append(float(token))
        except ValueError:
            continue
    return parts or list(legacy_script.CANE_MASK_VALUES)


def _build_namespace(params: Dict[str, Any], job_dir: str) -> argparse.Namespace:
    """Translate the platform params dict into the v2 script's Namespace.

    Important: ALL workspace-anchored defaults in legacy_script (INPUT_FILE /
    INPUT_RASTER_DIR / CANE_MASK_TIF / OUTPUT_TIF / RAW_OUTPUT_TIF /
    OUTPUT_DIR) are redirected here so the algorithm reads/writes from the
    per-job working directory instead of the package's own WORKSPACE.
    """
    job_dir_path = Path(job_dir).resolve()
    no_cane_mask = bool(params.get("no_cane_mask", False))

    cane_mask_tif: Optional[str]
    if no_cane_mask:
        cane_mask_tif = None
    elif params.get("cane_mask_path"):
        cane_mask_tif = str(Path(str(params["cane_mask_path"])).resolve())
    else:
        cane_mask_tif = None

    return argparse.Namespace(
        input_file=str(Path(str(params["excel_path"])).resolve()),
        sheet=params.get("sheet_name", legacy_script.SHEET_NAME),
        input_raster_dir=str(Path(str(params["raster_dir"])).resolve()),
        target_month=params["target_month"],
        train_start_year=int(params["train_start_year"]),
        train_end_year=int(params["train_end_year"]),
        allow_target_in_training=bool(
            params.get("allow_target_in_training", False)
        ),
        regime_start_year=int(
            params.get("regime_start_year", legacy_script.REGIME_START_YEAR)
        ),
        ridge_alpha=float(
            params.get("ridge_alpha", legacy_script.RIDGE_ALPHA)
        ),
        final_result_scale=float(
            params.get("final_result_scale", legacy_script.FINAL_RESULT_SCALE)
        ),
        raster_template=params.get(
            "raster_template", legacy_script.RASTER_NAME_TEMPLATE
        ),
        cane_mask_tif=cane_mask_tif,
        cane_mask_values=_parse_cane_mask_values(params.get("cane_mask_values")),
        no_cane_mask=no_cane_mask,
        # Output redirection: all four paths point at the per-job directory.
        output_dir=str(job_dir_path),
        output_tif=str(
            job_dir_path
            / params.get("output_filename", "yield_v2_final.tif")
        ),
        raw_output_tif=str(
            job_dir_path
            / params.get("raw_output_filename", "yield_v2_raw.tif")
        ),
        block_size=int(params.get("block_size", 512)),
        # CLI-only flags the wrapper never sets True on the platform path.
        list_required=False,
        model_only=False,
    )


def _safe_metric(value: Any) -> Optional[float]:
    """Convert a possibly-NaN / non-finite scalar to a JSON-safe Python float."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _sanitize_metric_dict(d: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Drop / sanitize non-finite numeric values from a metric dict."""
    if not d:
        return {}
    out: Dict[str, Any] = {}
    for key, value in d.items():
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int,)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = value
        else:
            out[key] = _safe_metric(value)
    return out


def run(
    params: Dict[str, Any],
    job_dir: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    """Execute the v2 local yield estimation and write outputs into job_dir."""
    job_dir_path = Path(job_dir)
    job_dir_path.mkdir(parents=True, exist_ok=True)

    # ----- 1. Required-param check (readable error instead of KeyError) -----
    missing = [
        k for k in (
            "excel_path", "raster_dir", "target_month",
            "train_start_year", "train_end_year",
        )
        if not params.get(k)
    ]
    if missing:
        return make_result(
            status="failed",
            result_type="json",
            message=f"缺少必填参数：{', '.join(missing)}",
        )

    # ----- 2. On-disk existence check -----
    path_problems: List[str] = []
    for key in ("excel_path", "raster_dir"):
        p = Path(str(params[key]))
        if not p.exists():
            path_problems.append(f"{key} 路径不存在: {p}")
    no_cane_mask = bool(params.get("no_cane_mask", False))
    if not no_cane_mask and params.get("cane_mask_path"):
        mp = Path(str(params["cane_mask_path"]))
        if not mp.is_file():
            path_problems.append(f"cane_mask_path 路径不存在: {mp}")
    if path_problems:
        return make_result(
            status="failed",
            result_type="json",
            message="；".join(path_problems),
        )

    ns = _build_namespace(params, str(job_dir_path))

    # ----- 3. Train + sanity check -----
    if progress_callback:
        progress_callback(5, "读取建模表并训练模型")

    try:
        result = legacy_script.run_model(ns)
    except ValueError as exc:
        return make_result(
            status="failed",
            result_type="json",
            message=f"建模失败：{exc}",
        )
    except FileNotFoundError as exc:
        return make_result(
            status="failed",
            result_type="json",
            message=f"建模文件不存在：{exc}",
        )

    spec = result["spec"]
    folded = result["folded"]

    # Print the model / fold check report to the worker log.
    legacy_script.print_model_report(result, ns)

    # ----- 4. Write GeoTIFFs (blockwise) -----
    if progress_callback:
        progress_callback(55, "分块生成 GeoTIFF")

    raw_path = Path(ns.raw_output_tif)
    final_path = Path(ns.output_tif)
    try:
        legacy_script.predict_rasters_blockwise(
            result, ns, raw_path, final_path
        )
    except FileNotFoundError as exc:
        return make_result(
            status="failed",
            result_type="json",
            message=f"本地月度影像缺失：{exc}",
        )
    except ValueError as exc:
        return make_result(
            status="failed",
            result_type="json",
            message=f"本地 GeoTIFF 计算失败：{exc}",
        )

    # ----- 5. Save model tables (CSVs + month-fill JSON) -----
    if progress_callback:
        progress_callback(90, "保存参数表")
    legacy_script.save_model_tables(result, ns)

    # ----- 6. Build files list -----
    # Only the two GeoTIFFs are surfaced as user-facing files; the
    # 县级预测 / 本地动态参数 / 折叠历史项 / 月份填充值 artefacts are
    # still written to job_dir by legacy_script.save_model_tables for
    # offline analysis / debugging, but they are intentionally excluded
    # from the result panel because:
    #   * county_predictions is already rendered as the in-page
    #     "县级预测结果" table via metrics.county_predictions;
    #   * the others are pure model-internals (regression coefficients
    #     / fold contributions / NaN-fill medians) and add no value to
    #     the result display, but their Chinese filenames trigger an
    #     HTTP 400 in the CSV preview endpoint (which only accepts .tif).
    files: List[Dict[str, Any]] = []
    for label, path in (("raw", raw_path), ("final", final_path)):
        if path.exists():
            files.append(
                {
                    "name": path.name,
                    "label": label,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                }
            )

    # ----- 7. Count valid pixels from the final GeoTIFF -----
    valid_pixel_count = _count_valid_pixels(final_path, legacy_script.NODATA_VALUE)

    # ----- 8. Build metrics + cards -----
    final_scale = float(ns.final_result_scale)

    metrics: Dict[str, Any] = {
        "run_mode": spec.run_mode,
        "run_tag": spec.run_tag,
        "target_month": spec.target_dt.strftime("%Y-%m"),
        "target_season": int(spec.target_season),
        "cutoff_idx": int(spec.cutoff_idx),
        "cutoff_label": spec.cutoff_label,
        "ridge_alpha": float(ns.ridge_alpha),
        "regime_start_year": int(ns.regime_start_year),
        "final_result_scale": final_scale,
        "train_n": int(len(result["train"])),
        "target_n": int(len(result["target"])),
        "feature_count": int(len(spec.dynamic_cols)),
        "model_feature_count": int(len(spec.model_feature_cols)),
        "interaction_feature_count": int(
            sum(
                1
                for col in spec.model_feature_cols
                if col.startswith(legacy_script.INTERACTION_PREFIX)
            )
        ),
        "dynamic_param_count": int(len(folded.dynamic_params)),
        "folded_history_count": int(len(folded.fold_table)),
        "raw_intercept": _safe_metric(folded.raw_intercept),
        "folded_intercept_raw": _safe_metric(folded.folded_intercept),
        "folded_intercept_final": _scale_or_none(
            folded.folded_intercept, final_scale
        ),
        "train_metrics": _sanitize_metric_dict(result.get("train_metrics")),
        "target_metrics": _sanitize_metric_dict(result.get("target_metrics")),
        "target_has_actual": bool(result.get("target_has_actual")),
        "province_model_prediction": _safe_metric(
            folded.province_model_prediction
        ),
        "province_folded_prediction": _safe_metric(
            folded.province_folded_prediction
        ),
        "province_model_prediction_final": _scale_or_none(
            folded.province_model_prediction, final_scale
        ),
        "province_folded_prediction_final": _scale_or_none(
            folded.province_folded_prediction, final_scale
        ),
        "county_predictions": _safe_records(result["target"]),
        "valid_pixel_count": valid_pixel_count,
    }

    train_metrics = metrics["train_metrics"] or {}
    cards: List[Dict[str, Any]] = [
        make_card("target_month", "目标月份", metrics["target_month"]),
        make_card("target_season", "目标榨季", metrics["target_season"], "年"),
        make_card(
            "run_mode",
            "运行模式",
            {"forecast": "外推", "with_target": "目标年入训"}.get(
                spec.run_mode, spec.run_mode
            ),
            "",
        ),
        make_card("train_n", "训练样本数", metrics["train_n"], "条"),
        make_card("target_n", "县级数量", metrics["target_n"], "个"),
        make_card("feature_count", "动态特征数", metrics["feature_count"], "个"),
        make_card(
            "model_feature_count",
            "模型特征数",
            metrics["model_feature_count"],
            "个",
        ),
        make_card(
            "interaction_feature_count",
            "regime 交互项数",
            metrics["interaction_feature_count"],
            "个",
        ),
        make_card("ridge_alpha", "Ridge α", metrics["ridge_alpha"], ""),
        make_card(
            "regime_start_year",
            "regime 起始榨季",
            metrics["regime_start_year"],
            "年",
        ),
        make_card(
            "final_result_scale",
            "最终结果系数",
            metrics["final_result_scale"],
            "",
        ),
        make_card("train_r2", "训练集 R²", train_metrics.get("R2"), ""),
        make_card("train_mae", "训练集 MAE", train_metrics.get("MAE"), "分数"),
        make_card(
            "train_rmse", "训练集 RMSE", train_metrics.get("RMSE"), "分数"
        ),
        make_card(
            "province_folded_prediction_final",
            "省级折叠预测",
            metrics["province_folded_prediction_final"],
            "分数",
        ),
        make_card(
            "folded_intercept_final",
            "折叠截距(final)",
            metrics["folded_intercept_final"],
            "分数",
        ),
        make_card(
            "dynamic_param_count",
            "动态参数数",
            metrics["dynamic_param_count"],
            "个",
        ),
        make_card(
            "folded_history_count",
            "折叠历史项数",
            metrics["folded_history_count"],
            "个",
        ),
        make_card("valid_pixel_count", "有效像元数", valid_pixel_count, "个"),
    ]
    if spec.run_mode == "with_target":
        cards.append(
            make_card(
                "warning",
                "运行模式提示",
                "目标榨季已入训，结果不解释为外推精度",
                "",
            )
        )
    metrics["cards"] = cards

    if progress_callback:
        progress_callback(100, "估产完成")

    return make_result(
        status="completed",
        result_type="raster",
        files=files,
        metrics=metrics,
        message=(
            f"本地持续估产2完成：目标月份 {metrics['target_month']}，"
            f"运行模式 {spec.run_mode}，输出 {len(files)} 个 GeoTIFF，"
            f"有效像元 {valid_pixel_count or 0}"
        ),
    )


__all__ = ["run"]