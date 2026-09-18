"""Local yield estimation algorithm adapter.

Delegates to algorithm.run() which wraps the legacy script. Math is
preserved exactly in app.algorithms.yield_local.legacy_script.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.algorithms.base import AlgorithmAdapter, ProgressCallback
from app.algorithms.yield_local import algorithm as yield_algorithm


class YieldLocalAdapter(AlgorithmAdapter):
    id = "yield_local"
    name = "本地持续估产2"
    type = "LOCAL"
    description = (
        "本地持续估产：基于 Excel 建模表 + 月度 GeoTIFF，"
        "训练 Ridge 模型并分块写出最终估产结果。"
    )

    params_schema = [
        {
            "key": "excel_path",
            "label": "建模 Excel",
            "type": "file",
            "accept": ".xlsx,.zip",
            "kind": "file",
            "slot": "excel",
            "required": True,
        },
        {
            "key": "raster_dir",
            "label": "月度 GeoTIFF 目录（可选本地目录或 ZIP 压缩包）",
            "type": "file",
            "accept": ".zip",
            "kind": "directory",
            "slot": "rasters",
            "group_by": "target_month",
            "directory_picker": True,
            "required": True,
        },
        {
            "key": "cane_mask_path",
            "label": "甘蔗掩膜 GeoTIFF（可选）",
            "type": "file",
            "accept": ".tif,.tiff",
            "kind": "file",
            "slot": "mask",
            "required": False,
        },
        {
            "key": "sheet_name",
            "label": "Excel 工作表名",
            "type": "text",
            "default": "Monthly_With_Yield_Area",
            "required": False,
        },
        {
            "key": "target_month",
            "label": "目标月份 (YYYY-MM)",
            "type": "text",
            "default": "2026-06",
            "required": True,
        },
        {
            "key": "train_start_year",
            "label": "训练起始榨季",
            "type": "number",
            "default": 2020,
            "required": True,
        },
        {
            "key": "train_end_year",
            "label": "训练结束榨季",
            "type": "number",
            "default": 2024,
            "required": True,
        },
        {
            "key": "block_size",
            "label": "分块窗口像素",
            "type": "number",
            "default": 512,
            "required": False,
        },
        {
            "key": "raster_template",
            "label": "输入 tif 命名模板",
            "type": "text",
            "default": "{year}_{month:02d}_{feature}.tif",
            "required": False,
        },
        {
            "key": "output_filename",
            "label": "最终结果文件名",
            "type": "text",
            "default": "yield_final.tif",
            "required": False,
        },
    ]

    def run(
        self,
        params: Dict[str, Any],
        job_dir: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        return yield_algorithm.run(params, job_dir, progress_callback)