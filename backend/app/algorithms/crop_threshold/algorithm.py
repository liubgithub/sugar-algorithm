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
from typing import Any, Dict, List, Optional

import ee

from app.algorithms.base import ProgressCallback, make_card, make_result
from app.services.ingest_service import ensure_asset_for_local
from app.services.gee_auth import GeeAuthRequired, is_auth_mismatch_error


BAND_NAMES = ["B2", "B3", "B4", "B8", "B11", "B12", "NDVI", "EVI", "NDWI", "VV", "VH"]
THRESHOLD_FILENAME = "threshold.json"


def __ee(project: Optional[str]) -> None:
    """委托给 app.services.gee_auth.initialize_ee —— 项目不匹配时抛 GeeAuthRequired。"""
    from app.services.gee_auth import initialize_ee as _shared_init
    _shared_init(project)


def _mask_s2_clouds(image: ee.Image) -> ee.Image:
    """S2 SR cloud mask via QA60 bits 10/11, then scale reflectance."""
    qa = image.select("QA60")
    mask = qa.bitwiseAnd(1 << 10).eq(0).And(qa.bitwiseAnd(1 << 11).eq(0))
    return image.updateMask(mask).divide(10000)


def _build_annual_combined(
    roi_bounds: ee.Geometry, year: int, cloud_max: float = 60
) -> ee.Image:
    """Annual median composite: B2..B12, NDVI, EVI, NDWI, VV, VH (11 bands).

    `cloud_max` 由前端表单传入，默认 60 与原硬编码一致；不改 NDVI/EVI/NDWI
    与 S1 VV/VH 的计算方式。
    """
    start = f"{year}-01-01"
    end = f"{year}-12-31"

    s2 = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(roi_bounds)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", cloud_max))
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
    project = (
        params.get("gee_project")
        or os.environ.get("EE_PROJECT")
        or "sodium-ray-505904-i3"
    )
    try:
        # 函数在本模块里被定义为 ``__ee``（避免与本文件内的旧私有符号冲突），
        # 由于处于模块顶层（非类内部），双下划线前缀不会触发 Python 名称改写
        # （name mangling），这里直接按字面名字访问即可。
        __ee(project)
    except GeeAuthRequired as exc:
        return make_result(status="failed", result_type="json", message=str(exc))

    # 进度消息使用对用户友好的简短文案（需求："运行过程中不要把大量 Python/GEE
    # 日志直接展示给用户"），前端只在卡片上展示单行 message。
    if progress_callback:
        progress_callback(5, "正在连接 GEE…")

    year = int(params["year"])
    # 研究区 asset 解析：优先用用户手动填的 roi_asset_id；否则把本地文件
    # 自动 ingest 到 GEE，把返回的 asset id(s) 写回 params 后继续原计算逻辑。
    # 注：ensure_asset_for_local 现在返回 List[str]（chunked 上传场景下可能多元素），
    # 我们存到 roi_asset_ids，并在构造 FeatureCollection 时用 .flatten() 合并。
    if not params.get("roi_asset_id") and not params.get("roi_asset_ids") and params.get("roi_local_path"):
        if progress_callback:
            progress_callback(10, "正在把本地研究区注册为 GEE 资产…")
        try:
            roi_ids = ensure_asset_for_local(
                local_path=str(params["roi_local_path"]),
                slot="roi",
                algorithm="crop_threshold",
                project=project,
                kind_hint=None,
            )
            params["roi_asset_ids"] = list(roi_ids)
            params["roi_asset_id"] = roi_ids[0] if roi_ids else None
        except GeeAuthRequired as exc:
            return make_result(status="failed", result_type="json", message=str(exc))
        except Exception as exc:  # noqa: BLE001
            # 把 OAuth / project 不匹配类错误翻译成统一提示，其它保留原始
            # 报错以便排查。
            if is_auth_mismatch_error(exc):
                raise GeeAuthRequired(project) from exc
            return make_result(
                status="failed",
                result_type="json",
                message=f"研究区上传到 GEE 失败：{exc}",
            )
    # 兼容旧 params：可能只有 roi_asset_id（字符串）
    if not params.get("roi_asset_ids"):
        legacy = params.get("roi_asset_id")
        if legacy:
            params["roi_asset_ids"] = [legacy]
    if not params.get("roi_asset_ids"):
        return make_result(
            status="failed",
            result_type="json",
            message=(
                "缺少研究区：请上传本地研究区文件（basic 参数区『研究区文件』），"
                "或在高级参数中直接填写 GEE Asset 路径。"
            ),
        )
    roi = ee.FeatureCollection(params["roi_asset_ids"]).flatten()
    roi_bounds = roi.geometry().bounds()

    # 高级参数：默认值与原硬编码完全一致 → 用户不动时行为不变。
    cloud_max = float(params.get("cloud_max", 60))
    p_min = float(params.get("percentile_min", 2))
    p_max = float(params.get("percentile_max", 98))
    dynamic_scale = float(params.get("dynamic_scale", 5000))
    dem_scale = float(params.get("dem_scale", 500))

    if progress_callback:
        progress_callback(20, "正在构建年度合成影像…")

    annual = _build_annual_combined(roi_bounds, year, cloud_max)

    if progress_callback:
        progress_callback(
            45,
            f"正在计算 Sentinel-1 / Sentinel-2 特征范围（{int(p_min)}/{int(p_max)} 百分位）…",
        )

    # Server-side percentile reduction; scale 由前端高级参数控制，默认 5000 m。
    dynamic_stats = annual.reduceRegion(
        reducer=ee.Reducer.percentile([p_min, p_max]),
        geometry=roi_bounds,
        scale=dynamic_scale,
        maxPixels=1e13,
    )

    # Build the p{lo} / p{hi} lists on the server using .map() over a fixed
    # band order, then pull the resulting arrays in a single getInfo().
    band_list = ee.List(BAND_NAMES)
    p_lo = int(p_min)
    p_hi = int(p_max)

    def _get_lo(band: ee.String) -> ee.Number:
        return dynamic_stats.getNumber(ee.String(band).cat(f"_p{p_lo}"))

    def _get_hi(band: ee.String) -> ee.Number:
        return dynamic_stats.getNumber(ee.String(band).cat(f"_p{p_hi}"))

    ee_min_array = band_list.map(_get_lo)
    ee_max_array = band_list.map(_get_hi)
    min_values = ee_min_array.getInfo()
    max_values = ee_max_array.getInfo()

    if progress_callback:
        progress_callback(75, "正在计算 DEM 高程范围…")

    dem = ee.Image("NASA/NASADEM_HGT/001").select("elevation").clip(roi_bounds)
    dem_stats = dem.reduceRegion(
        reducer=ee.Reducer.minMax(),
        geometry=roi_bounds,
        scale=dem_scale,
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
        progress_callback(100, "计算完成")

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
            "roi_asset_id": params["roi_asset_id"],
            "gee_project": project or "",
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
        message=f"计算完成，已写入 {THRESHOLD_FILENAME}",
    )


__all__ = ["run", "BAND_NAMES", "THRESHOLD_FILENAME"]
