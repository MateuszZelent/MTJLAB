"""Global difference over closed archives preserves raw data and publication rules."""

from dataclasses import replace
import json
import subprocess
import sys

import h5py
import pytest

pytest.importorskip("scipy")

from app.domain.errors import ExecutionError, ProcessingCancelled
from app.spectrum.spectral_difference_test import SpectralDifferenceTestConfig
from app.storage.spectral_difference_store import analyze_spectral_difference_archives
from tests.test_spectrum_resonance_bootstrap_store import archives


def calculate(paths, destination, **extra):
    return analyze_spectral_difference_archives(*paths, destination, block_sweeps=2,
        search_start_hz=1e6, search_stop_hz=2e6, **extra)


def qualified():
    return SpectralDifferenceTestConfig(permutations=99, independent_blocks_qualified=True,
        null_exchangeability_qualified=True, qualification_evidence="Synthetic API test; no laboratory qualification")


def test_archive_difference_has_exact_provenance_and_preserves_sources(tmp_path):
    paths = archives(tmp_path)
    before = [path.read_bytes() for path in paths]
    unqualified = calculate(paths, tmp_path / "unqualified.json")
    assert unqualified["p_value"] is None and unqualified["global_difference_detected"] is None
    report = calculate(paths, tmp_path / "conditional.json", config=qualified())
    assert report["global_difference_detected"] and report["p_value"] == .01
    assert report["source_manifests"][0]["source_frame_ranges"][0] == [0, 1]
    assert report["source_manifests"][1]["interval_s"] == [101, 148]
    assert len(report["source_sha256"]) == 2
    assert report["search_frequencies_hz"][0] == 1e6
    assert all(source["simulation"]["enabled"] for source in report["source_run_metadata"])
    assert not report["recorded_context_qualification"]["settings_verified"]
    assert not report["false_alarm_rate_qualified"] and not report["laboratory_qualified"]
    assert [path.read_bytes() for path in paths] == before
    with pytest.raises(ExecutionError, match="new report"):
        calculate(paths, tmp_path / "conditional.json")


@pytest.mark.parametrize("defect", ["model", "running", "incomplete", "profile"])
def test_invalid_archive_never_publishes_a_difference_test(tmp_path, defect):
    paths = archives(tmp_path)
    with h5py.File(paths[1], "r+") as file:
        if defect == "model":
            file["run"].attrs["spectrum_correction_initial_interference_model_id"] = "model"
        elif defect == "running":
            file["run"].attrs["status"] = "running"
        elif defect == "incomplete":
            file["points/0"].attrs["complete"] = False
        else:
            next(iter(file["spectrum_processing_v1/profiles"].values()))["mean_w"][0] *= 2
    with pytest.raises((ExecutionError, ValueError)):
        calculate(paths, tmp_path / "report.json")
    assert not (tmp_path / "report.json").exists()


def test_budget_precedes_allocation_and_cancel_precedes_publication(tmp_path, monkeypatch):
    from app.storage import resonance_bootstrap_store as shared

    paths = archives(tmp_path)
    original = shared._collect

    def forbidden(*args, **kwargs):
        raise AssertionError("Allocated before preflight")

    monkeypatch.setattr(shared, "_collect", forbidden)
    # Profile fits the memory budget but complete analysis buffers do not.
    with pytest.raises(ExecutionError, match="block/memory budget"):
        calculate(paths, tmp_path / "report.json", config=replace(qualified(), working_memory_limit_bytes=25000))
    monkeypatch.setattr(shared, "_collect", original)

    def cancel():
        if list(tmp_path.glob(".report.json.*.pending")):
            raise ProcessingCancelled("cancel after fsync")

    with pytest.raises(ProcessingCancelled):
        calculate(paths, tmp_path / "report.json", cancellation_check=cancel)
    assert not (tmp_path / "report.json").exists()
    assert not list(tmp_path.glob("*.pending"))


def test_changed_source_hash_is_rejected_before_publication(tmp_path, monkeypatch):
    from app.storage import resonance_bootstrap_store as shared

    paths = archives(tmp_path)
    original = shared.file_sha256
    calls = []

    def changed(path, **kwargs):
        calls.append(path)
        result = original(path, **kwargs)
        return "changed" if len(calls) > 2 else result

    monkeypatch.setattr(shared, "file_sha256", changed)
    with pytest.raises(ExecutionError, match="changed during analysis"):
        calculate(paths, tmp_path / "report.json")
    assert not (tmp_path / "report.json").exists()


def test_cli_requires_explicit_frequency_units_and_is_unqualified_by_default(tmp_path):
    paths = archives(tmp_path)
    output = tmp_path / "cli.json"
    command = [sys.executable, "-m", "tools.detect_spectrum_difference", *map(str, paths),
               "--output", str(output), "--block-sweeps", "2", "--search-start", "1 MHz",
               "--search-stop", "2 MHz"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["config"]["alpha"] == .005
    assert report["p_value"] is None
    command[command.index(str(output))] = str(tmp_path / "missing-units.json")
    command[command.index("1 MHz")] = "1"
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert not (tmp_path / "missing-units.json").exists()
