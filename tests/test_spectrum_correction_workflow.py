"""Reference and measurement workflow through the real adapter with simulated VISA."""

import os
import json
from threading import Event
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import h5py
import numpy as np
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.anritsu_ms2830a.module import MODULE
from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.devices.simulators import SimulatedVisaFactory
from app.spectrum.replay import replay_quantitative_session
from tests.helpers import simulation_settings
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_finalized_store import archives
from app.devices.anritsu_ms2830a.ui.correction_card import StationFileDialog
from app.storage.finalized_spectrum_store import replay_finalized_artifact


@pytest.mark.parametrize("use_interference", [False, True])
def test_reference_then_signal_archives_every_completed_sweep_and_replays(tmp_path, use_interference):
    application = QApplication.instance() or QApplication([])
    settings = simulation_settings()
    settings = settings.model_copy(update={"devices": settings.devices.model_copy(update={
        "anritsu": settings.anritsu.model_copy(update={
            "spectrum_correction": settings.anritsu.spectrum_correction.model_copy(update={
                "calibration_min_sweeps": 3,
            }),
        }),
    })})
    adapter = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
    adapter.connect()
    workspace = SpectrumCorrectionWorkspace(settings, single_sweep_available=True, simulation_mode=True)
    errors = []
    requests = []
    signal_sweeps = 0
    workspace._cpu.failed.connect(lambda *args: errors.append(args))
    published = []
    workspace.display_changed.connect(lambda *args: published.append(args))

    def request(operation, payload):
        def deliver():
            nonlocal signal_sweeps
            try:
                requests.append(operation)
                result = MODULE.dispatch(adapter, operation, payload)
                if operation == "single_sweep" and workspace._kind == "signal":
                    signal_sweeps += 1
                    if signal_sweeps == 4:
                        workspace.stop_acquisition()
                assert workspace.handle_result(operation, result)
            except Exception as exc:
                errors.append((operation, str(exc)))
                workspace.handle_error(operation, str(exc))
        QTimer.singleShot(0, deliver)

    workspace.request_device.connect(request)
    try:
        workspace.set_available(True, device_idn="ANRITSU,SIM")
        workspace.reference_state.setText("simulated off resonance")
        workspace.duration.setText("1 ms")
        reference_path, signal_path = tmp_path / "reference.h5", tmp_path / "signal.h5"
        workspace.start_acquisition("reference", reference_path)
        assert workspace.running
        wait_until(application, lambda: not workspace.running or bool(errors), timeout=15)
        assert not errors
        assert workspace._profile is not None and workspace._profile.sweep_count == 3
        assert workspace.recording_title.text() == "Recording finished"
        assert workspace.recording_activity.isHidden()
        assert "3 raw spectra saved" in workspace.state_label.text()
        if use_interference:
            from app.domain.spectrum_interference import SpectrumInterferenceCalibration

            profile, context = workspace._profile, workspace._context
            calibration = SpectrumInterferenceCalibration(
                "simulated-amplitude", context, profile.mean_w, profile.mean_w[:, None] * .1,
                np.ones(profile.mean_w.size, dtype=bool), np.zeros(profile.mean_w.size, dtype=bool),
                np.full(profile.mean_w.size, 1e-12), ((-.9, 10),),
                ((profile.profile_id, profile.content_hash),), signal_control_regions_qualified=True,
                qualification_evidence="synthetic control only; no laboratory qualification",
            )
            workspace._processed("import_interference", (reference_path, (calibration,), ()))
            workspace.interference_mode.setCurrentIndex(1)
        workspace.set_available(True)
        workspace.mode.setCurrentIndex(workspace.mode.findData("block"))
        workspace.start_acquisition("signal", signal_path)
        assert workspace.running
        assert workspace.recording_title.text() == "Starting corrected spectrum recording…"
        wait_until(application, lambda: not workspace.running or bool(errors), timeout=15)
        assert not errors
        assert signal_sweeps == 4
        assert workspace.recording_title.text() == "Recording stopped"
        assert "4 raw spectra saved" in workspace.state_label.text()
        assert workspace._latest_result is not None and workspace._latest_result.count == 4
        assert workspace._latest_result.standard_uncertainty_w is None
        workspace._render()
        published_context, published_result, description = published[-1]
        assert published_result is workspace._latest_result
        assert published_context.context_id == published_result.context_id
        assert str(signal_path) in description and str(reference_path) not in description
        reference_frames = list(replay_quantitative_session(reference_path))
        signal_frames = list(replay_quantitative_session(signal_path))
        assert len(reference_frames) == 3 and len(signal_frames) == 4
        assert signal_frames[-1].result.count == 4
        assert (signal_frames[-1].result.interference_model_id is not None) is use_interference
        if use_interference:
            assert "model simulated-amplitude" in description
        for path, status in ((reference_path, "completed"), (signal_path, "aborted")):
            with h5py.File(path, "r") as file:
                assert file["run"].attrs["status"] == status
                metadata = json.loads(file["run/simulation_json"].asstr()[()])
                assert metadata["enabled"] is True and metadata["mode"] == "simulation"
                assert metadata["mode_source"] == "application_runtime"
        assert requests.count("read_acquisition_configuration") == 2
        assert requests.count("single_sweep") == 7
    finally:
        assert workspace.shutdown()
        workspace.deleteLater()
        adapter.disconnect()
        application.processEvents()


def test_interleaved_policy_waits_for_operator_confirmation_before_any_device_request_or_archive(tmp_path):
    application = QApplication.instance() or QApplication([])
    settings = simulation_settings()
    correction = settings.anritsu.spectrum_correction
    settings = settings.model_copy(update={"devices": settings.devices.model_copy(update={
        "anritsu": settings.anritsu.model_copy(update={
            "spectrum_correction": correction.model_copy(update={
                "reference_policy": correction.reference_policy.model_copy(update={"mode": "interleaved"}),
            }),
        }),
    })})
    workspace = SpectrumCorrectionWorkspace(settings, single_sweep_available=True)
    requests = []
    workspace.request_device.connect(lambda *args: requests.append(args))
    try:
        workspace.set_available(True)
        workspace.reference_state.setText("synthetic reference")
        workspace.signal_state.setText("synthetic signal")
        destination = tmp_path / "alternating.h5"
        workspace.start_acquisition("interleaved", destination)
        assert workspace.running and not requests and not destination.exists()
        assert "prepare REFERENCE" in workspace.recording_title.text()
        assert not workspace.confirm_interleaved_state.isEnabled()
        workspace.stop_acquisition()
        assert not workspace.running and not requests
    finally:
        assert workspace.shutdown()
        workspace.deleteLater()
        application.processEvents()


def test_record_dialog_validates_before_file_selection_and_starts_without_second_click(tmp_path, monkeypatch):
    application = QApplication.instance() or QApplication([])
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    selected, requests = [], []

    def choose(*args):
        selected.append(True)
        return str(tmp_path / "reference.h5"), ""

    monkeypatch.setattr(StationFileDialog, "getSaveFileName", choose)
    workspace.request_device.connect(lambda *args: requests.append(args))
    try:
        workspace.set_available(True)
        workspace._start_dialog("reference")
        assert not selected and not requests and not workspace.running
        assert "Enter the reference state description" in workspace.state_label.text()
        assert not workspace.acquire_signal.isEnabled()
        assert "profile first" in workspace.acquire_signal.toolTip()
        workspace.reference_state.setText("control state; signal absence unknown")
        workspace._start_dialog("reference")
        assert selected and workspace.running
        assert requests == [("read_acquisition_configuration", None)]
        assert not workspace.acquire_reference.isEnabled()
        assert workspace.acquire_reference.text() == "Recording background…"
        assert workspace.stop.isEnabled()
        # Feedback is set synchronously, before any instrument response arrives.
        assert workspace.recording_title.text() == "Starting background recording…"
        assert "Save accepted" in workspace.state_label.text()
        assert str(tmp_path / "reference.h5") in workspace.archive_label.text()
        assert not workspace.recording_activity.isHidden()
        assert "Waiting for the first spectrum" in workspace.frame_label.text()
    finally:
        assert workspace.shutdown()
        workspace.deleteLater()
        application.processEvents()


def test_save_cancel_does_not_claim_recording_and_preflight_failure_stays_visible(tmp_path, monkeypatch):
    application = QApplication.instance() or QApplication([])
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    requests = []
    workspace.request_device.connect(lambda *args: requests.append(args))
    try:
        workspace.resize(800, 700)
        workspace.show()
        workspace.set_available(True)
        workspace.reference_state.setText("control; signal absence unknown")
        monkeypatch.setattr(StationFileDialog, "getSaveFileName", lambda *args: ("", ""))
        workspace._start_dialog("reference")
        assert not workspace.running and not requests
        assert workspace.recording_title.text() == "Recording canceled"
        assert "not started" in workspace.state_label.text()
        assert workspace.recording_activity.isHidden()
        monkeypatch.setattr(StationFileDialog, "getSaveFileName",
                            lambda *args: (str(tmp_path / "failed-reference.h5"), ""))
        workspace._start_dialog("reference")
        application.processEvents()
        assert workspace.recording_status.isVisible() and workspace.recording_activity.isVisible()
        with pytest.raises(ValueError, match="during a recording"):
            workspace.set_simulation_mode(True)
        assert workspace.recording_title.text() == "Starting background recording…"
        assert workspace.handle_error("read_acquisition_configuration", "Instrument timeout")
        wait_until(application, lambda: "No archive was opened" in workspace.archive_label.text())
        assert not workspace.running and workspace.recording_activity.isHidden()
        assert workspace.recording_title.text() == "Recording failed"
        assert "Instrument timeout" in workspace.state_label.text()
        assert not (tmp_path / "failed-reference.h5").exists()
    finally:
        assert workspace.shutdown()
        workspace.deleteLater()
        application.processEvents()


def test_shell_propagates_runtime_mode_to_correction_workspace_with_isolated_catalogue(tmp_path):
    from app.settings import SettingsRepository
    from app.ui.shell import MainWindow

    application = QApplication.instance() or QApplication([])
    settings = simulation_settings()
    settings = settings.model_copy(update={"storage": {
        **settings.storage, "output_directory": str(tmp_path / "measurements"),
        "catalogue_directory": str(tmp_path / "catalogue"),
    }})
    path = tmp_path / "settings.yml"
    SettingsRepository(path).save(settings)
    window = MainWindow(path, simulation=True)
    try:
        window.show()
        window._navigate_to("anritsu")
        window.anritsu_page._open_recording_setup()
        window.anritsu_page.recording_tabs.setCurrentIndex(1)
        application.processEvents()
        workspace = window.anritsu_page.correction_workspace
        assert workspace.simulation_mode is True
        assert workspace.isVisible() and workspace.width() > 0 and workspace.height() > 0
    finally:
        window.close()
        window.deleteLater()
        application.processEvents()


def test_offline_cancel_action_remains_visible_and_reports_cancellation(tmp_path, monkeypatch):
    application = QApplication.instance() or QApplication([])
    if not QFontDatabase.families():
        font_path = Path("C:/Windows/Fonts/segoeui.ttf")
        if font_path.exists():
            assert QFontDatabase.addApplicationFont(str(font_path)) >= 0
            application.setFont(QFont("Segoe UI", 10))
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    entered = Event()
    selected = iter(tmp_path / name for name in ("signal.h5", "before.h5", "after.h5"))
    destination = tmp_path / "cancelled-final.h5"
    monkeypatch.setattr(StationFileDialog, "getOpenFileName", lambda *args: (str(next(selected)), ""))
    monkeypatch.setattr(StationFileDialog, "getSaveFileName", lambda *args: (str(destination), ""))

    def slow_finalize(*args, cancellation_check=None, **_selection):
        entered.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            cancellation_check()
            time.sleep(.002)
        raise AssertionError("Cancel processing did not interrupt the offline worker")

    monkeypatch.setattr("app.storage.finalized_spectrum_store.finalize_spectrum_archives", slow_finalize)
    requests = []
    workspace.request_device.connect(lambda *args: requests.append(args))
    try:
        workspace.resize(800, 700)
        workspace.show()
        workspace._finalize_dialog()
        wait_until(application, entered.is_set)
        assert workspace.stop.isVisible() and workspace.stop.isEnabled()
        assert workspace.stop.text() == "Cancel processing"
        directory = Path("artifacts/spectrum-correction-layout")
        directory.mkdir(parents=True, exist_ok=True)
        assert workspace.grab().save(str(directory / "finalization-processing.png"))
        workspace.stop.click()
        assert workspace.recording_title.text() == "Canceling finalization…"
        assert not workspace.stop.isEnabled()
        wait_until(application, lambda: not workspace._profile_io_busy)
        assert workspace.recording_title.text() == "Finalization canceled"
        assert not requests and not destination.exists()
        assert workspace.progress.isHidden() and workspace.finalize_button.isEnabled()
        assert workspace.stop.text() == "Stop acquisition" and not workspace.stop.isEnabled()
        assert workspace.grab().save(str(directory / "finalization-canceled.png"))
    finally:
        assert workspace.shutdown()
        workspace.deleteLater()
        application.processEvents()


def test_offline_finalization_action_runs_worker_and_displays_final_block(tmp_path, monkeypatch):
    application = QApplication.instance() or QApplication([])
    _context, expected, paths = archives(tmp_path)
    destination = tmp_path / "finalized-from-ui.h5"
    selections = iter(paths)
    monkeypatch.setattr(StationFileDialog, "getOpenFileName", lambda *args: (str(next(selections)), ""))
    monkeypatch.setattr(StationFileDialog, "getSaveFileName", lambda *args: (str(destination), ""))
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    errors, device_requests = [], []
    workspace._cpu.failed.connect(lambda *args: errors.append(args))
    workspace.request_device.connect(lambda *args: device_requests.append(args))
    try:
        workspace.set_available(False)  # Offline processing needs no connected hardware.
        assert workspace.finalize_button.isEnabled()
        workspace._finalize_dialog()
        assert workspace._profile_io_busy and not workspace.finalize_button.isEnabled()
        wait_until(application, lambda: not workspace._profile_io_busy or bool(errors), timeout=15)
        assert not errors and not device_requests
        workspace.resize(1000, 800)
        workspace.show()
        application.processEvents()
        workspace._render()
        assert workspace._latest_result.final and "final block" in workspace.frame_label.text()
        assert workspace._result_archive_path == destination
        assert {"Reference before", "Reference after", "Raw"} <= workspace.raw_plot._traces.keys()
        np.testing.assert_allclose(workspace.corrected_plot._traces["Signed residual"][1], expected, rtol=1e-12)
        assert replay_finalized_artifact(destination).result.count == 3
        assert workspace.finalize_button.isEnabled()
        workspace.resize(1000, 800)
        workspace.show()
        application.processEvents()
        assert workspace.corrected_plot.isVisible() and workspace.corrected_plot.height() > 100
        artifact_dir = Path("artifacts/spectrum-correction-layout")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        assert workspace.grab().save(str(artifact_dir / "finalized-block.png"))
    finally:
        assert workspace.shutdown()
        workspace.deleteLater()
        application.processEvents()
