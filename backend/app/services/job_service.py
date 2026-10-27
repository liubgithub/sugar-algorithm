"""Job service: creates jobs, runs LOCAL algorithms in a worker thread,
and updates the job store as the algorithm reports progress.

For GEE algorithms the adapter returns immediately with gee_task_id; the
service then queries ee.data.getTaskStatus on subsequent get_job() calls
to mirror GEE state (READY/RUNNING/COMPLETED/FAILED/CANCELLED) into the
job record. The query is throttled to one call per few seconds per job.
"""
from __future__ import annotations

import contextlib
import concurrent.futures
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
from app.services.gee_auth import (
    GeeAuthRequired,
    GeeNetworkError,
    gee_session,
    is_auth_mismatch_error,
    is_network_error,
    project_for_job,
)
from app.storage import job_store


# Cache so we don't hammer the GEE API on every poll.
_GEE_CHECK_INTERVAL = 3.0  # seconds
_last_gee_check: Dict[str, float] = {}
# Hard wall-clock cap on a single GEE status probe. earthengine-api's HTTP
# call (via google-auth + requests) inherits the global socket timeout,
# which is unlimited on most systems, so a flaky/blocked network can hang
# for tens of seconds and bubble up to the HTTP handler. We cap each probe
# at this value to keep `GET /api/jobs/{id}` (and especially
# `GET /api/jobs` which used to refresh every row synchronously) snappy.
_GEE_QUERY_TIMEOUT = 12.0  # seconds
# How long a status probe waits for the process-wide GEE lock before giving
# up and skipping the cycle. Kept short on purpose: the probe runs on the
# HTTP request path too, so it must never stall behind a long job — the next
# sweep (or the front-end's next poll) retries anyway.
_GEE_PROBE_LOCK_TIMEOUT = 2.0  # seconds
# Single shared executor — one thread per concurrent GEE probe is plenty
# given that calls are throttled to one per few seconds per job.
# Bumped from 4 → 8 workers because the previous 4-thread pool kept
# getting exhausted by long ``getTaskStatus`` calls that the
# ``Future.result(timeout=...)`` cap couldn't actually cancel (the HTTP
# request inside earthengine-api has no cancellation hook). Once all 4
# workers were stuck, every subsequent probe queued indefinitely — the
# job sat at "submitted" forever even though the GEE task was COMPLETED.
_GEE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="gee-probe"
)

# Background poller: fires once at module import time and keeps polling
# every `_GEE_POLLER_INTERVAL` seconds for any GEE-backed job whose state
# is still "submitted" / "running". Without it, jobs that returned early
# (Pattern B: e.g. `crop-classification` submits `Export.image.toDrive`
# and returns immediately, and the legacy version of `crop_features` did
# the same) would sit at "submitted" forever — `GET /api/jobs` and
# `GET /api/jobs/{id}` only refresh state on-demand, and the on-demand
# path can time out on a flaky/blocked network to GEE.
_GEE_POLLER_INTERVAL = 10.0  # seconds between background sweeps
_GEE_POLLER_STARTED = False
_GEE_POLLER_LOCK = threading.Lock()

# Job ids whose ``finalize()`` hook is currently executing *in this process*.
#
# This set replaces the old "status == 'running' means a finalize is in
# flight" heuristic, which conflated two completely different states:
#
#   * "running" because the **GEE task itself** is still computing — the job
#     MUST keep being polled, otherwise it never notices the task finishing
#     and stays stuck forever;
#   * "running" because a **finalize()** is downloading the result — the job
#     must NOT be polled again, otherwise we'd start a second concurrent
#     download of the same asset.
#
# Tracking the in-flight finalizes in-process also means a job whose
# finalize was interrupted by a server restart is picked up again on the
# next sweep (it is still "running" in the DB, but not in this set).
_FINALIZE_IN_FLIGHT: set[str] = set()
_FINALIZE_IN_FLIGHT_LOCK = threading.Lock()

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
    # Make sure the background GEE poller is running before we hand back a
    # job_id — otherwise a freshly submitted Pattern-B GEE job could sit
    # at "submitted" until the user manually opens the detail page.
    start_gee_poller()
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
    """List all jobs as stored, WITHOUT refreshing GEE status.

    Earlier this looped over every job and called `_refresh_gee_status`,
    which issues a synchronous `ee.data.getTaskStatus` HTTP call per GEE
    job. With one stuck `submitted` job plus a flaky/blocked network to
    earthengine.googleapis.com, a single GET could hang for tens of
    seconds and exceed the 30s axios timeout on the frontend — the list
    page would never paint.

    GEE jobs therefore render with their last known status in the list
    view; the detail endpoint (`get_job` → `_refresh_gee_status`) still
    refreshes per-job state, with the same hard timeout cap added in
    `_query_gee_status` below.
    """
    return job_store.list_jobs()


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


def retry_job(job_id: str) -> Optional[str]:
    """Re-queue a finished-but-broken job so its result is pulled again.

    A job that failed in the **finalize** stage (waiting for the GEE asset /
    downloading the GeoTIFF) is not recoverable on its own: the poller skips
    terminal rows, so the job would sit at "failed" forever even though GEE
    still holds the finished asset.  This function gives the operator the
    missing "再试一次" button.

    Two shapes of failure are covered:

      * **finalize failed** — the GEE task itself succeeded, so
        ``result.metrics.asset_id`` still points at the finished asset.  We
        flip the row back to a non-terminal status and re-dispatch the
        adapter's ``finalize`` hook.  The resumable downloader picks up
        the tiles it already fetched, so a retry after a flaky proxy
        resumes instead of starting over.
      * **the GEE task failed** — there is no asset to pull, so the
        algorithm is re-submitted from scratch (a fresh GEE task).

    Returns the new status string, or ``None`` when the job does not exist.
    Raises ``ValueError`` when the job is in a state that cannot be retried.
    """
    job = job_store.get_job(job_id)
    if job is None:
        return None
    status = job.get("status")
    if status not in ("failed", "cancelled"):
        raise ValueError(
            f"只有失败或已取消的任务可以重试（当前状态：{status}）"
        )
    algorithm_id = job.get("algorithm")
    if not algorithm_id:
        raise ValueError("任务缺少算法标识，无法重试")

    result = job.get("result") or {}
    metrics = result.get("metrics") or {}

    # --- 1) GEE 侧已经有结果：只需重新拉取 ---------------------------
    if metrics.get("asset_id"):
        job_store.update_job(
            job_id,
            status="submitted",
            progress=0,
            message="正在重新拉取 GEE 结果（已下载的图块会断点续传）…",
            finished_at="",
        )
        # 轮询器的 finalize 保护（_FINALIZE_IN_FLIGHT）保证不会有两个
        # 下载线程同时抢同一个资产。
        _dispatch_finalize(job_id)
        return "submitted"

    # --- 2) GEE 任务本身就失败了：重新提交 --------------------------
    params = job.get("params") or {}
    job_dir = os.path.join(str(JOBS_DIR), job_id)
    try:
        os.makedirs(job_dir, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"无法创建任务目录：{exc}") from exc
    job_store.update_job(
        job_id,
        status="queued",
        progress=0,
        message="正在重新提交 GEE 任务…",
        finished_at="",
    )
    _last_gee_check.pop(job_id, None)
    thread = threading.Thread(
        target=_run_gee,
        args=(job_id, algorithm_id, params, job_dir),
        name=f"gee-retry-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return "queued"


def delete_job(job_id: str) -> bool:
    """Remove a job record plus its working directory.

    Worker thread (if still running) keeps writing until it next touches the
    deleted dir and hits an OSError — acceptable for MVP. We also evict the
    preview cache and the GEE throttle entry so a future recreate of the same
    id (extremely unlikely) would start clean.
    """
    if job_store.get_job(job_id) is None:
        return False
    # 关键顺序：先关掉 rasterio dataset（preview_service LRU 缓存持有的 .tif
    # 文件句柄），再删目录。Windows 上 .tif 被持有时 shutil.rmtree 会
    # PermissionError；之前用 ignore_errors=True 静默吞掉，导致 job_dir
    # 里残留几十 MB 的 .tif 文件。
    preview_service.clear_cache(job_id)
    job_store.delete_job(job_id)
    job_dir = os.path.join(str(JOBS_DIR), job_id)
    _rmtree_force(job_dir)
    _last_gee_check.pop(job_id, None)
    return True


def _rmtree_force(path: str) -> None:
    """Windows 友好的强制删目录：解只读 + 重试，不用 ignore_errors 静默跳过。

    之前的 shutil.rmtree(ignore_errors=True) 在 Windows 上遇到被持有句柄的
    .tif 时直接跳过该文件，留下 50MB 孤儿 .tif + 空目录树。改成 onerror
    解锁重试，3 次失败后 warning 但继续清掉能清的。
    """
    import gc
    import stat
    import time

    def _onerror(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception as exc:
            print(f"[job_service] rmtree skip {path}: {exc}", file=sys.stderr)

    if not os.path.isdir(path):
        return
    # 强制 GC 一次，让 rasterio 等 C 扩展有机会释放 fd
    gc.collect()
    for _ in range(3):
        shutil.rmtree(path, onerror=_onerror)
        if not os.path.exists(path):
            return
        time.sleep(0.1)
    # 最终兜底：能删多少算多少，不让 process 整体崩
    if os.path.isdir(path):
        print(f"[job_service] job_dir not fully cleaned: {path}", file=sys.stderr)


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

    # Project this run belongs to. Non-GEE algorithms carry no project param;
    # they skip the session entirely and never contend for the GEE lock.
    session_project = (
        params.get("gee_project_id")
        or params.get("gee_project")
        or os.environ.get("EE_PROJECT")
    )

    started_at = _utc_now_iso()
    job_store.update_job(
        job_id, status="running", progress=0, message="提交 GEE 任务", started_at=started_at
    )
    try:
        with _capture_job_log(job_dir):
            try:
                # EE keeps ONE project binding for the whole process, shared by
                # every thread. Without holding it for the whole run, the status
                # poller can rebind EE to some other job's project midway
                # through and every GEE call below goes out under the wrong
                # project — the operator then sees a permission error naming a
                # project they never selected, which re-authenticating cannot
                # fix. See gee_auth.gee_session.
                if session_project:
                    with gee_session(session_project):
                        result = adapter.run(
                            params, job_dir, progress_callback=_on_progress
                        )
                else:
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
            # Pattern B algorithms (e.g. crop_features) return
            # ``status="submitted"`` with ``progress=10`` to mean "the
            # worker is done; the GEE poller will fill in the rest." We
            # honor whatever progress the adapter returned instead of
            # hardcoding 100 — a Pattern A adapter that doesn't set
            # ``progress`` falls back to 100 to keep the previous UX.
            job_store.update_job(
                job_id,
                status=result.get("status", "submitted"),
                progress=result.get("progress", 100),
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
            # Raw GEE permission errors name whichever project EE was bound to
            # and append an IAM-console URL. Replace them with the Chinese
            # guidance so the operator is pointed at the project they actually
            # selected — and at the IAM role, which is the usual real blocker
            # when switching to a different Google account.
            if session_project and is_auth_mismatch_error(exc):
                msg = str(GeeAuthRequired(session_project))
            elif is_network_error(exc):
                # Transport failure, not a credentials problem — say so, so
                # nobody re-authenticates a perfectly good account.
                msg = str(GeeNetworkError(str(exc)))
            else:
                msg = f"GEE 任务失败: {exc}"
            job_store.update_job(
                job_id,
                status="failed",
                progress=0,
                message=msg,
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
    per job. When GEE transitions to COMPLETED, also dispatches to the
    adapter's ``finalize`` hook (Pattern B algorithms) and writes the
    returned result to the DB.

    On GEE FAILED/CANCELLED we capture the original ``error_message`` from
    ``ee.data.getTaskStatus`` so the frontend shows the real reason
    ("Unable to export features with null geometry. (Error code: 3)"
    etc.) instead of a generic "GEE 状态: failed" line — otherwise the
    user sees the badge flip to 失败 with no clue why.
    """
    result = job.get("result") or {}
    task_id = result.get("gee_task_id")
    if not task_id:
        return
    if job.get("status") in ("failed", "completed", "cancelled"):
        return
    # A job whose finalize() is currently downloading its result must not be
    # probed again (nor finalized twice).  Note this is NOT keyed off the
    # "running" status: a job is also "running" while the **GEE task** is
    # still computing, and that job absolutely has to keep being polled —
    # that is how it ever learns the task finished.  The old
    # `status in (..., "running")` guard froze exactly those jobs, which is
    # why a 17-hour harvest export sat at "running" forever.
    if _finalize_in_flight(job["job_id"]):
        return

    job_id = job["job_id"]
    now = time.monotonic()
    if now - _last_gee_check.get(job_id, 0.0) < _GEE_CHECK_INTERVAL:
        return
    _last_gee_check[job_id] = now

    # Use the job's own ``gee_project_id`` for the getTaskStatus probe —
    # GEE scopes the call to whichever project EE is initialized to, and
    # we MUST stay bound to the project that owns the task. See
    # ``_query_gee_status`` for the failure mode this guards against.
    # Single source of truth for "which project owns this task" — the same
    # helper the finalize hooks use to rebind EE. Letting the probe and the
    # download resolve the project independently is how they ended up
    # disagreeing: the probe ran against one project while finalize's GEE
    # calls went out under another, and the user saw a permission error
    # naming a project they never selected.
    job_project = project_for_job(job)

    mapped, error_msg = _query_gee_status(task_id, project=job_project)
    if mapped is None:
        return  # GEE not reachable; keep current status
    if mapped == "completed":
        # Don't write "completed" here — defer to _dispatch_finalize so the
        # finalize() hook owns the "running"/"submitted" → "completed"
        # transition (and has a chance to write the new result payload).
        # Writing "completed" first would make the re-fetch inside
        # _dispatch_finalize trip its own idempotency guard and skip the
        # finalize() call entirely (Pattern B regression).
        _dispatch_finalize(job_id)
    elif mapped != job["status"]:
        # Other transitions (e.g. SUBMITTED → RUNNING, RUNNING → FAILED)
        # are written through directly. They don't need finalize().
        # FAILED/CANCELLED carry an optional error_msg from GEE — surface
        # that to the user verbatim instead of just "GEE 状态: failed".
        if mapped == "failed":
            msg = error_msg or "GEE 任务失败（未返回详细原因）"
            job_store.update_job(
                job_id,
                status="failed",
                message=f"GEE 任务失败：{msg}",
                result={**result, "error": msg},
            )
        else:
            job_store.update_job(
                job_id, status=mapped, message=f"GEE 状态: {mapped}"
            )
        job["status"] = mapped


def _finalize_error_message(exc: BaseException, project: Optional[str]) -> str:
    """Turn a finalize failure into something worth showing a user.

    Raw GEE permission errors are swapped for the Chinese re-auth guidance.
    They name whichever project EE was bound to at the time and append an
    IAM-console URL — even though the finalize hooks now rebind EE to the
    job's own project first, a foreign call can still surface from
    earthengine-api / geemap internals, and dumping its English text into
    the failed-job card sends the operator to the wrong console.
    """
    if project and is_auth_mismatch_error(exc):
        return str(GeeAuthRequired(project))
    if is_network_error(exc):
        # The download died on the transport, not on the credentials.  Say so
        # plainly — the resumable downloader keeps the tiles it already got,
        # so the fix is "make the network work and retry", not "re-auth".
        return str(GeeNetworkError(str(exc)))
    return f"finalize 失败: {exc}"


def _mark_finalize_failed(job_id: str, message: str) -> None:
    """Best-effort write of a terminal `failed` status from the poller."""
    try:
        job_store.update_job(job_id, status="failed", message=message)
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _finalize_in_flight(job_id: str) -> bool:
    """Is a ``finalize()`` currently running for this job in this process?"""
    with _FINALIZE_IN_FLIGHT_LOCK:
        return job_id in _FINALIZE_IN_FLIGHT


def _dispatch_finalize(job_id: str) -> None:
    """Drive a job from a non-terminal state to ``completed`` via the adapter.

    Called by :func:`_refresh_gee_status` when GEE reports the task as
    COMPLETED. Two patterns are supported:

      * **Pattern B with finalize()**: the adapter has a ``finalize`` hook
        (e.g. ``crop_features``, ``crop_classification``, ``harvest``); we
        flip status to ``running``, run the hook (which waits for the Asset
        and downloads the result), then write the new ``result`` payload to
        the DB and flip status to ``completed``.

      * **Pattern B without finalize()**: the adapter has no override;
        we just flip status to ``completed`` and leave the run() payload
        alone.

    The finalize hook runs on its **own background thread**.  That matters
    for two reasons:

      * a finalize can take many minutes (a Guangxi-scale raster is ~5 G
        pixels and is downloaded in ~300 aligned tiles) — running it inline
        would block the HTTP request path (``GET /api/jobs/{id}``) and the
        whole poller sweep, stalling every other GEE job in the process;
      * a server restart then kills only the download, not the poller, and
        the next sweep re-dispatches the finalize (the job is still
        "running" in the DB but no longer in ``_FINALIZE_IN_FLIGHT``).

    Idempotency:
      * Already terminal (failed/cancelled/completed) → bail.
      * A finalize is already in flight for this job → bail.
    """
    full = job_store.get_job(job_id)
    if full is None:
        return
    status_now = full.get("status")
    if status_now in ("failed", "cancelled", "completed"):
        # Already terminal — nothing to finalize.
        return
    algorithm_id = full.get("algorithm")
    if not algorithm_id:
        return
    adapter = algorithm_registry.get_algorithm(algorithm_id)
    finalize = getattr(adapter, "finalize", None)

    job_dir = os.path.join(str(JOBS_DIR), job_id)

    # Every adapter inherits ``AlgorithmAdapter.finalize``, so the mere
    # presence of the attribute says nothing — check whether the adapter
    # actually *overrode* it.  An adapter that did not is a "Pattern B
    # without finalize": the GEE task is done and there is nothing to
    # download, so the job simply flips to completed.
    from app.algorithms.base import AlgorithmAdapter

    has_finalize_hook = (
        finalize is not None
        and getattr(type(adapter), "finalize", None) is not AlgorithmAdapter.finalize
    )

    if not has_finalize_hook:
        # Pattern B without finalize: just flip to "completed". The result
        # payload from run() stays as-is.
        try:
            job_store.update_job(
                job_id,
                status="completed",
                progress=100,
                message=f"GEE 任务已完成（{algorithm_id} 无 finalize hook）",
                finished_at=_utc_now_iso(),
            )
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return

    # Claim the job before starting the thread so two sweeps (or a sweep and
    # an HTTP request) can't both start a download of the same asset.
    with _FINALIZE_IN_FLIGHT_LOCK:
        if job_id in _FINALIZE_IN_FLIGHT:
            return
        _FINALIZE_IN_FLIGHT.add(job_id)

    thread = threading.Thread(
        target=_run_finalize,
        args=(job_id, full, finalize, job_dir),
        name=f"gee-finalize-{job_id[:8]}",
        daemon=True,
    )
    thread.start()


def _run_finalize(
    job_id: str,
    full: Dict[str, Any],
    finalize,
    job_dir: str,
) -> None:
    """Run the adapter's finalize hook and persist its result.

    Runs on the finalize background thread.  Any exception is turned into a
    ``status='failed'`` row; the caller never sees it.
    """
    try:
        # Mark "running" while finalize is in flight.  The polling guard in
        # _refresh_gee_status skips jobs listed in _FINALIZE_IN_FLIGHT, so a
        # second sweep won't pile a concurrent download onto the same asset.
        job_store.update_job(
            job_id,
            status="running",
            message="GEE 任务已完成，开始 finalize（下载结果文件）",
        )

        def _on_progress(pct: int, message: str) -> None:
            """Surface the download's progress on the job row.

            A finalize can take many minutes (a province-scale raster is
            ~5 G pixels, downloaded as ~300 aligned tiles).  Without this
            the card would sit at "running / 10%" for the whole download
            and look frozen.  Clamped to 99 because "completed" is the
            job service's call to make.
            """
            try:
                job_store.update_job(
                    job_id,
                    status="running",
                    progress=max(1, min(99, int(pct))),
                    message=message,
                )
            except Exception:  # noqa: BLE001
                traceback.print_exc()

        # Same reasoning as _run_gee: finalize makes GEE calls while a
        # concurrently-submitted run may be using EE.  The session keeps the
        # binding pinned to this job's project for the whole download (the
        # finalize hooks also call initialize_ee themselves — redundant, but
        # cheap, and it keeps them correct if called directly).
        finalize_project = project_for_job(full)
        if finalize_project:
            with gee_session(finalize_project):
                new_result = finalize(full, job_dir, _on_progress)
        else:
            new_result = finalize(full, job_dir, _on_progress)
        if new_result is None:
            # The adapter declined to finalize (e.g. the job row has no
            # asset_id to download).  Leaving the job at "running" would
            # strand it forever — mark it failed with an explanation.
            _mark_finalize_failed(
                job_id,
                "finalize 未返回结果：任务缺少 GEE 资产信息，无法拉取结果。",
            )
            return
        finished_at = _utc_now_iso()
        job_store.update_job(
            job_id,
            status="completed",
            progress=100,
            result=new_result,
            message=new_result.get("message", ""),
            finished_at=finished_at,
        )
    except GeeAuthRequired as exc:
        # Already a user-facing Chinese message from gee_auth. Show it
        # verbatim — prefixing "finalize 失败:" would bury the actionable
        # `earthengine authenticate --force` line that the front-end greps
        # for when deciding to render the copy-command hint.
        _mark_finalize_failed(job_id, str(exc))
    except GeeNetworkError as exc:
        # Same reasoning: the network message is already actionable
        # ("check the proxy, then retry"), so don't bury it.
        _mark_finalize_failed(job_id, str(exc))
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _mark_finalize_failed(job_id, _finalize_error_message(exc, project_for_job(full)))
    finally:
        with _FINALIZE_IN_FLIGHT_LOCK:
            _FINALIZE_IN_FLIGHT.discard(job_id)


def _poll_one_gee_job(job: Dict[str, Any]) -> None:
    """Background variant of `_refresh_gee_status` for the poller loop.

    Same throttling dict (`_last_gee_check`) is shared with the request
    path so we never double-hit GEE for the same job within the throttle
    interval, regardless of which path initiates the probe.
    """
    _refresh_gee_status(job)


def _poll_all_gee_jobs() -> None:
    """Sweep the job store once; refresh GEE state for every active GEE job.

    A job is "active for GEE polling" iff:
      - it has a `gee_task_id` in its `result`,
      - it is NOT in a terminal state (`failed` / `completed` / `cancelled`),
      - the per-job throttle window has elapsed.

    Updates are written to the DB, so the next time the frontend hits
    `GET /api/jobs` or `GET /api/jobs/{id}` it sees the fresh status.
    """
    try:
        jobs = job_store.list_jobs()
    except Exception as exc:
        print(f"[gee-poller] list_jobs failed: {exc}", file=sys.stderr)
        return
    for job in jobs:
        try:
            _poll_one_gee_job(job)
        except Exception as exc:
            # Never let one bad job kill the whole sweep.
            print(
                f"[gee-poller] poll failed for {job.get('job_id')}: {exc}",
                file=sys.stderr,
            )


def _gee_poller_loop() -> None:
    """Long-lived background loop. Sleeps `_GEE_POLLER_INTERVAL` between sweeps.

    Daemon thread — dies with the FastAPI process. Sweep errors are logged
    but do not break the loop.
    """
    while True:
        try:
            _poll_all_gee_jobs()
        except Exception as exc:
            print(f"[gee-poller] sweep crashed: {exc}", file=sys.stderr)
        time.sleep(_GEE_POLLER_INTERVAL)


def start_gee_poller() -> None:
    """Idempotently start the background GEE poller thread.

    Called from `create_app()` (and at import time as a fallback) so the
    poller is guaranteed to be running before the first request handler
    can enqueue a GEE job. Subsequent calls are no-ops.
    """
    global _GEE_POLLER_STARTED
    with _GEE_POLLER_LOCK:
        if _GEE_POLLER_STARTED:
            return
        thread = threading.Thread(
            target=_gee_poller_loop,
            name="gee-poller",
            daemon=True,
        )
        thread.start()
        _GEE_POLLER_STARTED = True


def _query_gee_status(
    task_id: str,
    *,
    project: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Query GEE for the task state and map it to our internal status.

    Returns ``(mapped_state, error_message)``. Either is ``None`` when GEE
    cannot be initialized or queried, OR when the probe exceeds
    ``_GEE_QUERY_TIMEOUT`` seconds wall-clock.

    ``project`` MUST be the GEE project the task was submitted under (the
    job's ``params.gee_project_id``), not the global ``EE_PROJECT`` env
    var. GEE scopes ``getTaskStatus`` to whichever project EE is currently
    initialized to, and earthengine-api's ``ee.Initialize()`` with no
    argument silently resets to the OAuth quota_project — if that differs
    from the project that owns the task, the call fails with
    "Caller does not have required permission to use project ..." and
    ``_probe`` returns ``(None, None)``, leaving the job stuck at
    "submitted" forever. The poller therefore threads the job's project
    through explicitly here (see ``_refresh_gee_status``).

    ``error_message`` is only populated for FAILED/CANCELLED states and
    comes straight from GEE's task descriptor (e.g. ``"Unable to export
    features with null geometry. (Error code: 3)"``). The frontend uses
    this verbatim in the failed-status banner; falling back to a generic
    "GEE 状态: failed" loses all diagnostic value.

    The timeout is enforced by running the underlying HTTP call in a
    worker thread and racing it against a `Future.result(timeout=...)`.
    Without this cap, a flaky/blocked network to
    earthengine.googleapis.com makes the call hang on socket defaults
    and the HTTP handler blocks long enough that the frontend
    (axios timeout 30s) gives up. Treat any timeout as "GEE unavailable"
    so the caller falls back to the cached status.
    """
    def _probe() -> tuple[Optional[str], Optional[str]]:
        try:
            import ee  # local import so non-GEE paths don't pay the cost
        except ImportError:
            return (None, None)

        # EE has one project binding per process. A run or a finalize may be
        # using GEE right now under its own project; rebinding underneath it
        # corrupts *its* requests — that is exactly how a run ends up failing
        # with a permission error naming a project nobody selected. So a busy
        # GEE means "skip this cycle", never "wait and steal".
        from app.services.gee_auth import gee_probe_lock, initialize_ee

        with gee_probe_lock(_GEE_PROBE_LOCK_TIMEOUT) as acquired:
            if not acquired:
                return (None, None)
            try:
                # Bind EE to the job's OWN project while we hold the lock.
                # ``ee.Initialize()`` with no argument (what would happen if we
                # relied solely on EE_PROJECT, or omitted the arg here) silently
                # rebinds to the OAuth quota_project and breaks subsequent
                # getTaskStatus() calls — see this function's docstring.
                if project:
                    initialize_ee(project)
                # No project available (e.g. a malformed DB row): skip the
                # re-init rather than disturb whatever binding is in place.
                info = ee.data.getTaskStatus(task_id)
                # earthengine-api returns a *list* of task descriptors (one per
                # requested task id), not a single dict. Older docs and snippets
                # treat it as a dict — that path raises AttributeError which is
                # swallowed below and silently turns every probe into
                # "(None, None)", so COMPLETED is never detected. Normalize to
                # the descriptor dict before reading state/error_message.
                if isinstance(info, list):
                    info = info[0] if info else None
                if not isinstance(info, dict):
                    return (None, None)
                state = info.get("state", "UNKNOWN")
                err = info.get("error_message") or None
                return (_GEE_STATE_MAP.get(state, "submitted"), err)
            except Exception:
                return (None, None)

    try:
        future = _GEE_EXECUTOR.submit(_probe)
        return future.result(timeout=_GEE_QUERY_TIMEOUT)
    except concurrent.futures.TimeoutError:
        # Probe is still running in the worker; let it finish in the
        # background. Returning None here keeps the cached status and
        # ensures the HTTP handler returns promptly.
        return (None, None)
    except Exception:
        return (None, None)