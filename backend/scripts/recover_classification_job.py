"""Re-pull the GEE result of a job that failed in the *finalize* stage.

Background
----------
A Pattern-B job (``crop_classification`` …) submits a GEE export, then a
background poller downloads the finished Asset into a local GeoTIFF.  When
the download dies — typically because the machine's proxy node went down
mid-transfer — the job row is left at ``failed`` and the poller refuses to
touch it again (terminal rows are never re-probed).  The GEE side is fine:
the Asset is still there, fully computed.

This script performs the missing half of the recovery **offline**:

  1. moves the half-written GeoTIFF of the failed attempt aside,
  2. re-runs the adapter's ``finalize()`` hook, which uses the resumable
     tiled downloader (already-downloaded tiles are kept),
  3. writes the resulting payload back to the job row
     (status ``completed``, progress 100),
  4. pre-renders the preview PNG + ``.info.json`` so the web UI shows the
     map right away instead of on the first request.

Run it **while the API server is stopped** — the server's own GEE poller
would otherwise start a second download of the same asset into the same
``.partial`` file and corrupt it.  (If you keep the server up, use
``POST /api/jobs/{id}/retry`` instead: that path goes through the job
service's in-flight guard.)

Usage from ``backend/``::

    python scripts/recover_classification_job.py [job_id]
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import DB_PATH, JOBS_DIR  # noqa: E402
from app.services import algorithm_registry  # noqa: E402
from app.services import preview_service  # noqa: E402
from app.services.gee_auth import gee_session, project_for_job  # noqa: E402


DEFAULT_JOB = "595bc31b8ace42e28e3b251c386c11f4"


def load_job(job_id: str) -> Dict[str, Any]:
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


def write_job_result(job_id: str, payload: Dict[str, Any], message: str) -> None:
    """Persist a completed result payload + status back into the job row."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            "UPDATE jobs SET status = 'completed', progress = 100, message = ?, "
            "result = ?, finished_at = ?, updated_at = ? WHERE id = ?",
            (
                message,
                json.dumps(payload, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
                datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
                job_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def quarantine_stale_raster(job_dir: str, filenames) -> None:
    """Move the half-written GeoTIFF(s) of the failed attempt aside.

    The failed attempt wrote *directly* to the final name (the old
    downloader had no partial-file discipline), so a leftover file there is
    an incomplete raster.  Left in place it would be picked up by the
    preview service and rendered as a mostly-empty map — exactly the
    confusing artefact we are trying to get rid of.
    """
    for name in filenames:
        path = os.path.join(job_dir, name)
        if not os.path.isfile(path):
            continue
        broken = path + ".broken"
        try:
            if os.path.exists(broken):
                os.remove(broken)
            shutil.move(path, broken)
            print(f"  旧的不完整文件已挪走: {broken}")
        except OSError as exc:
            print(f"  (挪走旧文件失败，忽略: {exc})")


def main() -> int:
    job_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_JOB
    job = load_job(job_id)
    result = job["result"]
    metrics = result.get("metrics") or {}

    asset_id = str(metrics.get("asset_id") or "").strip()
    if not asset_id:
        raise SystemExit(
            "该任务没有 asset_id —— GEE 侧可能根本没跑完，只能重新提交算法，"
            "本脚本不适用。"
        )

    algorithm_id = job["algorithm"]
    adapter = algorithm_registry.get_algorithm(algorithm_id)
    finalize = getattr(adapter, "finalize", None)
    if finalize is None:
        raise SystemExit(f"算法 {algorithm_id} 没有 finalize hook，无法离线恢复")

    job_dir = os.path.join(str(JOBS_DIR), job_id)
    os.makedirs(job_dir, exist_ok=True)

    print(f"job      : {job_id} ({algorithm_id})")
    print(f"status   : {job['status']} / {job['progress']}%")
    print(f"asset    : {asset_id}")
    print(f"job_dir  : {job_dir}")

    # 1. 旧的不完整文件挪走
    previous_files = [f.get("name") for f in (result.get("files") or []) if f.get("name")]
    tif_name = str(metrics.get("output_tif_name") or "")
    if tif_name and tif_name not in previous_files:
        previous_files.append(tif_name)
    quarantine_stale_raster(job_dir, previous_files)

    # 2. 重新拉取
    def _progress(pct: int, message: str) -> None:
        print(f"[{pct:3d}%] {message}", flush=True)

    project = project_for_job(job)
    print(f"project  : {project}")
    print("\n--- 开始下载 ---", flush=True)
    with gee_session(project):
        new_result = finalize(job, job_dir, _progress)
    if new_result is None:
        raise SystemExit("finalize() 没有返回结果，无法恢复")

    # 3. 写回数据库
    message = new_result.get("message") or "结果已恢复"
    write_job_result(job_id, new_result, message)
    print(f"\n已写回任务状态: completed / 100%")
    print(f"  {message}")

    # 4. 预渲染预览
    files = new_result.get("files") or []
    for entry in files:
        name = entry.get("name")
        if not name:
            continue
        info = preview_service.get_info(job_id, name)
        print(
            f"  preview {name}: {info.get('preview_width')}x{info.get('preview_height')} "
            f"bounds={info.get('bounds')}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1)
