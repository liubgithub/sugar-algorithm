"""Sixth algorithm adapter (empty stub).

Per project requirements, no logic is guessed before the real Python file
is provided. The adapter exists so the registry / API / UI flows are
exercised end-to-end.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.algorithms.base import AlgorithmAdapter, ProgressCallback, make_card, make_result


class Algorithm6Adapter(AlgorithmAdapter):
    id = "algorithm_6"
    name = "第六个算法"
    type = "LOCAL"
    description = "占位算法，等待接入真实 Python 文件。"

    params_schema = []

    def run(
        self,
        params: Dict[str, Any],
        job_dir: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        return make_result(
            status="failed",
            result_type="json",
            metrics={
                "cards": [
                    make_card("task_status", "任务状态", "未实现", ""),
                    make_card(
                        "notice",
                        "提示",
                        "adapter 尚未实现，等待真实算法接入",
                        "",
                    ),
                ],
            },
            message="adapter 尚未实现，等待真实算法接入",
        )