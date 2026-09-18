"""Sugarcane classification - monthly feature generation (GEE).

Reads the threshold JSON produced by crop_threshold, builds the
12-month Sentinel-2 + Sentinel-1 time series (133 bands: 11 features x
12 months + DEM), reduces to sample points, and exports a CSV through
Export.table.toDrive. Returns immediately after task.start() with the
GEE task id; no large getInfo is performed on per-sample features.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import ee

from app.algorithms.base import ProgressCallback, make_card, make_result


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


def _load_threshold(path: str) -> Dict[str, Any]:
    """Load the JSON written by crop_threshold."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    required = {"band_names", "min_values", "max_values", "dem_min", "dem_max"}
    missing = required - set(data)
    if missing:
        raise ValueError(f"threshold JSON missing fields: {sorted(missing)}")
    if len(data["min_values"]) != len(data["band_names"]):
        raise ValueError("min_values length must match band_names")
    if len(data["max_values"]) != len(data["band_names"]):
        raise ValueError("max_values length must match band_names")
    return data


def _make_pipeline(threshold: Dict[str, Any], year: int, roi: ee.FeatureCollection):
    """Build the 12-month stack + DEM. Closes over threshold arrays."""
    band_names = threshold["band_names"]
    min_values = threshold["min_values"]
    max_values = threshold["max_values"]
    dem_min = float(threshold["dem_min"])
    dem_max = float(threshold["dem_max"])

    min_img = ee.Image.constant(min_values).rename(band_names)
    max_img = ee.Image.constant(max_values).rename(band_names)

    def _normalize(img: ee.Image) -> ee.Image:
        return (
            img.subtract(min_img)
            .divide(max_img.subtract(min_img))
            .clamp(0, 1)
        )

    months = ee.List.sequence(1, 12)

    def _mask_s2_clouds(image: ee.Image) -> ee.Image:
        qa = image.select("QA60")
        mask = qa.bitwiseAnd(1 << 10).eq(0).And(qa.bitwiseAnd(1 << 11).eq(0))
        return image.updateMask(mask).divide(10000)

    def _monthly_image(m: ee.Number) -> ee.Image:
        m_int = ee.Number(m)
        start = ee.Date.fromYMD(year, m_int, 1)
        end = start.advance(1, "month")

        s2 = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(roi)
            .filterDate(start, end)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
            .map(_mask_s2_clouds)
            .median()
        )
        s1 = (
            ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterBounds(roi)
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
        .clip(roi)
    )
    elevation_norm = (
        dem.select("elevation")
        .subtract(dem_min)
        .divide(dem_max - dem_min)
        .clamp(0, 1)
        .rename("elevation")
    )
    return stacked.addBands(elevation_norm)


def _add_coordinates(feature: ee.Feature) -> ee.Feature:
    coords = feature.geometry().coordinates()
    return feature.set(
        {
            "longitude": coords.get(0),
            "latitude": coords.get(1),
        }
    )


def _drop_geometry(feature: ee.Feature) -> ee.Feature:
    return feature.setGeometry(None)


def run(
    params: Dict[str, Any],
    job_dir: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    """Build the asset graph and submit Export.table.toDrive."""
    project = params.get("gee_project") or os.environ.get("EE_PROJECT")
    _initialize_ee(project)

    if progress_callback:
        progress_callback(5, "初始化 GEE")

    year = int(params["year"])
    threshold_path = params["threshold_json_path"]
    threshold = _load_threshold(threshold_path)

    if progress_callback:
        progress_callback(15, "加载 ROI 与样本点")

    roi = ee.FeatureCollection(params["roi_asset_id"])
    sample_points = ee.FeatureCollection(params["samples_asset_id"])

    if progress_callback:
        progress_callback(35, "构建 12 个月时序特征")

    final_image = _make_pipeline(threshold, year, roi)

    if progress_callback:
        progress_callback(65, "reduceRegions 提取样本")

    samples_with_coords = sample_points.map(_add_coordinates)
    extracted = final_image.reduceRegions(
        collection=samples_with_coords,
        reducer=ee.Reducer.first(),
        scale=10,
        tileScale=16,
    ).map(_drop_geometry)

    export_folder = params.get("export_folder") or "GEE_Exports"
    description = (
        params.get("export_description") or f"Crop_Features_{year}"
    )

    if progress_callback:
        progress_callback(85, "提交 Export.table.toDrive")

    task = ee.batch.Export.table.toDrive(
        collection=extracted,
        description=description,
        folder=export_folder,
        fileFormat="CSV",
    )
    task.start()
    task_id = getattr(task, "id", None) or task.status().get("id")

    if progress_callback:
        progress_callback(100, "GEE 表导出任务已提交")

    return make_result(
        status="submitted",
        result_type="gee_task",
        gee_task_id=task_id,
        metrics={
            "gee_task_id": task_id,
            "year": year,
            "band_count": len(threshold["band_names"]),
            "roi_asset_id": params.get("roi_asset_id"),
            "samples_asset_id": params.get("samples_asset_id"),
            "cards": [
                make_card("task_status", "任务状态", "已提交", ""),
                make_card("gee_task_id", "GEE 任务 ID", task_id, ""),
                make_card("year", "目标年份", year, "年"),
                make_card("band_count", "特征波段数", len(threshold["band_names"]), "个"),
                make_card(
                    "feature_asset", "样本 Asset", params.get("samples_asset_id"), ""
                ),
            ],
        },
        message=f"GEE 表导出任务已提交 (task_id={task_id})",
    )


__all__ = ["run"]
