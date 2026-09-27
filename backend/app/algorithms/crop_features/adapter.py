"""Crop time-series feature extraction adapter (GEE).

按需求文档：
- 算法显示名：作物时序特征提取 / 广西甘蔗分类--特征提取
- 算法 ID：crop_features
  （早期开发曾用 ``time-series-features``，已统一改回与目录命名对齐的
  ``crop_features``，以避免 ``algorithm_data/{crop_features,time-series-features}``
  两份同名算法缓存分裂的问题。）
- 输入：GEE Project ID、年份、本地 CSV 样本点文件（含 longitude / latitude）
- 输出：CSV via Export.table.toAsset（自动下载回本地供前端展示）
- ROI 直接由样本点几何范围派生，无需单独上传研究区
- 固定参数：min_values / max_values / DEM 范围 等来自
  ``app.algorithms.configs.time_series_config``，仅在前端以只读 notice 形式
  展示，不允许用户修改
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.algorithms.base import AlgorithmAdapter, ProgressCallback
from app.algorithms.configs import time_series_config as cfg
from app.algorithms.crop_features import algorithm as crop_features_algorithm


class CropFeaturesAdapter(AlgorithmAdapter):
    id = "crop_features"
    name = "广西甘蔗分类--特征提取"
    type = "GEE"
    description = (
        "输入 CSV 样本点（含 longitude / latitude）， "
        "基于Sentinel-1、Sentinel-2、DEM，构建 12 月 × 11 通道 + 高程的133 个时序特征 "
        "需经一定时间等待才可查看特征提取生成表csv"
    )

    params_schema = [
        # ===== 一、基础配置（用户输入）=====
        {
            "key": "gee_project_id",
            "label": "GEE 项目 ID",
            "type": "text",
            "required": True,
            "default": "sodium-ray-505904-i3",
            "hint": (
                "用于 ee.Initialize(project=...)。"
                "如需切换项目，请同时在本机执行 earthengine authenticate --force。"
            ),
        },
        {
            "key": "year",
            "label": "年份",
            "type": "number",
            "required": True,
            "default": 2025,
            "hint": "用于筛选 Sentinel-1 / Sentinel-2 数据的时间范围。",
        },
        # ===== 二、样本点文件（必填，阶段 2 实现自动注册 GEE Asset）=====
        {
            "key": "samples_local_path",
            "label": "样本点文件（CSV，含经纬度）",
            "type": "file",
            "accept": ".csv",
            "kind": "file",
            "slot": "samples",
            "required": False,
            "hint": (
                "上传本地 CSV 文件，UTF-8 编码，"
                "首行为英文表头，必须包含 longitude 与 latitude 两列"
                "（大小写不限，例如 longitude / Longitude / LONGITUDE 与 "
                "latitude / Latitude / LATITUDE），"
                "其它列会作为样本点的属性一并上传到 GEE。"
                "研究区范围 = 样本点几何范围，无需单独上传。"
                "留空则使用下方『已上传历史文件』中选一个，或在"
                "「GEE 样本点 Asset ID」文本框直接粘贴 Asset ID。"
            ),
            # 与 samples_asset_id 互斥：选择其一后，前端会自动清空另一个。
            "xors_with": ["samples_asset_id"],
        },
        # ===== 二.bis 直接填写 GEE Asset ID（与本地 CSV 二选一）=====
        # 文本框：用户可以直接粘贴 `projects/<project>/assets/<name>` 跳过
        # 本地 CSV 上传。后端 run() 会在收到该字段时做一次存在/可读校验，
        # 失败时给出明确报错而不会白等 30 分钟才发现失败。
        #
        # `saved_picker` 块声明下方会渲染「查看已保存 GEE Assets」复选框 +
        # 列表（资产 ID / 创建时间 / 使用 / 删除记录），记录由后端
        # `job_store.save_gee_asset()` 持久化，删除仅删除本系统的本地指针，
        # 不会触碰 GEE 中真实 Asset。
        {
            "key": "samples_asset_id",
            "label": "GEE 样本点 Asset ID",
            "type": "text",
            "required": False,
            "default": "",
            "hint": (
                "格式：projects/<project>/assets/<name>。"
                "填写后将跳过本地 CSV 上传，直接复用此 Asset；"
                "留空则使用上方的 CSV 上传或已上传历史文件。"
                "提交后会做一次存在/可访问性校验。"
            ),
            "xors_with": ["samples_local_path"],
            "saved_picker": {
                "target_key": "samples_asset_id",
                "label": "查看已保存 GEE Asset",
                "checkbox_label": "查看已保存的 GEE Assets",
                "endpoint": "/api/algorithms/{algorithm_id}/saved-gee-assets",
                "verify_endpoint": "/api/algorithms/{algorithm_id}/saved-gee-assets/verify",
            },
        },
        # ===== 三、固定参数（仅展示，不可修改）=====
        {
            "key": "__fixed_params_notice__",
            "type": "notice",
            "notice_type": "info",
            "notice_icon": "ℹ️",
            "label": "固定参数（仅展示，不可修改）",
            "hint": (
                f"本算法共输出 {cfg.TOTAL_FEATURE_COUNT} 个特征"
                f"（{cfg.FEATURE_BAND_COUNT} 个波段 × {cfg.TIME_SERIES_MONTHS} 个月 "
                f"+ 1 个 DEM 高程）。\n"
                f"min_values 长度 = {len(cfg.MIN_VALUES)}；"
                f"max_values 长度 = {len(cfg.MAX_VALUES)}；"
                f"DEM 归一化区间 [{cfg.DEM_MIN}, {cfg.DEM_MAX}]；"
                f"Sentinel-2 最大云量 ≤ {cfg.CLOUD_PCT_MAX}%。\n"
                "以上参数由算法内置，禁止用户修改。"
            ),
        },
        # ===== 四、高级（默认折叠）=====
        {
            "key": "cloud_pct_max",
            "label": "Sentinel-2 最大云量 (%)",
            "type": "number",
            "required": False,
            "default": cfg.CLOUD_PCT_MAX,
            "advanced": True,
            "hint": "CLOUDY_PIXEL_PERCENTAGE 阈值，0-100 之间。",
        },
        {
            "key": "export_description",
            "label": "GEE 任务名称（留空自动）",
            "type": "text",
            "required": False,
            "hint": (
                "默认按『TimeSeriesFeatures_<年份>』自动命名，"
                "后端会自动追加时间戳以避免 GEE Asset 冲突。"
                "本名称会用作下载 CSV 的文件名（不含时间戳）。"
            ),
        },
    ]

    def run(
        self,
        params: Dict[str, Any],
        job_dir: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        return crop_features_algorithm.run(params, job_dir, progress_callback)

    def finalize(
        self,
        job: Dict[str, Any],
        job_dir: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Optional[Dict[str, Any]]:
        # Delegate to the algorithm module's finalize() so the dispatcher in
        # job_service._dispatch_finalize can pick it up via getattr(adapter,
        # "finalize"). Without this override the base class's no-op
        # implementation would shadow the real one and Pattern B would
        # never actually download the CSV.
        return crop_features_algorithm.finalize(
            job, job_dir, progress_callback=progress_callback
        )