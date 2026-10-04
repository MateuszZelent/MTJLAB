"""Offline archive bootstrap remains cancellable and leaves the GUI responsive."""

from dataclasses import replace
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

pytest.importorskip("scipy", reason="Optional qualification dependencies are required")

from app.devices.anritsu_ms2830a.ui.correction_controller import (
    CorrectionSessionRequest, SpectrumCorrectionController, SpectrumResonanceBootstrapRequest,
)
from app.domain.spectrum_correction import CorrectionConfig
from app.spectrum.resonance_bootstrap import ResonanceBootstrapConfig
from app.storage.hdf5_reader import Hdf5RunReader
from tests.test_spectrum_correction_controller import frame, wait_until
from tests.test_spectrum_resonance_bootstrap_store import archives
from tests.test_spectrum_correction_store import fixture_profile


def request(tmp_path):
    reference, signal = archives(tmp_path)
    return SpectrumResonanceBootstrapRequest(reference, signal, tmp_path / "report.json", 2, 1.5e6, 1e5,
        config=ResonanceBootstrapConfig(resamples=200, independent_blocks_qualified=True,
            reference_equivalence_qualified=True, stationary_signal_qualified=True,
            qualification_evidence="Synthetic only"))


def test_real_archive_worker_bootstrap_progress_exclusivity_and_gui_heartbeat(tmp_path):
    application = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    controller = SpectrumCorrectionController()
    completed, failed, progress, heartbeats = [], [], [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    controller.processing_progress.connect(lambda *args: progress.append(args))
    timer = QTimer()
    timer.setInterval(1)
    timer.timeout.connect(lambda: heartbeats.append(True))
    try:
        timer.start()
        controller.bootstrap_archives(payload)
        with pytest.raises(ValueError, match="pending"):
            controller.bootstrap_archives(payload)
        context, profile = fixture_profile()
        with pytest.raises(ValueError, match="pending"):
            controller.start_session(CorrectionSessionRequest(context, CorrectionConfig(), tmp_path / "not-opened.h5",
                "synthetic: true", "NO_VISA", profile=profile))
        wait_until(application, lambda: bool(completed) or bool(failed), timeout=15)
        assert not failed
        assert completed[0][0] == "bootstrap_resonance"
        path, report = completed[0][1]
        assert path == payload.destination and path.exists()
        assert report["status"] == "conditional_interval"
        assert len(heartbeats) > 5
        assert progress[0] == ("bootstrap_resonance", 0, 200)
        assert progress[-1] == ("bootstrap_resonance", 200, 200)
        assert [item[1] for item in progress] == sorted(item[1] for item in progress)
        assert len(progress) < 100  # Worker progress is throttled, not one GUI event per replica.
        assert not controller._offline_busy
    finally:
        timer.stop()
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


def test_pending_bootstrap_cancels_without_creating_report_and_can_be_resubmitted(tmp_path):
    application = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    controller = SpectrumCorrectionController()
    cancelled, failed, completed = [], [], []
    controller.cancelled.connect(cancelled.append)
    controller.failed.connect(lambda *args: failed.append(args))
    controller.completed.connect(lambda *args: completed.append(args))
    try:
        controller.bootstrap_archives(payload)
        controller.cancel_processing()
        wait_until(application, lambda: bool(cancelled) or bool(failed), timeout=15)
        assert cancelled == ["bootstrap_resonance"] and not failed
        assert not payload.destination.exists() and not controller._offline_busy
        controller.bootstrap_archives(replace(payload, config=ResonanceBootstrapConfig(resamples=200)))
        wait_until(application, lambda: bool(completed) or bool(failed), timeout=15)
        assert not failed and completed[0][1][1]["confidence_intervals"] is None
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


def test_offline_request_during_acquisition_is_rejected_without_faulting_raw_archive(tmp_path):
    application = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    context, profile = fixture_profile()
    controller = SpectrumCorrectionController()
    completed, failed = [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    raw_archive = tmp_path / "active.h5"
    try:
        controller.start_session(CorrectionSessionRequest(context, CorrectionConfig(), raw_archive,
            "synthetic: true", "NO_VISA", profile=profile, simulation_mode=True))
        wait_until(application, lambda: any(op == "start" for op, _ in completed) or bool(failed))
        assert not failed
        with pytest.raises(ValueError, match="pending"):
            controller.bootstrap_archives(payload)
        envelope, trace = frame(context, 0, profile.mean_w + 1e-12)
        controller.ingest(envelope, trace)
        wait_until(application, lambda: any(op == "frame" for op, _ in completed) or bool(failed))
        assert not failed
        controller.stop_session("completed")
        wait_until(application, lambda: any(op == "stop" for op, _ in completed) or bool(failed))
        assert not failed
        assert Hdf5RunReader.summary(raw_archive).status == "completed"
        assert Hdf5RunReader.summary(raw_archive).point_count == 1
        assert not payload.destination.exists()
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()
