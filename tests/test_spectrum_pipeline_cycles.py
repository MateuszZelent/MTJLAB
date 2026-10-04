"""Actual repeated pipeline archives, cleanup telemetry and incomplete journals."""

import json
import os
import gc

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import shiboken6
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.storage.hdf5_reader import Hdf5RunReader
from tools.benchmark_spectrum_cycles import benchmark_cycles, drain_deleted_objects, resource_summary


def test_repeated_full_pipeline_retains_each_archive_and_measures_same_process(tmp_path):
    application = QApplication.instance() or QApplication([])
    drain_deleted_objects(application)
    timers_before = sum(isinstance(item, QTimer) and shiboken6.isValid(item) for item in gc.get_objects())
    output = tmp_path / "cycles.json"
    result = benchmark_cycles(output, cycles=3, warmup_cycles=1, points=101, frames=4, warmup_frames=1)
    assert result["cycles_completed"] == 3 and result["measured_cycles"] == 2
    assert result["total_frames_committed"] == 15 and result["total_lost_frames"] == 0
    assert not result["soak_qualified"] and not result["leak_free_qualified"]
    assert not result["gui_stop_response_qualified"] and not result["laboratory_qualified"]
    assert result["stop_archive_close"]["p50_ms"] >= result["stop_submission"]["p50_ms"]
    assert [row["warmup_cycle"] for row in result["cycles"]] == [True, False, False]
    for row in result["cycles"]:
        archive = output.with_suffix(".cycles") / f"cycle-{row['cycle']:03d}.h5"
        summary = Hdf5RunReader.summary(archive)
        assert summary.status == "aborted" and summary.point_count == 5
        residual = Hdf5RunReader.spectrum_correction(archive, 4)
        assert residual.count == 5 and residual.values_w[51] < 0 < residual.values_w[50]
        assert row["queue_max_observed"] <= 8
        if os.name == "nt":
            assert row["resources_after_cleanup"]["os_threads"] > 0
            assert row["resources_after_cleanup"]["os_handles"] > 0
    journal = [json.loads(line) for line in output.with_suffix(".cycles.jsonl").read_text(encoding="utf-8").splitlines()]
    assert journal[0]["type"] == "incomplete_pipeline_cycle_journal"
    assert journal[1:] == result["cycles"]
    assert json.loads(output.read_text(encoding="utf-8"))["cycles_completed"] == 3
    drain_deleted_objects(application)
    timers_after = sum(isinstance(item, QTimer) and shiboken6.isValid(item) for item in gc.get_objects())
    assert timers_after <= timers_before
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        benchmark_cycles(output, cycles=3, warmup_cycles=1, points=101, frames=4)
    assert output.read_bytes() == before


def test_resource_slope_is_descriptive_and_missing_os_metrics_remain_unavailable():
    rows = [{"resources_after_cleanup": {"rss_bytes": 1000 + 10 * index, "os_handles": 20,
                                        "os_threads": None}} for index in range(4)]
    result = resource_summary(rows)
    assert result["rss_bytes"]["last_minus_first"] == 30
    assert result["rss_bytes"]["linear_slope_per_cycle"] == pytest.approx(10)
    assert result["os_handles"]["linear_slope_per_cycle"] == 0
    assert result["os_threads"] is None
    assert resource_summary(rows[:1])["rss_bytes"]["linear_slope_per_cycle"] is None


def test_failed_cycle_retains_completed_raw_and_journal_without_final_report(tmp_path, monkeypatch):
    import tools.benchmark_spectrum_cycles as module

    original = module.benchmark_async_pipeline
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("injected cycle failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "benchmark_async_pipeline", fail_second)
    output = tmp_path / "interrupted.json"
    with pytest.raises(RuntimeError, match="cycle failure"):
        benchmark_cycles(output, cycles=3, warmup_cycles=1, points=101, frames=3, warmup_frames=0)
    assert not output.exists()
    archive = output.with_suffix(".cycles") / "cycle-000.h5"
    assert Hdf5RunReader.summary(archive).point_count == 3
    rows = [json.loads(line) for line in output.with_suffix(".cycles.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and rows[1]["cycle"] == 0
    assert len(calls) == 2


@pytest.mark.parametrize("parameters", [{"cycles": 0}, {"cycles": 201}, {"warmup_cycles": 20},
                                        {"cycles": True}, {"points": 10002}, {"frames": 0},
                                        {"rate_hz": float("nan")}],
                         ids=["zero", "cap", "all-warmup", "boolean", "points", "frames", "rate"])
def test_invalid_cycle_schedule_creates_no_files(tmp_path, parameters):
    with pytest.raises(ValueError):
        benchmark_cycles(tmp_path / "invalid.json", **parameters)
    assert not list(tmp_path.iterdir())


def test_bundle_disk_preflight_runs_before_any_directory_creation(tmp_path, monkeypatch):
    import tools.benchmark_spectrum_cycles as module

    def insufficient(*args, **kwargs):
        raise OSError("insufficient bundle budget")

    monkeypatch.setattr(module, "archive_disk_budget", insufficient)
    output = tmp_path / "new" / "cycles.json"
    with pytest.raises(OSError, match="bundle budget"):
        benchmark_cycles(output)
    assert not output.parent.exists()
