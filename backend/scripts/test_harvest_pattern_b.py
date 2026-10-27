"""Verify harvest's Pattern B (submit / finalize split) contract.

The export itself is mocked out — this test must never submit a real GEE
export, and it must never touch the real ``Harvest_2025_20260421`` asset
that the recovered job produced.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Windows 控制台默认 cp1252，印中文会抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import ee

from app.algorithms.harvest import adapter as harvest_adapter
from app.algorithms.harvest import algorithm as harvest_algorithm


class _FakeTask:
    id = "FAKE_TASK_ID_1234"

    def start(self):  # noqa: D102
        return None

    def status(self):  # noqa: D102
        return {"id": self.id, "state": "READY"}


def main() -> int:
    # --- mock the export so nothing is submitted to GEE --------------------
    captured = {}

    def _fake_to_asset(**kwargs):
        captured.update(kwargs)
        return _FakeTask()

    original_to_asset = ee.batch.Export.image.toAsset
    ee.batch.Export.image.toAsset = staticmethod(_fake_to_asset)
    try:
        params = {
            "gee_project_id": "guangxi-try",
            "year": 2025,
            "sugarcane_mask_asset": "projects/guangxi-try/assets/sugercane_class_089",
            # Unique description: a real submission could never clobber the
            # recovered job's asset.
            "export_description": "Harvest_selftest_do_not_submit",
        }
        with tempfile.TemporaryDirectory() as tmp:
            progress = []
            result = harvest_algorithm.run(
                params, tmp, progress_callback=lambda p, m: progress.append((p, m))
            )
    finally:
        ee.batch.Export.image.toAsset = original_to_asset

    # --- assertions --------------------------------------------------------
    assert result["status"] == "submitted", result["status"]
    assert result["result_type"] == "gee_task", result["result_type"]
    assert result["gee_task_id"] == "FAKE_TASK_ID_1234", result
    assert result["progress"] == 10, result["progress"]

    metrics = result["metrics"]
    assert metrics["gee_task_id"] == "FAKE_TASK_ID_1234", metrics
    assert metrics["description"] == "Harvest_selftest_do_not_submit", metrics
    assert metrics["asset_id"] == (
        "projects/guangxi-try/assets/Harvest_selftest_do_not_submit"
    ), metrics
    assert metrics["year"] == 2025, metrics
    assert metrics["base_window"] == "2025-10-01 → 2025-11-30", metrics
    assert metrics["current_window"] == "2025-12-01 → 2026-04-21", metrics
    assert metrics["scale"] == 10 and metrics["crs"] == "EPSG:4326", metrics
    assert metrics["cards"], "cards must be present for the ResultRenderer"
    # GEE 的内部细节（任务 ID / 资产路径 / 输出文件名）不再做成指标卡 ——
    # 它们属于任务信息，摆在指标区里只是噪音。指标卡只留业务指标。
    card_keys = {c["key"] for c in metrics["cards"]}
    assert not card_keys & {"gee_task_id", "asset_id", "output_tif"}, metrics["cards"]

    # The export really was configured the way we expect.
    assert captured["scale"] == 10, captured
    assert captured["crs"] == "EPSG:4326", captured
    assert captured["description"] == "Harvest_selftest_do_not_submit", captured
    assert captured["assetId"] == (
        "projects/guangxi-try/assets/Harvest_selftest_do_not_submit"
    ), captured

    # run() must NOT wait for the task — it returns immediately.
    assert not any("等待 GEE 任务完成" in m for _, m in progress), progress

    # --- the adapter must expose the finalize hook ------------------------
    ad = harvest_adapter.HarvestAdapter()
    assert callable(getattr(ad, "finalize", None)), "adapter.finalize missing"

    # finalize() must refuse a job row without asset info rather than
    # silently doing nothing.
    try:
        harvest_algorithm.finalize({"result": {"metrics": {}}}, tempfile.gettempdir())
    except RuntimeError as exc:
        assert "asset_id" in str(exc), exc
    else:
        raise AssertionError("finalize() should have raised for a job without asset_id")

    print("OK  Pattern B run() 契约通过")
    print("OK  adapter.finalize 已挂载")
    print("OK  finalize() 对缺少资产信息的任务报错")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
