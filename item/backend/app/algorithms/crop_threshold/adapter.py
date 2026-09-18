"""Sugarcane classification - threshold extraction adapter (GEE).

Synchronously computes Sentinel-1/2 + NASADEM percentile bounds and
writes them to job_dir/threshold.json. No export task is submitted;
the result has files=[threshold.json] and metrics populated.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.algorithms.base import AlgorithmAdapter, ProgressCallback
from app.algorithms.crop_threshold import algorithm as crop_threshold_algorithm


class CropThresholdAdapter(AlgorithmAdapter):
    id = "crop_threshold"
    name = "甘蔗分类-获取阈值"
    type = "GEE"
    description = (
        "Sentinel-2 + Sentinel-1 + NASADEM 2/98 阈值提取（GEE），"
        "结果写入 job_dir/threshold.json。"
    )

    params_schema = [
        {
            "key": "year",
            "label": "年份",
            "type": "number",
            "required": True,
            "default": 2025,
        },
        {
            "key": "roi_asset_id",
            "label": "研究区/样本 GEE Asset 路径",
            "type": "text",
            "required": True,
            "hint": "形如 projects/your-project/assets/your_roi（FeatureCollection）。",
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
        return crop_threshold_algorithm.run(params, job_dir, progress_callback)
