"""Shared acquisition, correction, DSP and plot controls in the Fluent page."""

from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest

from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import SpectrumAnalysisSettingsDialog
from app.devices.anritsu_ms2830a.ui.page import AnritsuPageState
from app.devices.anritsu_ms2830a.ui.spectrum_controls import parse_plot_amplitude
from app.spectrum import (
    SpectrumAnalysisParameters,
    apply_reference_operation,
    clean_spectrum_pipeline,
)
from app.ui.design_system import apply_application_theme
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_shared_background_filter import shared_page as shared_page  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.mark.parametrize("action,dispatch", [("single", "single_sweep"), ("live", "start_live")])
@pytest.mark.parametrize("correction", ["raw", "background", "reference"])
def test_correction_never_changes_the_acquisition_action(shared_page, action, dispatch, correction):
    app, page, _, _, trace, _, _, _ = shared_page
    if correction == "reference":
        reference = replace(trace, powers_dbm=tuple(np.asarray(trace.powers_dbm)-10))
        page._set_reference(page._build_reference(reference, kind="single", count=1))
        page.correction_controls.reference.setChecked(True)
    elif correction == "background":
        page.cleanup_filters["background"].setChecked(True)
        wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "W")
    page._set_page_state(AnritsuPageState.IDLE)
    assert page.single.text() == "Acquire once" and page.live.text() == "Start Live"
    with patch.object(page._controller, "call") as request:
        getattr(page, action).click()
        assert request.call_args.args[0] == dispatch
    assert not page.correction_workspace.running


@pytest.mark.parametrize("operation,unit", [
    ("difference_db", "dB"), ("ratio_linear", "ratio"), ("add_power", "dBm"),
    ("subtract_power", "dBm"), ("multiply_linear", "mW²"),
])
def test_reference_operation_and_filters_are_shared_with_spectrogram(shared_page, operation, unit):
    app, page, _, _, trace, _, requests, _ = shared_page
    noise = np.random.default_rng(41).normal(0, .01, len(trace.powers_dbm))
    reference = replace(trace, powers_dbm=tuple(-75 + noise))
    raw = replace(trace, powers_dbm=tuple(-60 + 2*noise))
    page._set_reference(page._build_reference(reference, kind="single", count=1))
    page.reference_operation.setCurrentIndex(page.reference_operation.findData(operation))
    page._show_trace(raw, update_controls=False)
    page.cleanup_filters["denoise"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None and "denoise" in page._cleanup_result.method)
    source, expected_unit = apply_reference_operation(raw.powers_dbm, reference.powers_dbm, operation)
    expected = clean_spectrum_pipeline(source, unit=expected_unit, modes=("denoise",),
        frequencies_hz=raw.frequencies_hz, parameters=page._analysis_parameters)
    np.testing.assert_array_equal(page._cleanup_result.values, expected.values)
    assert page._cleanup_result.unit == unit and page._active_spectrum_unit == unit
    page._spectrogram_buffer.clear()
    page._spectrogram_buffer.append(raw, now=10)
    page._spectrogram_buffer.append(raw, now=11)
    page.analysis_tabs.setCurrentIndex(1)
    wait_until(app, lambda: page._spectrogram_filter_outcome is not None)
    outcome = page._spectrogram_filter_outcome
    assert outcome.unit == unit and f"Reference {operation}" in outcome.method
    np.testing.assert_allclose(outcome.matrix[0], expected.values, rtol=2e-5, atol=1e-5 if unit in {"dB", "dBm"} else 1e-18)
    assert not requests
    assert page._spectrogram_analysis_controller._worker.thread() is not app.thread()


def test_corrections_are_exclusive_and_reference_operation_is_remembered(shared_page):
    app, page, _, _, trace, _, _, _ = shared_page
    reference = replace(trace, powers_dbm=tuple(np.asarray(trace.powers_dbm)-10))
    page._set_reference(page._build_reference(reference, kind="single", count=1))
    page.reference_operation.setCurrentIndex(page.reference_operation.findData("multiply_linear"))
    assert page.correction_controls.reference.isChecked()
    page.cleanup_filters["background"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "W")
    assert not page.correction_controls.reference.isChecked()
    assert page.reference_operation.currentData() == "none"
    page.correction_controls.reference.setChecked(True)
    assert not page.cleanup_filters["background"].isChecked()
    assert page.reference_operation.currentData() == "multiply_linear"
    wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "mW²")


def test_reference_averaging_uses_its_own_count_and_preserves_acquisition_metadata(shared_page):
    app, page, _, _, trace, _, _, _ = shared_page
    page.average_count.setValue(7)
    page.reference_average_count.setValue(2)
    page._set_page_state(AnritsuPageState.IDLE)
    second = replace(trace, powers_dbm=tuple(np.asarray(trace.powers_dbm) + 10),
                     acquired_at_utc=datetime.now(UTC))
    with patch.object(page._controller, "call") as request:
        page.start_reference_averaging()
        page._result("single_sweep", trace)
        assert page._reference_trace is None and page.average_progress.value() == 1
        app.processEvents()
        page._result("single_sweep", second)
        assert [call.args[0] for call in request.call_args_list] == ["single_sweep", "single_sweep"]
    reference = page._reference_trace
    assert reference is not None and "REFAVG2" in reference.trace_name
    assert reference.configuration_generation == trace.configuration_generation == 7
    assert reference.frequencies_hz == trace.frequencies_hz
    expected = 10 * np.log10((10 ** (np.asarray(trace.powers_dbm) / 10)
                             + 10 ** (np.asarray(second.powers_dbm) / 10)) / 2)
    np.testing.assert_allclose(reference.powers_dbm, expected)
    assert not page._averaging_active and page._page_state == AnritsuPageState.IDLE


@pytest.mark.parametrize("correction", ["background", "reference"])
def test_raw_spectrogram_preview_bypasses_the_selected_correction(shared_page, correction):
    app, page, _, _, trace, _, _, _ = shared_page
    if correction == "background":
        page.cleanup_filters["background"].setChecked(True)
    else:
        reference = replace(trace, powers_dbm=tuple(np.asarray(trace.powers_dbm) - 10))
        page._set_reference(page._build_reference(reference, kind="single", count=1))
        page.correction_controls.reference.setChecked(True)
    page.spectrogram_source.setCurrentIndex(page.spectrogram_source.findData("raw"))
    page.analysis_tabs.setCurrentIndex(1)
    wait_until(app, lambda: page._spectrogram_matrix(source="raw", window_s=30) is not None)
    _, _, matrix, unit, _ = page._spectrogram_matrix(source="raw", window_s=30)
    assert unit == "dBm"
    np.testing.assert_allclose(matrix[-1], trace.powers_dbm, atol=1e-5)


def test_reference_setup_is_available_after_live_starts_and_current_capture_sends_no_query(shared_page):
    _, page, _, _, trace, _, requests, _ = shared_page
    page._set_page_state(AnritsuPageState.LIVE)
    page.correction_controls.reference.setChecked(True)
    assert page.reference_dialog.isVisible()
    assert page.use_current_reference.isEnabled() and page.load_reference.isEnabled()
    assert not page.acquire_single_reference.isEnabled()
    page.use_current_reference.click()
    assert page._reference_trace is trace and page.correction_controls.reference.isChecked()
    assert not requests


def test_plot_scales_keep_manual_limits_across_frames_and_reset_y_on_unit_change(shared_page):
    app, page, _, _, trace, _, _, _ = shared_page
    page._open_plot_scales()
    dialog = page._scale_dialog
    dialog.editors["x"][0].setText("0.4 GHz")
    dialog.editors["x"][1].setText("700 MHz")
    dialog.apply_buttons["x"].click()
    np.testing.assert_allclose(page.spectrum_plot.plot.viewRange()[0], (400e6, 700e6))
    dialog.editors["y"][0].setText("-80 dBm")
    dialog.editors["y"][1].setText("-30 dBm")
    dialog.apply_buttons["y"].click()
    page._show_trace(replace(trace, acquired_at_utc=datetime.now(UTC)), update_controls=False)
    assert page.spectrum_ranges.fixed_ranges["y"] == (-80., -30.)
    dialog.editors["x"][0].setText("800 MHz")
    dialog.apply_buttons["x"].click()
    assert "Minimum must be smaller" in dialog.feedback.text()
    assert page.spectrum_ranges.fixed_ranges["x"] == (400e6, 700e6)
    page.cleanup_filters["background"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "W")
    assert "y" not in page.spectrum_ranges.fixed_ranges and "x" in page.spectrum_ranges.fixed_ranges
    assert dialog.y_label.text() == "Amplitude [W]"
    dialog.editors["y"][0].setText("-20 pW")
    dialog.editors["y"][1].setText("100 pW")
    dialog.apply_buttons["y"].click()
    np.testing.assert_allclose(page.spectrum_plot.plot.viewRange()[1], (-20e-12, 100e-12), atol=1e-24)
    dialog.fit_button.click()
    assert not page.spectrum_ranges.fixed_ranges
    dialog.close()


@pytest.mark.parametrize("text,unit,value", [
    ("-2 pW", "W", -2e-12), ("-70 dBm", "dBm", -70.),
    ("1e-9 mW²", "mW²", 1e-9), ("0.5 ratio", "ratio", .5),
])
def test_plot_limits_are_parsed_in_the_displayed_unit(text, unit, value):
    assert parse_plot_amplitude(text, unit) == value
    with pytest.raises(ValueError):
        parse_plot_amplitude("1 GHz", unit)


def test_background_export_keeps_the_displayed_raw_frame_while_a_new_frame_is_pending(shared_page):
    app, page, _, _, trace, _, _, _ = shared_page
    page.cleanup_filters["background"].setChecked(True)
    wait_until(app, lambda: page._analysis_raw_snapshot is trace)
    completed = page._cleanup_result
    next_trace = replace(trace, powers_dbm=tuple(np.asarray(trace.powers_dbm) + 1),
                         acquired_at_utc=datetime.now(UTC))
    with patch.object(page._analysis_controller, "submit"):
        page._show_trace(next_trace, update_controls=False)
        assert page._latest_trace is next_trace
        canonical, values, unit, _ = page._manual_trace_payload("analysis:raw")
        assert canonical is trace and unit == "W" and values == completed.values
        assert page._display_state.by_key["analysis:raw"].frame_id == page._analysis_source_snapshot.frame_id


def test_power_subtraction_preserves_undefined_bins_without_interpolating_across_them(shared_page):
    app, page, _, _, trace, _, _, _ = shared_page
    raw = replace(trace, powers_dbm=tuple(np.full(len(trace.powers_dbm), -60.)))
    reference_values = np.full(len(trace.powers_dbm), -70.)
    reference_values[200:250] = -50.
    reference = replace(trace, powers_dbm=tuple(reference_values))
    page._set_reference(page._build_reference(reference, kind="single", count=1))
    page.reference_operation.setCurrentIndex(page.reference_operation.findData("subtract_power"))
    page.cleanup_filters["denoise"].setChecked(True)
    page._show_trace(raw, update_controls=False)
    wait_until(app, lambda: page._analysis_raw_snapshot is raw)
    result = np.asarray(page._cleanup_result.values)
    assert np.all(np.isnan(result[200:250]))
    assert np.all(np.isfinite(np.r_[result[:200], result[250:]]))
    assert any("undefined in dBm" in note for note in page._cleanup_result.notes)
    page.analysis_tabs.setCurrentIndex(1)
    wait_until(app, lambda: page._spectrogram_filter_outcome is not None)
    np.testing.assert_array_equal(np.isnan(page._spectrogram_filter_outcome.matrix[-1]), np.isnan(result))
    assert page._latest_trace is raw and page._reference_trace is reference


def test_manual_peak_detection_with_auto_off_is_limited_to_its_own_frame(shared_page):
    app, page, _, _, trace, _, _, _ = shared_page
    page.cleanup_filters["background"].setChecked(True)
    wait_until(app, lambda: page._analysis_raw_snapshot is trace)
    assert not page.auto_peak_detection.isChecked() and not page._detected_peaks
    page._analyze_current_spectrum(force=True)
    wait_until(app, lambda: bool(page._detected_peaks))
    next_trace = replace(trace, acquired_at_utc=datetime.now(UTC))
    page._show_trace(next_trace, update_controls=False)
    wait_until(app, lambda: page._analysis_raw_snapshot is next_trace)
    assert not page._detected_peaks and len(page.spectrum_plot.peak_markers.points()) == 0


@pytest.mark.parametrize("section", ["peaks", "filters"])
def test_scoped_reset_preserves_parameters_from_the_other_settings_block(shared_page, section):
    _, page, _, _, _, _, _, _ = shared_page
    initial = replace(SpectrumAnalysisParameters(), peak_max_count=7, peak_min_width_hz=12e6,
                      narrow_max_width_hz=3e6, narrow_threshold_sigma=11,
                      narrow_protected_regions_hz=((450e6, 460e6),))
    dialog = SpectrumAnalysisSettingsDialog(page, current_parameters=initial, section=section)
    dialog.reset_to_defaults()
    actual = dialog.get_parameters()
    for field in fields(initial):
        if field.name.startswith("peak_") != (section == "peaks"):
            assert getattr(actual, field.name) == getattr(initial, field.name)
    assert (actual.peak_max_count if section == "peaks" else actual.narrow_threshold_sigma) == (
        SpectrumAnalysisParameters().peak_max_count if section == "peaks" else SpectrumAnalysisParameters().narrow_threshold_sigma)
    dialog.close()


@pytest.mark.parametrize("theme,size", [("light", (1450, 900)), ("dark", (950, 720))])
def test_correction_filter_and_plot_settings_are_separate_rendered_blocks(shared_page, theme, size):
    app, page, _, _, _, _, _, _ = shared_page
    apply_application_theme(app, theme)
    page.resize(*size)
    page.show()
    QTest.qWait(80)
    blocks = (page.correction_controls, page.signal_analysis_card, page.presentation_controls)
    for index, block in enumerate(blocks):
        assert block.isVisibleTo(page)
        rect = block.rect().translated(block.mapTo(page, QPoint()))
        assert page.rect().contains(rect)
        assert all(not rect.intersects(other.rect().translated(other.mapTo(page, QPoint()))) for other in blocks[index+1:])
    assert page.analysis_tabs.count() == 2
    assert not hasattr(page, "current_spectrum_view")
    assert not hasattr(page, "background_current_plot")
    assert not hasattr(page, "background_filters")
    page.peak_settings.click()
    assert page._analysis_settings_dialog.section == "peaks"
    assert page._analysis_settings_dialog.peak_card.isVisible()
    directory = Path("artifacts/spectrum-unified")
    directory.mkdir(parents=True, exist_ok=True)
    QTest.qWait(60)
    assert page._analysis_settings_dialog.grab().save(str(directory / f"peaks-{theme}.png"))
    page._analysis_settings_dialog.close()
    page.configure_analysis.click()
    assert page._analysis_settings_dialog.section == "filters"
    assert not page._analysis_settings_dialog.peak_card.isVisible()
    page._analysis_settings_dialog.close()
    page._open_reference_setup()
    QTest.qWait(60)
    assert page.reference_status.isVisibleTo(page.reference_dialog)
    assert page.reference_dialog.grab().save(str(directory / f"reference-{theme}.png"))
    page.reference_dialog.close()
    page._open_recording_setup()
    QTest.qWait(60)
    assert page.manual_save_card.isVisibleTo(page.recording_dialog)
    assert page.recording_dialog.grab().save(str(directory / f"recording-{theme}.png"))
    page.recording_dialog.close()
    page._open_plot_scales()
    QTest.qWait(60)
    for button in (*page._scale_dialog.apply_buttons.values(), *page._scale_dialog.auto_buttons.values()):
        assert button.parentWidget().rect().contains(button.geometry())
        assert button.height() >= 30
    assert page._scale_dialog.grab().save(str(directory / f"scales-{theme}.png"))
    page._scale_dialog.close()
    QTest.qWait(60)
    assert page.grab().save(str(directory / f"page-{theme}-{size[0]}.png"))


@pytest.mark.parametrize("size", [(1500, 900), (820, 560)])
def test_configure_reference_mouse_click_opens_visible_dialog(shared_page, size):
    from PySide6.QtCore import Qt
    app, page, _, _, _, _, requests, _ = shared_page
    page.resize(*size)
    page.show()
    QTest.qWait(80)
    if page.compact_plot_settings.isVisible():
        QTest.mouseClick(page.compact_plot_settings, Qt.MouseButton.LeftButton)
        QTest.qWait(50)
    button = page.correction_controls.configure_reference
    assert button.isVisible() and button.isEnabled()
    requests.clear()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QTest.qWait(80)
    assert page.reference_dialog.isVisible()
    assert not page.reference_dialog.isMinimized()
    assert page.reference_status.isVisibleTo(page.reference_dialog)
    assert page.load_reference.isVisibleTo(page.reference_dialog)
    assert page.reference_dialog.width() >= 420
    assert not requests
    page.reference_dialog.showMinimized()
    app.processEvents()
    assert page.reference_dialog.isMinimized()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QTest.qWait(80)
    assert not page.reference_dialog.isMinimized()
    page.reference_dialog.close()
