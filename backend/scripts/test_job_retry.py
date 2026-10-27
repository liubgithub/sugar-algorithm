"""Tests for the "重新拉取" (retry) path and the network-error classification.

Two things are checked here, both against a throw-away SQLite database so
the real job store is never touched:

  1. ``job_service.retry_job`` puts a terminal job back into a non-terminal
     state — re-dispatching ``finalize`` when GEE already has the asset,
     re-submitting the algorithm when it does not — and refuses to touch a
     job that is still running / completed.
  2. A transport failure is reported as a *network* problem, not as a
     credentials problem.  The old code wrapped every ``ee.Initialize``
     failure in ``GeeAuthRequired``, which told the operator to re-run
     ``earthengine authenticate --force`` for what was really a dead proxy.

Run from ``backend/``::

    python scripts/test_job_retry.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import job_service  # noqa: E402
from app.services.gee_auth import (  # noqa: E402
    GeeAuthRequired,
    GeeNetworkError,
    is_network_error,
)
from app.storage import job_store  # noqa: E402


# ---------------------------------------------------------------------------
# 1. retry_job
# ---------------------------------------------------------------------------


def _make_job(tmpdb: str, *, status: str, result: dict, params: dict | None = None) -> str:
    job = job_store.create_job("crop_classification", params or {"gee_project_id": "p"})
    job_store.update_job(job["job_id"], status=status, result=result)
    return job["job_id"]


def test_retry_dispatches_finalize(tmpdb: str) -> None:
    print("\n=== 1. finalize 失败 → 重新拉取 ===")
    job_id = _make_job(
        tmpdb,
        status="failed",
        result={
            "gee_task_id": "T1",
            "metrics": {"asset_id": "projects/p/assets/CROPClassification_2025"},
        },
    )

    dispatched: list[str] = []
    original = job_service._dispatch_finalize
    job_service._dispatch_finalize = lambda jid: dispatched.append(jid)
    try:
        new_status = job_service.retry_job(job_id)
    finally:
        job_service._dispatch_finalize = original

    assert new_status == "submitted", new_status
    assert dispatched == [job_id], dispatched
    row = job_store.get_job(job_id)
    assert row["status"] == "submitted", row["status"]
    assert row["progress"] == 0, row["progress"]
    # 结果载荷必须保留 —— finalize 靠它里面的 asset_id 找 GEE 上的成果。
    assert row["result"]["metrics"]["asset_id"], row["result"]
    print(f"  OK  状态 {status_of(row)}，已重新派发 finalize")


def test_retry_resubmits_when_no_asset(tmpdb: str) -> None:
    print("\n=== 2. GEE 任务本身失败 → 重新提交 ===")
    job_id = _make_job(
        tmpdb,
        status="failed",
        result={"gee_task_id": "T2", "metrics": {}},
    )

    resubmitted: list[str] = []
    original = job_service._run_gee
    job_service._run_gee = lambda jid, algo, params, jdir: resubmitted.append(jid)
    try:
        new_status = job_service.retry_job(job_id)
    finally:
        job_service._run_gee = original

    assert new_status == "queued", new_status
    assert resubmitted == [job_id], resubmitted
    print("  OK  已重新提交算法")


def test_retry_refuses_live_jobs(tmpdb: str) -> None:
    print("\n=== 3. 非终态任务不可重试 ===")
    for status in ("queued", "running", "submitted", "completed"):
        job_id = _make_job(
            tmpdb,
            status=status,
            result={"metrics": {"asset_id": "projects/p/assets/x"}},
        )
        try:
            job_service.retry_job(job_id)
        except ValueError as exc:
            print(f"  OK  {status} → 拒绝: {exc}")
        else:
            raise AssertionError(f"{status} 状态竟然允许重试")

    missing = job_service.retry_job("does-not-exist")
    assert missing is None, missing
    print("  OK  不存在的任务返回 None")


def status_of(row: dict) -> str:
    return str(row.get("status"))


# ---------------------------------------------------------------------------
# 2. network vs. auth classification
# ---------------------------------------------------------------------------


def test_network_error_classification() -> None:
    print("\n=== 4. 网络故障不能被当成认证失败 ===")
    samples = [
        "HTTPSConnectionPool(host='earthengine.googleapis.com', port=443): "
        "Max retries exceeded with url: /v1/projects/p/thumbnails/x:getPixels "
        "(Caused by ProxyError('Unable to connect to proxy', "
        "RemoteDisconnected('Remote end closed connection without response')))",
        "Connection reset by peer",
        "Failed to establish a new connection: Connection refused",
        "The read operation timed out",
        "下载 GEE 栅格失败：本机无法连接 GEE（代理 / 网络不通）。",
    ]
    for text in samples:
        assert is_network_error(Exception(text)), text
    print(f"  OK  {len(samples)} 条网络错误全部被识别为网络问题")

    # 真正的认证 / 权限问题不能被误判成网络问题。
    auth_samples = [
        "Request had invalid auth credentials.",
        "Caller does not have required permission to use project guangxi-try",
        "Earth Engine client library not found",
    ]
    for text in auth_samples:
        assert not is_network_error(Exception(text)), text
    print(f"  OK  {len(auth_samples)} 条权限/认证错误没有被误判")

    # 消息展示：网络错误要给出网络提示，而不是"请重新认证"。
    msg = job_service._finalize_error_message(
        Exception("Max retries exceeded ... ProxyError"), "guangxi-try"
    )
    assert "代理" in msg or "网络" in msg, msg
    assert "earthengine authenticate" not in msg, msg
    print("  OK  finalize 错误消息指向网络而不是重新认证")


def main() -> int:
    original_db = job_store.DB_PATH
    # SQLite 连接在 Windows 上会一直占着 .db 文件，临时目录可能因此删不掉
    # —— ignore_cleanup_errors 让它安静退场，别把测试结果淹没在 IOError 里。
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        tmpdb = os.path.join(tmpdir, "test_jobs.db")
        job_store.DB_PATH = tmpdb
        job_store._init_schema()
        try:
            test_retry_dispatches_finalize(tmpdb)
            test_retry_resubmits_when_no_asset(tmpdb)
            test_retry_refuses_live_jobs(tmpdb)
        finally:
            job_store.DB_PATH = original_db

    test_network_error_classification()

    # 冒烟：GeeNetworkError 的文案本身要能被 is_network_error 认出。
    assert is_network_error(GeeNetworkError("boom")), "GeeNetworkError 没被识别为网络错误"
    assert not isinstance(GeeNetworkError("boom"), GeeAuthRequired)
    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
