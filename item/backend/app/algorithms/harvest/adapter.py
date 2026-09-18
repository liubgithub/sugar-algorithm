"""Sugarcane harvest detection adapter (GEE).

Delegates to algorithm.run() which submits an Export.image.toDrive task and
returns the GEE task id immediately. Job service polls GEE for status.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.algorithms.base import AlgorithmAdapter, ProgressCallback
from app.algorithms.harvest import algorithm as harvest_algorithm


class HarvestAdapter(AlgorithmAdapter):
    id = "harvest"
    name = "甘蔗收割"
    type = "GEE"
    description = "基于 Sentinel-1 RVI/NDPI/Texture 的甘蔗收割检测，导出至 Google Drive。"

    params_schema = [
        {
            "key": "roi_bounds",
            "label": "研究区边界 [minLon,minLat,maxLon,maxLat]",
            "type": "text",
            "default": "104.295,21.069,112.358,26.466",
            "required": True,
            "hint": "4 个数字，逗号分隔，按 minLon,minLat,maxLon,maxLat 顺序。",
        },
        {
            "key": "sugarcane_mask_asset",
            "label": "甘蔗掩膜 GEE Asset",
            "type": "text",
            "default": "projects/your-project/assets/sugarcane_mask",
            "required": True,
        },
        {
            "key": "base_start",
            "label": "基期开始 (YYYY-MM-DD)",
            "type": "date",
            "default": "2025-10-01",
            "required": True,
        },
        {
            "key": "base_end",
            "label": "基期结束 (YYYY-MM-DD)",
            "type": "date",
            "default": "2025-11-30",
            "required": True,
        },
        {
            "key": "current_start",
            "label": "监测期开始 (YYYY-MM-DD)",
            "type": "date",
            "default": "2025-12-01",
            "required": True,
        },
        {
            "key": "current_end",
            "label": "监测期结束 (YYYY-MM-DD)",
            "type": "date",
            "default": "2026-04-21",
            "required": True,
        },
        {
            "key": "rvi_absolute_low",
            "label": "RVI_ABSOLUTE_LOW",
            "type": "number",
            "default": 0.6,
            "required": False,
        },
        {
            "key": "rvi_drop_min",
            "label": "RVI_DROP_MIN",
            "type": "number",
            "default": 0.2,
            "required": False,
        },
        {
            "key": "scale",
            "label": "导出 scale (m)",
            "type": "number",
            "default": 10,
            "required": False,
        },
        {
            "key": "crs",
            "label": "导出 CRS",
            "type": "text",
            "default": "EPSG:4326",
            "required": False,
        },
        {
            "key": "export_folder",
            "label": "Drive 目标文件夹",
            "type": "text",
            "default": "GEE_Exports",
            "required": False,
        },
        {
            "key": "export_description",
            "label": "导出任务描述",
            "type": "text",
            "default": "Export_Harvest",
            "required": False,
        },
        {
            "key": "gee_project",
            "label": "GEE Project（可选，留空使用 EE_PROJECT 环境变量）",
            "type": "text",
            "required": False,
        },
    ]

    def run(
        self,
        params: Dict[str, Any],
        job_dir: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        return harvest_algorithm.run(params, job_dir, progress_callback)