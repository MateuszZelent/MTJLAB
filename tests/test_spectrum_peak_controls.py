"""Peak geometry, units and rendered controls for recorded signed residuals."""

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPoint

from app.spectrum.analysis import SpectrumAnalysisParameters, detect_spectrum_peaks
from app.ui.design_system import apply_application_theme
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_background_display_filters import publish_residual
from tests.test_shared_background_filter import shared_page as shared_page  # noqa: PLC0414
from tests.test_background_display_filters import corrected_page as corrected_page  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


def signed_peaks():
    x = np.linspace(100e6, 1000e6, 4501)
    y = np.random.default_rng(42).normal(0, 1e-14, x.size)
    for center, width, amplitude in ((300e6, 20e6, 5e-12), (450e6, 40e6, 3e-12), (700e6, 60e6, -4e-12)):
        y += amplitude * np.exp(-4 * np.log(2) * ((x - center) / width) ** 2)
    return x, y


def test_signed_peak_geometry_count_polarity_and_scale_invariant_noise_thresholds():
    x, y = signed_peaks()
    params = SpectrumAnalysisParameters(peak_polarity="both", peak_min_width_hz=10e6, peak_max_width_hz=80e6)
    peaks = detect_spectrum_peaks(x, y, unit="W", parameters=params)
    assert len(peaks) == 3
    assert sorted(p.frequency_hz for p in peaks) == pytest.approx([300e6, 450e6, 700e6], abs=1e6)
    assert sorted(p.fwhm_hz for p in peaks) == pytest.approx([20e6, 40e6, 60e6], rel=.04)
    assert all(p.amplitude_unit == "W" and p.contrast_unit == "σ" for p in peaks)
    scaled = detect_spectrum_peaks(x, y * 1e12, unit="ratio", parameters=params)
    assert [p.snr_db for p in scaled] == pytest.approx([p.snr_db for p in peaks], rel=1e-10)
    descending = detect_spectrum_peaks(x[::-1], y[::-1], unit="W", parameters=params)
    assert [p.index for p in descending] == [len(x) - 1 - p.index for p in peaks]
    assert len(detect_spectrum_peaks(x, y, unit="W", parameters=replace(params, peak_max_count=1))) == 1
    negative = detect_spectrum_peaks(x, y, unit="W", parameters=replace(params, peak_polarity="negative"))
    assert len(negative) == 1 and negative[0].amplitude_dbm < 0
    assert len(detect_spectrum_peaks(x, y, unit="W", parameters=replace(params, peak_max_width_hz=30e6))) == 1
    assert len(detect_spectrum_peaks(x, y, unit="W", parameters=replace(params, peak_min_distance_hz=500e6))) == 1
    assert not detect_spectrum_peaks(x, y, unit="W", parameters=replace(params, peak_min_snr_sigma=1e6))
    assert not detect_spectrum_peaks(x, y, unit="W", parameters=replace(params, peak_min_prominence_sigma=1e6))


def test_background_auto_peak_toggle_and_replacement_markers_are_independent(corrected_page):
    application, page, _context, source, controller = corrected_page
    original = source.values_w.copy()
    page._analysis_parameters_applied(replace(page._analysis_parameters, peak_min_width_hz=20e6,
                                            peak_max_width_hz=150e6))
    page.auto_peak_detection.setChecked(True)
    wait_until(application, lambda: bool(page._detected_peaks))
    assert page._detected_peaks[0].frequency_hz == pytest.approx(850e6, abs=3e6)
    assert len(page.spectrum_plot.peak_markers.points()) > 0
    assert len(page.spectrum_plot.replacement_markers.points()) == 0
    page.highlight_peaks.setChecked(False)
    assert len(page.spectrum_plot.peak_markers.points()) == 0
    assert page._detected_peaks
    page.highlight_peaks.setChecked(True)
    page.cleanup_filters["narrow_reject"].setChecked(True)
    wait_until(application, lambda: page._cleanup_result is not None)
    page.highlight_replacements.setChecked(True)
    assert len(page.spectrum_plot.peak_markers.points()) > 0
    assert len(page.spectrum_plot.replacement_markers.points()) > 0
    page.peak_count.setValue(1)
    wait_until(application, lambda: bool(page._detected_peaks))
    assert page._analysis_parameters.peak_max_count == 1 and len(page._detected_peaks) == 1
    page.open_peak_table.click()
    dialog = page._peak_table_dialog
    assert dialog is not None and dialog.table.rowCount() == 1
    assert dialog.table.item(0, 3).text().endswith("σ")
    assert dialog.table.item(0, 2).text().endswith("W")
    page.auto_peak_detection.setChecked(False)
    assert not page._detected_peaks and len(page.spectrum_plot.peak_markers.points()) == 0
    wait_until(application, lambda: page._cleanup_result is not None)
    assert len(page.spectrum_plot.replacement_markers.points()) > 0
    assert np.array_equal(source.values_w, original)
    controller.call.assert_not_called()


def test_background_markers_follow_analyzed_snapshot_and_ignore_result_after_disabling(corrected_page):
    application, page, context, source, _controller = corrected_page
    outcomes = []
    page._analysis_controller.result.connect(outcomes.append)
    page.auto_peak_detection.setChecked(True)
    wait_until(application, lambda: bool(outcomes))
    latest = replace(source, frame_id=11, completed_at_s=source.completed_at_s + 1,
                     values_w=np.roll(source.values_w, 40))
    raw = publish_residual(page, context, latest)
    wait_until(application, lambda: page._analysis_raw_snapshot is raw)
    assert page._detected_peaks
    for peak in page._detected_peaks:
        assert peak.amplitude_dbm == pytest.approx(latest.values_w[peak.index], abs=1e-23)
    np.testing.assert_allclose(page._cleanup_result.input_values, latest.values_w, atol=1e-23)
    old_outcome = outcomes[-1]
    page.auto_peak_detection.setChecked(False)
    page._analysis_completed(old_outcome)
    assert not page._detected_peaks and len(page.spectrum_plot.peak_markers.points()) == 0


@pytest.mark.parametrize("theme,size", [("light", (1450, 900)), ("dark", (950, 720))])
def test_peak_controls_and_settings_render_without_overlap(corrected_page, theme, size):
    application, page, _context, _source, _controller = corrected_page
    apply_application_theme(application, theme)
    page.resize(*size)
    page.show()
    application.processEvents()
    controls = (page.auto_peak_detection, page.highlight_peaks, 
                page.peak_settings, page.open_peak_table)
    rectangles = []
    for control in controls:
        assert control.isVisibleTo(page)
        rectangle = control.rect().translated(control.mapTo(page.presentation_controls, QPoint(0, 0)))
        assert page.presentation_controls.rect().contains(rectangle)
        assert all(not rectangle.intersects(other) for other in rectangles)
        rectangles.append(rectangle)
    assert page.spectrum_plot.height() >= 100
    assert not page.presentation_controls.rect().translated(page.presentation_controls.mapTo(page, QPoint())).intersects(
        page.spectrum_plot.rect().translated(page.spectrum_plot.mapTo(page, QPoint())))
    artifacts = Path("artifacts/spectrum-peak-controls")
    artifacts.mkdir(parents=True, exist_ok=True)
    assert page.grab().save(str(artifacts / f"controls-{theme}-{size[0]}.png"))
    page.peak_settings.click()
    application.processEvents()
    dialog = page._analysis_settings_dialog
    assert dialog is not None and dialog.peak_polarity.isEnabled() and not dialog.peak_snr.isEnabled()
    dialog.resize(720 if theme == "light" else 580, 740)
    dialog.focus_peaks()
    application.processEvents()
    assert dialog.peak_min_width.isVisibleTo(dialog) and dialog.apply_button.isVisibleTo(dialog)
    viewport = dialog.settings_scroll.viewport()
    assert viewport.rect().contains(dialog.peak_min_width.rect().translated(
        dialog.peak_min_width.mapTo(viewport, QPoint(0, 0))))
    applied = []
    dialog.parameters_applied.connect(applied.append)
    dialog.peak_min_width.setText("5 mV")
    dialog.apply_button.click()
    assert dialog.validation_error.isVisible() and not applied
    dialog.peak_min_width.setText("10 MHz")
    dialog.peak_max_width.setText("5 MHz")
    dialog.apply_button.click()
    assert "minimum" in dialog.validation_error.text() and not applied
    dialog.peak_max_width.setText("80 MHz")
    dialog.peak_distance.setText("15 MHz")
    dialog.peak_polarity.setCurrentIndex(dialog.peak_polarity.findData("both"))
    params = dialog.get_parameters()
    assert params.peak_min_width_hz == 10e6 and params.peak_max_width_hz == 80e6
    assert params.peak_min_distance_hz == 15e6 and params.peak_polarity == "both"
    assert dialog.grab().save(str(artifacts / f"settings-{theme}.png"))
    dialog.apply_button.click()
    assert applied == [params]


def test_logarithmic_peak_width_controls_filter_original_detector():
    from tests.test_spectrum_analysis import SpectrumAnalysisTests

    x, y = SpectrumAnalysisTests._gaussian_trace()
    params = SpectrumAnalysisParameters(peak_fit_models=False, peak_min_width_hz=300e3, peak_max_width_hz=500e3)
    assert len(detect_spectrum_peaks(x, y, parameters=params)) == 1
    assert not detect_spectrum_peaks(x, y, parameters=replace(params, peak_max_width_hz=350e3))
    assert not detect_spectrum_peaks(x, y, parameters=replace(params, peak_min_width_hz=450e3))
