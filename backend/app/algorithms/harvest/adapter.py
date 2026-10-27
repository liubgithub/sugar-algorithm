"""Sugarcane SAR harvest detection adapter (GEE).

Simplified user-facing flow: GEE project id, year, sugarcane classification
mask (either a local 1-band TIFF OR a GEE Asset path — the two are
mutually exclusive). The algorithm

  - auto-derives the base (Oct-Nov) and monitoring (Dec-Apr next year)
    windows from `year`;
  - reads the sugarcane mask (either the local TIFF via rasterio or
    the pre-uploaded GEE Asset), constructs an `ee.Image` with proper
    WGS84 georeferencing, and uses the mask extent as the SAR analysis
    region (filterBounds + export region) — there is no separate
    research-area upload;
  - runs the SAR pipeline (RVI / NDPI / VH texture, base median vs
    current min, RVI thresholding);
  - submits the harvest mask through `Export.image.toAsset`, waits for
    it, and downloads the GeoTIFF back into the job directory so the
    platform's `ResultRenderer` can preview / download it directly.

All other algorithm parameters (RVI thresholds, scale, CRS) are baked
in as defaults and hidden behind an advanced collapsible section.

GEE Asset is strongly recommended for very large masks (>> 80亿解压像素
/ > 500 tiles under the local-tile strategy), where client-side
`ee.Image(arr).reproject(...)` tiling becomes prohibitively slow on the
GEE server side.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.algorithms.base import AlgorithmAdapter, ProgressCallback
from app.algorithms.harvest import algorithm as harvest_algorithm


class HarvestAdapter(AlgorithmAdapter):
    id = "harvest"
    name = "甘蔗 SAR 收割监测"
    type = "GEE"
    description = (
        "基于 Sentinel-1 RVI/NDPI/Texture 的甘蔗收割检测。"
        "仅需选择 GEE 项目、年份并上传本地甘蔗分类掩膜 TIFF，"
        "其他参数使用算法内置默认值；"
        "结果导出至本地 job_dir，可在平台内直接预览和下载。"
    )

    params_schema = [
        {
            "key": "gee_project_id",
            "label": "GEE 项目 ID",
            "type": "text",
            "required": True,
            # 刻意留空，不要预填任何具体项目。ee 的项目绑定是进程全局的，
            # 预填值会在表单一加载、用户还没点运行时就把整个后端的 EE 切走，
            # 污染同一进程里其它算法 / 其它任务的请求。
            "default": "",
            "placeholder": "例如：my-gee-project-123456",
            "hint": (
                "用于 ee.Initialize(project=...)，当前 GEE 账号必须对该项目有访问权限。"
                "如需切换项目，请先在本机执行 earthengine authenticate --force，"
                "再回来填这里的新项目 ID。"
            ),
        },
        {
            "key": "year",
            "label": "年份",
            "type": "number",
            "required": True,
            "default": 2025,
            "hint": (
                "用于自动计算基期（10/1 - 11/30）和监测期"
                "（12/1 - 次年 4/21）窗口。"
            ),
        },
        {
            "key": "sugarcane_mask_local_path",
            "label": "本地甘蔗分类掩膜 TIFF（单波段，1=甘蔗/0=非甘蔗）",
            "type": "file",
            "accept": ".tif,.tiff",
            "kind": "file",
            "slot": "sugarcane_mask",
            "required": False,
           
        },
        # 时间窗口提醒卡片 —— 由前端在用户修改年份时动态渲染。
        # 该字段不在 params 中提交，仅用于在表单里给出可视化的反馈。
        {
            "key": "__window_notice__",
            "type": "notice",
            "notice_type": "info",
            "notice_icon": "📅",
            "label": "时间窗口（自动）",
            "hint": "请先在上方选择年份，系统将自动计算基期与监测期窗口。",
        },
        # 基础参数（不再放在高级里）—— 与上方本地 TIFF 二选一
        {
            "key": "sugarcane_mask_asset",
            "label": "GEE Asset 路径（与上方本地 TIFF 二选一，留空则用上方）",
            "type": "text",
            "default": "",
            "hint": (
                "形如 projects/<project>/assets/<name>。"
                "【推荐用于超大掩膜】"
            ),
        },
    ]

    def run(
        self,
        params: Dict[str, Any],
        job_dir: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        return harvest_algorithm.run(params, job_dir, progress_callback)

    def finalize(
        self,
        job: Dict[str, Any],
        job_dir: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Optional[Dict[str, Any]]:
        """拉取已完成的 GEE 结果（平台后台轮询在任务 COMPLETED 后调用）。

        run() 只负责提交导出任务并立刻返回 task_id；这里负责等待资产就绪、
        分块下载到本地并产出最终结果。这样即使 GEE 端跑十几个小时、
        中途服务器重启，结果依然会被拉回来。
        """
        return harvest_algorithm.finalize(job, job_dir, progress_callback)
