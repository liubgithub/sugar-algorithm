"""Sugarcane classification - RandomForest classifier (GEE).

Rebuilds the 12-month Sentinel-2 + Sentinel-1 time-series stack using
fixed normalization constants (preserved verbatim from the user's
script), trains a RandomForest on a random-column split of the labeled
samples, evaluates on the held-out validation set, computes the
confusion matrix / OA / Kappa via small getInfo() calls, classifies the
full ROI, and submits Export.image.toDrive.

Returns gee_task_id immediately, with metrics containing the small
accuracy numbers.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import ee

from app.algorithms.base import ProgressCallback, make_card, make_result


# Fixed normalization constants - preserved from the user's original
# script. crop_classification does not consume the threshold JSON
# produced by crop_threshold; these values are part of the algorithm.
_MIN_VALUES = [
    0.029811290483320912, 0.03176467133205311, 0.02265296018903864,
    0.01476035587882513, 0.010809080768406401, 0.008888844547898145,
    -0.41744869852165156, -0.11149357040751527, -0.7302011647424305,
    -20.12210081625641, -34.85871184658997,
]
_MAX_VALUES = [
    0.10612499962250392, 0.12352142802306584, 0.12386583313345909,
    0.37224230628747207, 0.28026874735951424, 0.19630118991647447,
    0.831877062052808, 0.6309771123364061, 0.6596194424253563,
    -4.627526409648029, -11.896721160567347,
]
_BAND_NAMES = ["B2", "B3", "B4", "B8", "B11", "B12", "NDVI", "EVI", "NDWI", "VV", "VH"]


def _initialize_ee(project: Optional[str]) -> None:
    sa_path = os.environ.get("EE_SERVICE_ACCOUNT_FILE")
    if sa_path and os.path.exists(sa_path):
        credentials = ee.ServiceAccountCredentials.from_json_keyfile_name(sa_path)
        if project:
            ee.Initialize(credentials, project=project)
        else:
            ee.Initialize(credentials)
        return
    if project:
        ee.Initialize(project=project)
    else:
        ee.Initialize()


def _mask_s2_clouds(image: ee.Image) -> ee.Image:
    qa = image.select("QA60")
    mask = qa.bitwiseAnd(1 << 10).eq(0).And(qa.bitwiseAnd(1 << 11).eq(0))
    return image.updateMask(mask).divide(10000)


def _build_time_series_stack(year: int, roi_bounds: ee.Geometry) -> ee.Image:
    """12-month normalized stack + DEM, identical to user's original script."""
    min_img = ee.Image.constant(_MIN_VALUES).rename(_BAND_NAMES)
    max_img = ee.Image.constant(_MAX_VALUES).rename(_BAND_NAMES)

    def _normalize(img: ee.Image) -> ee.Image:
        return img.subtract(min_img).divide(max_img.subtract(min_img)).clamp(0, 1)

    months = ee.List.sequence(1, 12)

    def _monthly_image(m: ee.Number) -> ee.Image:
        m_int = ee.Number(m)
        start = ee.Date.fromYMD(year, m_int, 1)
        end = start.advance(1, "month")

        s2 = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(roi_bounds)
            .filterDate(start, end)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
            .map(_mask_s2_clouds)
            .median()
        )
        s1 = (
            ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterBounds(roi_bounds)
            .filterDate(start, end)
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
            .filter(ee.Filter.eq("instrumentMode", "IW"))
            .select(["VV", "VH"])
            .median()
        )

        combined = (
            s2.select(["B2", "B3", "B4", "B8", "B11", "B12"])
            .addBands(s2.normalizedDifference(["B8", "B4"]).rename("NDVI"))
            .addBands(
                s2.expression(
                    "2.5*((B8-B4)/(B8+6*B4-7.5*B2+1))",
                    {
                        "B8": s2.select("B8"),
                        "B4": s2.select("B4"),
                        "B2": s2.select("B2"),
                    },
                ).rename("EVI")
            )
            .addBands(s2.normalizedDifference(["B3", "B8"]).rename("NDWI"))
            .addBands(s1)
        )
        return _normalize(combined).set("month", m_int)

    monthly_images = months.map(_monthly_image)
    month_col = ee.ImageCollection.fromImages(monthly_images)
    annual_median = month_col.median()

    def _fill(img: ee.Image) -> ee.Image:
        img = ee.Image(img)
        m_str = ee.Number(img.get("month")).format("%02d")

        def _rename(b: ee.String) -> ee.String:
            return ee.String("M").cat(m_str).cat("_").cat(ee.String(b))

        new_names = img.bandNames().map(_rename)
        return img.unmask(annual_median).rename(new_names)

    filled = month_col.map(_fill)

    def _stack(img: ee.Image, prev: ee.Image) -> ee.Image:
        return ee.Image(prev).addBands(img)

    dummy = ee.Image.constant(0).rename("dummy")
    stacked = ee.Image(filled.iterate(_stack, dummy))
    valid = stacked.bandNames().remove("dummy")
    stacked = stacked.select(valid).unmask(-9999)

    dem = (
        ee.Image("NASA/NASADEM_HGT/001")
        .resample("bilinear")
        .reproject(crs="EPSG:4326", scale=10)
        .clip(roi_bounds)
    )
    elevation_norm = (
        dem.select("elevation")
        .subtract(-11)
        .divide(1749 - (-11))
        .clamp(0, 1)
        .rename("elevation")
    )
    return stacked.addBands(elevation_norm)


def run(
    params: Dict[str, Any],
    job_dir: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    """Train RF, evaluate, classify, submit export task."""
    project = params.get("gee_project") or os.environ.get("EE_PROJECT")
    _initialize_ee(project)

    if progress_callback:
        progress_callback(5, "初始化 GEE")

    year = int(params["year"])
    label_property = params.get("label_property") or "class"
    num_trees = int(params.get("num_trees", 100))
    train_ratio = float(params.get("train_ratio", 0.8))
    export_folder = params.get("export_folder") or "GEE_Exports"
    description = params.get("export_description") or f"Crop_Classification_{year}"

    roi = ee.FeatureCollection(params["roi_asset_id"])
    sample_points = ee.FeatureCollection(params["feature_asset"])
    # User's script uses samplePoints.geometry() (not bounds()).
    roi_bounds = sample_points.geometry()

    if progress_callback:
        progress_callback(20, "构建 12 个月时序特征")

    final_image = _build_time_series_stack(year, roi_bounds)
    feature_bands = final_image.bandNames()

    if progress_callback:
        progress_callback(50, f"训练 RandomForest (n={num_trees})")

    sample_with_random = sample_points.randomColumn("random", seed=42)
    training_set = sample_with_random.filter(ee.Filter.lt("random", train_ratio))
    validation_set = sample_with_random.filter(ee.Filter.gte("random", train_ratio))

    rf_model = ee.Classifier.smileRandomForest(num_trees).train(
        features=training_set,
        classProperty=label_property,
        inputProperties=feature_bands,
    )

    if progress_callback:
        progress_callback(70, "计算 OA / Kappa / 混淆矩阵")

    validated = validation_set.classify(rf_model)
    error_matrix = validated.errorMatrix(label_property, "classification")
    # Small numeric arrays - allowed.
    matrix_list = error_matrix.getInfo()
    oa = error_matrix.accuracy().getInfo()
    kappa = error_matrix.kappa().getInfo()

    if progress_callback:
        progress_callback(85, "提交 Export.image.toDrive")

    classified = final_image.select(feature_bands).classify(rf_model)

    task = ee.batch.Export.image.toDrive(
        image=classified.toByte(),
        description=description,
        folder=export_folder,
        region=roi.geometry(),
        scale=10,
        maxPixels=1e13,
    )
    task.start()
    task_id = getattr(task, "id", None) or task.status().get("id")

    if progress_callback:
        progress_callback(100, "GEE 分类导出任务已提交")

    return make_result(
        status="submitted",
        result_type="gee_task",
        gee_task_id=task_id,
        metrics={
            "oa": float(oa) if oa is not None else None,
            "kappa": float(kappa) if kappa is not None else None,
            "confusion_matrix": matrix_list,
            "num_trees": num_trees,
            "train_ratio": train_ratio,
            "year": year,
            "cards": [
                make_card("task_status", "任务状态", "已提交", ""),
                make_card("gee_task_id", "GEE 任务 ID", task_id, ""),
                make_card("year", "目标年份", year, "年"),
                make_card("num_trees", "RandomForest 树数", num_trees, "棵"),
                make_card("train_ratio", "训练集比例", train_ratio, ""),
                make_card("oa", "整体精度 OA", float(oa) if oa is not None else None, ""),
                make_card("kappa", "Kappa 系数", float(kappa) if kappa is not None else None, ""),
            ],
        },
        message=(
            f"GEE 分类导出任务已提交 (task_id={task_id}); "
            f"OA={oa:.4f}, Kappa={kappa:.4f}"
        ),
    )


__all__ = ["run"]
