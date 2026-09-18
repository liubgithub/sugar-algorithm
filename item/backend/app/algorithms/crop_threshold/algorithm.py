"""Sugarcane classification - threshold extraction (GEE).

Computes 2/98 percentile bounds for the 11-band Sentinel-2 + Sentinel-1
stack and min/max for NASADEM elevation, then writes the four numeric
arrays to job_dir/threshold.json. Only the small numeric arrays are
pulled via getInfo(); the heavy work stays server-side.

Input  : params (year, roi_asset_id, gee_project)
Output : threshold.json + make_result(status="completed", result_type="json")
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import ee

from app.algorithms.base import ProgressCallback, make_card, make_result


BAND_NAMES = ["B2", "B3", "B4", "B8", "B11", "B12", "NDVI", "EVI", "NDWI", "VV", "VH"]
THRESHOLD_FILENAME = "threshold.json"


def _initialize_ee(project: Optional[str]) -> None:
    """Initialize Earth Engine, mirroring the harvest helper.

    Honors EE_SERVICE_ACCOUNT_FILE for service-account auth; otherwise
    falls back to default credentials (gcloud ADC or prior
    ee.Authenticate()).
    """
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
    """S2 SR cloud mask via QA60 bits 10/11, then scale reflectance."""
    qa = image.select("QA60")
    mask = qa.bitwiseAnd(1 << 10).eq(0).And(qa.bitwiseAnd(1 << 11).eq(0))
    return image.updateMask(mask).divide(10000)


def _build_annual_combined(roi_bounds: ee.Geometry, year: int) -> ee.Image:
    """Annual median composite: B2..B12, NDVI, EVI, NDWI, VV, VH (11 bands)."""
    start = f"{year}-01-01"
    end = f"{year}-12-31"

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(roi_bounds)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
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

    return (
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


def run(
    params: Dict[str, Any],
    job_dir: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    """Compute thresholds synchronously and return make_result(completed)."""
    project = params.get("gee_project") or os.environ.get("EE_PROJECT")
    _initialize_ee(project)

    if progress_callback:
        progress_callback(5, "初始化 GEE")

    year = int(params["year"])
    roi = ee.FeatureCollection(params["roi_asset_id"])
    roi_bounds = roi.geometry().bounds()

    if progress_callback:
        progress_callback(20, "构建年度合成影像")

    annual = _build_annual_combined(roi_bounds, year)

    if progress_callback:
        progress_callback(45, "计算 2/98 百分位（云端）")

    # Server-side percentile reduction at coarse 5000 m to stay snappy.
    dynamic_stats = annual.reduceRegion(
        reducer=ee.Reducer.percentile([2, 98]),
        geometry=roi_bounds,
        scale=5000,
        maxPixels=1e13,
    )

    # Build the p2 / p98 lists on the server using .map() over a fixed
    # band order, then pull the resulting arrays in a single getInfo().
    band_list = ee.List(BAND_NAMES)

    def _get_p2(band: ee.String) -> ee.Number:
        return dynamic_stats.getNumber(ee.String(band).cat("_p2"))

    def _get_p98(band: ee.String) -> ee.Number:
        return dynamic_stats.getNumber(ee.String(band).cat("_p98"))

    ee_min_array = band_list.map(_get_p2)
    ee_max_array = band_list.map(_get_p98)
    min_values = ee_min_array.getInfo()
    max_values = ee_max_array.getInfo()

    if progress_callback:
        progress_callback(75, "计算 DEM 极值")

    dem = ee.Image("NASA/NASADEM_HGT/001").select("elevation").clip(roi_bounds)
    dem_stats = dem.reduceRegion(
        reducer=ee.Reducer.minMax(),
        geometry=roi_bounds,
        scale=500,
        maxPixels=1e13,
    )
    dem_min = dem_stats.getNumber("elevation_min").getInfo()
    dem_max = dem_stats.getNumber("elevation_max").getInfo()

    payload = {
        "band_names": BAND_NAMES,
        "min_values": list(min_values),
        "max_values": list(max_values),
        "dem_min": dem_min,
        "dem_max": dem_max,
        "year": year,
        "roi_asset_id": params["roi_asset_id"],
    }

    os.makedirs(job_dir, exist_ok=True)
    out_path = os.path.abspath(os.path.join(job_dir, THRESHOLD_FILENAME))
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    if progress_callback:
        progress_callback(100, "阈值计算完成")

    return make_result(
        status="completed",
        result_type="json",
        files=[
            {
                "name": THRESHOLD_FILENAME,
                "path": out_path,
                "label": "final",
                "size_bytes": os.path.getsize(out_path),
            }
        ],
        metrics={
            "band_names": BAND_NAMES,
            "min_values": list(min_values),
            "max_values": list(max_values),
            "dem_min": dem_min,
            "dem_max": dem_max,
            "year": year,
            "cards": [
                make_card("task_status", "任务状态", "完成", ""),
                make_card("year", "目标年份", year, "年"),
                make_card("band_count", "波段数量", len(BAND_NAMES), "个"),
                make_card("dem_min", "DEM 最小高程", dem_min, "m"),
                make_card("dem_max", "DEM 最大高程", dem_max, "m"),
                make_card(
                    "threshold_bands",
                    "阈值波段",
                    "、".join(BAND_NAMES),
                    "",
                ),
            ],
        },
        message=f"阈值计算完成，已写入 {THRESHOLD_FILENAME}",
    )


__all__ = ["run", "BAND_NAMES", "THRESHOLD_FILENAME"]
