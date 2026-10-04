"""Exercise cleanup through user controls and the real asynchronous worker."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from app.settings import SettingsRepository
from app.ui.design_system import apply_application_theme


@pytest.fixture(scope="module")
def application():
    application = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        application.setFont(QFont("Segoe UI", 10))
    return application


@pytest.fixture
def page(application):
    controller = MagicMock()
    controller.is_connected = False
    controller.visa_address = "GPIB0::23::INSTR"
    settings = SettingsRepository("app/resources/settings.template.yml").load().settings
    widget = AnritsuPage(controller, settings, single_sweep_available=True)
    yield widget
    widget.close()
    widget.deleteLater()
    application.processEvents()


def trace(values):
    return SpectrumTrace(
        tuple(np.linspace(200e6, 1200e6, len(values))),
        tuple(values), datetime.now(UTC), "TRAC1",
    )


def wait_for_analysis(application, page):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        application.processEvents()
        if page._cleanup_result is not None and not page._analysis_controller.busy:
            return page._cleanup_result
        QTest.qWait(10)
    pytest.fail(f"Cleanup did not complete: {page.analysis_status.text()}")


@pytest.mark.parametrize("theme,size", [("light", (1600, 1000)), ("dark", (1100, 800))])
def test_cleanup_follows_reference_subtraction_without_selecting_source(application, page, theme, size):
    apply_application_theme(application, theme)
    x = np.linspace(200e6, 1200e6, 2001)
    broad = 2 * np.exp(-4 * np.log(2) * ((x - 700e6) / 80e6) ** 2)
    spike = 5 * np.exp(-4 * np.log(2) * ((x - 450e6) / 3e6) ** 2)
    reference = trace(np.full(x.size, -80.))
    raw = trace(-80 + broad + spike)
    try:
        page.resize(*size)
        page.show()
        application.processEvents()
        # Capture reference first, then switch to the difference view, as in the report.
        page._show_trace(reference, update_controls=False)
        page._set_reference(page._build_reference(reference, kind="single", count=1))
        page.reference_operation.setCurrentIndex(page.reference_operation.findData("difference_db"))
        page._show_trace(raw, update_controls=False)
        page.cleanup_filters["narrow_reject"].setChecked(True)
        cleanup = wait_for_analysis(application, page)
        assert page._analysis_source_key == "processed"
        assert cleanup.unit == "dB"
        assert 500 in cleanup.removed_peak_indices
        assert abs(cleanup.values[500] - broad[500]) < .2
        analysis = page._display_state.by_key["analysis:processed"]
        assert analysis.values == cleanup.values
        curve = page.spectrum_plot._curves["Analysis"]
        assert curve.isVisible()
        assert not page.spectrum_plot._curves["Processed"].isVisible()
        np.testing.assert_allclose(curve.getData()[1], cleanup.values)
        assert page.signal_analysis_card.isVisible() and page.signal_analysis_card.width() > 300
        assert page.spectrum_plot.isVisible() and page.spectrum_plot.height() > 100
        assert "Processed" in page.analysis_source.currentText()
        assert "Analyzing" not in page.analysis_status.text()
        page.overlay_analysis_source.setChecked(True)
        assert page.spectrum_plot._curves["Processed"].isVisible()
        page.overlay_analysis_source.setChecked(False)
        assert not page.spectrum_plot._curves["Processed"].isVisible()
        page.cleanup_filters["narrow_reject"].setChecked(False)
        wait_for_analysis(application, page)
        assert not page.spectrum_plot._curves["Analysis"].isVisible()
        page.cleanup_filters["narrow_reject"].setChecked(True)
        wait_for_analysis(application, page)
        artifacts = Path("artifacts/spectrum-cleanup-workflow")
        artifacts.mkdir(parents=True, exist_ok=True)
        QTest.qWait(50)
        application.processEvents()
        for checkbox in page.cleanup_filters.values():
            assert checkbox.isVisible()
            assert checkbox.width() >= checkbox.sizeHint().width()
        assert page.grab().save(str(artifacts / f"processed-{theme}-{size[0]}.png"))
        assert page._latest_trace is raw and page._reference_trace is reference
        page._controller.call.assert_not_called()
    finally:
        apply_application_theme(application, "light")


def test_processed_stationary_rejection_receives_processed_history(application, page):
    x = np.linspace(200e6, 1200e6, 201)
    values = np.full(x.size, -80.)
    values[50] = -60.
    raw = trace(values)
    reference = trace(np.full(x.size, -80.))
    page._reference_trace = reference
    page.reference_operation.setCurrentIndex(page.reference_operation.findData("difference_db"))
    page._show_trace(raw, update_controls=False)
    page.analysis_source.setCurrentIndex(page.analysis_source.findData("processed"))
    page._spectrogram_buffer.clear()
    for index in range(6):
        page._spectrogram_buffer.append(raw, now=float(index))
    page.cleanup_filters["emi_reject"].setChecked(True)
    cleanup = wait_for_analysis(application, page)
    assert cleanup.unit == "dB"
    assert 50 in cleanup.stationary_interference_indices
    assert abs(cleanup.values[50]) < .1


def test_single_frame_stationary_rejection_explains_unchanged_spectrum(application, page):
    page._show_trace(trace([-80.] * 50 + [-60.] + [-80.] * 50), update_controls=False)
    page.cleanup_filters["emi_reject"].setChecked(True)
    cleanup = wait_for_analysis(application, page)
    assert not cleanup.stationary_interference_indices
    assert "history" in page.analysis_status.text().lower()
    assert "5" in page.analysis_status.text()


def test_hidden_reference_cannot_be_selected_for_relative_display(application, page):
    page._reference_trace = trace([-80.] * 101)
    page.reference_operation.setCurrentIndex(page.reference_operation.findData("difference_db"))
    page._show_trace(trace([-79.] * 101), update_controls=False)
    page.cleanup_filters["denoise"].setChecked(True)
    wait_for_analysis(application, page)
    assert page.analysis_source.findData("reference") == -1
    assert page._analysis_source_key == "processed"
    assert page._cleanup_result.unit == "dB"
    assert page.spectrum_plot._curves["Analysis"].isVisible()


def test_live_keeps_completed_status_while_next_frame_is_pending(application, page):
    raw = trace([-80. + .3 * (-1) ** index for index in range(101)])
    page._show_trace(raw, update_controls=False)
    page.cleanup_filters["denoise"].setChecked(True)
    wait_for_analysis(application, page)
    completed_status = page.analysis_status.text()
    with patch.object(page._analysis_controller, "submit"):
        page._show_trace(raw, update_controls=False)
        assert page.analysis_status.text() == completed_status
        assert page.spectrum_plot._curves["Analysis"].isVisible()


@pytest.mark.parametrize("relative", [False, True])
def test_checkbox_filters_compose_and_restore_source_without_analyze_now(application, page, relative):
    x = np.linspace(200e6, 1200e6, 2001)
    rng = np.random.default_rng(42)
    signal = rng.normal(0, .08, x.size)
    signal += 2 * np.exp(-4 * np.log(2) * ((x - 700e6) / 80e6) ** 2)
    signal[500] += 5
    raw = trace(-80 + signal)
    if relative:
        page._reference_trace = trace(np.full(x.size, -80.))
        page.reference_operation.setCurrentIndex(page.reference_operation.findData("difference_db"))
    page._show_trace(raw, update_controls=False)
    page.cleanup_filters["narrow_reject"].setChecked(True)
    first = wait_for_analysis(application, page)
    page.cleanup_filters["denoise"].setChecked(True)
    combined = wait_for_analysis(application, page)
    assert page.cleanup_filters["narrow_reject"].isChecked()
    assert combined.unit == ("dB" if relative else "dBm")
    assert abs(combined.values[500] - (-80 if not relative else 0)) < .3
    assert np.std(combined.values[:350]) < np.std(first.values[:350])
    assert abs(combined.values[1000] - (2 - (80 if not relative else 0))) < .2
    assert "Narrow" in combined.method and "denoise" in combined.method
    page.cleanup_filters["emi_reject"].setChecked(True)
    third = wait_for_analysis(application, page)
    assert "history" in page.analysis_status.text().lower()
    assert "stationary" in third.method
    page.cleanup_filters["denoise"].setChecked(False)
    page.cleanup_filters["emi_reject"].setChecked(False)
    restored = wait_for_analysis(application, page)
    np.testing.assert_allclose(restored.values, first.values)
    page.cleanup_filters["narrow_reject"].setChecked(False)
    wait_for_analysis(application, page)
    key = "Processed" if relative else "Raw"
    assert page.spectrum_plot._curves[key].isVisible()
    assert not page.spectrum_plot._curves["Analysis"].isVisible()
    assert page._latest_trace is raw
    np.testing.assert_array_equal(raw.powers_dbm, -80 + signal)
    page._controller.call.assert_not_called()


def test_live_pipeline_applies_results_when_worker_is_slower_than_frames(application, page):
    from app.devices.anritsu_ms2830a.ui.analysis_worker import clean_spectrum_pipeline

    raw = trace([-80. + .3 * (-1) ** index for index in range(201)])
    page._show_trace(raw, update_controls=False)
    page.cleanup_filters["denoise"].setChecked(True)
    wait_for_analysis(application, page)
    page._invalidate_analysis_results()
    frames = []
    applied = []

    def slow_pipeline(*args, **kwargs):
        time.sleep(.08)
        return clean_spectrum_pipeline(*args, **kwargs)

    def next_frame():
        page._show_trace(raw, update_controls=False)
        frames.append(page._display_revision)
        if page._cleanup_result is not None:
            applied.append(page._applied_analysis_generation)

    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(next_frame)
    try:
        with patch("app.devices.anritsu_ms2830a.ui.analysis_worker.clean_spectrum_pipeline", side_effect=slow_pipeline):
            timer.start()
            deadline = time.monotonic() + .6
            while time.monotonic() < deadline:
                application.processEvents()
                QTest.qWait(5)
            timer.stop()
            wait_for_analysis(application, page)
        assert len(frames) >= 10
        assert len(set(applied)) >= 2
        assert page.spectrum_plot._curves["Analysis"].isVisible()
        assert "Analyzing" not in page.analysis_status.text()
    finally:
        timer.stop()


def test_live_reports_worker_failure_even_when_newer_frame_is_queued(application, page):
    raw = trace([-80.] * 101)
    page._show_trace(raw, update_controls=False)
    page.cleanup_filters["denoise"].setChecked(True)
    wait_for_analysis(application, page)
    with patch.object(page._analysis_controller, "submit"):
        page._show_trace(raw, update_controls=False)
        generation = page._analysis_generation
        page._show_trace(raw, update_controls=False)
        page._analysis_failed(generation, "invalid source values")
        assert "invalid source values" in page.analysis_status.text()
        page._show_trace(raw, update_controls=False)
        assert "invalid source values" in page.analysis_status.text()


def test_frequency_grid_change_rejects_same_length_old_cleanup(application, page):
    from app.devices.anritsu_ms2830a.ui.analysis_worker import SpectrumAnalysisOutcome

    raw = trace([-80.] * 101)
    page._show_trace(raw, update_controls=False)
    page.cleanup_filters["denoise"].setChecked(True)
    cleanup = wait_for_analysis(application, page)
    outcome = SpectrumAnalysisOutcome(
        generation=page._analysis_generation, cleanup=cleanup, peaks=(),
        frequencies_hz=raw.frequencies_hz,
    )
    changed = SpectrumTrace(tuple(f + 1e9 for f in raw.frequencies_hz), raw.powers_dbm,
                            raw.acquired_at_utc, raw.trace_name)
    with patch.object(page._analysis_controller, "submit"):
        page._show_trace(changed, update_controls=False)
        page._analysis_completed(outcome)
        assert page._cleanup_result is None
        assert not page.spectrum_plot._curves["Analysis"].isVisible()


@pytest.mark.parametrize("relative", [False, True])
def test_parameters_dialog_recomputes_filters_and_remains_operable(application, page, relative):
    rng = np.random.default_rng(13)
    raw = trace(-80 + rng.normal(0, .25, 1001))
    if relative:
        page._reference_trace = trace(np.full(1001, -80.))
        page.reference_operation.setCurrentIndex(page.reference_operation.findData("difference_db"))
    page.resize(1600, 1000)
    page.show()
    application.processEvents()
    page._show_trace(raw, update_controls=False)
    page.cleanup_filters["denoise"].setChecked(True)
    before = wait_for_analysis(application, page)
    QTest.mouseClick(page.configure_analysis, Qt.MouseButton.LeftButton)
    application.processEvents()
    dialog = page._analysis_settings_dialog
    assert dialog is not None and dialog.isVisible()
    dialog.denoise_window.setValue(31)
    QTest.mouseClick(dialog.apply_button, Qt.MouseButton.LeftButton)
    application.processEvents()
    assert page._analysis_settings_dialog is None
    after = wait_for_analysis(application, page)
    assert page._analysis_parameters.denoise_window == 31
    assert after.values != before.values
    assert page._analysis_controller._thread.isRunning()
    assert page.isEnabled()
    page.cleanup_filters["narrow_reject"].setChecked(True)
    wait_for_analysis(application, page)
    page._show_trace(trace(-79 + rng.normal(0, .25, 1001)), update_controls=False)
    latest = wait_for_analysis(application, page)
    assert latest.values != after.values
    QTest.mouseClick(page.configure_analysis, Qt.MouseButton.LeftButton)
    application.processEvents()
    second = page._analysis_settings_dialog
    assert second is not None and second.isVisible()
    assert second.denoise_window.value() == 31
    second.cancel_button.click()
    application.processEvents()
    assert page.isEnabled()
    page._controller.call.assert_not_called()


def test_unsupported_narrow_width_after_apply_does_not_stop_other_filters(application, page):
    rng = np.random.default_rng(43)
    raw = trace(-80 + rng.normal(0, .25, 1001))
    page.resize(1600, 1000)
    page.show()
    application.processEvents()
    page._show_trace(raw, update_controls=False)
    page.cleanup_filters["narrow_reject"].setChecked(True)
    page.cleanup_filters["denoise"].setChecked(True)
    wait_for_analysis(application, page)
    QTest.mouseClick(page.configure_analysis, Qt.MouseButton.LeftButton)
    application.processEvents()
    dialog = page._analysis_settings_dialog
    assert dialog is not None and dialog.isVisible()
    dialog.narrow_width.setText("100 MHz")
    dialog.denoise_window.setValue(31)
    QTest.mouseClick(dialog.apply_button, Qt.MouseButton.LeftButton)
    application.processEvents()
    cleanup = wait_for_analysis(application, page)
    assert "denoise" in cleanup.method
    assert "Narrow-peak" in " ".join(cleanup.notes)
    assert "unavailable" in page.analysis_status.text().lower()
    assert "32 MHz" in page.analysis_status.text()
    assert cleanup.values != raw.powers_dbm
    assert page.spectrum_plot._curves["Analysis"].isVisible()
    assert page._analysis_controller._thread.isRunning()
    QTest.qWait(50)
    application.processEvents()
    artifacts = Path("artifacts/spectrum-cleanup-workflow")
    artifacts.mkdir(parents=True, exist_ok=True)
    assert page.grab().save(str(artifacts / "parameters-filter-error.png"))
    latest = trace(-79 + rng.normal(0, .25, 1001))
    page._show_trace(latest, update_controls=False)
    next_cleanup = wait_for_analysis(application, page)
    assert next_cleanup.values != cleanup.values
    page.cleanup_filters["narrow_reject"].setChecked(False)
    recovered = wait_for_analysis(application, page)
    assert not recovered.notes
    page._controller.call.assert_not_called()


def test_live_parameter_apply_supersedes_busy_worker_without_stalling(application, page):
    from app.devices.anritsu_ms2830a.ui.analysis_worker import clean_spectrum_pipeline

    rng = np.random.default_rng(14)
    raw = trace(-80 + rng.normal(0, .25, 1001))
    page.resize(1600, 1000)
    page.show()
    page._show_trace(raw, update_controls=False)
    page.cleanup_filters["denoise"].setChecked(True)
    before = wait_for_analysis(application, page)
    page._open_analysis_settings()
    application.processEvents()
    dialog = page._analysis_settings_dialog
    dialog.denoise_window.setValue(31)
    started = []
    frames = []

    def slow_pipeline(*args, **kwargs):
        started.append(kwargs["parameters"].denoise_window)
        time.sleep(.08)
        return clean_spectrum_pipeline(*args, **kwargs)

    def next_frame():
        frames.append(page._display_revision)
        page._show_trace(raw, update_controls=False)

    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(next_frame)
    try:
        with patch("app.devices.anritsu_ms2830a.ui.analysis_worker.clean_spectrum_pipeline", side_effect=slow_pipeline):
            page._show_trace(raw, update_controls=False)
            timer.start()
            QTest.mouseClick(dialog.apply_button, Qt.MouseButton.LeftButton)
            deadline = time.monotonic() + .5
            while time.monotonic() < deadline:
                application.processEvents()
                QTest.qWait(5)
            timer.stop()
            after = wait_for_analysis(application, page)
        assert len(frames) >= 10
        assert 31 in started
        assert page._analysis_parameters.denoise_window == 31
        assert after.values != before.values
        assert page.isEnabled()
        assert page.spectrum_plot._curves["Analysis"].isVisible()
        assert "Analyzing" not in page.analysis_status.text()
        page._controller.call.assert_not_called()
    finally:
        timer.stop()
