"""Re-attach a completed GEE task to a job whose result column is empty.

Background
----------
The old ``harvest`` implementation waited for the GEE task *inside*
``run()`` on a daemon worker thread.  When the server restarted
(uvicorn ``--reload`` does this on every file change) the waiter thread
was killed mid-flight, and because ``run()`` had not returned yet the
job's ``result`` column was still **empty** — so the platform had no
``gee_task_id`` to poll and the job sat at 92 % forever.

This script repairs such a job: it writes the same "submitted" payload
the new ``run()`` would have written, carrying the GEE task id and the
exported asset id.  From there the *normal* production path takes over —
the background GEE poller notices the task is COMPLETED and calls the
adapter's ``finalize()`` hook, which downloads the asset and writes the
final result.  Nothing about the download itself is special-cased here.

Usage::

    python scripts/recover_gee_job.py <job_id> [--task-id ID] [--asset-id ID]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.algorithms.base import make_card, make_result
from app.storage import job_store


def find_task(description: str) -> str:
    """Return the id of the COMPLETED GEE task with this description."""
    import ee

    from app.services.gee_auth import initialize_ee

    project = "guangxi-try"
    initialize_ee(project)
    for task in ee.batch.Task.list():
        cfg_desc = (task.config or {}).get("description")
        if cfg_desc != description:
            continue
        state = (task.status() or {}).get("state")
        if state == "COMPLETED":
            return task.id
    raise SystemExit(f"没有找到描述为 {description!r} 的已完成 GEE 任务")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--asset-id", default=None)
    parser.add_argument("--description", default=None)
    args = parser.parse_args()

    job = job_store.get_job(args.job_id)
    if job is None:
        raise SystemExit(f"找不到任务 {args.job_id}")

    params = job.get("params") or {}
    project = params.get("gee_project_id") or params.get("gee_project")
    year = int(params.get("year") or 0)
    description = args.description or f"Harvest_{year}_{(year + 1)}-04-21".replace(
        "-", ""
    )
    asset_id = args.asset_id or f"projects/{project}/assets/{description}"
    task_id = args.task_id or find_task(description)

    base_start = f"{year}-10-01"
    base_end = f"{year}-11-30"
    current_start = f"{year}-12-01"
    current_end = f"{year + 1}-04-21"

    metrics = {
        "year": year,
        "base_window": f"{base_start} → {base_end}",
        "current_window": f"{current_start} → {current_end}",
        "gee_task_id": task_id,
        "asset_id": asset_id,
        "description": description,
        "mask_source": "gee_asset",
        "mask_source_label": params.get("sugarcane_mask_asset") or "",
        "rvi_absolute_low": 0.6,
        "rvi_drop_min": 0.2,
        "scale": 10,
        "crs": "EPSG:4326",
        "max_pixels": int(1e13),
        "cards": [
            make_card("task_status", "任务状态", "已提交，等待 GEE 计算", ""),
            make_card("year", "目标年份", year, "年"),
            make_card("base_window", "基期窗口", f"{base_start} → {base_end}", ""),
            make_card(
                "current_window", "监测期窗口", f"{current_start} → {current_end}", ""
            ),
            make_card("rvi_low", "RVI 绝对下限", 0.6, ""),
            make_card("rvi_drop", "RVI 下降阈值", 0.2, ""),
            make_card("scale", "导出分辨率", 10, "m"),
            make_card("mask_source", "掩膜来源", "gee_asset", ""),
            make_card("gee_task_id", "GEE 任务 ID", task_id, ""),
            make_card("asset_id", "GEE 资产", asset_id, ""),
        ],
    }

    result = make_result(
        status="submitted",
        result_type="gee_task",
        gee_task_id=task_id,
        progress=10,
        metrics=metrics,
        message=(
            f"GEE 收割监测任务已提交（task_id={task_id}，asset={asset_id}，"
            f"年份={year}）。云端计算完成后会自动拉取结果。"
        ),
    )

    job_store.update_job(
        args.job_id,
        status="submitted",
        progress=10,
        message=(
            f"已接回 GEE 任务 {task_id}（{description}），"
            "等待后台拉取结果"
        ),
        result=result,
    )
    print(f"任务 {args.job_id} 已接回 GEE 任务 {task_id}，后台轮询将自动拉取结果。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
