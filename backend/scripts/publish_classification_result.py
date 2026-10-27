"""Publish an already-downloaded classification result as a finished job.

The download and the *publish* are two different things:

  * **download** — pull the GEE asset into a local GeoTIFF.  Slow, and the
    part that dies when the proxy hiccups.  Done by
    ``scripts/download_gee_asset.py`` (resumable) or by the algorithm's
    ``finalize()`` hook.
  * **publish** — turn the GeoTIFF on disk into a ``completed`` job row
    plus a rendered preview.  Instant, and never touches the network.

This script does only the second half.  It reads the job row, calls the
algorithm's ``build_completed_result()`` to assemble the result payload,
writes it to the database and pre-renders the preview PNG / ``.info.json``
so the web UI shows the map on the first request.

It never downloads anything, never renames or deletes existing files, and
never overwrites a job that is already ``completed``.

Usage from ``backend/``::

    python scripts/publish_classification_result.py [job_id]
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import DB_PATH, JOBS_DIR  # noqa: E402
from app.services import preview_service  # noqa: E402


def load_job(job_id: str) -> dict:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"job not found: {job_id}")
    job = dict(row)
    job["result"] = json.loads(job["result"] or "{}")
    job["params"] = json.loads(job["params"] or "{}")
    return job


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    job_id = args[0] if args else "595bc31b8ace42e28e3b251c386c11f4"
    job = load_job(job_id)
    if job["status"] == "completed" and not force:
        raise SystemExit(
            f"任务 {job_id} 已经是 completed 状态，不覆盖。"
            f"确需重建请加 --force。"
        )

    algorithm_id = job["algorithm"]
    if algorithm_id != "crop_classification":
        raise SystemExit(f"本脚本只支持 crop_classification，收到 {algorithm_id}")

    from app.algorithms.crop_classification.algorithm import build_completed_result

    job_dir = os.path.join(str(JOBS_DIR), job_id)
    payload = build_completed_result(job, job_dir)
    message = payload.get("message") or "结果已发布"

    now = datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "UPDATE jobs SET status = 'completed', progress = 100, message = ?, "
            "result = ?, finished_at = ?, updated_at = ? WHERE id = ?",
            (
                message,
                json.dumps(payload, ensure_ascii=False),
                now,
                now,
                job_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"job     : {job_id}")
    print(f"status  : completed / 100%")
    print(f"message : {message}")

    for entry in payload.get("files") or []:
        name = entry.get("name")
        if not name:
            continue
        info = preview_service.get_info(job_id, name)
        print(
            f"  preview {name}: {info.get('preview_width')}×{info.get('preview_height')} "
            f"bounds={info.get('bounds')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
