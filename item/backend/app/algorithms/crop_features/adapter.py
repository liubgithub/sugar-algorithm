"""Sugarcane classification - monthly feature generation adapter (GEE).

Takes the threshold JSON from crop_threshold (uploaded via
/api/uploads), builds the 12-month time-series stack and exports a
CSV through Export.table.toDrive. Returns gee_task_id immediately.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.algorithms.base import AlgorithmAdapter, ProgressCallback
from app.algorithms.crop_features import algorithm as crop_features_algorithm


class CropFeaturesAdapter(AlgorithmAdapter):
    id = "crop_features"
    name = "甘蔗分类-生成特征"
    type = "GEE"
    description = (
        "按样本点生成 12 个月时序特征并通过 Export.table.toDrive 输出 CSV（GEE）。"
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
            "label": "研究区 GEE Asset 路径",
            "type": "text",
            "required": True,
            "hint": "形如 projects/your-project/assets/your_roi（FeatureCollection）。",
        },
        {
            "key": "samples_asset_id",
            "label": "样本点 GEE Asset 路径",
            "type": "text",
            "required": True,
            "hint": "形如 projects/your-project/assets/your_samples（FeatureCollection）。",
        },
        {
            "key": "label_property",
            "label": "样本类别字段名",
            "type": "text",
            "default": "class",
            "required": False,
        },
        {
            "key": "threshold_json_path",
            "label": "阈值 JSON（来自 crop_threshold）",
            "type": "file",
            "accept": ".json",
            "kind": "file",
            "slot": "threshold_json",
            "required": True,
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
        return crop_features_algorithm.run(params, job_dir, progress_callback)
