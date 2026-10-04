"""Closed raw archives, block provenance and exclusive bootstrap publication."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import subprocess
import sys

import h5py
import numpy as np
import pytest

pytest.importorskip("scipy", reason="Optional qualification dependencies are required")

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.models import MeasurementPoint
from app.domain.spectrum_correction import SpectrumAcquisitionContext, SpectrumFrameEnvelope, SpectrumFrameRole, SweepEvidence
from app.spectrum.background_profile import BackgroundProfileBuilder
from app.spectrum.resonance_bootstrap import ResonanceBootstrapConfig
from app.spectrum.resonance_metrics import resonance_values
from app.spectrum.streaming_statistics import dbm_to_w
from app.storage.resonance_bootstrap_store import bootstrap_resonance_archives
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_spectrum_interference_store import writer


def archives(directory, *, count=48):
    context = SpectrumAcquisitionContext(np.linspace(1e6, 2e6, 101), "synthetic-bootstrap")
    shape = resonance_values(context.frequencies_hz, 1, 1.5e6, 1e5)
    random = np.random.default_rng(452)
    builder = BackgroundProfileBuilder(context, reference_state="synthetic REF only", minimum_sweeps=2)
    rows = []
    for index in range(count):
        dbm = 10 * np.log10(1e-9 + random.normal(0, 1e-11) * shape) + 30
        envelope = SpectrumFrameEnvelope(index, "ref", context.context_id, 0, index + 1,
            role=SpectrumFrameRole.REFERENCE, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
        builder.add(envelope, dbm_to_w(dbm))
        rows.append((envelope, dbm))
    profile = builder.finish()
    paths = directory / "reference.h5", directory / "signal.h5"
    for path, role in zip(paths, (SpectrumFrameRole.REFERENCE, SpectrumFrameRole.SIGNAL), strict=True):
        run = writer(path)
        try:
            run.store_background_profile(context, profile)
            for index, (envelope, dbm) in enumerate(rows):
                if role == SpectrumFrameRole.SIGNAL:
                    envelope = replace(envelope, segment_id="signal", role=role, acquired_at_s=101 + index)
                    dbm = 10 * np.log10(1e-9 + 1e-10 * shape) + 30
                trace = SpectrumTrace(tuple(context.frequencies_hz), tuple(dbm),
                    datetime.fromtimestamp(envelope.acquired_at_s, timezone.utc), "TRAC1")
                run.append(MeasurementPoint(index, {}, {}, metadata={"quantitative_accepted": True}),
                           trace, acquisition_envelope=envelope)
        finally:
            run.close("completed")
    return paths


def calculate(paths, output, **extra):
    return bootstrap_resonance_archives(*paths, output, block_sweeps=2,
        initial_center_hz=1.5e6, initial_fwhm_hz=1e5, **extra)


def test_raw_archives_produce_conditional_intervals_with_exact_block_provenance(tmp_path):
    paths = archives(tmp_path)
    before = [path.read_bytes() for path in paths]
    for path in paths:
        assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
    output = tmp_path / "report.json"
    report = calculate(paths, output, config=ResonanceBootstrapConfig(resamples=200,
        independent_blocks_qualified=True, reference_equivalence_qualified=True,
        stationary_signal_qualified=True, qualification_evidence="Known independent synthetic draws; no lab qualification"))
    assert report["status"] == "conditional_interval"
    assert not report["coverage_qualified"]
    assert report["reference_blocks"] == report["signal_blocks"] == 24
    ref, signal = report["source_manifests"]
    assert ref["source_frame_ranges"][0] == [0, 1]
    assert ref["source_time_ranges_s"][-1] == [47, 48]
    assert signal["interval_s"] == [101, 148]
    assert all(source["simulation"]["enabled"] for source in report["source_run_metadata"])
    assert not report["recorded_context_qualification"]["settings_verified"]
    assert json.loads(output.read_text(encoding="utf-8"))["units"]["amplitude_w"] == "W"
    assert all(path.read_bytes() == content for path, content in zip(paths, before, strict=True))
    with pytest.raises(ExecutionError, match="new report"):
        calculate(paths, output)


def test_partial_tail_requires_explicit_policy_and_is_recorded(tmp_path):
    paths = archives(tmp_path, count=49)
    output = tmp_path / "report.json"
    with pytest.raises(ExecutionError, match="partial final block"):
        calculate(paths, output)
    assert not output.exists()
    report = calculate(paths, output, discard_partial_tail=True)
    assert report["status"] == "unqualified" and report["confidence_intervals"] is None
    assert report["resamples_completed"] == 0
    assert all(source["used_sweeps"] == 48 and source["discarded_tail_sweeps"] == 1
               for source in report["source_manifests"])


def test_budget_is_enforced_before_block_matrix_allocation(tmp_path, monkeypatch):
    from app.storage import resonance_bootstrap_store as module

    paths = archives(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Block matrix allocated before budget validation")

    monkeypatch.setattr(module, "_collect", forbidden)
    with pytest.raises(ExecutionError, match="block/memory budget"):
        calculate(paths, tmp_path / "report.json", config=ResonanceBootstrapConfig(maximum_blocks=8, minimum_blocks=8))
    assert not (tmp_path / "report.json").exists()


def test_studentized_archive_budget_includes_centered_block_workspace(tmp_path, monkeypatch):
    from app.storage import resonance_bootstrap_store as module

    paths = archives(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Block matrix allocated before studentized budget validation")

    monkeypatch.setattr(module, "_collect", forbidden)
    with pytest.raises(ExecutionError, match="block/memory budget"):
        calculate(paths, tmp_path / "report.json", config=ResonanceBootstrapConfig(
            resamples=200, interval_method="studentized", working_memory_limit_bytes=80000))


@pytest.mark.parametrize("defect", ["model", "uncommitted", "role", "overlap", "profile"])
def test_malformed_or_unsupported_sources_never_publish_report(tmp_path, defect):
    paths = archives(tmp_path)
    with h5py.File(paths[1], "r+") as file:
        if defect == "model":
            file["run"].attrs["spectrum_correction_initial_interference_model_id"] = "model"
        elif defect == "uncommitted":
            file["points/0"].attrs["complete"] = False
        elif defect == "profile":
            group = next(iter(file["spectrum_processing_v1/profiles"].values()))
            group["mean_w"][0] *= 2
        else:
            raw = file["spectra/0"]
            # Envelope is serialized as JSON; mutate without changing public raw vectors.
            envelope = json.loads(raw.attrs["acquisition_envelope_json"])
            envelope["role" if defect == "role" else "acquired_at_s"] = "reference" if defect == "role" else 1
            raw.attrs["acquisition_envelope_json"] = json.dumps(envelope)
    with pytest.raises((ExecutionError, ValueError)):
        calculate(paths, tmp_path / "bad.json")
    assert not (tmp_path / "bad.json").exists()


@pytest.mark.parametrize("failure", ["cancel", "destination_race"])
def test_atomic_publication_cleanup_and_no_overwrite(tmp_path, monkeypatch, failure):
    from app.storage import resonance_bootstrap_store as module

    paths = archives(tmp_path)
    output = tmp_path / "report.json"
    if failure == "cancel":
        def cancel():
            if list(tmp_path.glob(".report.json.*.pending")):
                raise ProcessingCancelled("injected cancel after fsync")

        with pytest.raises(ProcessingCancelled):
            calculate(paths, output, cancellation_check=cancel)
        assert not output.exists()
    else:
        original = module.os.link

        def racing_link(source, target):
            target.write_bytes(b"existing report")
            return original(source, target)

        monkeypatch.setattr(module.os, "link", racing_link)
        with pytest.raises(FileExistsError):
            calculate(paths, output)
        assert output.read_bytes() == b"existing report"
    assert not list(tmp_path.glob("*.pending"))


def test_cli_requires_units_and_keeps_default_intervals_unqualified(tmp_path):
    paths = archives(tmp_path)
    output = tmp_path / "report.json"
    command = [sys.executable, "-m", "tools.bootstrap_spectrum_resonance", *map(str, paths),
        "--output", str(output), "--block-sweeps", "2", "--center", "1.5 MHz", "--fwhm", "100 kHz"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "unqualified" in result.stdout
    assert json.loads(output.read_text(encoding="utf-8"))["confidence_intervals"] is None
    command[command.index("1.5 MHz")] = "1.5"
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
