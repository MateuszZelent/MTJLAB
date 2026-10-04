"""Stop feedback during real checkpoints; preserve accepted raw and exclusive reports."""

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from app.storage.hdf5_reader import Hdf5RunReader
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tools.benchmark_spectrum_stop_response import benchmark_stop_response


@pytest.mark.parametrize("kwargs", [{"cycles": True}, {"cycles": 0}, {"cycles": 101},
    {"warmup_cycles": 2, "cycles": 2}, {"points": 2}, {"points": 10002},
    {"frames_before_stop": 2}, {"frames_before_stop": 1001}, {"rate_hz": 0}, {"rate_hz": np.nan},
    {"host_mode": "legacy"}])
def test_stop_benchmark_rejects_invalid_bounds_before_creating_files(tmp_path, kwargs):
    with pytest.raises(ValueError):
        benchmark_stop_response(tmp_path / "invalid.json", **kwargs)
    assert not list(tmp_path.iterdir())


def test_stop_benchmark_preserves_raw_paints_feedback_and_reports_separate_close_latency(tmp_path):
    output = tmp_path / "stop.json"
    report = benchmark_stop_response(output, cycles=3, warmup_cycles=1, points=101,
        frames_before_stop=5, rate_hz=20)
    assert report["cycles_completed"] == 3
    assert report["total_frames_committed"] == 15 and report["total_lost_frames"] == 0
    assert report["post_to_feedback_paint"]["p95_ms"] >= 0
    assert report["operator_input_qualified"] is False
    assert report["hardware_shutdown_qualified"] is False
    assert report["laboratory_qualified"] is False
    for row in report["cycles"]:
        assert row["request_posted_during_commit"] is True
        assert type(row["commit_active_at_mouse_dispatch"]) is bool
        assert row["post_to_mouse_dispatch_ms"] >= 0
        assert row["mouse_handler_ms"] >= 0
        assert row["post_to_feedback_paint_ms"] >= row["post_to_feedback_paint_entry_ms"]
        assert row["post_to_archive_close_ms"] >= row["post_to_mouse_dispatch_ms"]
        assert row["feedback_screenshot"] is not None
        archive = row["archive"]
        assert Hdf5RunReader.summary(archive).status == "aborted"
        assert ThatecCompatibilityValidator().validate(archive, require_pythat=True).valid
        assert len([Hdf5RunReader.spectrum(archive, index) for index in range(5)]) == 5
        result = Hdf5RunReader.spectrum_correction(archive, 4)
        assert result.count == 5 and result.values_w[51] < 0 < result.values_w[50]
    journal = [json.loads(line) for line in output.with_suffix(".stop-cycles.jsonl").read_text().splitlines()]
    assert journal[0]["type"] == "incomplete_gui_stop_journal"
    assert journal[1:] == report["cycles"]
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        benchmark_stop_response(output, cycles=1, warmup_cycles=0, points=101, frames_before_stop=5)
    assert output.read_bytes() == before


def test_shell_stop_uses_real_main_button_and_isolated_catalogue_and_ui_preferences(tmp_path):
    from app.settings import SettingsRepository

    output = tmp_path / "shell.json"
    report = benchmark_stop_response(output, cycles=1, warmup_cycles=0, points=101,
        frames_before_stop=5, host_mode="shell")
    assert report["host_mode"] == "shell"
    row = report["cycles"][0]
    assert row["stop_button_visible"] and row["stop_button_width"] > 0
    assert row["feedback_screenshot"] is not None
    assert Path(row["feedback_screenshot"]).exists()
    assert row["frames_committed"] == 5 and row["lost_frames"] == 0
    directory = output.with_suffix(".stop-cycles") / "cycle-000-shell"
    settings = SettingsRepository(directory / "settings.yml").load().settings
    assert Path(settings.storage["catalogue_directory"]).is_relative_to(directory.resolve())
    assert Path(settings.storage["output_directory"]).is_relative_to(directory.resolve())
    assert Path(settings.application["audit_log_directory"]).is_relative_to(directory.resolve())
    assert (directory / "ui-state.ini").exists()
    assert (directory / "catalogue").is_dir()
    assert ThatecCompatibilityValidator().validate(row["archive"], require_pythat=True).valid


def test_stop_bundle_disk_preflight_happens_before_mkdir(tmp_path, monkeypatch):
    import tools.benchmark_spectrum_stop_response as module

    observed = []

    def no_space(directory, *, points, frames):
        observed.append((points, frames))
        raise OSError("Insufficient disk")

    monkeypatch.setattr(module, "archive_disk_budget", no_space)
    with pytest.raises(OSError, match="Insufficient disk"):
        benchmark_stop_response(tmp_path / "report.json", cycles=3, warmup_cycles=1, points=101, frames_before_stop=5)
    assert observed == [(101, 18)]
    assert not list(tmp_path.iterdir())


def test_failed_stop_cycle_keeps_completed_archive_and_journal_without_final_report(tmp_path, monkeypatch):
    import tools.benchmark_spectrum_stop_response as module

    original = module._stop_cycle

    def fail_second(*args, **kwargs):
        if kwargs["index"] == 1:
            raise RuntimeError("Injected second-cycle failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_stop_cycle", fail_second)
    output = tmp_path / "partial.json"
    with pytest.raises(RuntimeError, match="second-cycle"):
        benchmark_stop_response(output, cycles=2, warmup_cycles=0, points=101, frames_before_stop=5)
    assert not output.exists()
    rows = [json.loads(line) for line in output.with_suffix(".stop-cycles.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and rows[1]["cycle"] == 0
    assert Hdf5RunReader.summary(rows[1]["archive"]).point_count == 5
