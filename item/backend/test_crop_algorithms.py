"""Unit test for the three new GEE algorithms.

Injects a mocked `ee` module so no network/auth is needed, then exercises
each algorithm's run() function and asserts on the result structure.
"""
import json
import os
import sys
import tempfile
from unittest.mock import MagicMock

# Build a fake ee module where every method returns a SHARED MagicMock,
# so per-instance configuration sticks across calls.
class FakeEE:
    def __init__(self):
        self.Image = MagicMock()
        self.List = MagicMock()
        self.FeatureCollection = MagicMock()
        self.Date = MagicMock()
        self.Filter = MagicMock()
        self.ImageCollection = MagicMock()
        self.Reducer = MagicMock()
        self.Kernel = MagicMock()
        self.Classifier = MagicMock()
        self.batch = MagicMock()
        self.Geometry = MagicMock()
        self.String = MagicMock()
        self.Number = MagicMock()

    def Initialize(self, *args, **kwargs):
        return None


fake_ee = FakeEE()
sys.modules["ee"] = fake_ee

from app.algorithms.crop_threshold import algorithm as crop_threshold_alg
from app.algorithms.crop_features import algorithm as crop_features_alg
from app.algorithms.crop_classification import algorithm as crop_classification_alg


def test_crop_threshold():
    print("--- crop_threshold ---")

    min_p2 = [0.01 + 0.001 * i for i in range(11)]
    max_p98 = [0.5 + 0.01 * i for i in range(11)]

    percentile_stats = {}
    for i, b in enumerate(crop_threshold_alg.BAND_NAMES):
        percentile_stats[f"{b}_p2"] = min_p2[i]
        percentile_stats[f"{b}_p98"] = max_p98[i]

    fake_ee.Reducer.percentile.return_value = "percentile_reducer"
    fake_ee.Reducer.minMax.return_value = "minmax_reducer"

    # Configure the chained image mocks up front so side_effects stick.
    annual_inst = MagicMock()
    annual_stats = MagicMock()
    annual_stats.getNumber.side_effect = lambda key: MagicMock(
        **{"getInfo.return_value": percentile_stats[key]}
    )
    annual_inst.reduceRegion.return_value = annual_stats

    dem_inst = MagicMock()
    dem_stats = MagicMock()
    dem_stats.getNumber.side_effect = lambda key: MagicMock(
        **{"getInfo.return_value": {"elevation_min": -12.5, "elevation_max": 1700.0}[key]}
    )
    # Algorithm chain: ee.Image("NASA/NASADEM_HGT/001").select("elevation").clip(...)
    dem_select = dem_inst.select.return_value
    dem_clipped = dem_select.clip.return_value
    dem_clipped.reduceRegion.return_value = dem_stats

    def fake_image_call(asset_id):
        if "NASADEM" in str(asset_id):
            return dem_inst
        return annual_inst

    fake_ee.Image.side_effect = fake_image_call

    # ee.List(BAND_NAMES).map(...).getInfo() — first call min, second max.
    list_inst = fake_ee.List.return_value
    map_inst = list_inst.map.return_value
    map_call_results = [min_p2, max_p98]
    map_inst.getInfo.side_effect = lambda: map_call_results.pop(0)

    with tempfile.TemporaryDirectory() as job_dir:
        result = crop_threshold_alg.run(
            params={"year": 2025, "roi_asset_id": "projects/foo/assets/roi"},
            job_dir=job_dir,
        )

        assert result["status"] == "completed", f"status={result['status']}"
        assert result["result_type"] == "json"
        assert len(result["files"]) == 1
        out_file = result["files"][0]
        assert out_file["name"] == "threshold.json"
        json_path = out_file["path"]
        assert os.path.exists(json_path), f"json not written: {json_path}"

        with open(json_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        assert payload["min_values"] == min_p2, f"min={payload['min_values']}"
        assert payload["max_values"] == max_p98, f"max={payload['max_values']}"
        assert payload["dem_min"] == -12.5
        assert payload["dem_max"] == 1700.0
        assert payload["year"] == 2025
        assert payload["band_names"] == crop_threshold_alg.BAND_NAMES

        m = result["metrics"]
        assert m["min_values"] == min_p2
        assert m["max_values"] == max_p98
        assert m["dem_min"] == -12.5
        assert m["dem_max"] == 1700.0
        print(f"  OK  status=completed, threshold.json written ({os.path.getsize(json_path)} bytes)")
        print(f"  OK  metrics.min_values is 11-element list")


def test_crop_features():
    print()
    print("--- crop_features ---")

    # Reset just the bits we care about.
    fake_table_task = MagicMock()
    fake_table_task.id = "TASK_FEAT_123"
    fake_ee.batch.Export.table.toDrive.return_value = fake_table_task

    with tempfile.TemporaryDirectory() as job_dir:
        threshold_payload = {
            "band_names": crop_threshold_alg.BAND_NAMES,
            "min_values": [0.01] * 11,
            "max_values": [0.5] * 11,
            "dem_min": -10,
            "dem_max": 1500,
        }
        threshold_path = os.path.join(job_dir, "threshold.json")
        with open(threshold_path, "w", encoding="utf-8") as fh:
            json.dump(threshold_payload, fh)

        progress = []

        def cb(pct, msg):
            progress.append((pct, msg))

        result = crop_features_alg.run(
            params={
                "year": 2025,
                "roi_asset_id": "projects/foo/assets/roi",
                "samples_asset_id": "projects/foo/assets/samples",
                "threshold_json_path": threshold_path,
            },
            job_dir=job_dir,
            progress_callback=cb,
        )

        assert result["status"] == "submitted", f"status={result['status']}"
        assert result["result_type"] == "gee_task"
        assert result["gee_task_id"] == "TASK_FEAT_123", f"got {result['gee_task_id']}"
        fake_ee.batch.Export.table.toDrive.assert_called_once()
        fake_table_task.start.assert_called_once()
        assert progress[-1][0] == 100
        print(f"  OK  submitted task_id={result['gee_task_id']}")
        print(f"  OK  Export.table.toDrive called once, task.start() called once")
        print(f"  OK  progress callback fired through 100%")


def test_crop_classification():
    print()
    print("--- crop_classification ---")

    fake_img_task = MagicMock()
    fake_img_task.id = "TASK_CLASS_456"
    fake_ee.batch.Export.image.toDrive.return_value = fake_img_task

    fake_matrix = MagicMock()
    fake_matrix.getInfo.return_value = [[42, 3], [2, 53]]
    fake_matrix.accuracy.return_value.getInfo.return_value = 0.95
    fake_matrix.kappa.return_value.getInfo.return_value = 0.9012

    # Make sample points a fixed instance so .randomColumn()/.filter() chain
    # all return mocks that share the same .classify(...).errorMatrix(...) config.
    sample_inst = fake_ee.FeatureCollection.return_value
    validated_inst = MagicMock()
    validated_inst.classify.return_value.errorMatrix.return_value = fake_matrix
    # randomColumn(...).filter(...) -> validated_inst (one chain, used twice).
    sample_inst.randomColumn.return_value.filter.return_value = validated_inst

    fake_ee.Classifier.smileRandomForest.return_value.train.return_value = "rf_model"

    with tempfile.TemporaryDirectory() as job_dir:
        progress = []

        def cb(pct, msg):
            progress.append((pct, msg))

        result = crop_classification_alg.run(
            params={
                "year": 2025,
                "roi_asset_id": "projects/foo/assets/roi",
                "feature_asset": "projects/foo/assets/samples",
                "num_trees": 50,
                "train_ratio": 0.7,
            },
            job_dir=job_dir,
            progress_callback=cb,
        )

        assert result["status"] == "submitted", f"status={result['status']}"
        assert result["result_type"] == "gee_task"
        assert result["gee_task_id"] == "TASK_CLASS_456"
        m = result["metrics"]
        assert m["oa"] == 0.95, f"oa={m['oa']}"
        assert m["kappa"] == 0.9012, f"kappa={m['kappa']}"
        assert m["confusion_matrix"] == [[42, 3], [2, 53]]
        assert m["num_trees"] == 50
        assert m["train_ratio"] == 0.7
        fake_ee.batch.Export.image.toDrive.assert_called_once()
        fake_img_task.start.assert_called_once()
        assert progress[-1][0] == 100
        print(f"  OK  task_id={result['gee_task_id']}")
        print(f"  OK  metrics.oa=0.95, metrics.kappa=0.9012")
        print(f"  OK  confusion_matrix = [[42,3],[2,53]]")
        print(f"  OK  Export.image.toDrive + task.start() invoked")


if __name__ == "__main__":
    test_crop_threshold()
    test_crop_features()
    test_crop_classification()
    print()
    print("ALL OK")
