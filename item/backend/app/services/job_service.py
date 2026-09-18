"""Job service: creates jobs, runs LOCAL algorithms in a worker thread,
and updates the job store as the algorithm reports progress.

For GEE algorithms the adapter returns immediately with gee_task_id; the
service then queries ee.data.getTaskStatus on subsequent get_job() calls
to mirror GEE state (READY/RUNNING/COMPLETED/FAILED/CANCELLED) into the
job record. The query is throttled to one call per few seconds per job.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import sys
import threading
import time
import traceback
from typing import Any, Dict, Iterator, List, Optional

from app.core.config import JOBS_DIR
from app.services import algorithm_registry
from app.services import preview_service
from app.storage import job_store


# Cache so we don't hammer the GEE API on every poll.
_GEE_CHECK_INTERVAL = 3.0  # seconds
_last_gee_check: Dict[str, float] = {}

# Map GEE Task state -> our internal status.
_GEE_STATE_MAP = {
    "READY": "submitted",
    "RUNNING": "running",
    "CANCELLING": "failed",
    "CANCELLED": "failed",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "UNKNOWN": "submitted",
}

LOG_FILENAME = "stdout.log"


def _utc_now_iso() -> str:
    """ISO 字符串（UTC），与 job_store._now() 同口径。"""
    from datetime import datetime
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


# 从 stdout 兜底提取简单 key=value / key: value 模式；适配未来忘记主动
# 构造 metrics 的旧算法时能捞回一点数字；当前 6 个适配器都主动返回 metrics，
# 这个函数不会被触发，但留着以防适配器忘写。
_LOG_METRIC_PATTERNS: List[re.Pattern] = [
    re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _\-]{1,40}?)\s*[:=]\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([A-Za-z%/]+)?\s*$"),
]


def extract_metrics_from_log(stdout_text: str) -> List[Dict[str, Any]]:
    """扫描 stdout 提取形如 'Key: 1.23 unit' / 'key = 4 unit' 的条目。

    仅作为旧算法兜底；前端优先用 `metrics.cards`，不会调到这里。
    """
    if not stdout_text:
        return []
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LOG_METRIC_PATTERNS[0].match(line)
        if not m:
            continue
        label, value, unit = m.group(1).strip(), m.group(2), m.group(3)
        key = label.lower().replace(" ", "_")
        if key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "label": label, "value": value, "unit": unit or ""})
        if len(out) >= 30:
            break
    return out


class _ThreadLogTee(io.TextIOBase):
    """Output tee: mirrors writes to the real stream, and additionally
    appends them to the job's log file when called from the owner thread.

    The thread check matters because redirect_stdout swaps the process-wide
    sys.stdout; prints from other threads (e.g. uvicorn access logs) must
    not leak into a job's log.
    """

    def __init__(self, owner_ident: int, log_file, passthrough) -> None:
        super().__init__()
        self._owner_ident = owner_ident
        self._log_file = log_file
        self._passthrough = passthrough

    def write(self, s: str) -> int:
        if s and threading.get_ident() == self._owner_ident:
            self._log_file.write(s)
            self._log_file.flush()
        if s:
            self._passthrough.write(s)
        self._passthrough.flush()
        return len(s)

    def flush(self) -> None:
        self._log_file.flush()
        self._passthrough.flush()

    @property
    def encoding(self) -> str:
        return getattr(self._passthrough, "encoding", None) or "utf-8"


@contextlib.contextmanager
def _capture_job_log(job_dir: str) -> Iterator[None]:
    """Redirect the current thread's stdout/stderr into <job_dir>/stdout.log.

    print() output produced by algorithms (e.g. the legacy script's model
    report) is captured so the frontend can show it in a log panel; the
    original terminal output is preserved as well.
    """
    log_path = os.path.join(job_dir, LOG_FILENAME)
    owner_ident = threading.get_ident()
    with open(log_path, "a", encoding="utf-8") as log_file:
        out_tee = _ThreadLogTee(owner_ident, log_file, sys.stdout)
        err_tee = _ThreadLogTee(owner_ident, log_file, sys.stderr)
        with contextlib.redirect_stdout(out_tee), contextlib.redirect_stderr(err_tee):
            yield


def create_job(algorithm_id: str, params: Dict[str, Any], name: Optional[str] = None) -> Dict[str, Any]:
    adapter = algorithm_registry.get_algorithm(algorithm_id)
    if adapter is None:
        raise KeyError(algorithm_id)
    job = job_store.create_job(adapter.id, params, name=name)

    job_dir = os.path.join(str(JOBS_DIR), job["job_id"])
    os.makedirs(job_dir, exist_ok=True)

    if adapter.type == "LOCAL":
        thread = threading.Thread(
            target=_run_local,
            args=(job["job_id"], adapter.id, dict(params), job_dir),
            daemon=True,
            name=f"job-{job['job_id'][:8]}",
        )
        thread.start()
    else:
        # GEE adapters return quickly with a gee_task_id; run in a thread
        # so the HTTP response is not blocked.
        thread = threading.Thread(
            target=_run_gee,
            args=(job["job_id"], adapter.id, dict(params), job_dir),
            daemon=True,
            name=f"gee-{job['job_id'][:8]}",
        )
        thread.start()
    return job


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    job = job_store.get_job(job_id)
    if job is None:
        return None
    _refresh_gee_status(job)
    return job


def list_jobs() -> list:
    jobs = job_store.list_jobs()
    for job in jobs:
        _refresh_gee_status(job)
    return jobs


def update_job_note(job_id: str, note: str) -> bool:
    """Persist a free-text note on a job. Returns False if job is missing."""
    if job_store.get_job(job_id) is None:
        return False
    job_store.update_job(job_id, note=note)
    return True


def update_job_name(job_id: str, name: str) -> bool:
    """Persist a (display) name on a job. Returns False if job is missing."""
    if job_store.get_job(job_id) is None:
        return False
    job_store.update_job(job_id, name=name)
    return True


def cancel_job(job_id: str) -> Optional[str]:
    """Cancel a queued job. Returns the resulting status, or None if missing.

    Only 'queued' jobs can be cancelled. running / submitted / completed /
    failed / cancelled return their current status (the API layer maps that
    to HTTP 409).
    """
    new_status = job_store.cancel_job(job_id)
    if new_status is None:
        return None
    if new_status == "cancelled":
        # Worker thread (if any) may still be starting; it's safe to leave
        # it — it'll write into job_dir, the result update will overwrite
        # the cancelled status with running/completed. Per the spec, only
        # `queued` is cancellable in this phase.
        _last_gee_check.pop(job_id, None)
    return new_status


def delete_job(job_id: str) -> bool:
    """Remove a job record plus its working directory.

    Worker thread (if still running) keeps writing until it next touches the
    deleted dir and hits an OSError — acceptable for MVP. We also evict the
    preview cache and the GEE throttle entry so a future recreate of the same
    id (extremely unlikely) would start clean.
    """
    if job_store.get_job(job_id) is None:
        return False
    job_store.delete_job(job_id)
    job_dir = os.path.join(str(JOBS_DIR), job_id)
    shutil.rmtree(job_dir, ignore_errors=True)
    preview_service.clear_cache(job_id)
    _last_gee_check.pop(job_id, None)
    return True


def _run_local(job_id: str, algorithm_id: str, params: Dict[str, Any], job_dir: str) -> None:
    adapter = algorithm_registry.get_algorithm(algorithm_id)

    def _on_progress(pct: int, message: str) -> None:
        job_store.update_job(job_id, status="running", progress=pct, message=message)

    started_at = _utc_now_iso()
    job_store.update_job(
        job_id, status="running", progress=0, message="开始执行", started_at=started_at
    )
    try:
        with _capture_job_log(job_dir):
            try:
                result = adapter.run(params, job_dir, progress_callback=_on_progress)
            except Exception:  # noqa: BLE001
                # 把堆栈写进任务日志，前端日志面板可见，方便排查。
                traceback.print_exc()
                raise
        # 如果任务已被用户取消（状态变为 cancelled），仍然写 result 以保留
        # 已生成的输出，但 status 不被覆盖 —— 取消状态不可被 worker 推翻。
        current = job_store.get_job(job_id)
        finished_at = _utc_now_iso()
        if current and current.get("status") == "cancelled":
            job_store.update_job(
                job_id, progress=100, message=result.get("message", ""), result=result,
                finished_at=finished_at,
            )
        else:
            job_store.update_job(
                job_id,
                status=result.get("status", "completed"),
                progress=100,
                message=result.get("message", ""),
                result=result,
                finished_at=finished_at,
            )
    except Exception as exc:  # noqa: BLE001
        current = job_store.get_job(job_id)
        finished_at = _utc_now_iso()
        if current and current.get("status") == "cancelled":
            job_store.update_job(
                job_id, finished_at=finished_at, message=f"已取消: {exc}",
                result={"error": str(exc)},
            )
        else:
            job_store.update_job(
                job_id,
                status="failed",
                progress=0,
                message=f"执行失败: {exc}",
                result={"error": str(exc)},
                finished_at=finished_at,
            )


def _run_gee(job_id: str, algorithm_id: str, params: Dict[str, Any], job_dir: str) -> None:
    """Invoke a GEE adapter, persist the returned task id, and update status."""
    adapter = algorithm_registry.get_algorithm(algorithm_id)

    def _on_progress(pct: int, message: str) -> None:
        job_store.update_job(job_id, status="running", progress=pct, message=message)

    started_at = _utc_now_iso()
    job_store.update_job(
        job_id, status="running", progress=0, message="提交 GEE 任务", started_at=started_at
    )
    try:
        with _capture_job_log(job_dir):
            try:
                result = adapter.run(params, job_dir, progress_callback=_on_progress)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                raise
        current = job_store.get_job(job_id)
        finished_at = _utc_now_iso()
        if current and current.get("status") == "cancelled":
            job_store.update_job(
                job_id, progress=100, message=result.get("message", ""), result=result,
                finished_at=finished_at,
            )
        else:
            job_store.update_job(
                job_id,
                status=result.get("status", "submitted"),
                progress=100,
                message=result.get("message", ""),
                result=result,
                finished_at=finished_at,
            )
    except Exception as exc:  # noqa: BLE001
        current = job_store.get_job(job_id)
        finished_at = _utc_now_iso()
        if current and current.get("status") == "cancelled":
            job_store.update_job(
                job_id, finished_at=finished_at, message=f"已取消: {exc}",
                result={"error": str(exc)},
            )
        else:
            job_store.update_job(
                job_id,
                status="failed",
                progress=0,
                message=f"GEE 任务失败: {exc}",
                result={"error": str(exc)},
                finished_at=finished_at,
            )


def get_job_log(job_id: str, offset: int = 0) -> Optional[Dict[str, Any]]:
    """Return the captured stdout log of a job, starting at byte offset.

    Returns None when the job does not exist. The frontend polls this with
    an incrementing offset to tail the log while the algorithm runs.
    """
    job = job_store.get_job(job_id)
    if job is None:
        return None
    log_path = os.path.join(str(JOBS_DIR), job_id, LOG_FILENAME)
    payload: Dict[str, Any] = {
        "job_id": job_id,
        "status": job["status"],
        "text": "",
        "size": 0,
        "exists": False,
    }
    if os.path.isfile(log_path):
        file_size = os.path.getsize(log_path)
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(min(max(0, offset), file_size))
            text = fh.read()
            size = fh.tell()
        payload.update(text=text, size=size, exists=True)
    return payload


def _refresh_gee_status(job: Dict[str, Any]) -> None:
    """If the job result contains a gee_task_id, query GEE and sync state.

    Reads/writes the underlying job_store when the GEE state changes, and
    mutates the passed-in job dict so the caller sees the fresh status.
    Throttled to at most one GEE call every _GEE_CHECK_INTERVAL seconds
    per job.
    """
    result = job.get("result") or {}
    task_id = result.get("gee_task_id")
    if not task_id:
        return
    if job.get("status") in ("failed", "completed"):
        return

    job_id = job["job_id"]
    now = time.monotonic()
    if now - _last_gee_check.get(job_id, 0.0) < _GEE_CHECK_INTERVAL:
        return
    _last_gee_check[job_id] = now

    mapped = _query_gee_status(task_id)
    if mapped is None:
        return  # GEE not reachable; keep current status
    if mapped != job["status"]:
        job_store.update_job(job_id, status=mapped, message=f"GEE 状态: {mapped}")
        job["status"] = mapped


def _query_gee_status(task_id: str) -> Optional[str]:
    """Query GEE for the task state and map it to our internal status.

    Returns None if GEE cannot be initialized or queried.
    """
    try:
        import ee  # local import so non-GEE paths don't pay the cost
    except ImportError:
        return None
    try:
        try:
            project = os.environ.get("EE_PROJECT")
            ee.Initialize(project=project) if project else ee.Initialize()
        except Exception:
            # Already initialized in another thread, or auth missing — try anyway.
            pass
        info = ee.data.getTaskStatus(task_id)
        state = (info or {}).get("state", "UNKNOWN")
        return _GEE_STATE_MAP.get(state, "submitted")
    except Exception:
        return None