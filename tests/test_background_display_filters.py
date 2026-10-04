"""The shared display pipeline filters signed residuals without changing raw data."""
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPoint

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.spectrum.analysis import bilateral_denoise_linear, clean_spectrum_pipeline
from app.ui.design_system import apply_application_theme
from tests.test_shared_background_filter import shared_page as shared_page  # noqa: PLC0414
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_correction_store import fixture_profile, signal_fixture


@pytest.fixture
def corrected_page(shared_page):
    application, page, context, _profile, trace, _, _, _ = shared_page
    page.cleanup_filters["background"].setChecked(True)
    wait_until(application, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "W")
    previous_context, previous_profile = fixture_profile()
    _, _, template = signal_fixture(previous_context, previous_profile)
    source = replace(template, context_id=context.context_id,
        values_w=np.asarray(page._cleanup_result.values), standard_uncertainty_w=None,
        frame_id=10, completed_at_s=trace.acquired_at_utc.timestamp())
    page.correction_workspace._latest_raw = trace
    page.correction_workspace._latest_result = source
    page._controller.call.reset_mock()
    return application, page, context, source, page._controller


def publish_residual(page, context, source):
    profile = page.correction_workspace._profile
    powers = tuple(10*np.log10(profile.mean_w + source.values_w)+30)
    raw = SpectrumTrace(tuple(context.frequencies_hz), powers,
        datetime.fromtimestamp(source.completed_at_s, UTC), "TRAC1",
        configuration_generation=context.configuration_generation)
    page.correction_workspace._latest_raw = raw
    page.correction_workspace._latest_result = source
    page._background_display_changed(context, source, "Recorded frame")
    return raw


def test_signed_residual_filters_remove_both_extrema_and_preserve_source(corrected_page):
    application, page, _context, source, controller = corrected_page
    original = source.values_w.copy()
    raw_original = page._latest_trace
    page.cleanup_filters["narrow_reject"].setChecked(True)
    wait_until(application, lambda: page._cleanup_result is not None)
    cleanup = page._cleanup_result
    assert cleanup.unit == "W" and {250, 400}.issubset(cleanup.removed_peak_indices)
    assert abs(cleanup.values[250]) < 1e-12 and abs(cleanup.values[400]) < 1e-12
    assert cleanup.values[650] == pytest.approx(source.values_w[650], rel=1e-3)
    np.testing.assert_array_equal(source.values_w, original)
    assert page._latest_trace is raw_original
    page.overlay_analysis_source.setChecked(True)
    assert page.spectrum_plot._curves["Corrected"].isVisible()
    assert page.spectrum_plot._curves["Analysis"].isVisible()
    np.testing.assert_array_equal(page.spectrum_plot._traces["Corrected"][1], original)
    assert page._active_spectrum_unit == "W"
    assert source is page.correction_workspace._latest_result
    controller.call.assert_not_called()


def test_new_publications_are_filtered_and_exported_from_their_own_snapshot(corrected_page):
    application, page, context, source, _ = corrected_page
    page.cleanup_filters["narrow_reject"].setChecked(True)
    wait_until(application, lambda: page._cleanup_result is not None)
    latest = replace(source, frame_id=11, completed_at_s=source.completed_at_s + 1,
                     values_w=source.values_w * 2)
    raw = publish_residual(page, context, latest)
    wait_until(application, lambda: page._analysis_raw_snapshot is raw)
    page.overlay_analysis_source.setChecked(True)
    np.testing.assert_allclose(page.spectrum_plot._traces["Corrected"][1], latest.values_w, atol=1e-23)
    canonical, values, unit, method = page._manual_trace_payload("analysis:raw")
    assert canonical is raw and unit == "W" and "background" in method.lower()
    assert values == page._cleanup_result.values
    page.cleanup_filters["narrow_reject"].setChecked(False)
    wait_until(application, lambda: page._cleanup_result is not None)
    np.testing.assert_allclose(page._cleanup_result.values, latest.values_w, atol=1e-23)


def test_parameter_protection_and_unavailable_grid_are_reported_without_corrupting_source(corrected_page):
    application, page, _context, source, _ = corrected_page
    page.cleanup_filters["narrow_reject"].setChecked(True)
    wait_until(application, lambda: page._cleanup_result is not None)
    page._analysis_parameters_applied(replace(page._analysis_parameters, narrow_protected_regions_hz=((440e6, 460e6),)))
    wait_until(application, lambda: page._cleanup_result is not None)
    assert 250 not in page._cleanup_result.removed_peak_indices and 400 in page._cleanup_result.removed_peak_indices
    page._analysis_parameters_applied(replace(page._analysis_parameters, narrow_max_width_hz=1e12))
    wait_until(application, lambda: page._cleanup_result is not None)
    assert "unavailable" in page.analysis_status.text()
    np.testing.assert_array_equal(page._cleanup_result.values, source.values_w)


def test_linear_denoise_is_scale_invariant_and_preserves_signed_edges():
    rng = np.random.default_rng(9)
    values = np.r_[np.full(100, -2.0), np.full(100, 3.0)] + rng.normal(0, .1, 200)
    normalized = np.asarray(bilateral_denoise_linear(values))
    watts = np.asarray(bilateral_denoise_linear(values * 1e-12))
    assert np.allclose(normalized, watts / 1e-12, rtol=1e-12, atol=1e-12)
    assert normalized[99] < -1.5 and normalized[100] > 2.5
    assert np.std(normalized[20:80]) < np.std(values[20:80])
    assert bilateral_denoise_linear([0.] * 20) == (0.,) * 20


def test_pipeline_keeps_power_unit_and_never_applies_db_emi_thresholds_to_watts():
    source = [-1e-12, 1e-12, -2e-12, 2e-12, 0.] * 10
    cleanup = clean_spectrum_pipeline(source, unit="W", modes=("denoise", "emi_reject"))
    assert cleanup.unit == "W"
    assert any("requires dB or dBm" in note for note in cleanup.notes)
    assert cleanup.noise_sigma_db < 1e-11


@pytest.mark.parametrize("theme,size", [("light", (1450, 900)), ("dark", (950, 720))])
def test_corrected_filter_controls_render_and_compare_without_overlap(corrected_page, theme, size):
    application, page, _context, _source, _ = corrected_page
    apply_application_theme(application, theme)
    page.resize(*size)
    page.show()
    page.cleanup_filters["narrow_reject"].setChecked(True)
    page.cleanup_filters["denoise"].setChecked(True)
    page.overlay_analysis_source.setChecked(True)
    wait_until(application, lambda: page._cleanup_result is not None)
    assert page.spectrum_plot.height() > 100
    assert not page.signal_analysis_card.rect().translated(page.signal_analysis_card.mapTo(page, QPoint())).intersects(
        page.spectrum_plot.rect().translated(page.spectrum_plot.mapTo(page, QPoint())))
    artifacts = Path("artifacts/background-display-filters")
    artifacts.mkdir(parents=True, exist_ok=True)
    assert page.grab().save(str(artifacts / f"{theme}-{size[0]}.png"))
    page.overlay_analysis_source.setChecked(False)
    application.processEvents()
    y_min, y_max = page.spectrum_plot.plot.viewRange()[1]
    assert y_max - y_min < 50e-12
    assert page.grab().save(str(artifacts / f"filtered-{theme}-{size[0]}.png"))
