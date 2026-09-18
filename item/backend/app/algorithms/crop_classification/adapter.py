"""Sugarcane classification adapter (GEE).

Trains a RandomForest on a labeled sample asset, evaluates via
errorMatrix (OA / Kappa / confusion matrix), classifies the full ROI
and exports a raster through Export.image.toDrive. Returns the GEE
task id plus the small accuracy metrics.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.algorithms.base import AlgorithmAdapter, ProgressCallback
from app.algorithms.crop_classification import algorithm as crop_classification_algorithm


class CropClassificationAdapter(AlgorithmAdapter):
    id = "crop_classification"
    name = "甘蔗分类-分类"
    type = "GEE"
    description = (
        "使用 Random Forest 对甘蔗样本进行训练、验证与全图分类，"
        "导出至 Google Drive（GEE），返回 OA / Kappa / 混淆矩阵。"
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
            "hint": "形如 projects/your-project/assets/your_roi。",
        },
        {
            "key": "feature_asset",
            "label": "样本点 GEE Asset 路径（含 class 列）",
            "type": "text",
            "required": True,
            "hint": "形如 projects/your-project/assets/your_samples。",
        },
        {
            "key": "label_property",
            "label": "类别字段名",
            "type": "text",
            "default": "class",
            "required": False,
        },
        {
            "key": "num_trees",
            "label": "RandomForest 树数",
            "type": "number",
            "default": 100,
            "required": False,
        },
        {
            "key": "train_ratio",
            "label": "训练集比例",
            "type": "number",
            "default": 0.8,
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
        return crop_classification_algorithm.run(params, job_dir, progress_callback)
