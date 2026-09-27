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
    # "running" is treated as a quiet state for polling purposes — the
    # finalize() hook owns the job until it lands on a terminal state.
    # Without this guard, a 30-minute finalize() (crop_features Pattern B)
    # would be hammered by a GEE status call every 10 s for no reason.
    if job.get("status") in ("failed", "completed", "running"):
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
    job_params = job.get("params") or {}
    job_project = job_params.get("gee_project_id") or job_params.get("gee_project")
    if not job_project:
        # Fallback: derive from the sample_asset_id / asset_id (both are
        # ``projects/<project>/assets/<name>``). This recovers jobs that
        # were submitted before ``params.gee_project_id`` was canonical,
        # or jobs whose params row got corrupted.
        for key in ("asset_id", "sample_asset_id"):
            ref = (result.get("metrics") or {}).get(key) or ""
            if ref.startswith("projects/") and "/assets/" in ref:
                job_project = ref.split("/")[1]
                break

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


def _dispatch_finalize(job_id: str) -> None:
    """Drive a job from a non-terminal state to ``completed`` via the adapter.

    Called by :func:`_refresh_gee_status` when GEE reports the task as
    COMPLETED. Two patterns are supported:

      * **Pattern B with finalize()**: the adapter has a ``finalize`` hook
        (e.g. ``crop_features``); we flip status to ``running``, run the
        hook (which waits for the Asset and downloads the CSV), then write
        the new ``result`` payload to the DB and flip status to
        ``completed``.

      * **Pattern B without finalize()**: the adapter has no override
        (e.g. ``crop_classification`` if it ever lands in this branch);
        we just flip status to ``completed`` and leave the run() payload
        alone.

    Idempotency:
      * Already terminal (failed/cancelled) → bail.
      * Already completed → bail (e.g. a previous sweep won the race).
      * Already running → bail (a previous sweep started finalize and is
        still waiting on the long GEE poll).

    Any exception inside the adapter's finalize() is caught here and
    written as ``status='failed'`` with a traceback in the logs — the
    caller never sees it.
    """
    full = job_store.get_job(job_id)
    if full is None:
        return
    status_now = full.get("status")
    if status_now in ("failed", "cancelled", "completed", "running"):
        # Either already terminal, or another sweep is mid-finalize.
        return
    algorithm_id = full.get("algorithm")
    if not algorithm_id:
        return
    adapter = algorithm_registry.get_algorithm(algorithm_id)
    finalize = getattr(adapter, "finalize", None)

    job_dir = os.path.join(str(JOBS_DIR), job_id)

    if finalize is None:
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

    try:
        # Mark "running" while finalize is in flight. The early-return
        # guard at the top of _refresh_gee_status now matches "running",
        # so a second sweep 10 s later will skip this job and not pile
        # concurrent finalize() calls onto the same adapter.
        job_store.update_job(
            job_id,
            status="running",
            message="GEE 任务已完成，开始 finalize（下载结果文件）",
        )
        new_result = finalize(full, job_dir)
        if new_result is None:
            # Adapter declined to finalize (e.g. missing asset_id). Leave
            # the job in whatever state the adapter thinks is right.
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
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        try:
            job_store.update_job(
                job_id,
                status="failed",
                message=f"finalize 失败: {exc}",
            )
        except Exception:  # noqa: BLE001
            traceback.print_exc()


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
        try:
            try:
                # Initialize EE against the job's OWN project. Calling
                # ``ee.Initialize()`` with no argument (which is what would
                # happen if we relied solely on EE_PROJECT or omitted the
                # arg here) silently rebinds to the OAuth quota_project
                # and breaks subsequent getTaskStatus() calls — see the
                # docstring above for the failure mode this guards against.
                from app.services.gee_auth import initialize_ee
                if project:
                    initialize_ee(project)
                else:
                    # No project available (e.g. a malformed DB row);
                    # skip the re-init rather than risk wiping whatever
                    # binding the previous run established.
                    pass
            except Exception:
                # Already initialized in another thread, or auth missing — try anyway.
                pass
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