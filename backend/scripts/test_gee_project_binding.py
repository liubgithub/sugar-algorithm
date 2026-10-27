"""Regression tests for the process-wide GEE project binding.

Run with plain Python (no pytest needed)::

    python scripts/test_gee_project_binding.py

Nothing here touches the network: ``ee.Initialize`` / ``ee.Reset`` and
``ee.data._get_state`` are all replaced with in-memory fakes, so the tests
assert on our own bookkeeping rather than on what GEE happens to allow the
account you happen to be authenticated as.

The bugs these guard against
----------------------------
1. ``initialize_ee`` used to trust ``_CURRENT_PROJECT`` — "the project we
   last passed to ``ee.Initialize``" — to decide it could skip a
   re-``Initialize``. But ``ee.Initialize`` writes the project into
   earthengine-api's module state *before* validating it, so a failed
   Initialize left EE bound to the bad project while our cache still
   advertised the previous one. Everything after that went out under the
   wrong project.

2. Even with correct bookkeeping, EE has ONE binding per *process*. The
   background status poller rebinding to another job's project mid-run made
   the running algorithm fail with "Caller does not have required
   permission to use project <some other job's project>" — a name the
   operator never selected and cannot fix by re-authenticating.

Both are invisible to a single-threaded smoke test, so the concurrency case
below first *demonstrates* the race against the unguarded code path, then
shows the guarded one holding.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ee  # noqa: E402

from app.services import gee_auth  # noqa: E402


class FakeState:
    """Stand-in for earthengine-api's per-process project binding."""

    def __init__(self, project: str | None = None) -> None:
        self.cloud_api_user_project: str | None = project


def _patched(state: FakeState, mtime: float = 1000.0):
    """Patch out every earthengine-api touchpoint and reset our cache."""

    def fake_initialize(*_args, **kwargs):
        # ee binds the project BEFORE validating it — that ordering is the
        # whole reason the cache used to drift.
        state.cloud_api_user_project = kwargs.get("project")

    return (
        mock.patch.object(gee_auth, "_credentials_file_mtime", return_value=mtime),
        mock.patch.object(ee.data, "_get_state", return_value=state),
        mock.patch.object(ee, "Initialize", side_effect=fake_initialize),
        mock.patch.object(ee, "Reset"),
    )


class Results:
    def __init__(self) -> None:
        self.failed: list[str] = []

    def check(self, label: str, condition: bool) -> None:
        print(("  PASS  " if condition else "  FAIL  ") + label)
        if not condition:
            self.failed.append(label)

    @property
    def ok(self) -> bool:
        return not self.failed


def test_race_is_real_without_a_session(r: Results) -> None:
    """Demonstrate the bug: unguarded rebinding corrupts an in-flight run."""
    print("1) race reproduced against the UNGUARDED code path")
    state = FakeState()
    patches = _patched(state)
    with patches[0], patches[1], patches[2], patches[3]:
        gee_auth._CURRENT_PROJECT = None
        gee_auth._CREDENTIALS_MTIME = None
        gee_auth.initialize_ee("PROJ_A")

        observed: list[str | None] = []

        def running_job() -> None:
            # Stands in for adapter.run(): one Initialize, then many GEE calls.
            for _ in range(40):
                observed.append(state.cloud_api_user_project)
                time.sleep(0.002)

        def status_poller() -> None:
            # Stands in for the old _probe(): rebinds, then queries.
            time.sleep(0.01)
            gee_auth.initialize_ee("PROJ_B")

        t1 = threading.Thread(target=running_job)
        t2 = threading.Thread(target=status_poller)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        corrupted = [p for p in observed if p != "PROJ_A"]

    r.check(
        f"mid-run binding WAS corrupted ({len(corrupted)}/{len(observed)} calls "
        "went out under the wrong project)",
        len(corrupted) > 0,
    )
    print("       -> if this ever stops failing, the test below has stopped "
          "proving anything; tighten the timings")


def test_session_pins_the_binding(r: Results) -> None:
    """The fix: hold the GEE lock for the whole run, skip busy probes."""
    print("2) same race with gee_session + gee_probe_lock")
    state = FakeState()
    patches = _patched(state)
    with patches[0], patches[1], patches[2], patches[3]:
        gee_auth._CURRENT_PROJECT = None
        gee_auth._CREDENTIALS_MTIME = None

        observed: list[str | None] = []
        skipped: list[int] = []

        def running_job() -> None:
            with gee_auth.gee_session("PROJ_A"):
                for _ in range(40):
                    observed.append(state.cloud_api_user_project)
                    time.sleep(0.002)

        def status_poller() -> None:
            time.sleep(0.01)
            with gee_auth.gee_probe_lock(0.05) as acquired:
                if acquired:
                    gee_auth.initialize_ee("PROJ_B")
                else:
                    skipped.append(1)

        t1 = threading.Thread(target=running_job)
        t2 = threading.Thread(target=status_poller)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        corrupted = [p for p in observed if p != "PROJ_A"]

        r.check("zero mid-run corruption", not corrupted)
        r.check("busy poller skipped the cycle instead of stealing", len(skipped) == 1)
        r.check("binding intact afterwards", state.cloud_api_user_project == "PROJ_A")

        # The lock must not leak: a later probe has to be able to run.
        with gee_auth.gee_probe_lock(0.05) as acquired:
            r.check("probe lock released once the run finishes", acquired is True)


def test_fast_path_trusts_the_real_binding(r: Results) -> None:
    """The cache must never be able to mask a foreign binding."""
    print("3) fast path consults earthengine-api's own state")
    state = FakeState("OTHER")  # someone rebound EE behind our back
    calls: list[str | None] = []

    def fake_initialize(*_args, **kwargs):
        calls.append(kwargs.get("project"))
        state.cloud_api_user_project = kwargs.get("project")

    with mock.patch.object(gee_auth, "_credentials_file_mtime", return_value=1000.0), \
         mock.patch.object(ee.data, "_get_state", return_value=state), \
         mock.patch.object(ee, "Initialize", side_effect=fake_initialize), \
         mock.patch.object(ee, "Reset"):
        gee_auth._CURRENT_PROJECT = "PROJ_A"  # stale cache claims we're set
        gee_auth._CREDENTIALS_MTIME = 1000.0
        gee_auth.initialize_ee("PROJ_A")

    r.check("re-initialized despite a matching _CURRENT_PROJECT", calls == ["PROJ_A"])
    r.check("EE ends bound to the requested project",
            state.cloud_api_user_project == "PROJ_A")


def test_failed_initialize_leaves_no_desync(r: Results) -> None:
    """A failed Initialize must not poison later calls."""
    print("4) failed Initialize leaves no desync behind")
    state = FakeState()

    def fake_initialize(*_args, **kwargs):
        state.cloud_api_user_project = kwargs.get("project")
        if kwargs.get("project") == "FORBIDDEN":
            raise RuntimeError(
                "Caller does not have required permission to use project FORBIDDEN"
            )

    with mock.patch.object(gee_auth, "_credentials_file_mtime", return_value=1000.0), \
         mock.patch.object(ee.data, "_get_state", return_value=state), \
         mock.patch.object(ee, "Initialize", side_effect=fake_initialize), \
         mock.patch.object(ee, "Reset"):
        gee_auth._CURRENT_PROJECT = None
        gee_auth._CREDENTIALS_MTIME = None

        raised = False
        try:
            gee_auth.initialize_ee("FORBIDDEN")
        except gee_auth.GeeAuthRequired:
            raised = True

        r.check("GeeAuthRequired raised", raised)
        r.check("_CURRENT_PROJECT reset", gee_auth._CURRENT_PROJECT is None)
        r.check("_CREDENTIALS_MTIME reset", gee_auth._CREDENTIALS_MTIME is None)

        gee_auth.initialize_ee("PROJ_A")
        r.check("next call rebinds for real", state.cloud_api_user_project == "PROJ_A")


def test_credentials_rotation_forces_reinit(r: Results) -> None:
    """`earthengine authenticate --force` must be picked up without a restart."""
    print("5) credential rotation forces a re-Initialize")
    state = FakeState()
    calls: list[str | None] = []

    def fake_initialize(*_args, **kwargs):
        calls.append(kwargs.get("project"))
        state.cloud_api_user_project = kwargs.get("project")

    with mock.patch.object(gee_auth, "_credentials_file_mtime", return_value=1000.0), \
         mock.patch.object(ee.data, "_get_state", return_value=state), \
         mock.patch.object(ee, "Initialize", side_effect=fake_initialize), \
         mock.patch.object(ee, "Reset"):
        gee_auth._CURRENT_PROJECT = None
        gee_auth._CREDENTIALS_MTIME = None
        gee_auth.initialize_ee("PROJ_A")
        r.check("fast path on the second identical call", calls == ["PROJ_A"])

        calls.clear()
        gee_auth._CURRENT_PROJECT = "PROJ_A"
        gee_auth._CREDENTIALS_MTIME = 1000.0
        gee_auth._credentials_file_mtime = lambda: 2000.0  # user re-authed
        gee_auth.initialize_ee("PROJ_A")
        r.check("re-initialized after the file changed", calls == ["PROJ_A"])


def test_unknown_binding_is_treated_as_untrusted(r: Results) -> None:
    """If we cannot read earthengine-api's state, never take the fast path."""
    print("6) unreadable binding falls back to re-initializing")
    state = FakeState()
    calls: list[str | None] = []

    def fake_initialize(*_args, **kwargs):
        calls.append(kwargs.get("project"))
        state.cloud_api_user_project = kwargs.get("project")

    with mock.patch.object(gee_auth, "_credentials_file_mtime", return_value=1000.0), \
         mock.patch.object(ee.data, "_get_state", side_effect=AttributeError), \
         mock.patch.object(ee, "Initialize", side_effect=fake_initialize), \
         mock.patch.object(ee, "Reset"):
        gee_auth._CURRENT_PROJECT = "PROJ_A"
        gee_auth._CREDENTIALS_MTIME = 1000.0
        gee_auth.initialize_ee("PROJ_A")

    r.check("re-initialized when the binding is unreadable", calls == ["PROJ_A"])


def test_idle_hands_the_lock_over_during_waits(r: Results) -> None:
    """A job sleeping between GEE polls must not starve a second job.

    ``harvest`` blocks inside ``run()`` for up to two hours waiting on an
    export. ``_run_gee`` wraps the whole run in ``gee_session``, so without
    ``gee_idle`` that sleep holds the process-wide lock the entire time and
    every other GEE job sits at "提交 GEE 任务" forever.
    """
    print("7) long waits release the lock; other jobs still get a turn")
    state = FakeState()
    patches = _patched(state)
    with patches[0], patches[1], patches[2], patches[3]:
        gee_auth._CURRENT_PROJECT = None
        gee_auth._CREDENTIALS_MTIME = None

        got_turn = threading.Event()
        observed: list[str | None] = []
        second_job_bound: list[str | None] = []

        def slow_export_job() -> None:
            # Stands in for harvest.run(): submit, then poll for 2 hours.
            # The read happens *outside* gee_idle (under the session lock,
            # exactly like task.status()), and only the sleep yields it.
            with gee_auth.gee_session("PROJ_A"):
                for _ in range(5):
                    observed.append(state.cloud_api_user_project)
                    with gee_auth.gee_idle():
                        got_turn.wait(0.02)
                        time.sleep(0.01)
                observed.append(state.cloud_api_user_project)

        def second_job() -> None:
            time.sleep(0.01)  # land inside the first job's sleep
            with gee_auth.gee_probe_lock(1.0) as acquired:
                if acquired:
                    gee_auth.initialize_ee("PROJ_B")
                    second_job_bound.append(state.cloud_api_user_project)
                    got_turn.set()

        t1 = threading.Thread(target=slow_export_job)
        t2 = threading.Thread(target=second_job)
        t1.start()
        t2.start()
        t1.join(5)
        t2.join(5)

        r.check("second job got the lock mid-wait", got_turn.is_set())
        r.check("second job really did rebind EE", second_job_bound == ["PROJ_B"])
        # gee_idle re-binds on resume, so the waiter's own reads stay correct
        # even though EE was pointed at another project while it slept.
        r.check(
            "waiter re-bound to its own project after the handoff",
            bool(observed) and all(p == "PROJ_A" for p in observed),
        )

        with gee_auth.gee_probe_lock(0.05) as acquired:
            r.check("lock fully released once both jobs finish", acquired is True)


def test_idle_is_a_noop_outside_a_session(r: Results) -> None:
    """Helpers can be driven by scripts/tests without a surrounding session."""
    print("8) gee_idle outside a session is a harmless no-op")
    state = FakeState("PROJ_A")
    patches = _patched(state)
    with patches[0], patches[1], patches[2], patches[3]:
        gee_auth._CURRENT_PROJECT = "PROJ_A"
        gee_auth._CREDENTIALS_MTIME = 1000.0

        with gee_auth.gee_idle():
            pass

        r.check("no exception, binding untouched", state.cloud_api_user_project == "PROJ_A")
        with gee_auth.gee_probe_lock(0.05) as acquired:
            r.check("lock was never taken or left held", acquired is True)


def main() -> int:
    r = Results()
    test_race_is_real_without_a_session(r)
    print()
    test_session_pins_the_binding(r)
    print()
    test_fast_path_trusts_the_real_binding(r)
    print()
    test_failed_initialize_leaves_no_desync(r)
    print()
    test_credentials_rotation_forces_reinit(r)
    print()
    test_unknown_binding_is_treated_as_untrusted(r)
    print()
    test_idle_hands_the_lock_over_during_waits(r)
    print()
    test_idle_is_a_noop_outside_a_session(r)
    print()
    if r.ok:
        print("ALL PASS")
        return 0
    print(f"{len(r.failed)} FAILED:")
    for label in r.failed:
        print("  -", label)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())