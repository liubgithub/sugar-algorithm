"""Sugarcane harvest detection (GEE) - runnable wrapper.

Preserves the original Sentinel-1 logic: focal_median, linear-scale
conversion, RVI / NDPI / VH-texture, base vs current aggregation, and
Export.image.toDrive. Inputs come from params (no CLI), and the function
returns immediately after task.start() with the GEE task id.

GEE authentication is expected to be pre-configured (ee.Authenticate()
run locally, or EE_SERVICE_ACCOUNT_FILE pointing to a service account
JSON). The job service polls ee.data.getTaskStatus separately so this
function never blocks on getInfo or task completion.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import ee

from app.algorithms.base import ProgressCallback, make_card, make_result


def _initialize_ee(project: Optional[str]) -> None:
    """Initialize Earth Engine.

    Uses a service account JSON if EE_SERVICE_ACCOUNT_FILE points to one.
    Otherwise falls back to default credentials (gcloud ADC or prior
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


def _build_roi(roi_bounds: Any) -> ee.Geometry:
    """Build ee.Geometry.Rectangle from [minLon, minLat, maxLon, maxLat]."""
    if isinstance(roi_bounds, str):
        parts = [float(x.strip()) for x in roi_bounds.split(",") if x.strip()]
    else:
        parts = [float(x) for x in roi_bounds]
    if len(parts) != 4:
        raise ValueError("roi_bounds must have 4 numbers: [minLon, minLat, maxLon, maxLat]")
    return ee.Geometry.Rectangle(parts)


def _calculate_sar_metrics(image: ee.Image) -> ee.Image:
    """Original Sentinel-1 metric function: RVI / NDPI / VH texture.

    RVI and NDPI use linear-scale backscatter; texture is computed on the
    raw dB VH band inside a 30 m circular kernel.
    """
    smoothed = image.focal_median(30, "circle", "meters")

    linear_vv = ee.Image(10).pow(smoothed.select("VV").divide(10))
    linear_vh = ee.Image(10).pow(smoothed.select("VH").divide(10))

    rvi = linear_vh.multiply(4).divide(linear_vv.add(linear_vh)).rename("RVI")
    ndpi = linear_vv.subtract(linear_vh).divide(linear_vv.add(linear_vh)).rename("NDPI")

    vh_texture = image.select("VH").reduceNeighborhood(
        reducer=ee.Reducer.variance(),
        kernel=ee.Kernel.circle(30, "meters"),
    ).rename("Texture")

    return ee.Image.cat([rvi, ndpi, vh_texture]).copyProperties(image, ["system:time_start"])


def run(
    params: Dict[str, Any],
    job_dir: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> Dict[str, Any]:
    """Build the GEE asset graph and submit the export task.

    Returns immediately with status='submitted' and gee_task_id set.
    """
    project = params.get("gee_project") or os.environ.get("EE_PROJECT", "sonorous-reach-489903-a5")
    _initialize_ee(project)

    if progress_callback:
        progress_callback(5, "初始化 GEE")

    roi = _build_roi(params["roi_bounds"])
    sugarcane_mask = ee.Image(params["sugarcane_mask_asset"])
    is_sugarcane = sugarcane_mask.eq(1)

    base_start = params["base_start"]
    base_end = params["base_end"]
    current_start = params["current_start"]
    current_end = params["current_end"]

    rvi_absolute_low = float(params.get("rvi_absolute_low", 0.6))
    rvi_drop_min = float(params.get("rvi_drop_min", 0.2))

    scale = int(params.get("scale", 10))
    crs = params.get("crs", "EPSG:4326")
    max_pixels = int(params.get("max_pixels", int(1e13)))
    export_folder = params.get("export_folder", "GEE_Exports")
    description = params.get("export_description", "Export_Harvest")

    if progress_callback:
        progress_callback(15, "构建 SAR 影像集合")

    s1_col = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(roi)
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .filter(ee.Filter.eq("instrumentMode", "IW"))
    )

    base_metrics = s1_col.filterDate(base_start, base_end).map(_calculate_sar_metrics).median()
    current_metrics = s1_col.filterDate(current_start, current_end).map(_calculate_sar_metrics).min()

    if progress_callback:
        progress_callback(50, "计算收割判定掩膜")

    rvi_drop = base_metrics.select("RVI").subtract(current_metrics.select("RVI"))

    is_harvested_sar = (
        current_metrics.select("RVI").lt(rvi_absolute_low)
        .And(rvi_drop.gt(rvi_drop_min))
    )

    final_harvest_mask = (
        is_harvested_sar.updateMask(is_sugarcane).rename("Harvest_Status_SAR")
    )

    if progress_callback:
        progress_callback(75, "提交导出任务")

    task = ee.batch.Export.image.toDrive(
        image=final_harvest_mask,
        description=description,
        folder=export_folder,
        fileNamePrefix=description,
        region=roi,
        scale=scale,
        crs=crs,
        maxPixels=max_pixels,
    )
    task.start()

    # task.id is available after start(); fall back to status()['id'] if absent.
    task_id = getattr(task, "id", None) or task.status().get("id")

    if progress_callback:
        progress_callback(100, "GEE 导出任务已提交")

    return make_result(
        status="submitted",
        result_type="gee_task",
        gee_task_id=task_id,
        metrics={
            "gee_task_id": task_id,
            "rvi_absolute_low": rvi_absolute_low,
            "rvi_drop_min": rvi_drop_min,
            "scale": scale,
            "base_window": f"{base_start} → {base_end}",
            "current_window": f"{current_start} → {current_end}",
            "cards": [
                make_card("task_status", "任务状态", "已提交", ""),
                make_card("gee_task_id", "GEE 任务 ID", task_id, ""),
                make_card("rvi_low", "RVI 绝对下限", rvi_absolute_low, ""),
                make_card("rvi_drop", "RVI 下降阈值", rvi_drop_min, ""),
                make_card("scale", "导出分辨率", scale, "m"),
                make_card("base_window", "基期窗口", f"{base_start} → {base_end}", ""),
                make_card("current_window", "监测期窗口", f"{current_start} → {current_end}", ""),
            ],
        },
        message=f"GEE 导出任务已提交 (task_id={task_id})",
    )


__all__ = ["run"]