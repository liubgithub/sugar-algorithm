"""Demo algorithm adapter.

Used to validate the entire job lifecycle in phase 1: enqueue -> running
with progress -> completed. Sleeps for a few seconds and writes a tiny
JSON file into job_dir.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

from app.algorithms.base import AlgorithmAdapter, ProgressCallback, make_card, make_result


class DemoAdapter(AlgorithmAdapter):
    id = "demo"
    name = "Demo 算法"
    type = "LOCAL"
    description = "用于验证任务流程的演示算法，会分阶段报告进度。"

    params_schema = [
        {"key": "message", "label": "备注信息", "type": "text", "default": "hello"},
        {"key": "duration_seconds", "label": "运行时长（秒）", "type": "number", "default": 3},
    ]

    def run(
        self,
        params: Dict[str, Any],
        job_dir: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Dict[str, Any]:
        message = str(params.get("message", "hello"))
        duration = max(1, int(params.get("duration_seconds", 3)))
        os.makedirs(job_dir, exist_ok=True)

        steps = 5
        started_at = time.time()
        for i in range(1, steps + 1):
            time.sleep(duration / steps)
            pct = int(i * 100 / steps)
            if progress_callback:
                progress_callback(pct, f"步骤 {i}/{steps}: {message}")
        elapsed = round(time.time() - started_at, 3)
        finished_at_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"

        output_path = os.path.join(job_dir, "demo_result.json")
        payload = {
            "message": message,
            "duration_seconds": duration,
            "finished_at": finished_at_iso,
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        return make_result(
            status="completed",
            result_type="json",
            files=[
                {
                    "name": "demo_result.json",
                    "path": output_path,
                    "size_bytes": os.path.getsize(output_path),
                }
            ],
            metrics={
                "steps": steps,
                "duration_seconds": duration,
                "elapsed_seconds": elapsed,
                "finished_at": finished_at_iso,
                "cards": [
                    make_card("task_status", "任务状态", "完成", ""),
                    make_card("steps", "总步数", steps, "步"),
                    make_card("duration", "运行时长", duration, "秒"),
                    make_card("elapsed", "实际耗时", elapsed, "秒"),
                    make_card("finished_at", "完成时间", finished_at_iso, ""),
                ],
            },
            message="demo 运行完成",
        )