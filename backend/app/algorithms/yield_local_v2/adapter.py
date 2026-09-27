"""Local yield estimation algorithm adapter (v2 — optimized).

Wraps the optimized legacy script in yield_local_v2.legacy_script. Differs from
yield_local (v1) in: fixed Ridge α (no RidgeCV), 16 dynamic features + 4 history
features folded, recent-regime correction with `regime_x__` interactions, and
blockwise WarpedVRT reprojection for grid alignment.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.algorithms.base import AlgorithmAdapter, ProgressCallback
from app.algorithms.yield_local_v2 import algorithm as yield_v2_algorithm


class YieldLocalV2Adapter(AlgorithmAdapter):
    id = "yield_local_v2"
    name = "广西26持续估产2"
    type = "LOCAL"
    description = (
        "基于县级历史产量与逐月遥感/气象数据训练带近期趋势校正的Ridge模型， "
        "并对目标月份GeoTIFF在甘蔗类别像元上逐像元预测甘蔗单产，输出估产栅格 "
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
            "label": "甘蔗掩膜 GeoTIFF（必填，脚本默认路径在生产环境不存在）",
            "type": "file",
            "accept": ".tif,.tiff",
            "kind": "file",
            "slot": "mask",
            "required": True,
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
            "default": "2026-6",
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
            "default": 2025,
            "required": True,
        },
        {
            "key": "allow_target_in_training",
            "label": "允许目标榨季进入训练集（外推任务请关闭）",
            "type": "boolean",
            "default": False,
            "required": False,
        },
        {
            "key": "regime_start_year",
            "label": "近期 regime 起始榨季",
            "type": "number",
            "default": 2025,
            "required": False,
            "hint": "crop_year >= 该值时启用 regime 校正项。",
        },
        {
            "key": "ridge_alpha",
            "label": "Ridge α（固定值，不做 CV）",
            "type": "number",
            "default": 0.30,
            "required": False,
        },
        {
            "key": "final_result_scale",
            "label": "最终结果缩放系数",
            "type": "number",
            "default": 0.0565,
            "required": False,
        },
        {
            "key": "cane_mask_values",
            "label": "掩膜保留类别（逗号分隔，如 1 或 1,255）",
            "type": "text",
            "default": "1",
            "required": False,
        },
        {
            "key": "no_cane_mask",
            "label": "关闭甘蔗掩膜",
            "type": "boolean",
            "default": False,
            "required": False,
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
            "default": "yield_v2_final.tif",
            "required": False,
        },
        {
            "key": "raw_output_filename",
            "label": "原始结果文件名",
            "type": "text",
            "default": "yield_v2_raw.tif",
            "required": False,
        },
    ]

    def run(
        self,
        params: Dict[str, Any],
        job_dir: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        return yield_v2_algorithm.run(params, job_dir, progress_callback)