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
    name = "广西甘蔗分类--获取阈值"
    type = "GEE"
    description = (
        "输入广西边界矢量，"
        "提取Sentinel-1 + Sentinel-2 的 11 个波段特征阈值，"
        "提取NASADEM 高程范围（计算最大值与最小值）。"
    )

    params_schema = [
        {
            "key": "gee_project",
            "label": "GEE 项目 ID",
            "type": "text",
            "required": False,
            "default": "sodium-ray-505904-i3",
            
        },
        {
            "key": "roi_local_path",
            "label": "研究区文件（请打包为 .zip 上传）",
            "type": "file",
            "accept": ".zip,.geojson,.json",
            "kind": "zip",
            "slot": "roi",
            "required": False,
            "hint": (
                "Shapefile 至少需要 .shp + .shx + .dbf 三件套同时上传，"
                "请把所有同伴文件一起打包为 .zip 再上传；"
                "后端会自动解压并把首个 .shp 注册为 GEE 资产。"
                ".geojson / .json 也可直接上传；"
                "栅格（.tif）请改用下方『研究区 GEE Asset 路径』字段，"
                "在 GEE Code Editor 上传后填入。"
            ),
        },
        {
            "key": "roi_asset_id",
            "label": "研究区 GEE Asset 路径（可选，留空则使用上方上传文件）",
            "type": "text",
            "required": False,
            "default": "",
         
        },
        {
            "key": "year",
            "label": "数据年份",
            "type": "number",
            "required": True,
            "default": 2025,
            "hint": (
               "自动选择相应年份（YYYY-01-01 ~ YYYY-12-31）时间段的数据。"
               
            ),
        },
        # 其他参数 —— UI 上折叠展示；默认值与原硬编码完全一致，用户不动时行为不变。
        {
            "key": "cloud_max",
            "label": "Sentinel-2 最大云量 (%)",
            "type": "number",
            "required": False,
            "default": 60,
            "advanced": True,
            "hint": "CLOUDY_PIXEL_PERCENTAGE 阈值，0-100 之间。",
        },
        {
            "key": "percentile_min",
            "label": "最小百分位 (%)",
            "type": "number",
            "required": False,
            "default": 2,
            "advanced": True,
            "hint": "动态特征范围的下分位数。",
        },
        {
            "key": "percentile_max",
            "label": "最大百分位 (%)",
            "type": "number",
            "required": False,
            "default": 98,
            "advanced": True,
            "hint": "动态特征范围的上分位数。",
        },
        {
            "key": "dynamic_scale",
            "label": "动态极值计算尺度 (m)",
            "type": "number",
            "required": False,
            "default": 5000,
            "advanced": True,
            "hint": "GEE reduceRegion 的 scale，用于 Sentinel min/max 计算。",
        },
        {
            "key": "dem_scale",
            "label": "DEM 计算尺度 (m)",
            "type": "number",
            "required": False,
            "default": 500,
            "advanced": True,
            "hint": "GEE reduceRegion 的 scale，用于 NASADEM min/max 计算。",
        },
    ]

    def run(
        self,
        params: Dict[str, Any],
        job_dir: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        return crop_threshold_algorithm.run(params, job_dir, progress_callback)
