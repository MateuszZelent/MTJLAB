"""Shown geometry, theme contrast and frozen quantitative preview regression."""

import os
import csv
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.ui.page import AnritsuPage, AnritsuPageState
from app.settings import SettingsRepository
from app.ui.design_system import apply_application_theme
from app.ui.widgets import SpectrumPlotWidget
from tests.helpers import SETTINGS_TEMPLATE
from tests.test_spectrum_correction_store import fixture_profile, signal_fixture


def test_committed_view_arrives_without_render_timer_and_freeze_only_stops_drawing(tmp_path):
    import time
    from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
    from app.devices.anritsu_ms2830a.ui.correction_controller import CorrectionViewSnapshot
    from tests.helpers import simulation_settings

    application = QApplication.instance() or QApplication([])
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    if font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
        application.setFont(QFont("Segoe UI", 10))
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    context, profile = fixture_profile()
    raw, _envelope, result = signal_fixture(context, profile)
    try:
        workspace.resize(1000, 800)
        workspace.show()
        workspace._render_timer.stop()
        workspace.freeze.setChecked(True)
        workspace._context, workspace._profile = context, profile
        workspace._archive_path = tmp_path / "committed.h5"
        workspace._started_monotonic = time.monotonic()
        workspace._kind = "signal"
        workspace._request = MagicMock()
        workspace._processed("frame", {"committed_point_count": 1, "accepted": True,
            "view": CorrectionViewSnapshot(result, raw)})
        application.processEvents()
        assert workspace._latest_result is result and workspace._latest_raw is raw
        assert workspace._result_archive_path == workspace._archive_path
        assert workspace._dirty_view
        assert workspace.corrected_plot.trace_point_count("Signed residual") == 0
        workspace.freeze.setChecked(False)
        application.processEvents()
        assert not workspace._dirty_view
        assert workspace.corrected_plot.isVisible() and workspace.corrected_plot.height() > 70
        np.testing.assert_array_equal(workspace.corrected_plot._traces["Signed residual"][1], result.values_w)
        assert workspace.corrected_plot._traces["Signed residual"][1][1] < 0
    finally:
        assert workspace.shutdown()
        workspace.close()
        workspace.deleteLater()
        application.processEvents()


def test_main_background_receives_first_committed_frame_with_workspace_tab_hidden(tmp_path):
    """The operator stays on Current spectrum throughout correction publication."""
    import time
    from app.devices.anritsu_ms2830a.ui.correction_controller import CorrectionViewSnapshot

    application = QApplication.instance() or QApplication([])
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    if font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
        application.setFont(QFont("Segoe UI", 10))
    controller = MagicMock()
    controller.is_connected = True
    page = AnritsuPage(controller, SettingsRepository(SETTINGS_TEMPLATE).load().settings,
                       single_sweep_available=True)
    workspace = page.correction_workspace
    context, profile = fixture_profile()
    raw, _envelope, result = signal_fixture(context, profile)
    try:
        page.resize(1500, 900)
        page.show()
        page.analysis_tabs.setCurrentIndex(0)
        page.current_spectrum_view.setCurrentIndex(page.current_spectrum_view.findData("background"))
        application.processEvents()
        assert not workspace.isVisible()
        assert page.background_current_empty.isVisible()
        # The first result must reach Current spectrum without a timer tick
        # from the hidden Background correction page.
        workspace._render_timer.stop()
        workspace._context, workspace._profile = context, profile
        workspace._archive_path = tmp_path / "committed-main.h5"
        workspace._started_monotonic = time.monotonic()
        workspace._kind = "signal"
        workspace._request = MagicMock()
        workspace._processed("frame", {"committed_point_count": 1, "accepted": True,
            "view": CorrectionViewSnapshot(result, raw)})
        assert page._background_display is not None
        deadline = time.monotonic() + 2
        while page._background_display is None and time.monotonic() < deadline:
            QTest.qWait(10)
        assert page._background_display is not None
        assert not page.background_current_empty.isVisible()
        plot = page.background_current_plot
        assert plot.isVisible() and plot.width() > 400 and plot.height() >= 180
        np.testing.assert_array_equal(plot._traces["Raw − background"][1], result.values_w)
        assert result.values_w[1] < 0
        lower, upper = plot.plot.viewRange()[1]
        assert lower < min(result.values_w) and upper > max(result.values_w)
        for mode in ("legacy", "background"):
            page.current_spectrum_view.setCurrentIndex(page.current_spectrum_view.findData(mode))
            application.processEvents()
        assert plot.isVisible() and not workspace.isVisible()
        assert plot.trace_point_count("Raw − background") == len(context.frequencies_hz)
        directory = Path("artifacts/spectrum-correction-layout")
        directory.mkdir(parents=True, exist_ok=True)
        assert page.grab().save(str(directory / "current-background-first-committed-frame.png"))
    finally:
        page.close()
        page.deleteLater()
        application.processEvents()


def test_background_empty_view_has_visible_action_and_rejection_reason(tmp_path):
    application = QApplication.instance() or QApplication([])
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    if font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
        application.setFont(QFont("Segoe UI", 10))
    controller = MagicMock()
    controller.is_connected = True
    controller.visa_address = "SIM::ANRITSU"
    page = AnritsuPage(controller, SettingsRepository(SETTINGS_TEMPLATE).load().settings,
                       single_sweep_available=True)
    workspace = page.correction_workspace
    try:
        page.resize(1200, 900)
        page.show()
        page.current_spectrum_view.setCurrentIndex(page.current_spectrum_view.findData("background"))
        application.processEvents()
        assert page.background_current_empty.isVisible()
        assert page.background_current_empty.height() >= 180
        assert page.current_start_background_measurement.isVisible()
        assert page.current_start_background_measurement.text() == "Record background…"
        assert "No background profile" in page.background_current_empty_text.text()
        context, profile = fixture_profile()
        workspace._context, workspace._profile = context, profile
        page._update_current_background_actions()
        assert page.current_start_background_measurement.text() == "Record corrected spectra…"
        workspace._kind = "signal"
        workspace._running = True
        workspace._archive_path = tmp_path / "rejected.h5"
        workspace._request = MagicMock()
        workspace._processed("frame", {
            "committed_point_count": 1, "accepted": False, "quality": "stale",
        })
        application.processEvents()
        assert "Correction rejected (stale)" in page.background_current_empty_text.text()
        assert "Record a new background" in page.background_current_empty_text.text()
        assert not page.background_current_plot.isVisible()
        workspace._processed("stop", None)
        workspace._render()
        assert "Correction rejected (stale)" in page.background_current_empty_text.text()
        assert "Background ready" not in page.background_current_empty_text.text()
        assert page.current_start_background_measurement.text() == "Record background…"
        assert page.live.text() == "Record background…"
        assert page.live.isEnabled() == workspace.acquire_reference.isEnabled()
        directory = Path("artifacts/spectrum-correction-layout")
        directory.mkdir(parents=True, exist_ok=True)
        assert page.grab().save(str(directory / "current-background-rejected.png"))
    finally:
        workspace._running = False
        page.close()
        application.processEvents()


@pytest.mark.parametrize("values", [(-1e-18, 0, 1e-18), (1e-18, 1e-18, 1e-18),
                                   (-1e-18, -1e-18, -1e-18), (0, 0, 0)])
def test_signed_watt_plot_reset_fits_weak_values_instead_of_one_watt(values):
    application = QApplication.instance() or QApplication([])
    plot = SpectrumPlotWidget()
    try:
        plot.set_labels(y="Signed residual", y_unit="W")
        plot.resize(1000, 650)
        plot.show()
        plot.set_trace("Residual", [1e6, 2e6, 3e6], values, primary=True)
        plot.auto_range()
        application.processEvents()
        x_range, y_range = plot.plot.viewRange()
        assert x_range[0] < 1e6 < 3e6 < x_range[1]
        assert y_range[0] <= min(values) <= max(values) <= y_range[1]
        assert y_range[1] - y_range[0] < (3e-15 if not any(values) else 3e-18)
        np.testing.assert_array_equal(plot._traces["Residual"][1], values)
    finally:
        plot.close()
        plot.deleteLater()
        application.processEvents()


def test_background_first_frame_refits_old_zoom_and_preserves_subsequent_user_zoom():
    application = QApplication.instance() or QApplication([])
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    if not QFontDatabase.families() and font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
        application.setFont(QFont("Segoe UI", 10))
    controller = MagicMock()
    controller.is_connected = True
    controller.visa_address = "SIM::ANRITSU"
    page = AnritsuPage(controller, SettingsRepository(SETTINGS_TEMPLATE).load().settings,
                       single_sweep_available=True)
    try:
        page.resize(1500, 900)
        page.show()
        page.current_spectrum_view.setCurrentIndex(page.current_spectrum_view.findData("background"))
        context, profile = fixture_profile()
        _raw, _envelope, result = signal_fixture(context, profile)
        result = replace(result, values_w=np.array([1e-18, -1e-18, 2e-18]))
        view = page.background_current_plot.plot.getViewBox()
        view.setRange(xRange=(0, 1), yRange=(0, 1), padding=0)
        page._background_display_changed(context, result, "weak signal")
        application.processEvents()
        x_range, y_range = view.viewRange()
        assert x_range[0] < 1e6 < 3e6 < x_range[1]
        assert y_range[0] < -1e-18 < 2e-18 < y_range[1]
        assert y_range[1] - y_range[0] < 4e-18
        workspace = page.correction_workspace
        workspace._context, workspace._profile = context, profile
        workspace._latest_result = result
        workspace._dirty_view = True
        workspace._render()
        corrected_range = workspace.corrected_plot.plot.viewRange()[1]
        assert corrected_range[0] < -1e-18 < 2e-18 < corrected_range[1]
        assert corrected_range[1] - corrected_range[0] < 4e-18
        directory = Path("artifacts/spectrum-correction-layout")
        directory.mkdir(parents=True, exist_ok=True)
        assert page.grab().save(str(directory / "current-background-weak-signal.png"))
        view.setRange(xRange=(1.4e6, 2.4e6), yRange=(-3e-18, 3e-18), padding=0)
        previous = view.viewRange()
        page._background_display_changed(context, replace(result, frame_id=8), "next frame")
        np.testing.assert_allclose(view.viewRange(), previous)
        page._background_display_changed(None, None, "new measurement")
        page._background_display_changed(context, result, "new first frame")
        assert view.viewRange()[0][0] < 1e6 < 3e6 < view.viewRange()[0][1]
        # A later constant residual outside the first frame's tiny range must
        # not look empty before the user deliberately zooms.
        shifted = replace(result, values_w=np.array([1e-12, 2e-12, 3e-12]))
        page._background_display_changed(context, shifted, "larger residual")
        assert view.viewRange()[1][0] < 1e-12 < 3e-12 < view.viewRange()[1][1]
        view.setRange(xRange=(0, 1), yRange=(0, 1), padding=0)
        view.sigRangeChangedManually.emit([True, True])
        page._background_display_changed(context, shifted, "user zoom retained")
        assert view.viewRange()[0] == [0, 1]
        page.background_current_plot.set_trace_visibility("Raw − background", False)
        page.current_fit_background.click()
        assert page.background_current_plot._curves["Raw − background"].isVisible()
        assert view.viewRange()[0][0] < 1e6 < 3e6 < view.viewRange()[0][1]
        # Native plot menu settings can hide negative watts on a log axis.
        # Returning to the view restores the actual signed recorded values.
        plot = page.background_current_plot
        recovered = replace(shifted, values_w=np.array([-1e-12, 2e-12, 3e-12]))
        page._background_display_changed(context, recovered, "signed residual")
        plot.plot.setLogMode(x=True, y=True)
        plot.set_trace_visibility("Raw − background", False)
        view.setRange(xRange=(0, 1), yRange=(0, 1), padding=0)
        page.current_spectrum_view.setCurrentIndex(page.current_spectrum_view.findData("legacy"))
        page.current_spectrum_view.setCurrentIndex(page.current_spectrum_view.findData("background"))
        application.processEvents()
        curve = plot._curves["Raw − background"]
        assert plot.isVisible() and curve.isVisible()
        assert curve.opts["logMode"] == [False, False]
        np.testing.assert_array_equal(curve.getData()[0], context.frequencies_hz)
        np.testing.assert_array_equal(curve.getData()[1], recovered.values_w)
        assert view.viewRange()[0][0] < 1e6 < 3e6 < view.viewRange()[0][1]
        assert view.viewRange()[1][0] < -1e-12 < 3e-12 < view.viewRange()[1][1]
        plot.plot.setLogMode(y=True)
        page.current_fit_background.click()
        assert curve.opts["logMode"] == [False, False]
        np.testing.assert_array_equal(curve.getData()[1], recovered.values_w)
        # A fresh publication must also recover while the operator remains on
        # this page: all-negative residuals otherwise vanish on a log axis.
        negative = replace(recovered, frame_id=9, values_w=np.array([-3e-12, -2e-12, -1e-12]))
        plot.plot.setLogMode(x=True, y=True)
        page._background_display_changed(context, negative, "negative signed residual")
        application.processEvents()
        assert curve.opts["logMode"] == [False, False]
        np.testing.assert_array_equal(curve.getData()[0], context.frequencies_hz)
        np.testing.assert_array_equal(curve.getData()[1], negative.values_w)
        assert view.viewRange()[1][0] < -3e-12 < -1e-12 < view.viewRange()[1][1]
        workspace.corrected_plot.plot.setLogMode(x=True, y=True)
        workspace._latest_result = negative
        workspace._dirty_view = True
        workspace._render()
        application.processEvents()
        workspace_curve = workspace.corrected_plot._curves["Signed residual"]
        assert workspace_curve.opts["logMode"] == [False, False]
        np.testing.assert_array_equal(workspace_curve.getData()[1], negative.values_w)
        corrected_range = workspace.corrected_plot.plot.viewRange()[1]
        assert corrected_range[0] < -3e-12 < -1e-12 < corrected_range[1]
        workspace.freeze.setChecked(True)
        application.processEvents()
        assert page.current_resume_background_preview.isVisible()
        assert "Preview is frozen" in page.background_current_status.text()
        assert page.current_resume_background_preview.parent() != page.background_current_empty
        assert page.current_fit_background.isVisible()
        assert page.grab().save(str(directory / "current-background-recovery.png"))
        page.current_resume_background_preview.click()
        assert not workspace.freeze.isChecked()
        assert "Preview is frozen" not in page.background_current_status.text()
    finally:
        page.close()
        application.processEvents()


def test_dense_curve_preserves_single_bin_peaks_full_arrays_and_csv_across_theme_change(tmp_path):
    application = QApplication.instance() or QApplication([])
    plot = SpectrumPlotWidget(csv_value_column="signed_power_w")
    x = np.linspace(1e6, 6e9, 10001)
    y = np.zeros(10001)
    y[4311], y[7432] = 1e-12, -2e-12
    try:
        plot.set_labels(y="Signed residual", y_unit="W")
        plot.resize(1100, 650)
        plot.show()
        plot.set_trace("Dense", x, y, primary=True)
        plot.auto_range()
        application.processEvents()
        plot.apply_theme("dark")
        np.testing.assert_array_equal(plot._traces["Dense"][1], y)
        np.testing.assert_array_equal(plot._curves["Dense"].yData, y)
        plot.peak_search()
        assert plot.marker.value() == x[4311]
        path = tmp_path / "full-spectrum.csv"
        plot._export_csv(path)
        exported = np.loadtxt(path, delimiter=",", skiprows=1, usecols=(1, 2))
        np.testing.assert_array_equal(exported[:, 0], x)
        np.testing.assert_array_equal(exported[:, 1], y)
        assert plot._curves["Dense"].opts["pen"].widthF() == 1.0
    finally:
        plot.close()
        plot.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("size", [(1500, 900), (800, 700)])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_correction_page_is_visible_and_signed_preview_freezes_as_one_frame(size, theme, tmp_path, monkeypatch):
    application = QApplication.instance() or QApplication([])
    # Windows' offscreen Qt platform does not enumerate system fonts. Load the
    # real application typeface so visual artifacts remain reviewable.
    if not QFontDatabase.families():
        font_path = Path("C:/Windows/Fonts/segoeui.ttf")
        if font_path.exists():
            assert QFontDatabase.addApplicationFont(str(font_path)) >= 0
            application.setFont(QFont("Segoe UI", 10))
    apply_application_theme(application, theme)
    controller = MagicMock()
    controller.is_connected = False
    controller.visa_address = "SIM::ANRITSU"
    page = AnritsuPage(controller, SettingsRepository(SETTINGS_TEMPLATE).load().settings,
                       single_sweep_available=True)
    try:
        page._set_page_state(AnritsuPageState.DISCONNECTED)
        page.resize(*size)
        page.show()
        page.current_spectrum_view.setCurrentIndex(page.current_spectrum_view.findData("background"))
        application.processEvents()
        assert page.background_current_empty.isVisible()
        assert "No background profile" in page.background_current_empty_text.text()
        assert not page.background_current_plot.isVisible()
        assert not page.current_record_corrected.isEnabled()
        assert not page.live.isEnabled()
        assert page.live.text() == "Record background…"
        for button in (page.current_record_background, page.current_record_corrected, page.current_stop_corrected):
            assert button.isVisible()
            assert button.width() >= button.minimumSizeHint().width()
        empty_directory = Path("artifacts/spectrum-correction-layout")
        empty_directory.mkdir(parents=True, exist_ok=True)
        assert page.grab().save(str(empty_directory / f"current-background-empty-{theme}-{size[0]}.png"))
        page.current_spectrum_view.setCurrentIndex(page.current_spectrum_view.findData("legacy"))
        page.analysis_tabs.setCurrentIndex(2)
        application.processEvents()
        workspace = page.correction_workspace
        assert workspace.isVisible()
        assert workspace.width() > 300
        controls = (workspace.acquire_reference, workspace.acquire_signal, workspace.stop,
                    workspace.freeze, workspace.load_profile, workspace.save_profile, workspace.finalize_button)
        for control in controls:
            assert control.isVisible()
            assert control.width() >= control.minimumSizeHint().width()
            if control in (workspace.stop, workspace.freeze, workspace.finalize_button):
                assert workspace.rect().contains(control.mapTo(workspace, QPoint(0, 0)))
            else:
                assert workspace.controls_scroll.widget().rect().contains(
                    control.mapTo(workspace.controls_scroll.widget(), QPoint(0, 0)))
        if size[0] < 900:
            assert workspace.controls_scroll.verticalScrollBar().maximum() > 0
        assert workspace.raw_plot.height() > 70
        assert workspace.corrected_plot.height() > 70
        context, profile = fixture_profile()
        raw, _envelope, result = signal_fixture(context, profile)
        workspace._context, workspace._profile = context, profile
        workspace._latest_raw, workspace._latest_result = raw, result
        workspace._dirty_view = True
        workspace._render()
        assert workspace.corrected_plot._traces["Signed residual"][1][1] < 0
        workspace.freeze.setChecked(True)
        previous = workspace.raw_plot._traces["Raw"][1].copy()
        previous_label = workspace.frame_label.text()
        workspace._latest_raw = type(raw)(raw.frequencies_hz, tuple(np.asarray(raw.powers_dbm) + 10),
                                         raw.acquired_at_utc, raw.trace_name)
        workspace._dirty_view = True
        workspace._render()
        np.testing.assert_array_equal(workspace.raw_plot._traces["Raw"][1], previous)
        assert workspace.frame_label.text() == previous_label
        assert workspace._dirty_view  # Unfreeze still has a pending complete frame.
        workspace.freeze.setChecked(False)
        np.testing.assert_allclose(workspace.raw_plot._traces["Raw"][1], previous + 10)
        workspace.set_available(False)
        assert not workspace.acquire_reference.isEnabled()
        assert not workspace.acquire_signal.isEnabled()
        assert workspace.load_profile.isEnabled()
        page._set_page_state(AnritsuPageState.IDLE)
        workspace.set_available(True)
        assert workspace.acquire_reference.isEnabled() and workspace.acquire_signal.isEnabled()
        workspace._message("unqualified · reference age 12.0 s · provisional signed power")
        application.processEvents()
        directory = Path("artifacts/spectrum-correction-layout")
        directory.mkdir(parents=True, exist_ok=True)
        assert page.grab().save(str(directory / f"correction-{theme}-{size[0]}.png"))
        device_calls = controller.call.call_count
        page.current_spectrum_view.setCurrentIndex(page.current_spectrum_view.findData("background"))
        page.analysis_tabs.setCurrentIndex(0)
        application.processEvents()
        QTest.qWait(300)  # Let Fluent's selected-tab indicator settle for the artifact.
        assert page.background_current_plot.isVisible() and page.background_current_plot.height() > 70
        assert not page.spectrum_plot.isVisible() and not page.signal_analysis_card.isVisible()
        assert not page.manual_save_card.isVisible() and not page.processing_card.isVisible()
        assert controller.call.call_count == device_calls
        np.testing.assert_array_equal(page.background_current_plot._traces["Raw − background"][1], result.values_w)
        assert page.background_current_plot._traces["Raw − background"][1][1] < 0
        page._latest_trace = raw
        page._refresh_spectrum_display()  # Unrelated raw reads cannot replace the signed result.
        np.testing.assert_array_equal(page.background_current_plot._traces["Raw − background"][1], result.values_w)
        assert "frame 7" in page.background_current_status.text()
        assert "unqualified" in page.background_current_status.text()
        assert page.background_current_plot.plot.getAxis("left").labelUnits == "W"
        csv_path = tmp_path / "signed.csv"
        page.background_current_plot._export_csv(csv_path)
        with csv_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        assert rows[0] == ["trace", "frequency_Hz", "signed_power_w"]
        np.testing.assert_allclose([float(row[2]) for row in rows[1:]], result.values_w, rtol=1e-12)
        assert page.grab().save(str(directory / f"current-background-{theme}-{size[0]}.png"))
        page.current_spectrum_view.setCurrentIndex(page.current_spectrum_view.findData("legacy"))
        application.processEvents()
        assert page.spectrum_plot.isVisible() and not page.background_current_plot.isVisible()
        assert page.signal_analysis_card.isVisible() and page.manual_save_card.isVisible()
        workspace.freeze.setChecked(True)
        workspace._processed("import_profile", (context, profile))
        assert page._background_display is None  # Explicit import invalidates even a frozen old result.
        workspace.freeze.setChecked(False)
        page.analysis_tabs.setCurrentIndex(2)
        # File-dialog Save must visibly acknowledge start even with no reply
        # from the analyzer. Scroll position cannot hide the persistent status.
        monkeypatch.setattr("app.devices.anritsu_ms2830a.ui.correction_card.StationFileDialog.getSaveFileName",
                            lambda *args: (str(tmp_path / "background.h5"), ""))
        workspace.reference_state.setText("control state; signal absence unknown")
        workspace.controls_scroll.verticalScrollBar().setValue(
            workspace.controls_scroll.verticalScrollBar().maximum())
        workspace.acquire_reference.click()
        application.processEvents()
        assert workspace.running
        assert page._background_display is None
        assert workspace.recording_activity.isVisible()
        assert "Starting background recording" in workspace.recording_title.text()
        assert "Save accepted" in workspace.state_label.text()
        for label in (workspace.recording_title, workspace.state_label, workspace.archive_label):
            top_left = label.mapTo(workspace, QPoint(0, 0))
            assert label.isVisible() and workspace.rect().contains(top_left)
            assert workspace.rect().contains(top_left + QPoint(label.width() - 1, label.height() - 1))
        assert page.grab().save(str(directory / f"recording-start-{theme}-{size[0]}.png"))
        workspace.handle_error("read_full_configuration", "Analyzer did not respond; check the connection.")
        application.processEvents()
        assert workspace.recording_title.text() == "Recording failed"
        assert "Analyzer did not respond" in workspace.state_label.text()
        assert workspace.recording_activity.isHidden()
        assert page.grab().save(str(directory / f"recording-error-{theme}-{size[0]}.png"))
    finally:
        page.close()
        page.deleteLater()
        application.processEvents()
