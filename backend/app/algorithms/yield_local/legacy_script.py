# -*- coding: utf-8 -*-
from __future__ import annotations

"""
广西甘蔗单产县级模型 -> 折叠参数 -> 本地 GeoTIFF 线性计算输出。

本版本包含最终结果缩放：
    final_result = raw_model_result * FINAL_RESULT_SCALE

脚本结构按功能组织：
1. 全局配置：输入数据、训练年份、目标月份、GEE 参数和最终结果系数。
2. 通用工具：榨季月份、评价指标、面积加权。
3. 数据与模型：读表补齐榨季字段、构造样本、训练 Ridge、还原参数。
4. 折叠与 GEE：把县级历史项折叠进截距，并生成 GEE JavaScript。
5. 本地栅格计算：读取本地月度 tif，复现 GEE 线性公式并输出 GeoTIFF。
"""

import argparse
import textwrap
import warnings
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import RidgeCV
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject
from rasterio.windows import Window

warnings.filterwarnings("ignore")

# =============================================================================
# 1. 全局配置
# =============================================================================
# ===================== 1. 数据文件与运行口径 =====================
# INPUT_FILE 是本地 Excel 建模表。原始文件不会被本脚本改写，只会被 pandas 读取。
# 路径使用正斜杠，避免 Windows 反斜杠把 \2026 误解释成转义字符。
INPUT_FILE = Path(
    "D:/postgraduate_0/algorithm_review/数据代码封装/数据代码封装/2026持续估产模型/GX_County_Sugarcane_Monthly_2020_2025_with_Yield_Area.xlsx"
)
SHEET_NAME = "Monthly_With_Yield_Area"

# 当前默认运行口径：用 2020-2024 榨季训练，预测 2026-06。
# TARGET_MONTH 会同时控制 Python 特征截断月份、GEE CUTOFF_INDEX 和导出文件名前缀。
TARGET_MONTH = "2026-06"
TRAIN_START_YEAR = 2020
TRAIN_END_YEAR = 2024

# True 表示允许目标榨季进入训练集。
# 这个设置只适合生成同表参数表和检查折叠公式，不应把目标榨季精度解释为外推精度。
# 真实预测未来年份时，应设置为 False，并保证 TRAIN_END_YEAR < target_season。
ALLOW_TARGET_IN_TRAINING = False

# ===================== 2. 最终结果缩放系数 =====================
#0.055为实际尺度全局参数单位t/mu;如想要计算统计年鉴尺度，则设为FINAL_RESULT_SCALE = 1，单位t/ha
FINAL_RESULT_SCALE = 0.055
SCALE_LABEL = f"{FINAL_RESULT_SCALE:.3f}".replace(".", "")
TARGET_LABEL = TARGET_MONTH.replace("-", "_")

# ===================== 3. GEE 导出参数 =====================
# 下面两个 Asset ID 是占位符。复制到 GEE 前，应替换为自己的广西边界和甘蔗掩膜资产。
GEE_PROVINCE_ASSET = "users/your_username/guangxi_boundary"
GEE_SUGARCANE_ASSET = "users/your_username/guangxi_sugarcane_mask"
GEE_EXPORT_FOLDER = "GX_Sugarcane_Yield_GEE"

# 导出前缀自动跟随训练年份、目标月份和缩放系数，避免 target_month=2024-05 但文件名写 target2024_07 的冲突。
GEE_EXPORT_PREFIX = (
    f"GX_countyFolded_pixelYield_train{TRAIN_START_YEAR}_{TRAIN_END_YEAR}_"
    f"target{TARGET_LABEL}_final{SCALE_LABEL}"
)

# ===================== 4. 表字段与原始特征声明 =====================
# 这些字段名必须和 Excel 表一致。字段名集合中放在这里，是为了后续函数只引用统一变量，避免硬编码散落各处。
COUNTY_COL = "地名"
DATE_COL = "date"
TARGET_COL = "Yield"
AREA_COL = "Sugarcane_Area_ha"
SEASON_COL = "crop_year"
SEASON_MONTH_COL = "season_month"

# RAW_FEATURES 是真正从表中读取并可在 GEE 端逐像素计算的原始变量。
# build_samples 会把这些原始变量展开成月度值、均值、标准差、极值、last、trend 等工程特征。
RAW_FEATURES = ["NDVI", "temperature_2m", "total_precipitation_sum"]
SUM_FEATURES = ["total_precipitation_sum"]

# HISTORY_COLS 是县级历史统计量，GEE 像素端无法逐像素计算，所以后面会折叠进截距。
HISTORY_COLS = [
    "county_yield_hist_mean",
    "county_yield_hist_last",
    "county_yield_hist_count",
    "county_area_hist_mean",
    "county_area_hist_last",
    "county_yield_last_minus_mean",
]

# season_month 的 1-12 对应一个甘蔗榨季内从 4 月到次年 3 月的月份。
MONTH_LABELS = {
    1: "Apr",
    2: "May",
    3: "Jun",
    4: "Jul",
    5: "Aug",
    6: "Sep",
    7: "Oct",
    8: "Nov",
    9: "Dec",
    10: "Jan",
    11: "Feb",
    12: "Mar",
}

# =============================================================================
# 2. 通用工具：榨季、指标和面积加权
# =============================================================================
@dataclass(frozen=True)
class FoldedFormula:
    """
    保存县级模型折叠到 GEE 像素公式后的核心结果。
    raw_intercept 是 Ridge 原始线性公式截距；folded_intercept 是把县级历史项折叠后的像素公式截距。
    dynamic_params 只保留 GEE 可以逐像素计算的特征参数，fold_table 记录被折叠进截距的县级历史项贡献。
    """
    raw_intercept: float
    folded_intercept: float
    fold_table: pd.DataFrame
    dynamic_params: pd.DataFrame
    target_dynamic_means: pd.DataFrame
    province_prediction_from_folded_formula: float


def season_year(dt: pd.Timestamp) -> int:
    """
    把自然日期转换为甘蔗榨季年份。
    4-12 月归入当年榨季，1-3 月归入上一年榨季；例如 2025-02 属于 2024 榨季。
    """
    return int(dt.year if dt.month >= 4 else dt.year - 1)


def season_month_index(dt: pd.Timestamp) -> int:
    """
    把自然月份转换为榨季内月份序号。
    4 月为 1，5 月为 2，一直到次年 3 月为 12；这个序号决定 Apr 到目标月的特征截断范围。
    """
    return int(dt.month - 3 if dt.month >= 4 else dt.month + 9)


def rmse(actual: Sequence[float], pred: Sequence[float]) -> float:
    """
    计算均方根误差 RMSE。
    该指标保留目标变量原单位，便于和 MAE、Bias 一起判断误差幅度。
    """
    return float(np.sqrt(mean_squared_error(actual, pred)))


def metric_dict(actual: Sequence[float], pred: Sequence[float], weights: Sequence[float] | None = None) -> Dict[str, float]:
    """
    统一计算模型评价指标，并在提供面积权重时补充省级面积加权误差。
    函数会先剔除 actual 或 pred 非有限值，避免 NaN 直接污染 R2、MAE、RMSE 等指标。
    """
    y = np.asarray(actual, dtype=float)
    yhat = np.asarray(pred, dtype=float)
    mask = np.isfinite(y) & np.isfinite(yhat)
    y = y[mask]
    yhat = yhat[mask]
    if len(y) == 0:
        out = {
            "N": 0,
            "R2": np.nan,
            "MAE": np.nan,
            "RMSE": np.nan,
            "nRMSE_mean": np.nan,
            "Bias": np.nan,
            "MAPE_pct": np.nan,
        }
        return out

    err = yhat - y
    nonzero = np.abs(y) > 1e-12
    out = {
        "N": int(len(y)),
        "R2": float(r2_score(y, yhat)) if len(y) > 1 else np.nan,
        "MAE": float(mean_absolute_error(y, yhat)),
        "RMSE": rmse(y, yhat),
        "nRMSE_mean": float(rmse(y, yhat) / np.mean(np.abs(y))),
        "Bias": float(np.mean(err)),
        "MAPE_pct": float(np.mean(np.abs(err[nonzero] / y[nonzero])) * 100) if nonzero.any() else np.nan,
    }

    if weights is not None:
        w_full = np.asarray(weights, dtype=float)
        w = w_full[mask]
        w = np.where(np.isfinite(w) & (w > 0), w, 0.0)
        if w.sum() > 0:
            actual_province = float(np.sum(y * w) / w.sum())
            pred_province = float(np.sum(yhat * w) / w.sum())
            out["Province_Actual"] = actual_province
            out["Province_Pred"] = pred_province
            out["Province_Error"] = pred_province - actual_province
    return out


def scaled_metric_dict(metrics: Dict[str, float], scale: float) -> Dict[str, float]:
    """
    把原始模型单位的评价指标转换为最终结果单位。
    R2、N、MAPE 不随线性缩放改变；MAE、RMSE、Bias 和省级误差会乘以 FINAL_RESULT_SCALE。
    """
    """Scale metrics whose units follow the target variable; dimensionless metrics stay unchanged."""
    out = dict(metrics)
    for key in ["MAE", "RMSE", "Bias", "Province_Actual", "Province_Pred", "Province_Error"]:
        if key in out:
            value = out[key]
            out[key] = float(value) * scale if np.isfinite(value) else np.nan
    return out


def print_metrics(title: str, metrics: Dict[str, float]) -> None:
    """
    按固定顺序打印评价指标，保证训练集、目标榨季、原始单位和最终单位的格式一致。
    只打印 metrics 中实际存在的字段，因此可兼容有无面积权重两类结果。
    """
    print("\n" + "=" * 24 + f" {title} " + "=" * 24)
    for key in [
        "N",
        "R2",
        "MAE",
        "RMSE",
        "nRMSE_mean",
        "Bias",
        "MAPE_pct",
        "Province_Actual",
        "Province_Pred",
        "Province_Error",
    ]:
        if key not in metrics:
            continue
        value = metrics[key]
        if key == "N":
            print(f"{key:<18}: {int(value)}")
        else:
            print(f"{key:<18}: {value:.6f}" if np.isfinite(value) else f"{key:<18}: NaN")


def weighted_mean(values: pd.Series, weights: pd.Series) -> float:
    """
    计算带权均值，主要用于县级单产按甘蔗面积汇总到省级口径。
    权重小于等于 0 或缺失的记录会被排除；如果有效权重为空，则返回 NaN。
    """
    v = pd.to_numeric(values, errors="coerce")
    w = pd.to_numeric(weights, errors="coerce")
    w = w.where(w > 0)
    mask = v.notna() & w.notna()
    if not mask.any() or float(w[mask].sum()) <= 0:
        return np.nan
    return float((v[mask] * w[mask]).sum() / w[mask].sum())

# =============================================================================
# 3. 数据读取、样本构建和 Ridge 参数还原
# =============================================================================
def load_table(path: str, sheet_name: str) -> pd.DataFrame:
    """
    读取 Excel 建模表并完成最基础的字段检查和类型转换。
    这里不做建模特征工程，只保证后续函数拿到的日期、数值和县名字段格式可用。
    """
    print(f"正在读取建模表: {path}")
    df = pd.read_excel(path, sheet_name=sheet_name)
    df.columns = [str(c).strip() for c in df.columns]

    required = [COUNTY_COL, DATE_COL, TARGET_COL, AREA_COL, SEASON_COL, SEASON_MONTH_COL] + RAW_FEATURES
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"建模表缺少字段: {missing}")

    df[DATE_COL] = pd.to_datetime(df[DATE_COL])
    df[COUNTY_COL] = df[COUNTY_COL].astype(str).str.strip()
    for col in [TARGET_COL, AREA_COL, SEASON_COL, SEASON_MONTH_COL] + RAW_FEATURES:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 有些新增预测月只有 date 和遥感/气象变量，season_month 没有随合并表一起写入。
    # build_samples 会按 season_month 截断 Apr 到目标月；如果这里不补，2025-05 这类行会被全部过滤掉。
    valid_date = df[DATE_COL].notna()
    inferred_season = pd.Series(np.nan, index=df.index, dtype="float64")
    inferred_month = pd.Series(np.nan, index=df.index, dtype="float64")
    month = df.loc[valid_date, DATE_COL].dt.month
    year = df.loc[valid_date, DATE_COL].dt.year
    inferred_season.loc[valid_date] = np.where(month >= 4, year, year - 1)
    inferred_month.loc[valid_date] = np.where(month >= 4, month - 3, month + 9)

    missing_season = df[SEASON_COL].isna() & valid_date
    missing_month = df[SEASON_MONTH_COL].isna() & valid_date
    if missing_season.any():
        df.loc[missing_season, SEASON_COL] = inferred_season.loc[missing_season]
    if missing_month.any():
        df.loc[missing_month, SEASON_MONTH_COL] = inferred_month.loc[missing_month]
    if missing_season.any() or missing_month.any():
        print(
            f"已按 date 补齐缺失榨季字段: "
            f"{int(missing_season.sum())} 行 crop_year, {int(missing_month.sum())} 行 season_month。"
        )
    return df


def build_samples(df: pd.DataFrame, cutoff_idx: int) -> Tuple[pd.DataFrame, List[str]]:
    """
    把月度长表聚合为"县-榨季"级建模样本。
    cutoff_idx 控制只使用 Apr 到目标月的数据；函数会生成月度原值、Apr-to-target 统计量、累计降水等工程特征。
    """
    data = df[df[SEASON_MONTH_COL].between(1, cutoff_idx)].copy()
    keys = [SEASON_COL, COUNTY_COL]

    base = (
        data.groupby(keys, as_index=False)
        .agg(
            actual_yield=(TARGET_COL, "mean"),
            current_area=(AREA_COL, "mean"),
            observed_month_count=(SEASON_MONTH_COL, "nunique"),
        )
    )

    pieces = []
    for feature in RAW_FEATURES:
        pivot = data.pivot_table(index=keys, columns=SEASON_MONTH_COL, values=feature, aggfunc="mean")
        for month_idx in range(1, cutoff_idx + 1):
            if month_idx not in pivot.columns:
                pivot[month_idx] = np.nan
        pivot = pivot[list(range(1, cutoff_idx + 1))]
        pivot.columns = [f"{feature}_m{month_idx:02d}_{MONTH_LABELS[month_idx]}" for month_idx in range(1, cutoff_idx + 1)]
        pieces.append(pivot)

        agg = data.groupby(keys)[feature].agg(["mean", "std", "min", "max", "last", "first"])
        agg[f"{feature}_trend"] = agg["last"] - agg["first"]
        agg = agg.drop(columns=["first"])
        agg.columns = [f"{feature}_{stat}_Apr_to_{MONTH_LABELS[cutoff_idx]}" for stat in agg.columns]
        pieces.append(agg)

    sums = data.groupby(keys)[SUM_FEATURES].sum(min_count=1)
    sums.columns = [f"{col}_sum_Apr_to_{MONTH_LABELS[cutoff_idx]}" for col in sums.columns]
    pieces.append(sums)

    features = pd.concat(pieces, axis=1).reset_index()
    samples = base.merge(features, on=keys, how="left")
    feature_cols = [c for c in samples.columns if c not in [SEASON_COL, COUNTY_COL, "actual_yield", "current_area"]]
    return samples, feature_cols


def add_history_features(train_rows: pd.DataFrame, rows: pd.DataFrame, exclude_own_year: bool) -> pd.DataFrame:
    """
    为每个县-榨季样本补充县级历史单产和历史面积特征。
    exclude_own_year=True 用于训练集内部，避免某行直接使用本榨季 Yield；目标预测时是否包含目标年由训练集年份和运行口径决定。
    """
    history = train_rows.dropna(subset=["actual_yield"]).copy()
    global_yield = float(history["actual_yield"].mean())
    out = rows.copy()

    hist_mean = []
    hist_last = []
    hist_count = []
    hist_area_mean = []
    hist_area_last = []
    hist_last_minus_mean = []

    for _, row in out.iterrows():
        county_hist = history[history[COUNTY_COL] == row[COUNTY_COL]].sort_values(SEASON_COL)
        if exclude_own_year:
            county_hist = county_hist[county_hist[SEASON_COL] != row[SEASON_COL]]

        y = county_hist["actual_yield"].dropna()
        area = county_hist["current_area"].replace(0, np.nan).dropna()
        county_mean = float(y.mean()) if len(y) else global_yield
        county_last = float(y.iloc[-1]) if len(y) else global_yield
        area_mean = float(area.mean()) if len(area) else 0.0
        area_last = float(area.iloc[-1]) if len(area) else 0.0

        hist_mean.append(county_mean)
        hist_last.append(county_last)
        hist_count.append(int(len(y)))
        hist_area_mean.append(area_mean)
        hist_area_last.append(area_last)
        hist_last_minus_mean.append(county_last - county_mean if len(y) else 0.0)

    out["county_yield_hist_mean"] = hist_mean
    out["county_yield_hist_last"] = hist_last
    out["county_yield_hist_count"] = hist_count
    out["county_area_hist_mean"] = hist_area_mean
    out["county_area_hist_last"] = hist_area_last
    out["county_yield_last_minus_mean"] = hist_last_minus_mean
    out["area_for_weight"] = out["current_area"].where(out["current_area"].fillna(0) > 0, out["county_area_hist_last"])
    out["area_for_weight"] = out["area_for_weight"].where(out["area_for_weight"].fillna(0) > 0, 0.0)
    return out


def make_ridge_model():
    """
    创建 RidgeCV 建模流水线。
    SimpleImputer 负责缺失值填充，StandardScaler 负责标准化，RidgeCV 在多个 alpha 中选择正则强度。
    """
    return make_pipeline(
        SimpleImputer(strategy="median"),
        StandardScaler(),
        RidgeCV(alphas=np.logspace(-2, 5, 80)),
    )


def extract_parameter_table(model, feature_cols: List[str]) -> Tuple[float, pd.DataFrame]:
    """
    把标准化 Ridge 模型还原为可在 GEE 中直接使用的原始量纲线性参数。
    返回的 gee_raw_coef 和 gee_intercept 对应公式：Yield = intercept + sum(raw_feature * raw_coef)。
    """
    imputer: SimpleImputer = model.named_steps["simpleimputer"]
    scaler: StandardScaler = model.named_steps["standardscaler"]
    ridge: RidgeCV = model.named_steps["ridgecv"]

    fill_values = imputer.statistics_.astype(float)
    scaler_mean = scaler.mean_.astype(float)
    scaler_scale = scaler.scale_.astype(float)
    beta_scaled = ridge.coef_.astype(float)

    gee_raw_coef = beta_scaled / scaler_scale
    gee_intercept = float(ridge.intercept_ - np.sum(beta_scaled * scaler_mean / scaler_scale))

    params = pd.DataFrame(
        {
            "feature": feature_cols,
            "fill_value_if_null": fill_values,
            "standardize_mean": scaler_mean,
            "standardize_std": scaler_scale,
            "coef_on_standardized_feature": beta_scaled,
            "gee_raw_coef": gee_raw_coef,
        }
    )
    params["abs_gee_raw_coef"] = params["gee_raw_coef"].abs()
    return gee_intercept, params


def filled_feature_frame(rows: pd.DataFrame, params: pd.DataFrame, feature_cols: List[str]) -> pd.DataFrame:
    """
    按训练期 imputer 的填充值补齐特征矩阵。
    这个函数用于确保 Python 原始公式、折叠公式和 GEE unmask 逻辑使用同一套缺失值处理规则。
    """
    fill_map = params.set_index("feature")["fill_value_if_null"].to_dict()
    out = rows[feature_cols].copy()
    for col in feature_cols:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(float(fill_map[col]))
    return out


def raw_formula_predict(rows: pd.DataFrame, raw_intercept: float, params: pd.DataFrame, feature_cols: List[str]) -> np.ndarray:
    """
    用还原到原始量纲的线性公式重新计算预测值。
    它用于核对 extract_parameter_table 没有改变 RidgeCV pipeline 的预测含义。
    """
    filled = filled_feature_frame(rows, params, feature_cols)
    beta = params.set_index("feature").loc[feature_cols, "gee_raw_coef"].to_numpy(dtype=float)
    return raw_intercept + filled[feature_cols].to_numpy(dtype=float).dot(beta)

# =============================================================================
# 4. 县级历史项折叠与 GEE 像素公式生成
# =============================================================================
def fold_county_model_to_pixel_formula(
    target_rows: pd.DataFrame,
    params: pd.DataFrame,
    raw_intercept: float,
    feature_cols: List[str],
    cutoff_idx: int,
) -> FoldedFormula:
    """
    把县级 Ridge 公式拆成 GEE 像素可计算部分和不可逐像素计算部分。
    县级历史项无法在像素影像上直接计算，因此按目标区面积权重求一个代表值并折叠进截距；NDVI、温度、降水等动态项保留给 GEE 逐像素代入。
    """
    fold_cols = [c for c in HISTORY_COLS if c in feature_cols]
    dynamic_cols = [c for c in feature_cols if c not in fold_cols]
    filled = filled_feature_frame(target_rows, params, feature_cols)
    weights = pd.to_numeric(target_rows["area_for_weight"], errors="coerce").fillna(0.0)
    weights = weights.where(weights > 0, 0.0)

    param_by_feature = params.set_index("feature")
    fold_records = []
    folded_intercept = float(raw_intercept)
    for col in fold_cols:
        folded_value = weighted_mean(filled[col], weights)
        if not np.isfinite(folded_value):
            folded_value = float(param_by_feature.loc[col, "fill_value_if_null"])
        coef = float(param_by_feature.loc[col, "gee_raw_coef"])
        contribution = folded_value * coef
        folded_intercept += contribution
        fold_records.append(
            {
                "folded_feature": col,
                "folded_value": folded_value,
                "gee_raw_coef": coef,
                "intercept_contribution": contribution,
            }
        )

    dynamic_records = []
    province_pred = folded_intercept
    for col in dynamic_cols:
        value = weighted_mean(filled[col], weights)
        if not np.isfinite(value):
            value = float(param_by_feature.loc[col, "fill_value_if_null"])
        coef = float(param_by_feature.loc[col, "gee_raw_coef"])
        province_pred += value * coef
        dynamic_records.append(
            {
                "feature": col,
                "target_area_weighted_feature_mean": value,
                "gee_raw_coef": coef,
                "province_contribution": value * coef,
            }
        )

    dynamic_params = params[params["feature"].isin(dynamic_cols)].copy()
    dynamic_params = dynamic_params.sort_values("abs_gee_raw_coef", ascending=False).drop(columns=["abs_gee_raw_coef"])
    return FoldedFormula(
        raw_intercept=raw_intercept,
        folded_intercept=float(folded_intercept),
        fold_table=pd.DataFrame(fold_records),
        dynamic_params=dynamic_params,
        target_dynamic_means=pd.DataFrame(dynamic_records).sort_values("feature").reset_index(drop=True),
        province_prediction_from_folded_formula=float(province_pred),
    )


def gee_feature_name(feature: str) -> str:
    """
    把 Python 特征名转换为 GEE band 名。
    统一加 b_ 前缀并替换特殊字符，可以减少 GEE 端 select/rename 时的命名不一致。
    """
    return "b_" + "".join(ch if ch.isalnum() else "_" for ch in feature)


def make_gee_pixel_formula_snippet(
    folded: FoldedFormula,
    target_dt: pd.Timestamp,
    cutoff_idx: int,
    args: argparse.Namespace,
) -> str:
    """
    根据折叠参数生成可复制到 GEE Code Editor 的 JavaScript 代码。
    GEE 端先生成 rawYieldImage，再统一乘 FINAL_RESULT_SCALE 得到 Yield_Final 并导出，保证 Python 和 GEE 缩放一致。
    """
    params = folded.dynamic_params.sort_values("feature").copy()
    param_lines = []
    image_feature_names = []
    for _, row in params.iterrows():
        name = str(row["feature"])
        band = gee_feature_name(name)
        image_feature_names.append((name, band))
        param_lines.append(
            "  {name: '"
            + name
            + "', band: '"
            + band
            + f"', fill: {float(row['fill_value_if_null']):.12g}, coef: {float(row['gee_raw_coef']):.12g}}},",
        )

    final_scale = float(getattr(args, "final_result_scale", FINAL_RESULT_SCALE))
    map_min = 50.0 * final_scale
    map_max = 110.0 * final_scale

    return "\n".join(
        [
            "// =====================================================================",
            "// 广西甘蔗单产实时估产：县级模型折叠后的像素级 GEE 公式",
            "// 输出 tif 不是全省常数：每个甘蔗像素按自己的 NDVI、temperature_2m、total_precipitation_sum 时序计算。",
            "// 使用方式：替换资产 ID 后，在 GEE Code Editor 中运行。",
            "// FINAL_RESULT_SCALE 定义最终结果系数：最终结果 = 原始模型结果 * FINAL_RESULT_SCALE。",
            "// =====================================================================",
            "",
            f"var FINAL_RESULT_SCALE = ee.Number({final_scale:.12g});",
            "",
            "var CONFIG = {",
            f"  provinceAsset: '{args.gee_province_asset}',",
            f"  sugarcaneAsset: '{args.gee_sugarcane_asset}',",
            f"  targetYear: {target_dt.year},",
            f"  targetMonth: {target_dt.month},",
            "  s2CloudPct: 60,",
            "  exportScale: 30,",
            "  tileScale: 4,",
            f"  exportFolder: '{args.gee_export_folder}',",
            f"  exportPrefix: '{args.gee_export_prefix}'",
            "};",
            "",
            "var MONTH_LABELS = ['Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec', 'Jan', 'Feb', 'Mar'];",
            f"var CUTOFF_INDEX = {cutoff_idx};",
            f"var FOLDED_INTERCEPT = ee.Number({folded.folded_intercept:.12g});",
            "var DYNAMIC_PARAMS = [",
            *param_lines,
            "];",
            "",
            "var province = ee.FeatureCollection(CONFIG.provinceAsset);",
            "var region = province.geometry();",
            "var caneMask = ee.Image(CONFIG.sugarcaneAsset).gt(0).selfMask().clip(region);",
            "",
            "function twoDigit(i) {",
            "  return (i < 10 ? '0' : '') + String(i);",
            "}",
            "",
            "function seasonStartDate(targetYear, targetMonth) {",
            "  var seasonYear = ee.Number(ee.Algorithms.If(ee.Number(targetMonth).gte(4), targetYear, ee.Number(targetYear).subtract(1)));",
            "  return ee.Date.fromYMD(seasonYear, 4, 1);",
            "}",
            "",
            "function maskS2Clouds(img) {",
            "  var scl = img.select('SCL');",
            "  var good = scl.neq(3).and(scl.neq(8)).and(scl.neq(9)).and(scl.neq(10)).and(scl.neq(11));",
            "  return img.updateMask(good);",
            "}",
            "",
            "function emptyMaskedBand(name) {",
            "  return ee.Image.constant(0).updateMask(ee.Image.constant(0)).rename(name);",
            "}",
            "",
            "function stdBand(collection, sourceBand, outName) {",
            "  var selected = collection.select(sourceBand);",
            "  var count = selected.count();",
            "  var mean = selected.mean();",
            "  var sumSq = selected.map(function(img) {",
            "    return img.subtract(mean).pow(2);",
            "  }).sum();",
            "  return sumSq.divide(count.subtract(1)).sqrt().updateMask(count.gt(1)).rename(outName);",
            "}",
            "",
            "function monthlyImage(monthIndex) {",
            "  monthIndex = ee.Number(monthIndex);",
            "  var start = seasonStartDate(CONFIG.targetYear, CONFIG.targetMonth).advance(monthIndex.subtract(1), 'month');",
            "  var end = start.advance(1, 'month');",
            "  var label = ee.String(ee.List(MONTH_LABELS).get(monthIndex.subtract(1)));",
            "",
            "  var s2Col = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')",
            "    .filterBounds(region)",
            "    .filterDate(start, end)",
            "    .filter(ee.Filter.lte('CLOUDY_PIXEL_PERCENTAGE', CONFIG.s2CloudPct))",
            "    .map(maskS2Clouds)",
            "    .map(function(img) {",
            "      return img.normalizedDifference(['B8', 'B4']).rename('NDVI').copyProperties(img, ['system:time_start']);",
            "    });",
            "  var s2 = ee.Image(ee.Algorithms.If(",
            "    s2Col.size().gt(0),",
            "    s2Col.mean().rename('NDVI'),",
            "    emptyMaskedBand('NDVI')",
            "  ));",
            "",
            "  var era = ee.ImageCollection('ECMWF/ERA5_LAND/MONTHLY_AGGR')",
            "    .filterDate(start, end)",
            "    .select(['temperature_2m', 'total_precipitation_sum'])",
            "    .mean();",
            "",
            "  // 与训练表保持一致：temperature_2m 使用 ERA5-Land 原始 K，不转摄氏度。",
            "  var tempK = era.select('temperature_2m').rename('temperature_2m');",
            "  var precip = era.select('total_precipitation_sum').rename('total_precipitation_sum');",
            "  return s2.addBands(tempK).addBands(precip).updateMask(caneMask).clip(region)",
            "    .set('monthIndex', monthIndex)",
            "    .set('monthLabel', label);",
            "}",
            "",
            "function buildPixelFeatureImage() {",
            "  var monthList = ee.List.sequence(1, CUTOFF_INDEX);",
            "  var monthly = ee.ImageCollection(monthList.map(monthlyImage));",
            "  var firstImg = ee.Image(monthly.filter(ee.Filter.eq('monthIndex', 1)).first());",
            "  var lastImg = ee.Image(monthly.filter(ee.Filter.eq('monthIndex', CUTOFF_INDEX)).first());",
            "",
            "  var out = ee.Image([]);",
            "  print('Using season month indexes', monthList);",
            "",
            "  // 月度原值特征。",
            "  for (var i = 1; i <= CUTOFF_INDEX; i++) {",
            "    var label = MONTH_LABELS[i - 1];",
            "    var img = ee.Image(monthly.filter(ee.Filter.eq('monthIndex', i)).first());",
            "    out = out.addBands(img.select('NDVI').rename('b_NDVI_m' + twoDigit(i) + '_' + label));",
            "    out = out.addBands(img.select('temperature_2m').rename('b_temperature_2m_m' + twoDigit(i) + '_' + label));",
            "    out = out.addBands(img.select('total_precipitation_sum').rename('b_total_precipitation_sum_m' + twoDigit(i) + '_' + label));",
            "  }",
            "",
            "  // Apr 到目标月的像素级统计量。",
            "  var ndvi = monthly.select('NDVI');",
            "  var temp = monthly.select('temperature_2m');",
            "  var prec = monthly.select('total_precipitation_sum');",
            "  var cutoffLabel = MONTH_LABELS[CUTOFF_INDEX - 1];",
            "  out = out.addBands(monthly.select('NDVI').count().rename('b_observed_month_count'));",
            "",
            "  out = out.addBands(ndvi.mean().rename('b_NDVI_mean_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(stdBand(monthly, 'NDVI', 'b_NDVI_std_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(ndvi.min().rename('b_NDVI_min_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(ndvi.max().rename('b_NDVI_max_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(lastImg.select('NDVI').rename('b_NDVI_last_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(lastImg.select('NDVI').subtract(firstImg.select('NDVI')).rename('b_NDVI_NDVI_trend_Apr_to_' + cutoffLabel));",
            "",
            "  out = out.addBands(temp.mean().rename('b_temperature_2m_mean_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(stdBand(monthly, 'temperature_2m', 'b_temperature_2m_std_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(temp.min().rename('b_temperature_2m_min_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(temp.max().rename('b_temperature_2m_max_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(lastImg.select('temperature_2m').rename('b_temperature_2m_last_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(lastImg.select('temperature_2m').subtract(firstImg.select('temperature_2m')).rename('b_temperature_2m_temperature_2m_trend_Apr_to_' + cutoffLabel));",
            "",
            "  out = out.addBands(prec.mean().rename('b_total_precipitation_sum_mean_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(stdBand(monthly, 'total_precipitation_sum', 'b_total_precipitation_sum_std_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(prec.min().rename('b_total_precipitation_sum_min_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(prec.max().rename('b_total_precipitation_sum_max_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(lastImg.select('total_precipitation_sum').rename('b_total_precipitation_sum_last_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(lastImg.select('total_precipitation_sum').subtract(firstImg.select('total_precipitation_sum')).rename('b_total_precipitation_sum_total_precipitation_sum_trend_Apr_to_' + cutoffLabel));",
            "  out = out.addBands(prec.sum().rename('b_total_precipitation_sum_sum_Apr_to_' + cutoffLabel));",
            "",
            "  return out.updateMask(caneMask).clip(region);",
            "}",
            "",
            "function fillBand(featureImage, p) {",
            "  var band = featureImage.select(p.band);",
            "  return band.unmask(ee.Number(p.fill)).rename(p.band);",
            "}",
            "",
            "function predictPixelYield(featureImage) {",
            "  var y = ee.Image.constant(FOLDED_INTERCEPT).rename('Yield_Predicted');",
            "  DYNAMIC_PARAMS.forEach(function(p) {",
            "    y = y.add(fillBand(featureImage, p).multiply(ee.Number(p.coef)));",
            "  });",
            "  return y.updateMask(caneMask).clip(region);",
            "}",
            "",
            "var featureImage = buildPixelFeatureImage();",
            "var rawYieldImage = predictPixelYield(featureImage).rename('Yield_Raw');",
            "var yieldImage = rawYieldImage.multiply(FINAL_RESULT_SCALE).rename('Yield_Final');",
            "",
            "// 原始模型结果：不乘系数，仅用于与 Python 输出核对。",
            "var rawProvinceYield = rawYieldImage.reduceRegion({",
            "  reducer: ee.Reducer.mean(),",
            "  geometry: region,",
            "  scale: CONFIG.exportScale,",
            "  maxPixels: 1e13,",
            "  tileScale: CONFIG.tileScale",
            "});",
            "print('Raw province yield from pixel image', rawProvinceYield);",
            "",
            "// 最终结果：由像素级原始结果乘以 FINAL_RESULT_SCALE 后求均值。",
            "var provinceYield = yieldImage.reduceRegion({",
            "  reducer: ee.Reducer.mean(),",
            "  geometry: region,",
            "  scale: CONFIG.exportScale,",
            "  maxPixels: 1e13,",
            "  tileScale: CONFIG.tileScale",
            "});",
            "print('Final province yield from pixel image', provinceYield);",
            "",
            "// 检查输出是否有像素差异：stdDev > 0 时说明不是常数 tif。",
            "var pixelVariationCheck = yieldImage.reduceRegion({",
            "  reducer: ee.Reducer.minMax().combine({reducer2: ee.Reducer.stdDev(), sharedInputs: true}),",
            "  geometry: region,",
            "  scale: CONFIG.exportScale,",
            "  maxPixels: 1e13,",
            "  tileScale: CONFIG.tileScale",
            "});",
            "print('Pixel variation check', pixelVariationCheck);",
            "",
            "Map.centerObject(region, 7);",
            f"Map.addLayer(yieldImage, {{min: {map_min:.6g}, max: {map_max:.6g}, palette: ['#b2182b', '#fddbc7', '#d1e5f0', '#2166ac']}}, 'Final pixel yield');",
            "",
            "Export.image.toDrive({",
            "  image: yieldImage.float(),",
            "  description: CONFIG.exportPrefix + '_' + CONFIG.targetYear + '_' + twoDigit(CONFIG.targetMonth),",
            "  folder: CONFIG.exportFolder,",
            "  fileNamePrefix: CONFIG.exportPrefix + '_' + CONFIG.targetYear + '_' + twoDigit(CONFIG.targetMonth),",
            "  region: region,",
            "  scale: CONFIG.exportScale,",
            "  maxPixels: 1e13",
            "});",
            "",
            "// 动态参数表，便于核对 Python 输出。",
            "print('Final result scale', FINAL_RESULT_SCALE);",
            "print('Folded intercept before scale', FOLDED_INTERCEPT);",
            "print('Dynamic params', DYNAMIC_PARAMS);",
        ]
    )

# =============================================================================
# 5. 建模流程和报告打印
# =============================================================================
def run_folded_formula_workflow(args: argparse.Namespace) -> Dict[str, object]:
    """
    按当前配置执行完整建模、参数折叠、最终结果缩放和 GEE 代码生成流程。

    关键口径说明：
    1. 当 allow_target_in_training=False 时，函数会禁止目标榨季进入训练集，这是严格外推或独立验证口径。
    2. 当 allow_target_in_training=True 时，函数允许目标榨季进入训练集，只适合生成同表参数和检查折叠公式；此时目标榨季精度不能解释为外推精度。
    3. FINAL_RESULT_SCALE 不参与模型训练，只在模型输出后统一乘到最终结果上；R2 和 MAPE 不变，MAE/RMSE/Bias 等量纲指标会同步缩放。
    """
    target_dt = pd.to_datetime(args.target_month).replace(day=1)
    target_season = season_year(target_dt)
    cutoff_idx = season_month_index(target_dt)
    result_scale = float(getattr(args, "final_result_scale", FINAL_RESULT_SCALE))

    if args.train_start_year > args.train_end_year:
        raise ValueError("训练起始年份不能大于训练结束年份。")

    target_in_training = args.train_start_year <= target_season <= args.train_end_year
    if target_in_training and not args.allow_target_in_training:
        raise ValueError(
            f"训练年份 {args.train_start_year}-{args.train_end_year} 包含目标榨季 {target_season}，"
            "这会造成验证/预测泄露。若只是为了生成同表参数，请显式设置 ALLOW_TARGET_IN_TRAINING=True。"
        )
    if target_in_training:
        print("注意：本次允许目标榨季进入训练集，仅用于获取参数表和 GEE 代码；目标榨季精度存在训练/验证重叠，不能作为外推精度。")

    df = load_table(args.input_file, args.sheet)
    samples, base_feature_cols = build_samples(df, cutoff_idx)

    train = samples[
        samples[SEASON_COL].between(args.train_start_year, args.train_end_year)
        & samples["actual_yield"].notna()
    ].copy()
    target = samples[samples[SEASON_COL] == target_season].copy()
    if train.empty:
        raise ValueError(f"训练年份 {args.train_start_year}-{args.train_end_year} 没有非空 Yield 样本。")
    if target.empty:
        raise ValueError(f"目标榨季 {target_season} 没有截至 {args.target_month} 的输入数据。")

    train = add_history_features(train, train, exclude_own_year=True)
    target = add_history_features(train, target, exclude_own_year=False)
    feature_cols = base_feature_cols + HISTORY_COLS

    model = make_ridge_model()
    model.fit(train[feature_cols], train["actual_yield"])
    raw_intercept, params = extract_parameter_table(model, feature_cols)

    train_pred = model.predict(train[feature_cols])
    target_pred = model.predict(target[feature_cols])
    target = target.copy()
    target["predicted_yield"] = target_pred
    target["raw_formula_pred"] = raw_formula_predict(target, raw_intercept, params, feature_cols)
    target["error"] = target["predicted_yield"] - target["actual_yield"]
    target["final_actual_yield"] = target["actual_yield"] * result_scale
    target["final_predicted_yield"] = target["predicted_yield"] * result_scale
    target["final_raw_formula_pred"] = target["raw_formula_pred"] * result_scale
    target["final_error"] = target["error"] * result_scale

    train_metrics = metric_dict(train["actual_yield"], train_pred, train["area_for_weight"])
    target_metrics = metric_dict(target["actual_yield"], target["predicted_yield"], target["area_for_weight"])
    train_metrics_final = scaled_metric_dict(train_metrics, result_scale)
    target_metrics_final = scaled_metric_dict(target_metrics, result_scale)

    full_rows = target[target["area_for_weight"].fillna(0) > 0].copy()
    if full_rows.empty:
        full_rows = target.copy()
    eval_rows = target[
        target["actual_yield"].notna()
        & target["predicted_yield"].notna()
        & (target["area_for_weight"].fillna(0) > 0)
    ].copy()
    if eval_rows.empty:
        eval_rows = full_rows.copy()

    folded_full = fold_county_model_to_pixel_formula(full_rows, params, raw_intercept, feature_cols, cutoff_idx)
    folded_eval = fold_county_model_to_pixel_formula(eval_rows, params, raw_intercept, feature_cols, cutoff_idx)

    province_actual = weighted_mean(eval_rows["actual_yield"], eval_rows["area_for_weight"]) if eval_rows["actual_yield"].notna().any() else np.nan
    province_county_pred_eval = weighted_mean(eval_rows["predicted_yield"], eval_rows["area_for_weight"])
    province_county_pred_full = weighted_mean(full_rows["predicted_yield"], full_rows["area_for_weight"])

    full_param_table = params.sort_values("abs_gee_raw_coef", ascending=False).drop(columns=["abs_gee_raw_coef"])
    gee_code = make_gee_pixel_formula_snippet(folded_full, target_dt, cutoff_idx, args)

    return {
        "model": model,
        "target_dt": target_dt,
        "target_season": target_season,
        "cutoff_idx": cutoff_idx,
        "result_scale": result_scale,
        "base_feature_cols": base_feature_cols,
        "feature_cols": feature_cols,
        "raw_intercept": raw_intercept,
        "params": params,
        "full_param_table": full_param_table,
        "folded_full": folded_full,
        "folded_eval": folded_eval,
        "train": train,
        "target": target,
        "train_metrics": train_metrics,
        "target_metrics": target_metrics,
        "train_metrics_final": train_metrics_final,
        "target_metrics_final": target_metrics_final,
        "province_actual": province_actual,
        "province_county_pred_eval": province_county_pred_eval,
        "province_county_pred_full": province_county_pred_full,
        "province_county_pred_eval_final": province_county_pred_eval * result_scale,
        "province_county_pred_full_final": province_county_pred_full * result_scale,
        "folded_formula_eval_final": folded_eval.province_prediction_from_folded_formula * result_scale,
        "folded_formula_full_final": folded_full.province_prediction_from_folded_formula * result_scale,
        "gee_code": gee_code,
        "args": args,
    }


def print_workflow_report(result: Dict[str, object]) -> None:
    """
    打印运行设置、原始/最终精度指标、参数表、折叠核对结果和 GEE 代码。

    这个函数只负责展示，不重新训练模型；因此如果只想修改打印格式，不会影响前面已经得到的模型和参数。
    """
    args = result["args"]
    target_dt = result["target_dt"]
    target_season = result["target_season"]
    cutoff_idx = result["cutoff_idx"]
    result_scale = result["result_scale"]
    model = result["model"]
    train = result["train"]
    target = result["target"]
    folded_full = result["folded_full"]
    folded_eval = result["folded_eval"]
    raw_intercept = result["raw_intercept"]
    full_param_table = result["full_param_table"]

    print("\n" + "=" * 24 + " 运行设置 " + "=" * 24)
    print(f"目标月份: {target_dt.strftime('%Y-%m')}")
    print(f"目标榨季: {target_season}")
    print(f"使用月份: Apr 到 {MONTH_LABELS[cutoff_idx]}")
    print(f"训练年份: {args.train_start_year}-{args.train_end_year}")
    print(f"允许目标榨季入训: {args.allow_target_in_training}")
    print(f"最终结果系数 FINAL_RESULT_SCALE: {result_scale:.12g}")
    print(f"GEE 导出前缀: {args.gee_export_prefix}")
    print(f"训练样本数: {len(train)}")
    print(f"目标样本数: {len(target)}")
    print(f"完整特征数: {len(result['feature_cols'])}")
    print(f"GEE 动态特征数: {len(folded_full.dynamic_params)}")
    print(f"折叠进截距的历史特征数: {len(folded_full.fold_table)}")
    print(f"Ridge alpha: {float(model.named_steps['ridgecv'].alpha_):.8f}")
    print("最终结果 = 原始模型结果 * FINAL_RESULT_SCALE")

    print_metrics("训练集拟合精度（原始模型单位）", result["train_metrics"])
    print_metrics("训练集拟合精度（最终结果单位）", result["train_metrics_final"])
    print_metrics("目标榨季同表预测精度（原始模型单位）", result["target_metrics"])
    print_metrics("目标榨季同表预测精度（最终结果单位）", result["target_metrics_final"])

    print("\n" + "=" * 24 + " 省级折叠核对 " + "=" * 24)
    province_actual = result["province_actual"]
    print(f"省级实际单产: {province_actual:.6f}" if np.isfinite(province_actual) else "省级实际单产: NaN")
    print(f"目标榨季县级预测面积加权: {result['province_county_pred_eval']:.6f}")
    print(f"折叠公式同口径还原省级: {folded_eval.province_prediction_from_folded_formula:.6f}")
    print(f"折叠还原差值: {folded_eval.province_prediction_from_folded_formula - result['province_county_pred_eval']:.12f}")
    print("\n最终结果 = 上述原始结果 * FINAL_RESULT_SCALE")
    print(f"最终省级实际结果: {province_actual * result_scale:.6f}" if np.isfinite(province_actual) else "最终省级实际结果: NaN")
    print(f"最终目标榨季县级预测面积加权: {result['province_county_pred_eval_final']:.6f}")
    print(f"最终折叠公式同口径还原省级: {result['folded_formula_eval_final']:.6f}")
    print(f"最终折叠还原差值: {(folded_eval.province_prediction_from_folded_formula - result['province_county_pred_eval']) * result_scale:.12f}")
    print(f"全目标区县级预测面积加权: {result['province_county_pred_full']:.6f}")
    print(f"全目标区折叠公式省级值: {folded_full.province_prediction_from_folded_formula:.6f}")
    print(f"最终全目标区县级预测面积加权: {result['province_county_pred_full_final']:.6f}")
    print(f"最终全目标区折叠公式省级值: {result['folded_formula_full_final']:.6f}")

    print("\n" + "=" * 24 + " 完整 Ridge 原始参数表 " + "=" * 24)
    print(f"raw_gee_intercept = {raw_intercept:.12f}")
    with pd.option_context("display.max_rows", None, "display.max_columns", None, "display.width", 260):
        print(
            full_param_table[
                [
                    "feature",
                    "fill_value_if_null",
                    "standardize_mean",
                    "standardize_std",
                    "coef_on_standardized_feature",
                    "gee_raw_coef",
                ]
            ].to_string(index=False)
        )

    print("\n" + "=" * 24 + " 折叠截距表 " + "=" * 24)
    print(f"folded_intercept = {folded_full.folded_intercept:.12f}")
    print(f"final_equivalent_folded_intercept = {folded_full.folded_intercept * result_scale:.12f}")
    print("原始像素公式: Raw_pixel = folded_intercept + sum(gee_raw_coef * filled_pixel_feature)")
    print("最终像素公式: Final_pixel = Raw_pixel * FINAL_RESULT_SCALE")
    with pd.option_context("display.max_rows", None, "display.max_columns", None, "display.width", 220):
        print(folded_full.fold_table.to_string(index=False))

    print("\n" + "=" * 24 + " GEE 像素级动态参数表 " + "=" * 24)
    with pd.option_context("display.max_rows", None, "display.max_columns", None, "display.width", 260):
        print(
            folded_full.dynamic_params[
                [
                    "feature",
                    "fill_value_if_null",
                    "standardize_mean",
                    "standardize_std",
                    "coef_on_standardized_feature",
                    "gee_raw_coef",
                ]
            ].to_string(index=False)
        )

    print("\n" + "=" * 24 + " GEE 像素差异 tif 代码 " + "=" * 24)
    print(
        textwrap.dedent(
            """
            说明:
            1. 下方代码不是常数 tif，而是把折叠后的线性公式逐像素应用到三特征时序影像。
            2. rawYieldImage 是原始模型结果，yieldImage = rawYieldImage * FINAL_RESULT_SCALE，是最终导出的结果。
            3. pixelVariationCheck 会打印 min/max/stdDev，用来确认输出 tif 有像素差异。
            """
        ).strip()
    )
    print(result["gee_code"])


def print_province_fold_check(result: Dict[str, object]) -> None:
    """只打印省级面积加权预测与折叠公式还原结果，供本地 TIF 运行时快速核对。"""
    folded_full = result["folded_full"]
    folded_eval = result["folded_eval"]
    province_actual = result["province_actual"]
    result_scale = float(result["result_scale"])

    print("\n" + "=" * 24 + " 省级折叠核对 " + "=" * 24)
    print(f"省级实际单产: {province_actual:.6f}" if np.isfinite(province_actual) else "省级实际单产: NaN")
    print(f"目标榨季县级预测面积加权: {result['province_county_pred_eval']:.6f}")
    print(f"折叠公式同口径还原省级: {folded_eval.province_prediction_from_folded_formula:.6f}")
    print(f"折叠还原差值: {folded_eval.province_prediction_from_folded_formula - result['province_county_pred_eval']:.12f}")
    print("\n最终结果 = 上述原始结果 * FINAL_RESULT_SCALE")
    print(f"最终省级实际结果: {province_actual * result_scale:.6f}" if np.isfinite(province_actual) else "最终省级实际结果: NaN")
    print(f"最终目标榨季县级预测面积加权: {result['province_county_pred_eval_final']:.6f}")
    print(f"最终折叠公式同口径还原省级: {result['folded_formula_eval_final']:.6f}")
    print(f"最终折叠还原差值: {(folded_eval.province_prediction_from_folded_formula - result['province_county_pred_eval']) * result_scale:.12f}")
    print(f"全目标区县级预测面积加权: {result['province_county_pred_full']:.6f}")
    print(f"全目标区折叠公式省级值: {folded_full.province_prediction_from_folded_formula:.6f}")
    print(f"最终全目标区县级预测面积加权: {result['province_county_pred_full_final']:.6f}")
    print(f"最终全目标区折叠公式省级值: {result['folded_formula_full_final']:.6f}")


# =============================================================================
# 6. 本地 GeoTIFF 输入输出配置
# =============================================================================

WORKSPACE = Path(__file__).resolve().parent

# 默认甘蔗/目标区掩膜 tif：与本脚本放在同一个文件夹。
# 分块预测写出前会用该 tif 做最终掩膜，只有 >0 的像素会保留。
CANE_MASK_TIF: Path | None = WORKSPACE / "classification_April_1.tif"

INPUT_RASTER_DIR = WORKSPACE / "本地影像输入"
OUTPUT_TIF = WORKSPACE / "本地持续估产_2026_06_final.tif"
RAW_OUTPUT_TIF = WORKSPACE / "本地持续估产_2026_06_raw.tif"
NODATA_VALUE = -9999.0
RASTER_NAME_TEMPLATE = "{year}_{month:02d}_{feature}.tif"


@dataclass(frozen=True)
class RasterBundle:
    """保存一组同网格月度影像和参考 profile。"""

    arrays: Dict[Tuple[int, str], np.ndarray]
    valid_masks: Dict[Tuple[int, str], np.ndarray]
    profile: dict


def month_sequence_for_target(target_month: str) -> List[Tuple[int, int, int, str]]:
    """返回目标月份对应的 Apr 到目标月自然年月序列。"""
    target_dt = pd.to_datetime(target_month).replace(day=1)
    target_season = season_year(target_dt)
    cutoff_idx = season_month_index(target_dt)
    out = []
    for idx in range(1, cutoff_idx + 1):
        natural_month = idx + 3 if idx <= 9 else idx - 9
        natural_year = target_season if natural_month >= 4 else target_season + 1
        out.append((idx, natural_year, natural_month, MONTH_LABELS[idx]))
    return out


def build_local_model_result(args: argparse.Namespace) -> Dict[str, object]:
    """直接调用本文件内的训练流程，得到折叠线性参数。"""
    model_args = argparse.Namespace(
        input_file=args.input_file,
        sheet=args.sheet,
        target_month=args.target_month,
        train_start_year=args.train_start_year,
        train_end_year=args.train_end_year,
        allow_target_in_training=args.allow_target_in_training,
        preview_rows=args.preview_rows,
        gee_sugarcane_asset=args.gee_sugarcane_asset,
        gee_province_asset=args.gee_province_asset,
        gee_export_folder=args.gee_export_folder,
        gee_export_prefix=args.gee_export_prefix,
        final_result_scale=args.final_result_scale,
    )
    return run_folded_formula_workflow(model_args)


# =============================================================================
# 7. 本地影像读取和网格检查
# =============================================================================

def raster_path(input_dir: Path, year: int, month: int, feature: str, template: str) -> Path:
    """按命名模板得到某月某个变量的 tif 路径。"""
    return input_dir / template.format(year=year, month=month, feature=feature)


def required_rasters(target_month: str, input_dir: Path, template: str) -> List[Path]:
    """列出当前目标月份本地计算所需的全部输入 tif。"""
    paths = []
    for _, year, month, _ in month_sequence_for_target(target_month):
        for feature in RAW_FEATURES:
            paths.append(raster_path(input_dir, year, month, feature, template))
    return paths


def check_required_rasters(paths: Iterable[Path]) -> None:
    """在真正读栅格前检查文件是否齐全。"""
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        msg = "缺少本地输入影像，需先下载或按命名规则放入目录:\n" + "\n".join(missing)
        raise FileNotFoundError(msg)


def read_single_band(path: Path) -> Tuple[np.ndarray, np.ndarray, dict]:
    """读取单波段 tif，并返回 float32 数组、有效像素掩膜和 profile。"""
    with rasterio.open(path) as src:
        if src.count != 1:
            raise ValueError(f"要求单波段 tif，但 {path} 有 {src.count} 个波段。")
        data = src.read(1, out_dtype="float32")
        mask = src.read_masks(1) > 0
        nodata = src.nodata
        if nodata is not None:
            mask &= data != nodata
        mask &= np.isfinite(data)
        profile = src.profile.copy()
    return data.astype("float32"), mask, profile


def same_grid(a: dict, b: dict) -> bool:
    """判断两个 raster profile 是否可逐像素计算。"""
    return (
        a.get("crs") == b.get("crs")
        and a.get("transform") == b.get("transform")
        and a.get("width") == b.get("width")
        and a.get("height") == b.get("height")
    )


def reproject_to_reference(
    arr: np.ndarray,
    mask: np.ndarray,
    src_profile: dict,
    ref_profile: dict,
    value_resampling: Resampling = Resampling.bilinear,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    把输入影像重投影/重采样到参考网格。

    GEE 在不同数据源叠加时会隐式重投影；本地 numpy 逐像素计算前必须显式完成这一步。
    连续变量用双线性重采样，掩膜用最近邻重采样。
    """
    dst_shape = (int(ref_profile["height"]), int(ref_profile["width"]))
    src_nodata = src_profile.get("nodata")
    dst_nodata = np.float32(np.nan)
    dst = np.full(dst_shape, np.nan, dtype="float32")
    src_values = arr.astype("float32", copy=True)
    src_values[~mask] = np.nan

    reproject(
        source=src_values,
        destination=dst,
        src_transform=src_profile["transform"],
        src_crs=src_profile["crs"],
        src_nodata=src_nodata if src_nodata is not None else np.nan,
        dst_transform=ref_profile["transform"],
        dst_crs=ref_profile["crs"],
        dst_nodata=dst_nodata,
        resampling=value_resampling,
    )

    src_mask = mask.astype("uint8")
    dst_mask = np.zeros(dst_shape, dtype="uint8")
    reproject(
        source=src_mask,
        destination=dst_mask,
        src_transform=src_profile["transform"],
        src_crs=src_profile["crs"],
        src_nodata=0,
        dst_transform=ref_profile["transform"],
        dst_crs=ref_profile["crs"],
        dst_nodata=0,
        resampling=Resampling.nearest,
    )
    valid = (dst_mask > 0) & np.isfinite(dst)
    return dst.astype("float32"), valid


def load_monthly_rasters(target_month: str, input_dir: Path, template: str) -> RasterBundle:
    """读取 Apr 到目标月的全部月度输入影像，并自动对齐到第一个 tif 的参考网格。"""
    req = required_rasters(target_month, input_dir, template)
    check_required_rasters(req)

    arrays: Dict[Tuple[int, str], np.ndarray] = {}
    masks: Dict[Tuple[int, str], np.ndarray] = {}
    reference_profile = None

    for month_idx, year, month, _ in month_sequence_for_target(target_month):
        for feature in RAW_FEATURES:
            path = raster_path(input_dir, year, month, feature, template)
            arr, mask, profile = read_single_band(path)
            if reference_profile is None:
                reference_profile = profile
            elif not same_grid(reference_profile, profile):
                print(f"影像网格不一致，已重投影到参考网格: {path}")
                arr, mask = reproject_to_reference(arr, mask, profile, reference_profile)
            arrays[(month_idx, feature)] = arr
            masks[(month_idx, feature)] = mask

    if reference_profile is None:
        raise ValueError("没有读取到任何影像。")
    return RasterBundle(arrays=arrays, valid_masks=masks, profile=reference_profile)


def load_cane_mask(mask_path: Path | None, profile: dict) -> np.ndarray | None:
    """读取可选甘蔗掩膜；若网格不同，则自动用最近邻重投影到参考网格。"""
    if mask_path is None:
        return None
    arr, mask, mask_profile = read_single_band(mask_path)
    if not same_grid(profile, mask_profile):
        print(f"甘蔗掩膜网格不一致，已重投影到参考网格: {mask_path}")
        arr, mask = reproject_to_reference(arr, mask, mask_profile, profile, Resampling.nearest)
    return mask & (arr > 0)


# =============================================================================
# 8. 本地动态特征构建
# =============================================================================

def stack_feature(bundle: RasterBundle, feature: str, cutoff_idx: int) -> Tuple[np.ndarray, np.ndarray]:
    """把某个原始变量按月份堆叠为 shape=(month, row, col)。"""
    arrays = []
    masks = []
    for idx in range(1, cutoff_idx + 1):
        arrays.append(bundle.arrays[(idx, feature)])
        masks.append(bundle.valid_masks[(idx, feature)])
    return np.stack(arrays, axis=0), np.stack(masks, axis=0)


def nan_array_like(reference: np.ndarray) -> np.ndarray:
    """创建与参考二维栅格同形状的 NaN 数组。"""
    return np.full(reference.shape, np.nan, dtype="float32")


def nanstd_sample(values: np.ndarray) -> np.ndarray:
    """按 GEE stdBand 逻辑计算样本标准差：count > 1 时使用 ddof=1。"""
    valid = np.isfinite(values)
    count = valid.sum(axis=0)
    out = nan_array_like(values[0])
    enough = count > 1
    if np.any(enough):
        mean = np.nanmean(values, axis=0)
        sq = (values - mean) ** 2
        ss = np.nansum(sq, axis=0)
        out[enough] = np.sqrt(ss[enough] / (count[enough] - 1))
    return out


def build_feature_images(bundle: RasterBundle, cutoff_idx: int) -> Dict[str, np.ndarray]:
    """构建与 GEE buildPixelFeatureImage() 对应的全部动态特征影像。"""
    features: Dict[str, np.ndarray] = {}
    reference = next(iter(bundle.arrays.values()))

    for idx in range(1, cutoff_idx + 1):
        label = MONTH_LABELS[idx]
        for feature in RAW_FEATURES:
            arr = bundle.arrays[(idx, feature)].copy()
            arr = np.where(bundle.valid_masks[(idx, feature)], arr, np.nan).astype("float32")
            features[f"{feature}_m{idx:02d}_{label}"] = arr

    cutoff_label = MONTH_LABELS[cutoff_idx]
    ndvi_stack, ndvi_mask = stack_feature(bundle, "NDVI", cutoff_idx)
    ndvi_values = np.where(ndvi_mask, ndvi_stack, np.nan).astype("float32")
    features["observed_month_count"] = np.sum(np.isfinite(ndvi_values), axis=0).astype("float32")

    for feature in RAW_FEATURES:
        raw_stack, raw_mask = stack_feature(bundle, feature, cutoff_idx)
        values = np.where(raw_mask, raw_stack, np.nan).astype("float32")
        first = values[0]
        last = values[-1]
        features[f"{feature}_mean_Apr_to_{cutoff_label}"] = np.nanmean(values, axis=0).astype("float32")
        features[f"{feature}_std_Apr_to_{cutoff_label}"] = nanstd_sample(values)
        features[f"{feature}_min_Apr_to_{cutoff_label}"] = np.nanmin(values, axis=0).astype("float32")
        features[f"{feature}_max_Apr_to_{cutoff_label}"] = np.nanmax(values, axis=0).astype("float32")
        features[f"{feature}_last_Apr_to_{cutoff_label}"] = last.astype("float32")
        features[f"{feature}_{feature}_trend_Apr_to_{cutoff_label}"] = (last - first).astype("float32")

    for feature in SUM_FEATURES:
        raw_stack, raw_mask = stack_feature(bundle, feature, cutoff_idx)
        values = np.where(raw_mask, raw_stack, np.nan).astype("float32")
        out = np.nansum(values, axis=0).astype("float32")
        out[np.sum(np.isfinite(values), axis=0) == 0] = np.nan
        features[f"{feature}_sum_Apr_to_{cutoff_label}"] = out

    no_observation = features["observed_month_count"] <= 0
    for key, arr in features.items():
        if arr.shape != reference.shape:
            raise ValueError(f"特征 {key} 的数组形状异常。")
        features[key] = np.where(no_observation, np.nan, arr).astype("float32")
    return features


# =============================================================================
# 9. 线性公式计算与 GeoTIFF 输出
# =============================================================================

def predict_raster(feature_images: Dict[str, np.ndarray], folded, final_scale: float, cane_mask: np.ndarray | None) -> Tuple[np.ndarray, np.ndarray]:
    """按折叠后的线性公式计算原始结果和最终结果。"""
    first = next(iter(feature_images.values()))
    raw = np.full(first.shape, float(folded.folded_intercept), dtype="float64")

    for _, row in folded.dynamic_params.iterrows():
        name = str(row["feature"])
        if name not in feature_images:
            raise KeyError(f"本地特征缺失，无法计算模型参数: {name}")
        arr = feature_images[name].astype("float64")
        fill = float(row["fill_value_if_null"])
        coef = float(row["gee_raw_coef"])
        filled = np.where(np.isfinite(arr), arr, fill)
        raw += filled * coef

    final = raw * float(final_scale)
    valid = np.isfinite(raw)
    if cane_mask is not None:
        valid &= cane_mask
    raw = np.where(valid, raw, np.nan).astype("float32")
    final = np.where(valid, final, np.nan).astype("float32")
    return raw, final


def write_tif(path: Path, array: np.ndarray, profile: dict, nodata: float) -> None:
    """写出单波段 float32 GeoTIFF。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    out_profile = profile.copy()
    out_profile.update(
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
    data = np.where(np.isfinite(array), array, nodata).astype("float32")
    with rasterio.open(path, "w", **out_profile) as dst:
        dst.write(data, 1)


def raster_stats(array: np.ndarray) -> Dict[str, float]:
    """计算输出栅格的基本统计量。"""
    valid = np.isfinite(array)
    if not np.any(valid):
        return {"count": 0, "mean": np.nan, "min": np.nan, "max": np.nan, "std": np.nan}
    vals = array[valid].astype("float64")
    return {
        "count": int(vals.size),
        "mean": float(vals.mean()),
        "min": float(vals.min()),
        "max": float(vals.max()),
        "std": float(vals.std(ddof=0)),
    }


def raster_profile_from_dataset(ds) -> dict:
    """从已打开的 rasterio 数据集提取用于网格比较的 profile。"""
    return {
        "crs": ds.crs,
        "transform": ds.transform,
        "width": ds.width,
        "height": ds.height,
    }


def make_output_profile(profile: dict, nodata: float) -> dict:
    """按参考影像 profile 创建单波段 float32 输出 profile。"""
    out_profile = profile.copy()
    out_profile.update(
        driver="GTiff",
        count=1,
        dtype="float32",
        nodata=nodata,
        compress="deflate",
        predictor=2,
        tiled=True,
        blockxsize=256,
        blockysize=256,
        BIGTIFF="IF_SAFER",
    )
    return out_profile


def iter_windows(width: int, height: int, block_size: int) -> Iterable[Window]:
    """按固定窗口遍历整幅影像，避免一次性把大数组读入内存。"""
    for row_off in range(0, height, block_size):
        win_height = min(block_size, height - row_off)
        for col_off in range(0, width, block_size):
            win_width = min(block_size, width - col_off)
            yield Window(col_off=col_off, row_off=row_off, width=win_width, height=win_height)


def read_window_float(ds, window: Window) -> Tuple[np.ndarray, np.ndarray]:
    """从已对齐数据集中读取一个窗口，返回 float32 数组和有效掩膜。"""
    data = ds.read(1, window=window, out_dtype="float32", masked=True)
    arr = data.filled(np.nan).astype("float32")
    mask = (~np.ma.getmaskarray(data)) & np.isfinite(arr)
    nodata = ds.nodata
    if nodata is not None and np.isfinite(nodata):
        mask &= arr != nodata
    arr = np.where(mask, arr, np.nan).astype("float32")
    return arr, mask


def open_aligned_dataset(stack: ExitStack, path: Path, ref_profile: dict, resampling: Resampling):
    """
    打开一个 tif；若网格和参考影像不同，则用 WarpedVRT 延迟重投影。

    这个函数不会把整幅影像重投影到内存里，而是在 read(window) 时只重采样当前窗口。
    """
    src = stack.enter_context(rasterio.open(path))
    if src.count != 1:
        raise ValueError(f"要求单波段 tif，但 {path} 有 {src.count} 个波段。")
    src_profile = raster_profile_from_dataset(src)
    if same_grid(ref_profile, src_profile):
        return src

    print(f"影像网格不一致，将按窗口重投影到参考网格: {path}")
    return stack.enter_context(
        WarpedVRT(
            src,
            crs=ref_profile["crs"],
            transform=ref_profile["transform"],
            width=int(ref_profile["width"]),
            height=int(ref_profile["height"]),
            resampling=resampling,
            nodata=np.nan,
        )
    )


def write_window_tif_outputs(
    target_month: str,
    input_dir: Path,
    template: str,
    folded,
    final_scale: float,
    cane_mask_path: Path | None,
    raw_output_tif: Path,
    output_tif: Path,
    nodata: float,
    block_size: int,
) -> None:
    """
    分块计算本地估产 tif。

    原先的整幅计算会同时保存月度影像、29 个动态特征和 raw/final 输出，大范围 30 m 影像会触发 MemoryError。
    这里按窗口读取、构造特征、预测并立即写出，内存占用主要由单个 block 决定。
    """
    req = required_rasters(target_month, input_dir, template)
    check_required_rasters(req)
    if cane_mask_path is not None and not cane_mask_path.exists():
        raise FileNotFoundError(f"默认/指定掩膜 tif 不存在: {cane_mask_path}")
    months = month_sequence_for_target(target_month)
    cutoff_idx = len(months)
    first_path = req[0]
    raw_output_tif.parent.mkdir(parents=True, exist_ok=True)
    output_tif.parent.mkdir(parents=True, exist_ok=True)

    with ExitStack() as stack:
        ref_src = stack.enter_context(rasterio.open(first_path))
        if ref_src.count != 1:
            raise ValueError(f"要求单波段 tif，但 {first_path} 有 {ref_src.count} 个波段。")
        ref_profile = ref_src.profile.copy()
        ref_grid = raster_profile_from_dataset(ref_src)
        print(f"参考网格: {first_path}")
        print(f"参考影像大小: {ref_src.width} x {ref_src.height}, block_size={block_size}")

        datasets = {}
        for month_idx, year, month, _ in months:
            for feature in RAW_FEATURES:
                path = raster_path(input_dir, year, month, feature, template)
                if path.resolve() == first_path.resolve():
                    datasets[(month_idx, feature)] = ref_src
                else:
                    datasets[(month_idx, feature)] = open_aligned_dataset(stack, path, ref_grid, Resampling.bilinear)

        mask_ds = None
        if cane_mask_path is not None:
            print(f"最终掩膜 tif: {cane_mask_path}")
            mask_ds = open_aligned_dataset(stack, cane_mask_path, ref_grid, Resampling.nearest)

        out_profile = make_output_profile(ref_profile, nodata)
        with rasterio.open(raw_output_tif, "w", **out_profile) as raw_dst, rasterio.open(output_tif, "w", **out_profile) as final_dst:
            total_windows = int(np.ceil(ref_src.width / block_size) * np.ceil(ref_src.height / block_size))
            for n, window in enumerate(iter_windows(ref_src.width, ref_src.height, block_size), start=1):
                arrays: Dict[Tuple[int, str], np.ndarray] = {}
                masks: Dict[Tuple[int, str], np.ndarray] = {}

                for month_idx, _, _, _ in months:
                    for feature in RAW_FEATURES:
                        arr, mask = read_window_float(datasets[(month_idx, feature)], window)
                        arrays[(month_idx, feature)] = arr
                        masks[(month_idx, feature)] = mask

                window_bundle = RasterBundle(arrays=arrays, valid_masks=masks, profile=ref_profile)
                feature_images = build_feature_images(window_bundle, cutoff_idx)

                cane_mask = None
                if mask_ds is not None:
                    mask_arr, mask_valid = read_window_float(mask_ds, window)
                    cane_mask = mask_valid & (mask_arr > 0)

                raw, final = predict_raster(feature_images, folded, final_scale, cane_mask)
                raw_dst.write(np.where(np.isfinite(raw), raw, nodata).astype("float32"), 1, window=window)
                final_dst.write(np.where(np.isfinite(final), final, nodata).astype("float32"), 1, window=window)

                if n == 1 or n == total_windows or n % 50 == 0:
                    print(f"分块计算进度: {n}/{total_windows}")


# =============================================================================
# 10. 命令行入口
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地计算广西甘蔗持续估产线性结果并输出 GeoTIFF。")
    parser.add_argument("--input-file", default=str(INPUT_FILE), help="输入 Excel 建模表路径。")
    parser.add_argument("--sheet", default=SHEET_NAME, help="输入 Excel 工作表名称。")
    parser.add_argument("--input-raster-dir", default=str(INPUT_RASTER_DIR), help="本地月度输入 tif 目录。")
    parser.add_argument("--target-month", default=TARGET_MONTH, help="目标月份，格式 YYYY-MM。")
    parser.add_argument("--train-start-year", type=int, default=TRAIN_START_YEAR, help="训练起始榨季年份。")
    parser.add_argument("--train-end-year", type=int, default=TRAIN_END_YEAR, help="训练结束榨季年份。")
    parser.add_argument("--allow-target-in-training", action="store_true", default=ALLOW_TARGET_IN_TRAINING, help="允许目标榨季进入训练集；真实预测应保持 False。")
    parser.add_argument("--preview-rows", type=int, default=20, help="目标县级预测预览行数。")
    parser.add_argument("--gee-sugarcane-asset", default=GEE_SUGARCANE_ASSET, help="GEE 甘蔗掩膜 Asset ID，仅用于同步生成 GEE 代码字段。")
    parser.add_argument("--gee-province-asset", default=GEE_PROVINCE_ASSET, help="GEE 广西边界 Asset ID，仅用于同步生成 GEE 代码字段。")
    parser.add_argument("--gee-export-folder", default=GEE_EXPORT_FOLDER, help="GEE Drive 导出文件夹，仅用于同步生成 GEE 代码字段。")
    parser.add_argument("--gee-export-prefix", default=None, help="GEE 导出前缀；不传时自动生成。")
    parser.add_argument("--final-result-scale", type=float, default=FINAL_RESULT_SCALE, help="最终结果缩放系数。")
    parser.add_argument("--output-tif", default=str(OUTPUT_TIF), help="最终结果 tif 输出路径。")
    parser.add_argument("--raw-output-tif", default=str(RAW_OUTPUT_TIF), help="原始模型结果 tif 输出路径。")
    parser.add_argument("--cane-mask-tif", default=str(CANE_MASK_TIF) if CANE_MASK_TIF else None, help="最终掩膜 tif，>0 像素保留；默认使用脚本同目录 classification_April_1.tif。")
    parser.add_argument("--raster-template", default=RASTER_NAME_TEMPLATE, help="输入 tif 命名模板。")
    parser.add_argument("--block-size", type=int, default=512, help="分块计算窗口大小；影像很大或内存较小时可设为 256。")
    parser.add_argument("--list-required", action="store_true", help="只列出需要下载/准备的影像，不执行计算。")
    return parser.parse_args()


def build_runtime_args(cli: argparse.Namespace) -> argparse.Namespace:
    """整理训练流程和本地计算共享的运行参数。"""
    scale_label = f"{cli.final_result_scale:.3f}".replace(".", "")
    target_label = cli.target_month.replace("-", "_")
    export_prefix = cli.gee_export_prefix or (
        f"GX_countyFolded_pixelYield_train{cli.train_start_year}_{cli.train_end_year}_"
        f"target{target_label}_final{scale_label}"
    )
    return argparse.Namespace(
        input_file=cli.input_file,
        sheet=cli.sheet,
        target_month=cli.target_month,
        train_start_year=cli.train_start_year,
        train_end_year=cli.train_end_year,
        allow_target_in_training=cli.allow_target_in_training,
        preview_rows=cli.preview_rows,
        gee_sugarcane_asset=cli.gee_sugarcane_asset,
        gee_province_asset=cli.gee_province_asset,
        gee_export_folder=cli.gee_export_folder,
        gee_export_prefix=export_prefix,
        final_result_scale=cli.final_result_scale,
    )


def print_required_downloads(target_month: str, input_dir: Path, template: str) -> None:
    """打印本地计算需要准备的影像清单。"""
    print("\n" + "=" * 24 + " 本地影像下载/准备清单 " + "=" * 24)
    print("本地计算会把所有 tif 自动重投影/重采样到第一个输入 tif 的网格。")
    print("建议让第一个输入 tif（通常为 4 月 NDVI）采用你希望最终输出的投影、范围和分辨率。")
    print("NDVI: Sentinel-2 SR Harmonized, SCL 云掩膜后月 median NDVI。")
    print("temperature_2m / total_precipitation_sum: ERA5-Land MONTHLY_AGGR，单位保持训练表口径。")
    for path in required_rasters(target_month, input_dir, template):
        print(path)


def main() -> None:
    cli = parse_args()
    runtime_args = build_runtime_args(cli)
    input_dir = Path(cli.input_raster_dir)
    output_tif = Path(cli.output_tif)
    raw_output_tif = Path(cli.raw_output_tif)
    mask_tif = Path(cli.cane_mask_tif) if cli.cane_mask_tif else None

    print_required_downloads(runtime_args.target_month, input_dir, cli.raster_template)
    if cli.list_required:
        return

    model_result = build_local_model_result(runtime_args)
    cutoff_idx = int(model_result["cutoff_idx"])
    folded = model_result["folded_full"]
    print("\n" + "=" * 24 + " 模型参数 " + "=" * 24)
    print(f"目标月份: {model_result['target_dt'].strftime('%Y-%m')}")
    print(f"使用月份: Apr 到 {MONTH_LABELS[cutoff_idx]}")
    print(f"动态参数数: {len(folded.dynamic_params)}")
    print(f"折叠截距 raw: {folded.folded_intercept:.12f}")
    print(f"最终结果系数: {runtime_args.final_result_scale:.12g}")
    print_province_fold_check(model_result)

    write_window_tif_outputs(
        target_month=runtime_args.target_month,
        input_dir=input_dir,
        template=cli.raster_template,
        folded=folded,
        final_scale=runtime_args.final_result_scale,
        cane_mask_path=mask_tif,
        raw_output_tif=raw_output_tif,
        output_tif=output_tif,
        nodata=NODATA_VALUE,
        block_size=cli.block_size,
    )

    print("\n" + "=" * 24 + " 输出结果 " + "=" * 24)
    print(f"原始模型 tif: {raw_output_tif}")
    print(f"最终结果 tif: {output_tif}")
    #print("原始模型统计:", raster_stats(raw))
    #print("最终结果统计:", raster_stats(final))


if __name__ == "__main__":
    main()