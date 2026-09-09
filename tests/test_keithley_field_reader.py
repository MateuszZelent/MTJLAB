"""Independent persisted-series reader checks, including incomplete runs."""

import json

import pytest

from app.devices.keithley_2600.characterization.field_reader import load_field_series
from tests.test_keithley_field_worker import make_worker


def test_completed_series_roundtrip_retains_skips_history_and_observations(tmp_path):
    worker, _ = make_worker(tmp_path)
    worker.run()
    result = load_field_series(worker.directory)
    assert [curve.current_a for curve in result.curves] == [0, 1e-5, 0, 5e-6]
    assert result.curves[1].dataset is None
    assert result.curves[1].status == "skipped_field_compliance"
    assert result.curves[2].history_segment == 1
    assert result.curves[-1].dataset.points[0].field_before.measured_current_a == 5e-6
    assert result.curves[-1].dataset.config == worker.config.sweep


@pytest.mark.parametrize("mutation", ["schema", "path", "current", "count", "checksum", "missing"])
def test_corrupt_manifest_or_snapshot_is_rejected(tmp_path, mutation):
    worker, _ = make_worker(tmp_path)
    worker.run()
    path = worker.directory / "series.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    entry = manifest["entries"][0]
    if mutation == "schema":
        manifest["schema_version"] = 999
    elif mutation == "path":
        entry["directory"] = "../outside"
    elif mutation == "current":
        entry["current_a"] = 1
    elif mutation == "count":
        entry["point_count"] += 1
    else:
        dataset_path = worker.directory / entry["directory"] / "dataset.json"
        if mutation == "missing":
            dataset_path.unlink()
        else:
            data = json.loads(dataset_path.read_text(encoding="utf-8"))
            data["points"][0]["measured_voltage_v"] += 0.1
            dataset_path.write_text(json.dumps(data), encoding="utf-8")
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        load_field_series(worker.directory)


def test_running_manifest_is_readable_but_never_promoted_to_completed(tmp_path):
    worker, _ = make_worker(tmp_path)
    worker.run()
    path = worker.directory / "series.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "running"
    data.pop("outputs_confirmed_off")
    path.write_text(json.dumps(data), encoding="utf-8")
    result = load_field_series(worker.directory)
    assert result.manifest["status"] == "running"
    assert "outputs_confirmed_off" not in result.manifest
    assert len(result.curves) == 4
