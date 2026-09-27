# -*- coding: utf-8 -*-
from __future__ import annotations

"""
广西甘蔗持续估产本地化优化版。

核心流程：
1. 从脚本同目录的 GX_County_Sugarcane_Monthly_2020_2025_26.xlsx 读取参数表。
2. 补全“县×榨季×月份”网格，用训练期同月份中位数填补缺失。
3. 构建16个去冗余动态特征和4个历史特征，拟合带2025+斜率校正的Ridge模型。
4. 把县级历史项折叠进省级截距，再读取脚本同目录“本地影像输入”中的月度GeoTIFF逐像元计算。
5. 默认使用脚本同目录的 classification_April_1.tif，仅在甘蔗类别像元输出结果。
6. 输出原始结果、最终缩放结果、县级预测、动态参数和折叠参数。

目标年度入训说明：
- ALLOW_TARGET_IN_TRAINING=False：训练年份不得包含目标榨季，属于外推模式。
- ALLOW_TARGET_IN_TRAINING=True：允许目标榨季入训，属于同表拟合/参数生成模式，
  对应目标年度指标不能解释为外推精度。
"""

import argparse
import json
import warnings
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject
from rasterio.windows import Window
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

# =============================================================================
# 1. 默认配置
# =============================================================================
WORKSPACE = Path(__file__).resolve().parent
INPUT_FILE = WORKSPACE / "GX_County_Sugarcane_Monthly_2020_2025_26.xlsx"
SHEET_NAME = "Monthly_With_Yield_Area"

# 与待转换Notebook保持一致。
TARGET_MONTH = "2026-6"
TRAIN_START_YEAR = 2020
TRAIN_END_YEAR = 2025
ALLOW_TARGET_IN_TRAINING = False
REGIME_START_YEAR = 2025
RIDGE_ALPHA = 0.30
FINAL_RESULT_SCALE = 0.0565

# 所有默认输入都放在脚本所在的“转python”目录，便于整体移动和本地运行。
INPUT_RASTER_DIR = WORKSPACE / "本地影像输入"
RASTER_NAME_TEMPLATE = "{year}_{month:02d}_{feature}.tif"
CANE_MASK_TIF = WORKSPACE / "classification_April_1.tif"
# classification_April_1.tif 中类别1代表甘蔗；255为nodata，不应参与输出。
CANE_MASK_VALUES = (1.0,)
NODATA_VALUE = -9999.0

COUNTY_COL = "地名"
DATE_COL = "date"
TARGET_COL = "Yield"
AREA_COL = "Sugarcane_Area_ha"
SEASON_COL = "crop_year"
SEASON_MONTH_COL = "season_month"
RAW_FEATURES = ["NDVI", "temperature_2m", "total_precipitation_sum"]
HISTORY_COLS = [
    "county_yield_hist_mean",
    "county_yield_hist_last",
    "county_yield_hist_count",
    "county_area_hist_last",
]
INTERACTION_PREFIX = "regime_x__"
MONTH_LABELS = {
    1: "Apr", 2: "May", 3: "Jun", 4: "Jul", 5: "Aug", 6: "Sep",
    7: "Oct", 8: "Nov", 9: "Dec", 10: "Jan", 11: "Feb", 12: "Mar",
}


@dataclass(frozen=True)
class ModelSpec:
    target_dt: pd.Timestamp
    target_season: int
    cutoff_idx: int
    cutoff_label: str
    target_in_training: bool
    run_mode: str
    run_tag: str
    regime_col: str
    dynamic_cols: List[str]
    base_feature_cols: List[str]
    model_feature_cols: List[str]


@dataclass(frozen=True)
class FoldedFormula:
    raw_intercept: float
    folded_intercept: float
    fold_table: pd.DataFrame
    dynamic_params: pd.DataFrame
    province_model_prediction: float
    province_folded_prediction: float


@dataclass(frozen=True)
class RasterBundle:
    arrays: Dict[Tuple[int, str], np.ndarray]
    valid_masks: Dict[Tuple[int, str], np.ndarray]
    profile: dict


def season_year(dt: pd.Timestamp) -> int:
    return int(dt.year if dt.month >= 4 else dt.year - 1)


def season_month_index(dt: pd.Timestamp) -> int:
    return int(dt.month - 3 if dt.month >= 4 else dt.month + 9)


def make_spec(args: argparse.Namespace) -> ModelSpec:
    target_dt = pd.to_datetime(args.target_month).replace(day=1)
    target_season = season_year(target_dt)
    cutoff_idx = season_month_index(target_dt)
    cutoff_label = MONTH_LABELS[cutoff_idx]
    if args.train_start_year > args.train_end_year:
        raise ValueError("训练起始年份不能大于训练结束年份。")
    target_in_training = args.train_start_year <= target_season <= args.train_end_year
    if target_in_training and not args.allow_target_in_training:
        raise ValueError(
            f"训练年份 {args.train_start_year}-{args.train_end_year} 包含目标榨季 {target_season}。"
            "真实外推请缩短训练期；若明确需要目标年入训，请使用 --allow-target-in-training。"
        )
    run_mode = "with_target" if target_in_training and args.allow_target_in_training else "forecast"
    target_label = target_dt.strftime("%Y_%m")
    run_tag = f"train{args.train_start_year}_{args.train_end_year}_target{target_label}_{run_mode}"
    regime_col = f"recent_regime_{args.regime_start_year}_plus"

    def dcol(feature: str, stat: str) -> str:
        return f"{feature}_{stat}_Apr_to_{cutoff_label}"

    dynamic_cols: List[str] = []
    for feature in RAW_FEATURES:
        dynamic_cols.extend([dcol(feature, stat) for stat in ["mean", "std", "min", "max", "trend"]])
    dynamic_cols.append(dcol("total_precipitation_sum", "sum"))
    base_feature_cols = dynamic_cols + HISTORY_COLS
    model_feature_cols = (
        base_feature_cols
        + [regime_col]
        + [INTERACTION_PREFIX + col for col in base_feature_cols]
    )
    return ModelSpec(
        target_dt=target_dt,
        target_season=target_season,
        cutoff_idx=cutoff_idx,
        cutoff_label=cutoff_label,
        target_in_training=target_in_training,
        run_mode=run_mode,
        run_tag=run_tag,
        regime_col=regime_col,
        dynamic_cols=dynamic_cols,
        base_feature_cols=base_feature_cols,
        model_feature_cols=model_feature_cols,
    )


def dcol(spec: ModelSpec, feature: str, stat: str) -> str:
    return f"{feature}_{stat}_Apr_to_{spec.cutoff_label}"


# =============================================================================
# 2. 表格数据、缺失修复和模型特征
# =============================================================================
def load_table(path: Path, sheet_name: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"参数表不存在: {path}")
    print(f"正在读取参数表: {path}")
    df = pd.read_excel(path, sheet_name=sheet_name)
    df.columns = [str(c).strip() for c in df.columns]
    required = [COUNTY_COL, DATE_COL, TARGET_COL, AREA_COL, SEASON_COL, SEASON_MONTH_COL] + RAW_FEATURES
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"参数表缺少字段: {missing}")

    df[DATE_COL] = pd.to_datetime(df[DATE_COL], errors="coerce")
    df[COUNTY_COL] = df[COUNTY_COL].astype(str).str.strip()
    for col in [TARGET_COL, AREA_COL, SEASON_COL, SEASON_MONTH_COL] + RAW_FEATURES:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    valid = df[DATE_COL].notna()
    month = df.loc[valid, DATE_COL].dt.month
    year = df.loc[valid, DATE_COL].dt.year
    inferred_season = pd.Series(np.nan, index=df.index, dtype="float64")
    inferred_month = pd.Series(np.nan, index=df.index, dtype="float64")
    inferred_season.loc[valid] = np.where(month >= 4, year, year - 1)
    inferred_month.loc[valid] = np.where(month >= 4, month - 3, month + 9)
    missing_season = df[SEASON_COL].isna() & valid
    missing_month = df[SEASON_MONTH_COL].isna() & valid
    df[SEASON_COL] = df[SEASON_COL].fillna(inferred_season)
    df[SEASON_MONTH_COL] = df[SEASON_MONTH_COL].fillna(inferred_month)
    if missing_season.any() or missing_month.any():
        print(
            f"按date补齐榨季字段: crop_year={int(missing_season.sum())}行, "
            f"season_month={int(missing_month.sum())}行"
        )
    return df


def fit_month_fill_values(df: pd.DataFrame, spec: ModelSpec, args: argparse.Namespace) -> Dict[str, Dict[int, float]]:
    train = df[
        df[SEASON_COL].between(args.train_start_year, args.train_end_year)
        & df[SEASON_MONTH_COL].between(1, spec.cutoff_idx)
    ]
    fill: Dict[str, Dict[int, float]] = {}
    for feature in RAW_FEATURES:
        global_median = float(train[feature].median())
        if not np.isfinite(global_median):
            raise ValueError(f"训练期特征 {feature} 全部缺失，无法生成填充值。")
        month_median = train.groupby(SEASON_MONTH_COL)[feature].median()
        fill[feature] = {
            idx: float(month_median.get(idx, global_median))
            for idx in range(1, spec.cutoff_idx + 1)
        }
    return fill


def build_complete_samples(
    df: pd.DataFrame,
    month_fill: Dict[str, Dict[int, float]],
    spec: ModelSpec,
    args: argparse.Namespace,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    year_min = min(args.train_start_year, spec.target_season)
    year_max = max(args.train_end_year, spec.target_season)
    src = df[
        df[SEASON_COL].between(year_min, year_max)
        & df[SEASON_MONTH_COL].between(1, spec.cutoff_idx)
    ].copy()
    if src.empty:
        raise ValueError("当前年份和月份配置没有匹配到参数表数据。")

    base = src.groupby([SEASON_COL, COUNTY_COL], as_index=False).agg(
        actual_yield=(TARGET_COL, "mean"),
        current_area=(AREA_COL, "mean"),
        observed_month_count=(SEASON_MONTH_COL, "nunique"),
    )
    counties = sorted(src[COUNTY_COL].dropna().unique())
    grid = pd.MultiIndex.from_product(
        [range(year_min, year_max + 1), counties, range(1, spec.cutoff_idx + 1)],
        names=[SEASON_COL, COUNTY_COL, SEASON_MONTH_COL],
    )
    monthly = (
        src.groupby([SEASON_COL, COUNTY_COL, SEASON_MONTH_COL])[RAW_FEATURES]
        .mean()
        .reindex(grid)
        .reset_index()
    )
    audit = (
        monthly.groupby([SEASON_COL, COUNTY_COL])[RAW_FEATURES]
        .apply(lambda group: int(group.isna().any(axis=1).sum()))
        .rename("filled_month_count")
        .reset_index()
    )
    for feature in RAW_FEATURES:
        monthly[feature] = monthly[feature].fillna(
            monthly[SEASON_MONTH_COL].map(month_fill[feature])
        )
    if monthly[RAW_FEATURES].isna().any().any():
        raise ValueError("月份网格补值后仍有缺失，请检查参数表。")

    pieces: List[pd.DataFrame] = []
    for feature in RAW_FEATURES:
        pivot = monthly.pivot(
            index=[SEASON_COL, COUNTY_COL], columns=SEASON_MONTH_COL, values=feature
        )
        values = pivot.to_numpy(float)
        summary = pd.DataFrame(index=pivot.index)
        summary[dcol(spec, feature, "mean")] = values.mean(axis=1)
        summary[dcol(spec, feature, "std")] = values.std(axis=1, ddof=1)
        summary[dcol(spec, feature, "min")] = values.min(axis=1)
        summary[dcol(spec, feature, "max")] = values.max(axis=1)
        summary[dcol(spec, feature, "trend")] = values[:, -1] - values[:, 0]
        if feature == "total_precipitation_sum":
            summary[dcol(spec, feature, "sum")] = values.sum(axis=1)
        pieces.append(summary)
    features = pd.concat(pieces, axis=1).reset_index()
    samples = (
        features.merge(base, on=[SEASON_COL, COUNTY_COL], how="left")
        .merge(audit, on=[SEASON_COL, COUNTY_COL], how="left")
    )
    return samples, monthly


def add_history_features(
    train_rows: pd.DataFrame,
    rows: pd.DataFrame,
    exclude_own_year: bool,
) -> pd.DataFrame:
    history = train_rows.dropna(subset=["actual_yield"]).copy()
    if history.empty:
        raise ValueError("没有可用于历史特征的非空Yield样本。")
    global_yield = float(history["actual_yield"].mean())
    out = rows.copy()
    records = {col: [] for col in HISTORY_COLS}
    for _, row in out.iterrows():
        county_history = history[history[COUNTY_COL].eq(row[COUNTY_COL])].sort_values(SEASON_COL)
        if exclude_own_year:
            county_history = county_history[~county_history[SEASON_COL].eq(row[SEASON_COL])]
        yields = county_history["actual_yield"].dropna()
        areas = county_history["current_area"].replace(0, np.nan).dropna()
        records["county_yield_hist_mean"].append(float(yields.mean()) if len(yields) else global_yield)
        records["county_yield_hist_last"].append(float(yields.iloc[-1]) if len(yields) else global_yield)
        records["county_yield_hist_count"].append(float(len(yields)))
        records["county_area_hist_last"].append(float(areas.iloc[-1]) if len(areas) else 0.0)
    for col, values in records.items():
        out[col] = values
    out["area_for_weight"] = out["current_area"].where(
        out["current_area"].fillna(0) > 0, out["county_area_hist_last"]
    )
    out["area_for_weight"] = out["area_for_weight"].fillna(0).clip(lower=0)
    return out


def add_regime_features(rows: pd.DataFrame, spec: ModelSpec, regime_start_year: int) -> pd.DataFrame:
    out = rows.copy()
    out[spec.regime_col] = (out[SEASON_COL] >= regime_start_year).astype(float)
    for col in spec.base_feature_cols:
        out[INTERACTION_PREFIX + col] = out[spec.regime_col] * out[col]
    return out


# =============================================================================
# 3. 模型训练、参数还原和省级折叠
# =============================================================================
def make_model(alpha: float):
    return make_pipeline(
        SimpleImputer(strategy="median"),
        StandardScaler(),
        Ridge(alpha=alpha),
    )


def metric_dict(
    actual: Sequence[float],
    predicted: Sequence[float],
    weights: Sequence[float] | None = None,
) -> Dict[str, float]:
    y = np.asarray(actual, float)
    pred = np.asarray(predicted, float)
    valid = np.isfinite(y) & np.isfinite(pred)
    y, pred = y[valid], pred[valid]
    if len(y) == 0:
        return {"N": 0, "R2": np.nan, "MAE": np.nan, "RMSE": np.nan, "Bias": np.nan}
    result = {
        "N": int(len(y)),
        "R2": float(r2_score(y, pred)) if len(y) > 1 else np.nan,
        "MAE": float(mean_absolute_error(y, pred)),
        "RMSE": float(np.sqrt(mean_squared_error(y, pred))),
        "Bias": float(np.mean(pred - y)),
    }
    if weights is not None:
        w = np.asarray(weights, float)[valid]
        ok = np.isfinite(w) & (w > 0)
        if ok.any():
            result["Province_Actual"] = float(np.average(y[ok], weights=w[ok]))
            result["Province_Pred"] = float(np.average(pred[ok], weights=w[ok]))
            result["Province_Error"] = result["Province_Pred"] - result["Province_Actual"]
    return result


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    values_array = np.asarray(values, float)
    weights_array = np.asarray(weights, float)
    valid = np.isfinite(values_array) & np.isfinite(weights_array) & (weights_array > 0)
    if not valid.any():
        return np.nan
    return float(np.average(values_array[valid], weights=weights_array[valid]))


def extract_raw_params(model, feature_cols: List[str]) -> Tuple[float, pd.DataFrame]:
    imputer: SimpleImputer = model.named_steps["simpleimputer"]
    scaler: StandardScaler = model.named_steps["standardscaler"]
    ridge: Ridge = model.named_steps["ridge"]
    beta = ridge.coef_.astype(float)
    raw_coef = beta / scaler.scale_
    raw_intercept = float(ridge.intercept_ - np.sum(beta * scaler.mean_ / scaler.scale_))
    params = pd.DataFrame(
        {
            "feature": feature_cols,
            "fill_value": imputer.statistics_.astype(float),
            "coef_standardized": beta,
            "raw_coef": raw_coef,
        }
    )
    return raw_intercept, params


def build_effective_formula(
    raw_intercept: float,
    params: pd.DataFrame,
    target_rows: pd.DataFrame,
    spec: ModelSpec,
) -> Tuple[float, pd.DataFrame, pd.DataFrame]:
    param_index = params.set_index("feature")
    coef = param_index["raw_coef"]
    regime_value = float(target_rows[spec.regime_col].iloc[0])
    folded_intercept = float(raw_intercept + regime_value * coef[spec.regime_col])
    records = []
    for col in spec.base_feature_cols:
        records.append(
            {
                "feature": col,
                "fill_value": float(param_index.loc[col, "fill_value"]),
                "effective_raw_coef": float(
                    coef[col] + regime_value * coef[INTERACTION_PREFIX + col]
                ),
            }
        )
    effective = pd.DataFrame(records)
    dynamic = effective[effective["feature"].isin(spec.dynamic_cols)].copy()
    history = effective[effective["feature"].isin(HISTORY_COLS)].copy()
    fold_records = []
    for _, row in history.iterrows():
        value = weighted_mean(target_rows[row["feature"]], target_rows["area_for_weight"])
        contribution = value * float(row["effective_raw_coef"])
        folded_intercept += contribution
        fold_records.append(
            {
                "feature": row["feature"],
                "folded_value": value,
                "effective_raw_coef": row["effective_raw_coef"],
                "contribution": contribution,
            }
        )
    return folded_intercept, dynamic, pd.DataFrame(fold_records)


def run_model(args: argparse.Namespace) -> Dict[str, object]:
    spec = make_spec(args)
    df = load_table(Path(args.input_file), args.sheet)
    month_fill = fit_month_fill_values(df, spec, args)
    samples, completed_monthly = build_complete_samples(df, month_fill, spec, args)
    train_base = samples[
        samples[SEASON_COL].between(args.train_start_year, args.train_end_year)
        & samples["actual_yield"].notna()
    ].copy()
    target_base = samples[samples[SEASON_COL].eq(spec.target_season)].copy()
    if train_base.empty:
        raise ValueError("当前训练年份没有非空Yield样本。")
    if target_base.empty:
        raise ValueError(f"目标榨季 {spec.target_season} 没有输入数据。")

    target_exclude_own = bool(spec.target_in_training and args.allow_target_in_training)
    train = add_regime_features(
        add_history_features(train_base, train_base, exclude_own_year=True),
        spec,
        args.regime_start_year,
    )
    target = add_regime_features(
        add_history_features(train_base, target_base, exclude_own_year=target_exclude_own),
        spec,
        args.regime_start_year,
    )
    model = make_model(args.ridge_alpha)
    model.fit(train[spec.model_feature_cols], train["actual_yield"])
    train_pred = model.predict(train[spec.model_feature_cols])
    target_pred = model.predict(target[spec.model_feature_cols])

    train_metrics = metric_dict(train["actual_yield"], train_pred, train["area_for_weight"])
    target_valid = target["actual_yield"].notna().to_numpy()
    target_metrics = metric_dict(
        target.loc[target_valid, "actual_yield"],
        target_pred[target_valid],
        target.loc[target_valid, "area_for_weight"] if target_valid.any() else None,
    )

    raw_intercept, params = extract_raw_params(model, spec.model_feature_cols)
    folded_intercept, dynamic_params, fold_table = build_effective_formula(
        raw_intercept, params, target, spec
    )
    dynamic_coef = dynamic_params.set_index("feature")["effective_raw_coef"]
    formula_pred = folded_intercept + target[spec.dynamic_cols].to_numpy(float).dot(
        dynamic_coef.loc[spec.dynamic_cols].to_numpy(float)
    )
    province_model = weighted_mean(target_pred, target["area_for_weight"])
    province_folded = weighted_mean(formula_pred, target["area_for_weight"])

    target = target.copy()
    target["predicted_yield_raw"] = target_pred
    target["predicted_yield_final"] = target_pred * args.final_result_scale
    target["folded_formula_raw"] = formula_pred
    folded = FoldedFormula(
        raw_intercept=raw_intercept,
        folded_intercept=folded_intercept,
        fold_table=fold_table,
        dynamic_params=dynamic_params,
        province_model_prediction=province_model,
        province_folded_prediction=province_folded,
    )
    return {
        "spec": spec,
        "model": model,
        "month_fill": month_fill,
        "completed_monthly": completed_monthly,
        "train": train,
        "target": target,
        "train_metrics": train_metrics,
        "target_metrics": target_metrics,
        "target_has_actual": bool(target_valid.any()),
        "params": params,
        "folded": folded,
    }


def print_metrics(title: str, metrics: Dict[str, float]) -> None:
    print("\n" + "=" * 20 + f" {title} " + "=" * 20)
    for key, value in metrics.items():
        if key == "N":
            print(f"{key:<18}: {int(value)}")
        else:
            print(f"{key:<18}: {value:.6f}" if np.isfinite(value) else f"{key:<18}: NaN")


def print_model_report(result: Dict[str, object], args: argparse.Namespace) -> None:
    spec: ModelSpec = result["spec"]
    folded: FoldedFormula = result["folded"]
    note = (
        "目标榨季已入训；目标指标为同表拟合，不是外推精度。"
        if spec.target_in_training and args.allow_target_in_training
        else "目标榨季未入训；当前为外推模式。"
    )
    print("\n" + "=" * 20 + " 运行设置 " + "=" * 20)
    print(f"参数表: {args.input_file}")
    print(f"目标月份: {spec.target_dt.strftime('%Y-%m')}")
    print(f"目标榨季: {spec.target_season}")
    print(f"训练年份: {args.train_start_year}-{args.train_end_year}")
    print(f"允许目标榨季入训: {args.allow_target_in_training}")
    print(f"运行模式: {spec.run_mode}；{note}")
    print(f"使用月份: Apr 到 {spec.cutoff_label}")
    print(f"动态特征数: {len(spec.dynamic_cols)}")
    print(f"完整模型特征数: {len(spec.model_feature_cols)}")
    print(f"Ridge alpha: {args.ridge_alpha}")
    print_metrics("训练集指标", result["train_metrics"])
    if result["target_has_actual"]:
        print_metrics(f"目标榨季{spec.target_season}指标", result["target_metrics"])
    else:
        print(f"\n目标榨季{spec.target_season}没有实际Yield，无法计算精度指标。")
    print("\n" + "=" * 20 + " 折叠核对 " + "=" * 20)
    print(f"县级模型面积加权原始结果: {folded.province_model_prediction:.6f}")
    print(f"折叠公式面积加权原始结果: {folded.province_folded_prediction:.6f}")
    print(f"折叠差值: {folded.province_folded_prediction-folded.province_model_prediction:.12f}")
    print(f"县级模型面积加权最终结果: {folded.province_model_prediction*args.final_result_scale:.6f}")


# =============================================================================
# 4. 本地影像读取、对齐和特征构建
# =============================================================================
def month_sequence_for_target(target_month: str) -> List[Tuple[int, int, int, str]]:
    target_dt = pd.to_datetime(target_month).replace(day=1)
    target_season = season_year(target_dt)
    cutoff_idx = season_month_index(target_dt)
    sequence = []
    for idx in range(1, cutoff_idx + 1):
        natural_month = idx + 3 if idx <= 9 else idx - 9
        natural_year = target_season if natural_month >= 4 else target_season + 1
        sequence.append((idx, natural_year, natural_month, MONTH_LABELS[idx]))
    return sequence


def raster_path(input_dir: Path, year: int, month: int, feature: str, template: str) -> Path:
    return input_dir / template.format(year=year, month=month, feature=feature)


def required_rasters(target_month: str, input_dir: Path, template: str) -> List[Path]:
    return [
        raster_path(input_dir, year, month, feature, template)
        for _, year, month, _ in month_sequence_for_target(target_month)
        for feature in RAW_FEATURES
    ]


def check_required_rasters(paths: Iterable[Path]) -> None:
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise FileNotFoundError("缺少本地输入影像:\n" + "\n".join(missing))


def read_single_band(path: Path) -> Tuple[np.ndarray, np.ndarray, dict]:
    with rasterio.open(path) as src:
        if src.count != 1:
            raise ValueError(f"要求单波段GeoTIFF，但 {path} 有 {src.count} 个波段。")
        data = src.read(1, out_dtype="float32")
        valid = src.read_masks(1) > 0
        if src.nodata is not None:
            valid &= data != src.nodata
        valid &= np.isfinite(data)
        profile = src.profile.copy()
    return data.astype("float32"), valid, profile


def same_grid(left: dict, right: dict) -> bool:
    return (
        left.get("crs") == right.get("crs")
        and left.get("transform") == right.get("transform")
        and left.get("width") == right.get("width")
        and left.get("height") == right.get("height")
    )


def reproject_to_reference(
    array: np.ndarray,
    valid_mask: np.ndarray,
    src_profile: dict,
    reference_profile: dict,
    value_resampling: Resampling = Resampling.bilinear,
) -> Tuple[np.ndarray, np.ndarray]:
    shape = (int(reference_profile["height"]), int(reference_profile["width"]))
    destination = np.full(shape, np.nan, dtype="float32")
    source = array.astype("float32", copy=True)
    source[~valid_mask] = np.nan
    reproject(
        source=source,
        destination=destination,
        src_transform=src_profile["transform"],
        src_crs=src_profile["crs"],
        src_nodata=src_profile.get("nodata") if src_profile.get("nodata") is not None else np.nan,
        dst_transform=reference_profile["transform"],
        dst_crs=reference_profile["crs"],
        dst_nodata=np.nan,
        resampling=value_resampling,
    )
    dst_mask = np.zeros(shape, dtype="uint8")
    reproject(
        source=valid_mask.astype("uint8"),
        destination=dst_mask,
        src_transform=src_profile["transform"],
        src_crs=src_profile["crs"],
        src_nodata=0,
        dst_transform=reference_profile["transform"],
        dst_crs=reference_profile["crs"],
        dst_nodata=0,
        resampling=Resampling.nearest,
    )
    valid = (dst_mask > 0) & np.isfinite(destination)
    return destination.astype("float32"), valid


def load_monthly_rasters(target_month: str, input_dir: Path, template: str) -> RasterBundle:
    paths = required_rasters(target_month, input_dir, template)
    check_required_rasters(paths)
    arrays: Dict[Tuple[int, str], np.ndarray] = {}
    masks: Dict[Tuple[int, str], np.ndarray] = {}
    reference_profile = None
    for month_idx, year, month, _ in month_sequence_for_target(target_month):
        for feature in RAW_FEATURES:
            path = raster_path(input_dir, year, month, feature, template)
            array, valid, profile = read_single_band(path)
            if reference_profile is None:
                reference_profile = profile
            elif not same_grid(reference_profile, profile):
                print(f"影像网格不一致，重投影到参考网格: {path}")
                array, valid = reproject_to_reference(array, valid, profile, reference_profile)
            arrays[(month_idx, feature)] = array
            masks[(month_idx, feature)] = valid
    if reference_profile is None:
        raise ValueError("没有读取到任何本地影像。")
    return RasterBundle(arrays=arrays, valid_masks=masks, profile=reference_profile)


def load_cane_mask(mask_path: Path | None, profile: dict) -> np.ndarray | None:
    if mask_path is None:
        return None
    array, valid, mask_profile = read_single_band(mask_path)
    if not same_grid(profile, mask_profile):
        array, valid = reproject_to_reference(
            array, valid, mask_profile, profile, Resampling.nearest
        )
    return valid & (array > 0)


def build_feature_images(
    bundle: RasterBundle,
    month_fill: Dict[str, Dict[int, float]],
    spec: ModelSpec,
) -> Dict[str, np.ndarray]:
    """先按训练期同月份中位数补齐像元，再构造与模型一致的16个汇总特征。"""
    features: Dict[str, np.ndarray] = {}
    for feature in RAW_FEATURES:
        month_arrays = []
        for idx in range(1, spec.cutoff_idx + 1):
            array = bundle.arrays[(idx, feature)]
            valid = bundle.valid_masks[(idx, feature)]
            filled = np.where(valid & np.isfinite(array), array, month_fill[feature][idx])
            month_arrays.append(filled.astype("float32"))
        values = np.stack(month_arrays, axis=0).astype("float64")
        features[dcol(spec, feature, "mean")] = values.mean(axis=0).astype("float32")
        features[dcol(spec, feature, "std")] = values.std(axis=0, ddof=1).astype("float32")
        features[dcol(spec, feature, "min")] = values.min(axis=0).astype("float32")
        features[dcol(spec, feature, "max")] = values.max(axis=0).astype("float32")
        features[dcol(spec, feature, "trend")] = (values[-1] - values[0]).astype("float32")
        if feature == "total_precipitation_sum":
            features[dcol(spec, feature, "sum")] = values.sum(axis=0).astype("float32")
    missing = [col for col in spec.dynamic_cols if col not in features]
    if missing:
        raise KeyError(f"本地动态特征构建不完整: {missing}")
    return features


# =============================================================================
# 5. 像元预测与GeoTIFF输出
# =============================================================================
def predict_raster(
    feature_images: Dict[str, np.ndarray],
    folded: FoldedFormula,
    final_scale: float,
    cane_mask: np.ndarray | None,
) -> Tuple[np.ndarray, np.ndarray]:
    first = next(iter(feature_images.values()))
    raw = np.full(first.shape, folded.folded_intercept, dtype="float64")
    for _, row in folded.dynamic_params.iterrows():
        name = str(row["feature"])
        if name not in feature_images:
            raise KeyError(f"本地特征缺失: {name}")
        array = feature_images[name].astype("float64")
        fill = float(row["fill_value"])
        coef = float(row["effective_raw_coef"])
        raw += np.where(np.isfinite(array), array, fill) * coef
    valid = np.isfinite(raw)
    if cane_mask is not None:
        valid &= cane_mask
    raw = np.where(valid, raw, np.nan).astype("float32")
    final = np.where(valid, raw * final_scale, np.nan).astype("float32")
    return raw, final


def write_tif(path: Path, array: np.ndarray, profile: dict, nodata: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output_profile = profile.copy()
    output_profile.update(
        driver="GTiff",
        count=1,
        dtype="float32",
        nodata=nodata,
        compress="deflate",
        predictor=2,
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )
    with rasterio.open(path, "w", **output_profile) as dst:
        dst.write(np.where(np.isfinite(array), array, nodata).astype("float32"), 1)


def raster_stats(array: np.ndarray) -> Dict[str, float]:
    valid = np.isfinite(array)
    if not valid.any():
        return {"count": 0, "mean": np.nan, "min": np.nan, "max": np.nan, "std": np.nan}
    values = array[valid].astype("float64")
    return {
        "count": int(values.size),
        "mean": float(values.mean()),
        "min": float(values.min()),
        "max": float(values.max()),
        "std": float(values.std(ddof=0)),
    }


def save_model_tables(result: Dict[str, object], args: argparse.Namespace) -> None:
    spec: ModelSpec = result["spec"]
    folded: FoldedFormula = result["folded"]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target: pd.DataFrame = result["target"]
    target_cols = [
        COUNTY_COL, SEASON_COL, "actual_yield", "predicted_yield_raw",
        "predicted_yield_final", "filled_month_count",
    ]
    target[target_cols].to_csv(
        output_dir / f"县级预测_{spec.run_tag}.csv", index=False, encoding="utf-8-sig"
    )
    folded.dynamic_params.to_csv(
        output_dir / f"本地动态参数_{spec.run_tag}.csv", index=False, encoding="utf-8-sig"
    )
    folded.fold_table.to_csv(
        output_dir / f"折叠历史项_{spec.run_tag}.csv", index=False, encoding="utf-8-sig"
    )
    with (output_dir / f"月份填充值_{spec.run_tag}.json").open("w", encoding="utf-8") as file:
        json.dump(result["month_fill"], file, ensure_ascii=False, indent=2)


# =============================================================================
# 6. 真实大影像分块计算
# =============================================================================
@dataclass
class RunningStats:
    """不回读整幅输出影像，分块累计基本统计量。"""

    count: int = 0
    total: float = 0.0
    total_sq: float = 0.0
    minimum: float = np.inf
    maximum: float = -np.inf

    def update(self, array: np.ndarray) -> None:
        values = np.asarray(array, dtype="float64")
        values = values[np.isfinite(values)]
        if values.size == 0:
            return
        self.count += int(values.size)
        self.total += float(values.sum())
        self.total_sq += float(np.square(values).sum())
        self.minimum = min(self.minimum, float(values.min()))
        self.maximum = max(self.maximum, float(values.max()))

    def as_dict(self) -> Dict[str, float]:
        if self.count == 0:
            return {"count": 0, "mean": np.nan, "min": np.nan, "max": np.nan, "std": np.nan}
        mean = self.total / self.count
        variance = max(self.total_sq / self.count - mean * mean, 0.0)
        return {
            "count": self.count,
            "mean": mean,
            "min": self.minimum,
            "max": self.maximum,
            "std": float(np.sqrt(variance)),
        }


def iter_windows(width: int, height: int, block_size: int) -> Iterable[Window]:
    """按固定窗口遍历参考网格，避免一次把整幅10米影像展开进内存。"""
    for row_off in range(0, height, block_size):
        window_height = min(block_size, height - row_off)
        for col_off in range(0, width, block_size):
            window_width = min(block_size, width - col_off)
            yield Window(col_off, row_off, window_width, window_height)


def open_aligned_reader(
    stack: ExitStack,
    path: Path,
    reference_profile: dict,
    resampling: Resampling,
):
    """打开影像；网格不一致时创建按窗口、按需重投影的WarpedVRT。"""
    source = stack.enter_context(rasterio.open(path))
    if same_grid(reference_profile, source.profile):
        return source
    print(f"影像网格不一致，启用分块重投影: {path}")
    return stack.enter_context(
        WarpedVRT(
            source,
            crs=reference_profile["crs"],
            transform=reference_profile["transform"],
            width=int(reference_profile["width"]),
            height=int(reference_profile["height"]),
            resampling=resampling,
            nodata=np.nan,
            dtype="float32",
        )
    )


def read_reader_window(reader, window: Window) -> Tuple[np.ndarray, np.ndarray]:
    data = reader.read(1, window=window, out_dtype="float32")
    valid = reader.read_masks(1, window=window) > 0
    nodata = reader.nodata
    if nodata is not None and np.isfinite(nodata):
        valid &= data != nodata
    valid &= np.isfinite(data)
    return data, valid


def predict_rasters_blockwise(
    result: Dict[str, object],
    args: argparse.Namespace,
    raw_path: Path,
    final_path: Path,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """分块读取、重投影、构造特征并直接写出两个GeoTIFF。"""
    spec: ModelSpec = result["spec"]
    folded: FoldedFormula = result["folded"]
    month_fill: Dict[str, Dict[int, float]] = result["month_fill"]
    input_dir = Path(args.input_raster_dir)
    if args.block_size <= 0:
        raise ValueError("--block-size 必须为正整数。")
    paths = required_rasters(args.target_month, input_dir, args.raster_template)
    check_required_rasters(paths)
    reference_path = paths[0]
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.parent.mkdir(parents=True, exist_ok=True)

    coefficient_map = folded.dynamic_params.set_index("feature")["effective_raw_coef"].to_dict()
    fill_map = folded.dynamic_params.set_index("feature")["fill_value"].to_dict()
    raw_stats = RunningStats()
    final_stats = RunningStats()

    with ExitStack() as stack:
        reference = stack.enter_context(rasterio.open(reference_path))
        reference_profile = reference.profile.copy()
        readers = {}
        for month_idx, year, month, _ in month_sequence_for_target(args.target_month):
            for feature in RAW_FEATURES:
                path = raster_path(input_dir, year, month, feature, args.raster_template)
                if path.resolve() == reference_path.resolve():
                    readers[(month_idx, feature)] = reference
                else:
                    readers[(month_idx, feature)] = open_aligned_reader(
                        stack, path, reference_profile, Resampling.bilinear
                    )

        mask_reader = None
        if args.cane_mask_tif and not args.no_cane_mask:
            mask_path = Path(args.cane_mask_tif)
            if not mask_path.is_file():
                raise FileNotFoundError(f"甘蔗掩膜不存在: {mask_path}")
            mask_reader = open_aligned_reader(
                stack, mask_path, reference_profile, Resampling.nearest
            )
            print(
                f"甘蔗掩膜: {mask_path}；保留类别: "
                + ", ".join(f"{value:g}" for value in args.cane_mask_values)
            )

        output_profile = reference_profile.copy()
        output_profile.update(
            driver="GTiff",
            count=1,
            dtype="float32",
            nodata=NODATA_VALUE,
            compress="deflate",
            predictor=2,
            tiled=True,
            blockxsize=256,
            blockysize=256,
            BIGTIFF="IF_SAFER",
        )
        raw_dst = stack.enter_context(rasterio.open(raw_path, "w", **output_profile))
        final_dst = stack.enter_context(rasterio.open(final_path, "w", **output_profile))

        total_windows = int(
            np.ceil(int(reference_profile["width"]) / args.block_size)
            * np.ceil(int(reference_profile["height"]) / args.block_size)
        )
        print(
            f"参考影像大小: {reference_profile['width']} x {reference_profile['height']}, "
            f"block_size={args.block_size}, 分块数={total_windows}"
        )
        for window_number, window in enumerate(
            iter_windows(
                int(reference_profile["width"]),
                int(reference_profile["height"]),
                args.block_size,
            ),
            start=1,
        ):
            shape = (int(window.height), int(window.width))
            raw = np.full(shape, folded.folded_intercept, dtype="float64")

            for feature in RAW_FEATURES:
                month_arrays = []
                for month_idx in range(1, spec.cutoff_idx + 1):
                    data, valid = read_reader_window(readers[(month_idx, feature)], window)
                    filled = np.where(
                        valid,
                        data,
                        float(month_fill[feature][month_idx]),
                    )
                    month_arrays.append(filled.astype("float32", copy=False))
                values = np.stack(month_arrays, axis=0).astype("float64", copy=False)
                feature_arrays = {
                    dcol(spec, feature, "mean"): values.mean(axis=0),
                    dcol(spec, feature, "std"): values.std(axis=0, ddof=1),
                    dcol(spec, feature, "min"): values.min(axis=0),
                    dcol(spec, feature, "max"): values.max(axis=0),
                    dcol(spec, feature, "trend"): values[-1] - values[0],
                }
                if feature == "total_precipitation_sum":
                    feature_arrays[dcol(spec, feature, "sum")] = values.sum(axis=0)
                for name, array in feature_arrays.items():
                    filled_feature = np.where(np.isfinite(array), array, float(fill_map[name]))
                    raw += filled_feature * float(coefficient_map[name])

            output_valid = np.isfinite(raw)
            if mask_reader is not None:
                mask_data, mask_valid = read_reader_window(mask_reader, window)
                # 使用明确的分类值，避免把nodata或异常分类值误当成甘蔗。
                output_valid &= mask_valid & np.isin(mask_data, args.cane_mask_values)
            final = raw * float(args.final_result_scale)
            raw_output = np.where(output_valid, raw, NODATA_VALUE).astype("float32")
            final_output = np.where(output_valid, final, NODATA_VALUE).astype("float32")
            raw_dst.write(raw_output, 1, window=window)
            final_dst.write(final_output, 1, window=window)
            raw_stats.update(np.where(output_valid, raw, np.nan))
            final_stats.update(np.where(output_valid, final, np.nan))

            if window_number == 1 or window_number % 100 == 0 or window_number == total_windows:
                print(f"分块进度: {window_number}/{total_windows}")

    return raw_stats.as_dict(), final_stats.as_dict()


# =============================================================================
# 7. 命令行入口
# =============================================================================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="广西甘蔗持续估产本地GeoTIFF优化版。")
    parser.add_argument("--input-file", default=str(INPUT_FILE), help="参数Excel；默认使用脚本同目录文件。")
    parser.add_argument("--sheet", default=SHEET_NAME, help="参数Excel工作表名称。")
    parser.add_argument("--input-raster-dir", default=str(INPUT_RASTER_DIR), help="月度遥感/气象GeoTIFF目录。")
    parser.add_argument("--target-month", default=TARGET_MONTH, help="目标月份，格式YYYY-MM。")
    parser.add_argument("--train-start-year", type=int, default=TRAIN_START_YEAR, help="训练起始榨季。")
    parser.add_argument("--train-end-year", type=int, default=TRAIN_END_YEAR, help="训练结束榨季。")
    parser.add_argument(
        "--allow-target-in-training",
        action=argparse.BooleanOptionalAction,
        default=ALLOW_TARGET_IN_TRAINING,
        help="是否允许目标榨季进入训练集；可用--no-allow-target-in-training关闭。",
    )
    parser.add_argument("--regime-start-year", type=int, default=REGIME_START_YEAR, help="近期校正起始榨季。")
    parser.add_argument("--ridge-alpha", type=float, default=RIDGE_ALPHA, help="Ridge正则强度。")
    parser.add_argument("--final-result-scale", type=float, default=FINAL_RESULT_SCALE, help="最终结果缩放系数。")
    parser.add_argument("--raster-template", default=RASTER_NAME_TEMPLATE, help="输入影像文件名模板。")
    parser.add_argument(
        "--cane-mask-tif",
        default=str(CANE_MASK_TIF),
        help="甘蔗分类掩膜；默认使用脚本同目录 classification_April_1.tif。",
    )
    parser.add_argument(
        "--cane-mask-values",
        type=float,
        nargs="+",
        default=list(CANE_MASK_VALUES),
        help="掩膜中作为甘蔗保留的分类值；默认仅保留类别1。",
    )
    parser.add_argument(
        "--no-cane-mask",
        action="store_true",
        help="明确关闭甘蔗掩膜；默认不关闭。",
    )
    parser.add_argument("--output-dir", default=str(WORKSPACE), help="模型表和GeoTIFF输出目录。")
    parser.add_argument("--output-tif", default=None, help="最终结果GeoTIFF；不传则按运行配置自动命名。")
    parser.add_argument("--raw-output-tif", default=None, help="原始结果GeoTIFF；不传则自动命名。")
    parser.add_argument("--block-size", type=int, default=512, help="分块计算窗口边长；默认512像元。")
    parser.add_argument("--list-required", action="store_true", help="只列出当前目标月所需影像。")
    parser.add_argument("--model-only", action="store_true", help="只重训模型和保存参数表，不读取影像。")
    return parser.parse_args()


def print_required_downloads(args: argparse.Namespace) -> None:
    print("\n" + "=" * 20 + " 本地影像清单 " + "=" * 20)
    print(f"影像目录: {args.input_raster_dir}")
    for path in required_rasters(args.target_month, Path(args.input_raster_dir), args.raster_template):
        print(path)
    if args.no_cane_mask:
        print("甘蔗掩膜: 已通过 --no-cane-mask 关闭")
    else:
        print(f"甘蔗掩膜: {args.cane_mask_tif}")
        print("保留类别: " + ", ".join(f"{value:g}" for value in args.cane_mask_values))


def main() -> None:
    args = parse_args()
    spec = make_spec(args)
    print_required_downloads(args)
    if args.list_required:
        return

    result = run_model(args)
    print_model_report(result, args)
    save_model_tables(result, args)
    if args.model_only:
        print("\n已按 --model-only 完成模型重训和参数表输出。")
        return

    output_dir = Path(args.output_dir)
    raw_path = Path(args.raw_output_tif) if args.raw_output_tif else output_dir / f"本地持续估产_{spec.run_tag}_raw.tif"
    final_path = Path(args.output_tif) if args.output_tif else output_dir / f"本地持续估产_{spec.run_tag}_final.tif"
    raw_stats, final_stats = predict_rasters_blockwise(
        result, args, raw_path, final_path
    )

    print("\n" + "=" * 20 + " 本地GeoTIFF结果 " + "=" * 20)
    print(f"原始结果: {raw_path}")
    print(f"最终结果: {final_path}")
    print("原始统计:", raw_stats)
    print("最终统计:", final_stats)


if __name__ == "__main__":
    main()
