"""Shown model selector and asynchronous read-only calibration import."""

import os
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace, StationFileDialog
from app.domain.spectrum_correction import CorrectionConfig
from app.spectrum.realtime_processor import RealtimeSpectrumProcessor
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.test_spectrum_correction_controller import frame, wait_until
from tests.test_spectrum_interference_store import fixture, save, writer


@pytest.mark.parametrize("size,theme", [((1200, 900), "light"), ((800, 700), "dark")])
def test_shown_selector_imports_offline_preserves_display_and_clears_on_profile_refresh(tmp_path, monkeypatch, size, theme):
    application = QApplication.instance() or QApplication([])
    if not QFontDatabase.families() and Path("C:/Windows/Fonts/segoeui.ttf").exists():
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        application.setFont(QFont("Segoe UI", 10))
    apply_application_theme(application, theme)
    path = tmp_path / "calibration.h5"
    calibration = save(path)
    _x, profile, _ = fixture()
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    device_requests = []
    workspace.request_device.connect(lambda *args: device_requests.append(args))
    try:
        workspace.resize(*size)
        workspace.show()
        assert not workspace.load_interference.isEnabled()
        workspace._processed("import_profile", (calibration.context, profile))
        processor = RealtimeSpectrumProcessor(calibration.context, CorrectionConfig())
        processor.set_background_profile(profile)
        envelope, trace = frame(calibration.context, 0, profile.mean_w * 1.1)
        assert processor.ingest(envelope, trace.powers_dbm)
        displayed = processor.snapshot()
        workspace._latest_result, workspace._latest_raw = displayed, trace
        workspace._dirty_view = True
        workspace._render()
        displayed_label = workspace.frame_label.text()
        workspace.freeze.setChecked(True)
        assert workspace.load_interference.isEnabled()
        assert not workspace.acquire_signal.isEnabled()  # Offline import needs no device ownership.
        monkeypatch.setattr(StationFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
        workspace.load_interference.click()
        assert workspace._profile_io_busy
        assert "Loading interference" in workspace.recording_title.text()
        assert not workspace.load_interference.isEnabled()
        wait_until(application, lambda: not workspace._profile_io_busy)
        assert workspace.interference_mode.count() == 2
        assert workspace.interference_mode.currentIndex() == 0  # Loading alone does not enable fitting.
        assert "Choose a background model" in workspace.state_label.text()
        workspace.interference_mode.setCurrentIndex(1)
        assert workspace._latest_result is displayed
        assert workspace.frame_label.text() == displayed_label
        assert workspace.interference_mode.currentData().content_hash == calibration.content_hash
        assert "line-model" in workspace.interference_status.text()
        assert workspace.clear_interference.isEnabled()
        workspace.controls_scroll.ensureWidgetVisible(workspace.interference_status)
        application.processEvents()
        assert workspace.interference_mode.isVisible() and workspace.interference_mode.width() > 100
        assert workspace.stop.isVisible() and workspace.corrected_plot.height() > 70
        viewport = workspace.controls_scroll.viewport()
        for control in (workspace.interference_mode, workspace.load_interference, workspace.interference_status):
            assert viewport.rect().intersects(control.geometry().translated(control.parentWidget().pos()))
        directory = Path("artifacts/spectrum-interference-ui")
        directory.mkdir(parents=True, exist_ok=True)
        assert workspace.grab().save(str(directory / f"selected-{theme}-{size[0]}.png"))
        workspace._running = True
        workspace.set_available(False)
        assert not workspace.interference_mode.isEnabled() and not workspace.clear_interference.isEnabled()
        assert workspace.stop.isEnabled()
        workspace._running = False
        workspace.set_available(False)
        workspace.clear_interference.click()
        assert workspace.interference_mode.currentData() is None
        workspace.interference_mode.setCurrentIndex(1)
        workspace._processed("import_profile", (calibration.context, profile))
        assert workspace.interference_mode.count() == 1
        assert workspace.interference_mode.currentData() is None
        assert not device_requests
    finally:
        workspace.shutdown()
        workspace.close()
        workspace.deleteLater()
        application.processEvents()


def test_unqualified_import_fails_without_replacing_selected_model(tmp_path, monkeypatch):
    application = QApplication.instance() or QApplication([])
    _x, profile, calibration = fixture()
    path = tmp_path / "unqualified.h5"
    run = writer(path)
    try:
        run.store_background_profile(calibration.context, profile)
        run.store_interference_calibration(replace(calibration, signal_control_regions_qualified=False))
    finally:
        run.close("aborted")
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    try:
        workspace._processed("import_profile", (calibration.context, profile))
        workspace._processed("import_interference", (path, (calibration,), ()))
        workspace.interference_mode.setCurrentIndex(1)
        monkeypatch.setattr(StationFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
        workspace.load_interference.click()
        wait_until(application, lambda: not workspace._profile_io_busy)
        assert workspace.interference_mode.currentData().content_hash == calibration.content_hash
        assert "No usable interference calibration" in workspace.state_label.text()
        assert "failed" in workspace.recording_title.text()
    finally:
        workspace.shutdown()
        workspace.deleteLater()
        application.processEvents()
