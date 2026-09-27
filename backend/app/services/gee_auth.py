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
"""
from __future__ import annotations

import os
import threading
from typing import Optional

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


class GeeAuthRequired(RuntimeError):
    """Raised when cached GEE credentials are bound to a different project.

    The message is intentionally user-facing: it tells the operator
    exactly which command to run on their workstation before retrying.
    The frontend renders this string verbatim in the failed-job card.
    """

    def __init__(self, project: str) -> None:
        self.project = project
        super().__init__(
            f"当前 GEE 认证与请求项目 {project!r} 不匹配。"
            " 请在本机执行：\n\n"
            "    earthengine authenticate --force\n\n"
            "完成后重新运行本算法即可。"
        )


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


def initialize_ee(project: Optional[str]) -> None:
    """Initialize Earth Engine, or raise ``GeeAuthRequired``.

    Idempotent for the same project *and* the same on-disk credentials
    file. The first call performs ``ee.Initialize``; subsequent calls
    with the same project + unchanged credentials file are no-ops.
    When the project changes, OR when the credentials file on disk has
    been updated (e.g. the user just ran ``earthengine authenticate
    --force`` on their workstation), we force an ``ee.Reset()`` so
    earthengine-api drops its in-memory credential cache and re-reads
    the file before re-initializing.

    Without the credentials-mtime check the FastAPI process would happily
    keep using the credentials it loaded at startup, and every algorithm
    run would fail with the project-mismatch error until the server was
    restarted. Tracking the mtime fixes that without sacrificing the
    fast-path optimization in the common case (no re-auth happened).

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
            return

        import ee

        # Detect user-driven re-authentication on the workstation side.
        # `earthengine authenticate --force` rewrites the credentials
        # file, but the FastAPI process's in-memory OAuth credentials
        # are still the old ones. Comparing the file's mtime against
        # what we saw at last successful init tells us when to flush
        # the cache. If the file vanished (deleted), treat that the
        # same as "credentials changed" and force a re-init attempt.
        current_mtime = _credentials_file_mtime()
        creds_changed = (
            _CURRENT_PROJECT is not None
            and (
                current_mtime is None
                or _CREDENTIALS_MTIME is None
                or current_mtime > _CREDENTIALS_MTIME + 0.001
            )
        )
        if creds_changed:
            _CURRENT_PROJECT = None
            try:
                ee.Reset()
            except Exception:
                # Reset is best-effort; if it fails we'll still attempt
                # Initialize below and surface the real error to the user.
                pass

        # Fast path: same project + unchanged credentials file as last
        # successful init -> nothing to do.
        if project == _CURRENT_PROJECT:
            return

        # Project changed (or first call with a project) — DO NOT try to
        # re-authenticate from here. Either the cached credentials still
        # work for `project` (then we just Initialize) or they don't (then
        # ee.Initialize will fail). We let ee tell us by attempting the
        # call; if it succeeds we update the cache, if it fails we raise
        # the actionable error.
        #
        # The previous implementation called ee.Reset() before this point,
        # but that just hides the real error from the user — the GEE
        # backend still rejects calls because the OAuth scope is wrong.
        # Skipping Reset lets the natural error bubble up cleanly.
        if not project:
            ee.Initialize()
            _CURRENT_PROJECT = None
            _CREDENTIALS_MTIME = current_mtime
            return

        try:
            ee.Initialize(project=project)
        except Exception as exc:
            # Surface a precise, user-facing error. We deliberately do not
            # call ee.Authenticate / spawn gcloud here — see module docstring.
            raise GeeAuthRequired(project) from exc

        _CURRENT_PROJECT = project
        _CREDENTIALS_MTIME = current_mtime


def current_ee_project() -> Optional[str]:
    """Return the project the helper last successfully bound credentials to."""
    return _CURRENT_PROJECT


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
    "current_ee_project",
    "GeeAuthRequired",
    "is_auth_mismatch_error",
]