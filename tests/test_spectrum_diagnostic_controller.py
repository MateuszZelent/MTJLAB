"""Async bounded reference diagnostics and cooperative cancellation."""

from dataclasses import replace
import json
import os
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.ui.correction_controller import (
    CorrectionSessionRequest, SpectrumCorrectionController, SpectrumInterferenceValidationRequest,
    SpectrumReferenceDiagnosticRequest,
)
from app.domain.spectrum_correction import CorrectionConfig
from app.spectrum.reference_diagnostics import ReferenceDiagnosticConfig
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_correction_store import fixture_profile
from tests.test_spectrum_reference_diagnostics import archive


def test_worker_reports_verified_raw_diagnostics_without_qualification(tmp_path):
    app = QApplication.instance() or QApplication([])
    source, _context, _profile = archive(tmp_path)
    before = source.read_bytes()
    payload = SpectrumReferenceDiagnosticRequest(source, tmp_path / "diagnostic.json", bin_indices=(0, 1, 2))
    controller = SpectrumCorrectionController()
    completed, failed, progress = [], [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    controller.processing_progress.connect(lambda *args: progress.append(args))
    try:
        controller.diagnose_reference_archive(payload)
        with pytest.raises(ValueError, match="pending"):
            controller.diagnose_reference_archive(payload)
        with pytest.raises(ValueError, match="pending"):
            controller.validate_interference_archive(SpectrumInterferenceValidationRequest(
                tmp_path / "model.h5", source, tmp_path / "validation.json"))
        context, profile = fixture_profile()
        with pytest.raises(ValueError, match="pending"):
            controller.start_session(CorrectionSessionRequest(context, CorrectionConfig(), tmp_path / "not-opened.h5",
                "synthetic: true", "NO_VISA", profile=profile))
        wait_until(app, lambda: bool(completed) or bool(failed), timeout=20)
        assert not failed and completed[0][0] == "diagnose_reference"
        path, report = completed[0][1]
        persisted = json.loads(path.read_text(encoding="utf-8"))
        assert persisted["total_sweeps"] == report["total_sweeps"] == 132
        assert report["raw_profile_verified"] and report["qualified_ttl_s"] is None
        assert not report["sweep_independence_inferred"] and report["qualification"] == "diagnostic_only"
        assert report["units"]["allan_variance_w2"] == "W^2"
        assert progress == [("diagnose_reference", 0, 0)]
        assert not controller._offline_busy and source.read_bytes() == before
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_mid_read_cancellation_and_shutdown_preserve_raw(tmp_path, monkeypatch, action):
    from app.storage import reference_diagnostic_store as module

    app = QApplication.instance() or QApplication([])
    source, _context, _profile = archive(tmp_path)
    before = source.read_bytes()
    payload = SpectrumReferenceDiagnosticRequest(source, tmp_path / "report.json")
    original = module.file_sha256
    entered, proceed = Event(), Event()

    def delayed_hash(*args, **kwargs):
        entered.set()
        if not proceed.wait(5):
            raise RuntimeError("Test release timed out")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "file_sha256", delayed_hash)
    controller = SpectrumCorrectionController()
    completed, failed, cancelled = [], [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    controller.cancelled.connect(cancelled.append)
    try:
        controller.diagnose_reference_archive(payload)
        wait_until(app, entered.is_set)
        if action == "close":
            assert not controller.close(wait_ms=0)
        else:
            controller.cancel_processing()
        proceed.set()
        wait_until(app, lambda: bool(cancelled) or bool(failed), timeout=15)
        assert cancelled == ["diagnose_reference"] and not failed
        assert not payload.destination.exists() and not controller._offline_busy
        assert source.read_bytes() == before
        if action == "close":
            wait_until(app, lambda: not controller._thread.isRunning())
        else:
            controller.diagnose_reference_archive(payload)
            wait_until(app, lambda: bool(completed) or bool(failed), timeout=20)
            assert not failed and payload.destination.exists()
    finally:
        proceed.set()
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        app.processEvents()


def test_existing_report_error_releases_worker(tmp_path):
    app = QApplication.instance() or QApplication([])
    source, _context, _profile = archive(tmp_path)
    payload = SpectrumReferenceDiagnosticRequest(source, tmp_path / "report.json")
    payload.destination.write_bytes(b"existing report")
    controller = SpectrumCorrectionController()
    completed, failed = [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    try:
        controller.diagnose_reference_archive(payload)
        wait_until(app, lambda: bool(failed), timeout=20)
        assert failed[0][0] == "diagnose_reference" and not controller._offline_busy
        assert payload.destination.read_bytes() == b"existing report"
        failed.clear()
        retry = replace(payload, destination=tmp_path / "new.json")
        controller.diagnose_reference_archive(retry)
        wait_until(app, lambda: bool(completed) or bool(failed), timeout=20)
        assert not failed and retry.destination.exists()
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("bins", [[0], (), (True,), (-1,), (1, 1), tuple(range(17))],
                         ids=["mutable", "empty", "boolean", "negative", "duplicate", "cap"])
def test_request_rejects_mutable_or_invalid_bins(tmp_path, bins):
    with pytest.raises(ValueError):
        SpectrumReferenceDiagnosticRequest(tmp_path / "ref.h5", tmp_path / "report.json", bin_indices=bins)
    assert SpectrumReferenceDiagnosticRequest(tmp_path / "ref.h5", tmp_path / "report.json",
        bin_indices=(0,), config=ReferenceDiagnosticConfig()).bin_indices == (0,)
