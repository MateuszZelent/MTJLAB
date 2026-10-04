"""Async global difference, cancellation and exclusive ownership of archives."""

from dataclasses import replace
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

pytest.importorskip("scipy")

from app.devices.anritsu_ms2830a.ui.correction_controller import (
    CorrectionSessionRequest, SpectrumCorrectionController, SpectrumDifferenceRequest, SpectrumResonanceBootstrapRequest,
)
from app.domain.spectrum_correction import CorrectionConfig
from app.spectrum.spectral_difference_test import SpectralDifferenceTestConfig
from app.storage.hdf5_reader import Hdf5RunReader
from tests.test_spectrum_correction_controller import frame, wait_until
from tests.test_spectrum_correction_store import fixture_profile
from tests.test_spectrum_resonance_bootstrap_store import archives


def request(tmp_path):
    ref, signal = archives(tmp_path)
    return SpectrumDifferenceRequest(ref, signal, tmp_path / "difference.json", 2, 1e6, 2e6,
        config=SpectralDifferenceTestConfig(permutations=999, alpha=.005,
            independent_blocks_qualified=True, null_exchangeability_qualified=True,
            qualification_evidence="Synthetic API testing; no laboratory qualification"))


def test_difference_worker_progress_heartbeat_and_cross_operation_exclusivity(tmp_path):
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
        controller.difference_archives(payload)
        with pytest.raises(ValueError, match="pending"):
            controller.difference_archives(payload)
        with pytest.raises(ValueError, match="pending"):
            controller.bootstrap_archives(SpectrumResonanceBootstrapRequest(payload.reference_path,
                payload.signal_path, tmp_path / "bootstrap.json", 2, 1.5e6, 1e5))
        context, profile = fixture_profile()
        with pytest.raises(ValueError, match="pending"):
            controller.start_session(CorrectionSessionRequest(context, CorrectionConfig(), tmp_path / "not-opened.h5",
                "synthetic: true", "NO_VISA", profile=profile))
        wait_until(application, lambda: bool(completed) or bool(failed), timeout=15)
        assert not failed
        assert completed[0][0] == "spectral_difference"
        path, report = completed[0][1]
        assert path == payload.destination and path.exists()
        assert report["global_difference_detected"] and report["p_value"] == .001
        assert len(heartbeats) > 5
        assert progress[0] == ("spectral_difference", 0, 999)
        assert progress[-1] == ("spectral_difference", 999, 999)
        assert len(progress) < 100
        assert [row[1] for row in progress] == sorted(row[1] for row in progress)
        assert not controller._offline_busy
    finally:
        timer.stop()
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


def test_cancel_during_permutations_preserves_sources_and_allows_reuse(tmp_path):
    application = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    before = [path.read_bytes() for path in (payload.reference_path, payload.signal_path)]
    controller = SpectrumCorrectionController()
    cancelled, failed, completed = [], [], []
    controller.cancelled.connect(cancelled.append)
    controller.failed.connect(lambda *args: failed.append(args))
    controller.completed.connect(lambda *args: completed.append(args))

    def progress(operation, done, total):
        if operation == "spectral_difference" and done > 0:
            controller.cancel_processing()

    controller.processing_progress.connect(progress)
    try:
        controller.difference_archives(replace(payload, config=replace(payload.config, permutations=99999)))
        wait_until(application, lambda: bool(cancelled) or bool(failed) or bool(completed), timeout=15)
        assert cancelled == ["spectral_difference"] and not failed and not completed
        assert not payload.destination.exists() and not controller._offline_busy
        assert [path.read_bytes() for path in (payload.reference_path, payload.signal_path)] == before
        controller.processing_progress.disconnect(progress)
        controller.difference_archives(replace(payload, config=SpectralDifferenceTestConfig()))
        wait_until(application, lambda: bool(completed) or bool(failed), timeout=15)
        assert not failed and completed[0][1][1]["p_value"] is None
        assert not controller._offline_busy
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


def test_difference_failure_preserves_existing_destination_and_worker_can_retry(tmp_path):
    application = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    payload.destination.write_bytes(b"existing report")
    controller = SpectrumCorrectionController()
    failed, completed = [], []
    controller.failed.connect(lambda *args: failed.append(args))
    controller.completed.connect(lambda *args: completed.append(args))
    try:
        controller.difference_archives(payload)
        wait_until(application, lambda: bool(failed), timeout=15)
        assert failed[0][0] == "spectral_difference" and "new report" in failed[0][1]
        assert payload.destination.read_bytes() == b"existing report"
        assert not controller._offline_busy
        controller.difference_archives(replace(payload, destination=tmp_path / "new.json",
                                               config=SpectralDifferenceTestConfig()))
        wait_until(application, lambda: bool(completed), timeout=15)
        assert completed[0][1][0].exists() and not controller._offline_busy
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


def test_difference_during_acquisition_is_rejected_without_faulting_raw_writer(tmp_path):
    application = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    context, profile = fixture_profile()
    controller = SpectrumCorrectionController()
    completed, failed = [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    active = tmp_path / "active.h5"
    try:
        controller.start_session(CorrectionSessionRequest(context, CorrectionConfig(), active,
            "synthetic: true", "NO_VISA", profile=profile, simulation_mode=True))
        wait_until(application, lambda: any(op == "start" for op, _ in completed) or bool(failed))
        with pytest.raises(ValueError, match="pending"):
            controller.difference_archives(payload)
        envelope, trace = frame(context, 0, profile.mean_w + 1e-12)
        controller.ingest(envelope, trace)
        wait_until(application, lambda: any(op == "frame" for op, _ in completed) or bool(failed))
        controller.stop_session("completed")
        wait_until(application, lambda: any(op == "stop" for op, _ in completed) or bool(failed))
        assert not failed
        assert Hdf5RunReader.summary(active).point_count == 1
        assert Hdf5RunReader.summary(active).status == "completed"
        assert not payload.destination.exists()
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


def test_shutdown_cancels_pending_analysis_and_drains_worker_without_report(tmp_path):
    application = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    controller = SpectrumCorrectionController()
    before = [path.read_bytes() for path in (payload.reference_path, payload.signal_path)]
    try:
        controller.difference_archives(replace(payload, config=replace(payload.config, permutations=99999)))
        assert controller.close(wait_ms=5000)
        application.processEvents()
        assert not controller._thread.isRunning()
        assert not payload.destination.exists()
        assert [path.read_bytes() for path in (payload.reference_path, payload.signal_path)] == before
        with pytest.raises(ValueError, match="shutting down"):
            controller.difference_archives(payload)
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()
