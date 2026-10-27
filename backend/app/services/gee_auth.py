"""Shared Earth Engine initialization with project-aware mismatch detection.

The four GEE algorithms historically duplicated the same `_initialize_ee`
helper. The earlier version of this module tried to *fix* a project
mismatch by automatically calling `ee.Authenticate(force=True, ...)` or
spawning `gcloud auth application-default login --project=<p>`. That
turned out to be the wrong layer to do it from:

- on a headless server it has no chance to drive a browser OAuth flow
  (the user is not at the keyboard of the FastAPI process);
- spawning `gcloud` from inside a web request is invisible to the user
  who actually owns the credentials — they can't react to the prompt;
- any failure surfaces as a generic error far away from the cause.

This module therefore **does not perform authentication**. Its job is to
detect that the requested project differs from the one the cached
credentials were authorized against and to surface a precise,
actionable error that asks the user to run

    earthengine authenticate --force

on their workstation. The backend stays out of the OAuth flow entirely.

Why this works: `~/.config/earthengine/credentials` records a fixed
`quota_project` field the first time `ee.Authenticate()` runs. The GEE
backend then rejects any `ee.Initialize(project=...)` whose target
project does not match the OAuth scope. The only official cure is to
force re-consent (`--force`), which produces credentials with the new
project bound in. After the user does this once on their own machine,
the same `initialize_ee` call succeeds without further prompts.

The process-global EE binding — read this before touching anything
--------------------------------------------------------------------
``ee`` keeps ONE project binding per *process*, not per job. Every
``ee.Initialize(project=X)`` overwrites it, from any thread, for every
algorithm sharing the FastAPI process. Concretely, in earthengine-api
1.7.x ``ee.Initialize`` runs::

    data.initialize(..., project=project)   # writes X into ee.data state,
                                           # NO permission check
    ApiFunction.initialize()                # ~1.4 s round trip that does
                                           # NOT validate the project

Two consequences drive the design of :func:`initialize_ee` below:

1. **The permission error is deferred and mis-attributed.** Nothing at
   ``Initialize`` time checks whether the caller may use X; the check
   happens on the next real project-scoped call (``getAsset`` /
   ``getTaskStatus`` / ``getDownloadURL`` / ``Export.*``), and the error
   names X — the project we last *asked* for, which is not necessarily
   the project the user wants.
2. **A failed ``Initialize`` still leaves EE bound to the bad project.**
   So a bookkeeping cache that records "the last project we passed"
   can disagree with what ``ee`` is actually using. Trusting that cache
   to skip a re-``Initialize`` is what produced user-visible errors like
   ``Caller does not have required permission to use project guangxi-try``
   on runs that had correctly switched to a different project — see
   :func:`_bound_project` for how we read the real value instead.

Rule of thumb for every GEE entry point in this codebase: **call
:func:`initialize_ee` with the project that owns the work, immediately
before the first GEE call — including inside ``finalize()``**, which runs
on the poller thread long after the submitting ``run()`` returned.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from typing import Any, Dict, Iterator, Optional

_CURRENT_PROJECT: Optional[str] = None
# mtime (seconds since epoch) of the credentials file we last bound
# `_CURRENT_PROJECT` to. Compared on every call so that when the user
# runs `earthengine authenticate --force` on their workstation — which
# only updates the on-disk credentials file — the running FastAPI
# process notices the change and forces earthengine-api to drop its
# in-memory credential cache via `ee.Reset()`. Without this check the
# FastAPI process would happily keep using whatever credentials it had
# loaded at startup, and every subsequent algorithm run would fail
# with the project-mismatch error until the server was restarted.
_CREDENTIALS_MTIME: Optional[float] = None
_LOCK = threading.Lock()

# Serializes every GEE interaction in this process.
#
# earthengine-api keeps exactly ONE project binding per *process*, shared by
# every thread — algorithms, HTTP handlers and the background status poller
# all share it. `initialize_ee` can therefore only make the binding correct
# at the instant it returns; it cannot stop a concurrent thread from
# rebinding EE a moment later while our own GEE calls are still in flight.
# That race is directly observable: a crop_classification run submitted with
# project A fails with "Caller does not have required permission to use
# project B", where B is whatever the poller happened to be probing for some
# other job at that moment — a project the operator never selected and
# cannot fix by re-authenticating.
#
# So the cure is not "bind more carefully" but "do not use GEE concurrently".
# Holders: `_run_gee` (a whole algorithm run), `_dispatch_finalize` (the
# result download) and `_query_gee_status._probe` (the task-status check).
# Non-GEE algorithms never take it and are unaffected.
_GEE_LOCK = threading.RLock()
# Per-thread bookkeeping for :func:`gee_session` / :func:`gee_idle`.
_GEE_SESSION_DEPTH: Dict[int, int] = {}
_GEE_SESSION_PROJECT: Dict[int, Optional[str]] = {}


@contextlib.contextmanager
def gee_session(project: Optional[str]) -> Iterator[None]:
    """Hold the process-wide GEE binding steady for the duration of the block.

    Re-binds EE to `project` on entry and keeps every other thread out of
    GEE until the block exits, so a run's many GEE calls cannot have the
    project swapped underneath them halfway through.

    A falsy `project` still takes the lock — callers that resolve no project
    at all should skip the session instead of calling this.

    Long *waits* belong inside this block (their GEE calls need the same
    guarantees) but must yield the lock while sleeping — see
    :func:`gee_idle`. Without that, a Pattern A algorithm that polls a
    two-hour export would hold the lock for two hours and stall every other
    GEE job in the process.
    """
    with _GEE_LOCK:
        tid = threading.get_ident()
        _GEE_SESSION_DEPTH[tid] = _GEE_SESSION_DEPTH.get(tid, 0) + 1
        _GEE_SESSION_PROJECT[tid] = project
        try:
            if project:
                initialize_ee(project)
            yield
        finally:
            depth = _GEE_SESSION_DEPTH.get(tid, 1) - 1
            if depth > 0:
                _GEE_SESSION_DEPTH[tid] = depth
            else:
                _GEE_SESSION_DEPTH.pop(tid, None)
                _GEE_SESSION_PROJECT.pop(tid, None)


@contextlib.contextmanager
def gee_idle() -> Iterator[None]:
    """Hand the process-wide GEE lock to other threads for the block.

    Meant for the long *sleeps* inside GEE work — polling a task that will
    take minutes or hours, waiting for an Asset to land. Nothing goes over
    the wire while the lock is yielded, so other jobs can safely use EE in
    the meantime. On resume the session's project is re-bound, so the wait
    loop stays correct even though the binding was changed while it slept.

    A no-op when the calling thread is not inside a :func:`gee_session`,
    which keeps this safe to call from helpers that are sometimes driven
    directly by a script rather than by the job service.
    """
    tid = threading.get_ident()
    depth = _GEE_SESSION_DEPTH.get(tid, 0)
    if depth == 0:
        yield
        return

    project = _GEE_SESSION_PROJECT.get(tid)
    _GEE_SESSION_DEPTH[tid] = depth - 1
    _GEE_LOCK.release()
    try:
        yield
    finally:
        _GEE_LOCK.acquire()
        _GEE_SESSION_DEPTH[tid] = depth
        if project:
            initialize_ee(project)


@contextlib.contextmanager
def gee_probe_lock(timeout: float) -> Iterator[bool]:
    """Best-effort variant of :func:`gee_session` for the status poller.

    Yields ``True`` when the GEE lock was acquired, ``False`` when another
    thread held it for longer than `timeout`. The poller treats False as
    "skip this probe cycle": a task-status check is cheap, idempotent and
    retried every few seconds anyway, so deferring it is always better than
    stalling an HTTP request behind a long-running job — or disturbing that
    job's project binding.
    """
    acquired = _GEE_LOCK.acquire(timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired:
            _GEE_LOCK.release()


# ----------------------------------------------------------------------
# End-to-end flow (for the next maintainer reading this module)
# ----------------------------------------------------------------------
#
# 1. Operator starts the FastAPI process. earthengine-api reads
#    ~/.config/earthengine/credentials once and caches the OAuth tokens
#    and `quota_project` field in memory.
# 2. Operator opens the frontend and submits an algorithm with project
#    "sodium-ray-505904-i3" (different from the cached project).
# 3. The algorithm calls `initialize_ee("sodium-ray-505904-i3")`.
#    We compare against `_CURRENT_PROJECT`; they differ, so we call
#    `ee.Initialize(project="sodium-ray-505904-i3")` and let earthengine-api
#    surface the GEE backend's real error:
#        "Caller does not have required permission to use project X".
# 4. We catch that error here and re-raise as `GeeAuthRequired(project)`
#    with a user-facing message: "请在本机执行 earthengine authenticate --force".
# 5. The algorithm catches `GeeAuthRequired`, returns
#    `make_result(status="failed", message=str(exc))`. Job status becomes
#    `failed`; result.message carries the full instruction.
# 6. The frontend renders the message in a red preformatted block with a
#    "复制命令" button so the operator can paste `earthengine authenticate
#    --force` straight into their workstation terminal.
# 7. The OAuth flow runs in the operator's browser; the credentials file
#    on disk is rewritten with the new project bound.
# 8. The operator clicks "重新运行" (or submits the same algorithm again).
#    `initialize_ee` sees `_CURRENT_PROJECT` is None (we never wrote it on
#    failure), so it calls `ee.Initialize(project=...)` — which now
#    succeeds because the credentials are valid for that project.
#
# Why step 4 re-raises instead of auto-calling `ee.Authenticate(force=True)`:
# the FastAPI process has no interactive terminal, no keyboard, no
# browser. Any OAuth prompt would silently hang and the request would
# eventually time out. The user is at their workstation, not the server,
# so the workstation is where the OAuth dance must happen.


# How many times ``ee.Initialize`` may be retried when it fails with a
# *transport* error.  The call fetches the API discovery document through
# the machine's proxy; a proxy that drops one connection should not abort
# a job whose remaining 300 tile downloads would each retry on their own.
_INIT_ATTEMPTS = 5


class GeeAuthRequired(RuntimeError):
    """Raised when cached GEE credentials are bound to a different project.

    The message is intentionally user-facing: it tells the operator
    exactly which command to run on their workstation before retrying.
    The frontend renders this string verbatim in the failed-job card.
    """

    def __init__(self, project: str) -> None:
        self.project = project
        super().__init__(
            f"当前 GEE 认证与请求项目 {project!r} 不匹配。\n\n"
            "请按顺序确认两件事：\n\n"
            "1) 本机重新认证（换用对该项目有权限的 Google 账号）：\n\n"
            "       earthengine authenticate --force\n\n"
            "2) 该账号需要在项目 IAM 中拥有 roles/serviceusage.serviceUsageConsumer 角色：\n\n"
            f"       https://console.cloud.google.com/iam-admin/iam?project={project}\n\n"
            "两件事都完成后重新运行本算法即可。"
        )


class GeeNetworkError(RuntimeError):
    """Raised when GEE cannot be reached at all — a transport failure.

    Distinct from :class:`GeeAuthRequired` on purpose.  ``ee.Initialize``
    does a network round trip (it fetches the API discovery document), so
    a dead proxy or a blocked connection surfaces *there* — and reporting
    it as "your credentials do not match this project, run
    ``earthengine authenticate --force``" sends the operator to redo a
    perfectly fine authentication while the actual problem is the network
    path.

    The message therefore names the network as the suspect.
    """

    def __init__(self, detail: str = "") -> None:
        self.detail = detail
        super().__init__(
            "无法连接 GEE 服务（网络 / 代理不通）。\n\n"
            "ee.Initialize 会在初始化时访问 earthengine.googleapis.com，"
            "这一步失败说明请求根本没到达 GEE，与账号权限无关。\n\n"
            "请检查：\n"
            "1) 本机代理（Clash / v2ray 等）是否正常运行、节点是否可用；\n"
            "2) 代理是否已应用到系统设置（Windows：设置 → 网络和 Internet → 代理）；\n"
            "3) 直连是否可达（若必须走代理，这是正常现象）。\n\n"
            "网络恢复后重新运行本算法即可；已经下载过的图块会从断点继续，"
            "不会重头再来。\n\n"
            f"原始错误：{detail}"
        )


def is_network_error(exc: BaseException) -> bool:
    """Return True when ``exc`` is a transport failure, not a credentials problem.

    Recognises the shapes a dead proxy / unreachable host produces:
    ``requests`` connection errors, ``urllib3`` protocol errors, httplib2
    and google-auth transport errors, plus the socket-level resets those
    libraries wrap in plain ``OSError``.
    """
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True

    # httplib2 / google-auth wrap the underlying socket error.
    text = str(exc) or ""
    needles = (
        "connection aborted",
        "connection reset",
        "connection refused",
        "max retries exceeded",
        "failed to establish a new connection",
        "remote end closed connection",
        "timed out",
        "timeout",
        "temporary failure in name resolution",
        "name or service not known",
        "unable to connect to proxy",
        "proxyerror",
        "ssl eof",
        # Chinese markers used by the downloader itself, so its own
        # fast-fail message is recognised as a transport failure.
        "网络不通",
        "无法连接 gee",
    )
    lowered = text.lower()
    if any(needle in lowered for needle in needles):
        return True

    # Walk the __cause__ / __context__ chain: the useful type usually sits
    # at the bottom (e.g. requests.ConnectionError <- urllib3.ProtocolError
    # <- ConnectionResetError).
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ConnectionError, TimeoutError)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _credentials_file_mtime() -> Optional[float]:
    """mtime of the earthengine-api user-credentials file, or None.

    earthengine-api reads from ``EE_CREDENTIALS`` if set, else from
    ``~/.config/earthengine/credentials``. On Windows ``os.path.expanduser``
    uses ``%USERPROFILE%`` so ``~/.config/earthengine/credentials``
    resolves to ``C:\\Users\\<user>\\.config\\earthengine\\credentials``,
    which is the same place ``earthengine authenticate --force`` writes.
    """
    cred_path = os.environ.get(
        "EE_CREDENTIALS",
        os.path.expanduser("~/.config/earthengine/credentials"),
    )
    try:
        return os.path.getmtime(cred_path)
    except OSError:
        return None


def _bound_project() -> Optional[str]:
    """Return the project ``ee`` is *actually* bound to in this process.

    ``_CURRENT_PROJECT`` records what we last *passed* to
    ``ee.Initialize``, which is not the same thing. ``ee.Initialize``
    writes the project into earthengine-api's own module state before
    any permission check runs, so an ``Initialize`` that goes on to
    raise still leaves EE bound to the project we just failed on — while
    our cache, updated only on success, keeps advertising the previous
    one. A fast path keyed on ``_CURRENT_PROJECT`` then skips the
    re-``Initialize`` and every later GEE call goes out under the wrong
    project, which the user sees as a permission error naming a project
    they never asked for.

    Reading earthengine-api's own state closes that gap.

    Returns ``None`` when the binding can't be determined (internals
    changed, or EE was never initialized). Callers must treat ``None``
    as "unknown — re-initialize", never as "already correct".
    """
    try:
        import ee

        state = ee.data._get_state()
        return getattr(state, "cloud_api_user_project", None) or None
    except Exception:  # noqa: BLE001
        return None


def _credentials_unchanged(current_mtime: Optional[float]) -> bool:
    """True when the credentials file has not moved since we last read it.

    A file absent at both observations counts as unchanged — only
    appearing, disappearing, or being rewritten forces a re-init.
    """
    if _CREDENTIALS_MTIME is None:
        return False
    if (current_mtime is None) != (_CREDENTIALS_MTIME is None):
        return False
    if current_mtime is None:
        return True
    return current_mtime <= _CREDENTIALS_MTIME + 0.001


def initialize_ee(project: Optional[str]) -> None:
    """Bind Earth Engine to `project`, or raise :class:`GeeAuthRequired`.

    Safe to call from any thread and any number of times. Because ``ee``
    holds a single process-wide project binding shared by every algorithm
    and by the background poller, "is EE already on the right project?"
    must be answered from earthengine-api's own state (see
    :func:`_bound_project`) — never from a local cache that only tracks
    what we last requested.

    The fast path (skipping ``ee.Initialize``) requires BOTH:

      * earthengine-api's live binding already equals ``project``, and
      * the credentials file on disk has not been rewritten since we
        bound it — so the operator's ``earthengine authenticate --force``
        is still picked up without a server restart.

    Anything else falls through to a real re-``Initialize``. That is what
    stops one algorithm's project from leaking into another's.

    Authentication itself is deliberately not performed here — see the
    module docstring.
    """
    global _CURRENT_PROJECT, _CREDENTIALS_MTIME

    with _LOCK:
        # Service-account path: re-initializing with a different project is
        # a pure client-side operation, no browser involved, so it's fine
        # to do automatically. Service accounts are not affected by the
        # OAuth scope problem that affects user credentials.
        sa_path = os.environ.get("EE_SERVICE_ACCOUNT_FILE")
        if sa_path and os.path.exists(sa_path):
            import ee

            credentials = ee.ServiceAccountCredentials.from_json_keyfile_name(sa_path)
            if project:
                ee.Initialize(credentials, project=project)
            else:
                ee.Initialize(credentials)
            _CURRENT_PROJECT = project
            _CREDENTIALS_MTIME = _credentials_file_mtime()
            return

        import ee

        current_mtime = _credentials_file_mtime()
        bound = _bound_project()
        creds_ok = _credentials_unchanged(current_mtime)

        # Fast path: EE is genuinely on `project` AND the operator has not
        # rotated credentials since we bound it. ``project`` must be
        # truthy — with no project requested, `ee.Initialize()` resolves the
        # target from the credentials, so there is nothing stable to compare.
        if project and bound == project and creds_ok:
            return

        # The operator ran `earthengine authenticate --force` on their
        # workstation, which rewrites the file while this process keeps the
        # old tokens in memory. Drop earthengine-api's in-memory credential
        # cache so the Initialize below re-reads the file from disk.
        if not creds_ok and _CREDENTIALS_MTIME is not None:
            try:
                ee.Reset()
            except Exception:  # noqa: BLE001
                # Best-effort; the Initialize below surfaces any real error.
                pass

        # Project changed / binding unknown / credentials rotated — rebind.
        # DO NOT try to re-authenticate from here. Either the cached
        # credentials work for `project` (then we just Initialize) or they
        # don't (then ee.Initialize fails). We let ee tell us by attempting
        # the call; we deliberately do not call ee.Authenticate or spawn
        # gcloud here — see the module docstring.
        #
        # ``ee.Initialize`` fetches the API discovery document, i.e. it
        # makes a real HTTPS request through the machine's proxy.  A flaky
        # proxy therefore kills the *whole* job at this one point — even
        # though the 315 tile downloads that follow would have retried
        # their way through.  So: retry a transport failure a few times
        # before giving up.
        last_exc: Optional[BaseException] = None
        for attempt in range(1, _INIT_ATTEMPTS + 1):
            try:
                if project:
                    ee.Initialize(project=project)
                else:
                    ee.Initialize()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                # `ee.Initialize` binds the project BEFORE validating it, so
                # EE is now pointed at the project we just failed on. Clear
                # our cache and Reset so the next call rebinds for real
                # instead of trusting a `_CURRENT_PROJECT` that no longer
                # describes reality.
                _CURRENT_PROJECT = None
                _CREDENTIALS_MTIME = None
                try:
                    ee.Reset()
                except Exception:  # noqa: BLE001
                    pass
                if not is_network_error(exc):
                    # A permission / auth problem: retrying changes nothing.
                    raise GeeAuthRequired(
                        project or bound or "未指定项目"
                    ) from exc
                if attempt < _INIT_ATTEMPTS:
                    time.sleep(min(2.0 ** attempt, 15.0))

        if last_exc is not None:
            # A transport failure is NOT an auth failure.  Telling the
            # operator to re-authenticate sends them off to fix credentials
            # that were never the problem.  Only genuine permission errors
            # get the re-authenticate treatment.
            raise GeeNetworkError(str(last_exc)) from last_exc

        _CURRENT_PROJECT = project
        _CREDENTIALS_MTIME = current_mtime


def project_for_job(job: Dict[str, Any]) -> Optional[str]:
    """Best-effort GEE project for a stored job row.

    ``finalize()`` runs on the poller thread, minutes after the submitting
    ``run()`` returned, so it cannot rely on whatever EE happened to be
    bound to at the time. It must ask which project *this job* belongs to
    and call :func:`initialize_ee` with that, before any GEE call.

    Resolution order:

      1. ``job.params.gee_project_id`` / ``job.params.gee_project``.
      2. The project segment of an asset id in ``job.result.metrics``
         (``projects/<project>/assets/<name>``) — recovers jobs submitted
         before ``gee_project_id`` was canonical, and jobs whose params row
         was pruned.

    Returns ``None`` when neither yields a project; callers should treat
    that as "cannot safely touch GEE" rather than guessing.
    """
    job = job or {}
    params = job.get("params") or {}
    project = params.get("gee_project_id") or params.get("gee_project")
    if project:
        return str(project)

    metrics = (job.get("result") or {}).get("metrics") or {}
    for key in (
        "asset_id",
        "sample_asset_id",
        "roi_asset_id",
        "sugarcane_mask_asset",
        "output_tif_name",
    ):
        ref = metrics.get(key)
        if isinstance(ref, str) and ref.startswith("projects/") and "/assets/" in ref:
            return ref.split("/")[1]
    return None


def current_ee_project() -> Optional[str]:
    """Return the project EE is bound to right now.

    Prefers earthengine-api's live binding over the last project we
    *requested*, because those can legitimately disagree mid-flight (a
    rebind that failed, another thread calling ``initialize_ee``). Falls
    back to the request cache only when the live state is unreadable.
    """
    bound = _bound_project()
    return bound if bound is not None else _CURRENT_PROJECT


def raise_if_auth_mismatch(exc: BaseException, project: str) -> None:
    """Re-raise `exc` as :class:`GeeAuthRequired` when it's a credential problem.

    ``ee.Initialize(project=...)`` never talks to the GEE backend — it only
    writes the project into the client's local state. So the project /
    credential mismatch described in the module docstring does **not**
    surface at init time; it surfaces at the *first real API call*
    (``ee.data.getAsset``, an Export, a ``getInfo``...). That means every
    call site that wraps a GEE failure in a friendly "无法访问 Asset" /
    "上传失败" message has to translate it back, otherwise the user gets
    a dead end with no hint that they need to re-authenticate.

    Call this first thing inside such an ``except`` block:

        except Exception as exc:
            raise_if_auth_mismatch(exc, project)
            raise RuntimeError(f"无法访问 GEE {label} Asset：{asset_id}…") from exc

    Returns ``None`` when `exc` is an ordinary failure (asset typo, missing
    geometry, quota, ...) so the caller can raise its own message.
    """
    if isinstance(exc, GeeAuthRequired) or is_auth_mismatch_error(exc):
        raise GeeAuthRequired(project) from exc


def is_auth_mismatch_error(exc: BaseException) -> bool:
    """Return True if `exc` looks like a GEE OAuth / project permission error.

    earthengine-api surfaces these errors with text such as
    "Caller does not have required permission to use project X" or
    "Request had invalid auth credentials", regardless of the exception
    class. Treat any of those as a project / auth mismatch so the caller
    can substitute a friendly, actionable error instead of dumping the
    raw stack to the user.
    """
    msg = str(exc) or ""
    lowered = msg.lower()
    needles = (
        "caller does not have required permission",
        "permission to use project",
        "permission denied",
        "invalid auth credentials",
        "request had invalid auth",
        "authentication credentials",
        "serviceusage.serviceusageconsumer",
    )
    return any(needle in lowered for needle in needles)


__all__ = [
    "initialize_ee",
    "gee_session",
    "gee_idle",
    "gee_probe_lock",
    "current_ee_project",
    "project_for_job",
    "GeeAuthRequired",
    "GeeNetworkError",
    "is_network_error",
    "is_auth_mismatch_error",
    "raise_if_auth_mismatch",
]