"""Shared background subtraction for passive spectrum and spectrogram previews."""

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.anritsu_ms2830a.acquisition_context import spectrum_configuration_fingerprint
from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.devices.anritsu_ms2830a.module import MODULE
from app.devices.anritsu_ms2830a.ui.analysis_worker import (
    SpectrogramAnalysisRequest,
    SpectrumAnalysisController,
)
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage, AnritsuPageState
from app.devices.simulators import SimulatedVisaFactory
from app.domain.spectrum_averaging import InstrumentAverageProvenance
from app.domain.spectrum_correction import BackgroundProfile, SpectrumAcquisitionContext
from app.spectrum.analysis import SpectrumAnalysisParameters, clean_spectrum_pipeline
from app.spectrum.display_processing import clean_display_spectrum
from app.spectrum.streaming_statistics import dbm_to_w
from app.storage import Hdf5RunReader, ManualSpectrumArchive
from app.storage.background_profile_store import BackgroundProfileHdf5Store
from app.storage.saved_spectrum_view import SavedSpectrumView
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.fixture
def shared_page(shell_qt_application):
    settings = simulation_settings()
    adapter = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
    adapter.connect()
    full = adapter.read_full_configuration()
    advanced = adapter.read_advanced_spectrum_configuration()
    identity = adapter.identity.idn
    x = np.linspace(200e6, 1200e6, 1001)
    context = SpectrumAcquisitionContext(x, spectrum_configuration_fingerprint(full, advanced, identity),
                                         configuration_generation=7, settings_verified=True)
    now = datetime.now(UTC)
    profile = BackgroundProfile("shared-background", context.context_id, np.full(x.size, 1e-9),
                                np.full(x.size, 1e-26), None, 30, now.timestamp()-60, now.timestamp(),
                                "Operator-selected low bias")
    broad = 2e-11 * np.exp(-4 * np.log(2) * ((x - 850e6) / 100e6) ** 2)
    residual = broad + np.random.default_rng(12).normal(0, 1e-13, x.size)
    residual[250] += 8e-11
    residual[400] -= 9e-11
    trace = SpectrumTrace(tuple(x), tuple(10*np.log10(profile.mean_w + residual)+30), now,
                          "TRAC1", configuration_generation=7)
    controller = MagicMock()
    controller.is_connected = False
    page = AnritsuPage(controller, settings, single_sweep_available=True)
    page._device_idn = identity
    page.auto_peak_detection.setChecked(False)
    page._analysis_parameters = replace(page._analysis_parameters, narrow_max_width_hz=5e6, narrow_threshold_sigma=6)
    workspace = page.correction_workspace
    workspace._profile, workspace._context = profile, context
    page._shared_background_changed()
    requests = []

    def read(operation, payload=None):
        requests.append(operation)
        assert operation.startswith("read_background_filter_configuration:")
        QTimer.singleShot(0, lambda: page._result(operation, (full, advanced)))

    controller.call.side_effect = read
    page._show_trace(trace, update_controls=False)
    yield shell_qt_application, page, context, profile, trace, residual, requests, adapter
    page.close()
    page.deleteLater()
    shell_qt_application.processEvents()
    adapter.disconnect()


def test_background_subtraction_uses_the_completed_anritsu_block(shared_page):
    from app.devices.anritsu_ms2830a import InstrumentAverageResult

    app, page, _, profile, raw, _, _, _ = shared_page
    read = page._controller.call.side_effect
    page._controller.call.side_effect = lambda operation, payload=None: (
        None if operation == "instrument_average_live" else read(operation, payload)
    )
    page.cleanup_filters["background"].setChecked(True)
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10_000)
    target = page.live_average_target
    target.backend.setCurrentIndex(target.backend.findData("instrument"))
    target.frames.setValue(50)
    page.start_live_average.click()
    mean = replace(raw, powers_dbm=tuple(value + .1 for value in raw.powers_dbm), trace_name="TRAC1_ANRITSU_LAV50",
                   instrument_average=InstrumentAverageProvenance(50, .006, requested_sweep_count=50))
    page._result("instrument_average_live", InstrumentAverageResult(mean, 50, .006))
    wait_until(app, lambda: page._cleanup_result is not None and page._analysis_raw_snapshot is mean)
    np.testing.assert_allclose(page._cleanup_result.values, dbm_to_w(mean.powers_dbm) - profile.mean_w,
                               rtol=1e-9, atol=1e-23)
    assert page._display_state.selected.unit == "W"


def test_common_pipeline_subtracts_linear_power_before_cleanup_and_preserves_inputs(shared_page):
    app, page, context, profile, trace, residual, requests, _ = shared_page
    original_profile = profile.mean_w.copy()
    original_raw = tuple(trace.powers_dbm)
    page.cleanup_filters["background"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "W")
    np.testing.assert_allclose(page._cleanup_result.values, residual, rtol=1e-9, atol=1e-23)
    assert page._active_spectrum_unit == "W"
    assert not page.cleanup_filters["emi_reject"].isEnabled()
    assert page.spectrum_plot._csv_value_column == "signed_power_w"
    assert page._analysis_values() is not None
    page.cleanup_filters["narrow_reject"].setChecked(True)
    page.cleanup_filters["denoise"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None and "denoise" in page._cleanup_result.method)
    result = page._cleanup_result
    expected = clean_spectrum_pipeline(original_raw, unit="dBm", modes=("background", "narrow_reject", "denoise"),
                                      frequencies_hz=trace.frequencies_hz, parameters=page._analysis_parameters,
                                      background_profile=profile, background_context=context)
    np.testing.assert_array_equal(result.values, expected.values)
    assert result.method.index("Background") < result.method.index("Narrow") < result.method.index("denoise")
    assert {250, 400}.issubset(result.removed_peak_indices)
    assert trace.powers_dbm == original_raw
    np.testing.assert_array_equal(profile.mean_w, original_profile)
    assert len(requests) == 1  # Read-only validation once, never another sweep or source operation.
    page.cleanup_filters["background"].setChecked(False)
    wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "dBm")
    assert page._active_spectrum_unit == "dBm"
    assert page.cleanup_filters["emi_reject"].isEnabled()


def test_live_stripes_share_rendered_controls_and_use_the_rolling_power_input(shared_page):
    app, page, _, profile, trace, _, requests, _ = shared_page

    nuisance = np.zeros(len(trace.frequencies_hz))
    nuisance[[600, 601]] = [8e-11, 1e-10]
    page.correction_workspace._profile = replace(profile, mean_w=profile.mean_w + nuisance)
    page._shared_background_changed()
    page.resize(1440, 900)
    page.show()
    page._open_spectrum_window()
    floating = page._spectrum_window
    floating.resize(860, 580)
    app.processEvents()
    assert floating.stripes.settings.isVisibleTo(floating)
    assert floating.rect().contains(floating.stripes.settings.mapTo(floating, QPoint()))
    floating.stripes.settings.click()
    app.processEvents()
    assert page.stripe_dialog.isVisible()
    controls = page.stripe_controls
    assert controls.apply.isVisibleTo(page.stripe_dialog)
    controls.mode.setCurrentIndex(controls.mode.findData("manual"))
    controls.bands.setPlainText("799 MHz .. 802 MHz")
    controls.protected.setPlainText("801 MHz .. 801.5 MHz")
    controls.apply.click()
    wait_until(app, lambda: page._display_state.primary_key == "stripe_corrected")
    assert not page.cleanup_filters["background"].isChecked()
    assert len(requests) == 1
    assert "stripe_corrected" in {name.lower() for name in floating.spectrum._traces}

    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10000)
    page.live_average_target.frames.setValue(2)
    page.start_live_average.click()
    second = replace(trace, powers_dbm=tuple(np.asarray(trace.powers_dbm) + 3.))
    for frame in (trace, second):
        page._result("fetch_current_trace_fast", frame)
    mean_w = (dbm_to_w(trace.powers_dbm) + dbm_to_w(second.powers_dbm)) / 2
    corrected = mean_w.copy()
    corrected[600] -= nuisance[600]
    actual = page._display_state.selected
    assert actual.unit == "W"
    np.testing.assert_allclose(actual.values, corrected, rtol=1e-12)
    assert page._manual_trace_payload("stripe_corrected")[0] is page._display_average_trace()

    floating.show_live_frame.click()
    instant_w = dbm_to_w(second.powers_dbm)
    instant_w[600] -= nuisance[600]
    overlay = page._display_state.by_key["live_frame"]
    assert overlay.unit == "W" and overlay.label == "Live frame · Stripes"
    np.testing.assert_allclose(overlay.values, instant_w, rtol=1e-12)
    assert page._manual_trace_payload("live_frame")[0] is second
    assert page._display_state.primary_key == "stripe_corrected"

    controls.view.setCurrentIndex(controls.view.findData("removed"))
    actual = page._display_state.selected
    assert actual.key == "stripe_removed"
    assert not floating.show_live_frame.isEnabled()
    assert "live_frame" not in page._display_state.by_key
    assert actual.values[600] == pytest.approx(nuisance[600], rel=1e-12)
    assert actual.values[601] == 0
    assert len(requests) == 1
    controls.disable.click()
    assert page._display_state.primary_key == "averaged"
    floating.close()


@pytest.mark.parametrize("stripe_in_signal", [True, False])
def test_rolling_stripes_correct_signal_and_reference_in_db(shared_page, stripe_in_signal):
    app, page, _, profile, trace, _, _, _ = shared_page
    nuisance_w = np.zeros(len(trace.frequencies_hz))
    nuisance_w[600] = 3e-9
    page.correction_workspace._profile = replace(profile, mean_w=profile.mean_w + nuisance_w)
    page._shared_background_changed()
    reference = replace(trace, powers_dbm=tuple(10 * np.log10(profile.mean_w + nuisance_w) + 30))
    page._set_reference(page._build_reference(reference, kind="averaged", count=32))
    page.correction_controls.reference.setChecked(True)

    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(30000)
    page.live_average_target.mode.setCurrentIndex(page.live_average_target.mode.findData("seconds"))
    page.live_average_target.seconds.setValue(60)
    page.start_live_average.click()
    first_w = 1.25 * profile.mean_w
    second_w = 2.75 * profile.mean_w
    second_w[400] *= 2  # A real peak outside the nuisance band.
    frames = []
    for signal_w in (first_w, second_w):
        measured_w = signal_w + nuisance_w if stripe_in_signal else signal_w
        frame = replace(trace, powers_dbm=tuple(10 * np.log10(measured_w) + 30))
        frames.append(frame)
        page._result("fetch_current_trace_fast", frame)

    page.correction_controls.stripes.toggle.setChecked(True)
    # While the settings check is pending the current reference still applies.
    assert page._display_state.selected.unit == "dB"
    wait_until(app, lambda: page._display_state.primary_key == "stripe_corrected")
    expected = 10 * np.log10((first_w + second_w) / (2 * profile.mean_w))
    np.testing.assert_allclose(page._display_state.selected.values, expected, atol=1e-10)
    assert "1/1 bands matched to reference" in page.stripe_controls.status.text()
    assert page.correction_controls.reference.isChecked()
    assert page._display_state.selected.unit == "dB"
    assert page.spectrum_plot.plot.getAxis("left").labelText == "Relative power"
    assert page.spectrum_plot._csv_value_column != "power_w"

    page._open_spectrum_window()
    floating = page._spectrum_window
    try:
        floating.show_live_frame.click()
        overlay = page._display_state.by_key["live_frame"]
        assert overlay.unit == "dB"
        np.testing.assert_allclose(overlay.values, 10 * np.log10(second_w / profile.mean_w), atol=1e-10)
        assert floating.spectrum.plot.getAxis("left").labelUnits == "dB"
        np.testing.assert_allclose(floating.spectrum._traces["Stripe_corrected"][1], expected, atol=1e-10)
        assert page._manual_trace_payload("stripe_corrected")[0] is page._rolling_average_trace
        assert page._manual_trace_payload("live_frame")[0] is frames[-1]

        view = page.stripe_controls.view
        view.setCurrentIndex(view.findData("input"))
        restored = page._display_state.selected
        assert restored.unit == "dB"
        expected_input = np.asarray(page._rolling_average_trace.powers_dbm) - reference.powers_dbm
        np.testing.assert_allclose(restored.values, expected_input, atol=1e-10)
        view.setCurrentIndex(view.findData("removed"))
        assert page._display_state.selected.unit == "W"
        assert "live_frame" not in page._display_state.by_key
    finally:
        floating.close()


def test_manual_stripes_leave_gaps_for_nonpositive_corrected_power_in_db(shared_page):
    app, page, _, profile, trace, _, _, _ = shared_page
    nuisance_w = np.zeros(len(trace.frequencies_hz))
    nuisance_w[600] = 3e-9
    page.correction_workspace._profile = replace(profile, mean_w=profile.mean_w + nuisance_w)
    page._shared_background_changed()
    signal = replace(trace, powers_dbm=(-90.,) * len(trace.frequencies_hz))
    reference = replace(trace, powers_dbm=(-87.,) * len(trace.frequencies_hz))
    page._set_reference(page._build_reference(reference, kind="single", count=1))
    page.correction_controls.reference.setChecked(True)
    page._show_trace(signal, update_controls=False)
    controls = page.stripe_controls
    controls.mode.setCurrentIndex(controls.mode.findData("manual"))
    controls.bands.setPlainText("799 MHz .. 802 MHz")
    controls.apply.click()
    wait_until(app, lambda: page._display_state.primary_key == "stripe_corrected")
    current = page._display_state.selected
    assert current.unit == "dB"
    assert np.flatnonzero(np.isnan(current.values)).tolist() == [600]
    np.testing.assert_allclose(np.delete(current.values, 600), -3., atol=1e-10)
    assert "nonpositive" in controls.status.text()


def test_stripe_views_mask_or_reconstruct_known_bands_and_preserve_protected_signal(shared_page, tmp_path):
    app, page, _, profile, trace, _, _, _ = shared_page
    nuisance_w = np.zeros(len(trace.frequencies_hz))
    nuisance_w[[600, 640, 700]] = [3e-9, 2.5e-9, 2e-9]
    variance_w2 = profile.sample_variance_w2.copy()
    variance_w2[640] = (2e-9) ** 2
    page.correction_workspace._profile = replace(
        profile, mean_w=profile.mean_w + nuisance_w, sample_variance_w2=variance_w2,
    )
    page._shared_background_changed()
    reference = replace(trace, powers_dbm=tuple(10 * np.log10(profile.mean_w) + 30))
    measured_w = profile.mean_w + nuisance_w
    raw = replace(trace, powers_dbm=tuple(10 * np.log10(measured_w) + 30))
    page._set_reference(page._build_reference(reference, kind="single", count=1))
    page.correction_controls.reference.setChecked(True)
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(30000)
    page.start_live_average.click()
    page._result("fetch_current_trace_fast", raw)
    page.show_live_frame.click()
    controls = page.stripe_controls
    controls.protected.setPlainText("899 MHz .. 901 MHz")
    controls.apply.click()
    wait_until(app, lambda: page._display_state.primary_key == "stripe_corrected")

    mask_view = controls.view.findData("masked")
    assert mask_view >= 0
    controls.view.setCurrentIndex(mask_view)
    masked = page._display_state.by_key["stripe_masked"]
    assert masked.unit == "dB"
    assert len(controls._detected) == 1
    lower, upper = controls._detected[0]
    hz = np.asarray(trace.frequencies_hz)
    expected = (hz >= lower) & (hz <= upper)
    assert expected[600] and not expected[640] and not expected[700]
    np.testing.assert_array_equal(np.isnan(masked.values), expected)
    np.testing.assert_array_equal(np.isnan(page._display_state.by_key["live_frame"].values), expected)
    actual = np.asarray(masked.values)
    np.testing.assert_allclose(
        actual[~expected], 10 * np.log10(measured_w[~expected] / profile.mean_w[~expected]), atol=1e-10,
    )
    assert page._manual_trace_payload("stripe_masked")[0] is page._rolling_average_trace

    view = controls.view.findData("reconstructed")
    assert view >= 0
    controls.view.setCurrentIndex(view)
    reconstructed = page._display_state.selected
    assert reconstructed.key == "stripe_reconstructed"
    assert reconstructed.unit == "dB"
    estimated = np.zeros(len(hz), dtype=bool)
    for low, high in reconstructed.estimated_bands_hz:
        estimated |= (hz >= low) & (hz <= high)
    assert estimated[600] and estimated[640] and not estimated[700]
    np.testing.assert_allclose(np.asarray(reconstructed.values)[estimated], 0, atol=1e-10)
    np.testing.assert_array_equal(np.asarray(reconstructed.values)[~estimated], actual[~estimated])

    page.resize(1400, 900)
    page.show()
    page._open_spectrum_window()
    floating = page._spectrum_window
    try:
        app.processEvents()
        for plot in (page.spectrum_plot, floating.spectrum):
            assert plot.plot.width() > 500
            assert plot.trace_point_count("Reconstructed") == len(hz)
            assert tuple(region.getRegion() for region in plot._estimated_band_regions) == reconstructed.estimated_bands_hz
        assert floating.grab().save(str(tmp_path / "reconstructed-floating.png"))
        acquired, processed, unit, operation = page._manual_trace_payload(reconstructed.key)
        saved = ManualSpectrumArchive(simulation=True).save(
            acquired, destination=tmp_path / "reconstructed.h5", mode="timestamped",
            trace_variant=reconstructed.key, processed_values=processed, processed_unit=unit,
            processing_operation=operation, reference=page._reference_spectrum,
            saved_display=SavedSpectrumView(page._display_state.traces, reconstructed.key),
        )
        stored = Hdf5RunReader.spectrum(saved.path, 0)
        np.testing.assert_array_equal(stored.powers_dbm, acquired.powers_dbm)
        assert stored.reference_index == 0
        replay = stored.saved_display
        assert replay.primary_key == reconstructed.key
        restored = next(trace for trace in replay.traces if trace.key == replay.primary_key)
        assert restored.estimated_bands_hz == reconstructed.estimated_bands_hz
        np.testing.assert_array_equal(restored.values, reconstructed.values)
        controls.view.setCurrentIndex(mask_view)
        assert not page.spectrum_plot._estimated_band_regions
        assert not floating.spectrum._estimated_band_regions
    finally:
        floating.close()


@pytest.mark.parametrize("view,modes", [
    ("corrected", ("denoise",)),
    ("masked", ("narrow_reject", "denoise")),
    ("reconstructed", ("narrow_reject", "denoise")),
])
def test_live_filters_smooth_stripes_in_both_spectrum_windows(shared_page, view, modes):
    app, page, _, profile, trace, _, _, _ = shared_page
    frequency = np.asarray(trace.frequencies_hz)
    nuisance_w = np.zeros(frequency.size)
    nuisance_w[600] = 3e-9
    reference_w = profile.mean_w + nuisance_w
    page.correction_workspace._profile = replace(profile, mean_w=reference_w)
    page._shared_background_changed()
    reference = replace(trace, powers_dbm=tuple(10 * np.log10(reference_w) + 30))
    page._set_reference(page._build_reference(reference, kind="averaged", count=32))
    page.correction_controls.reference.setChecked(True)
    variation = np.random.default_rng(11).normal(0, .10, frequency.size)
    hump_db = 1.5 * np.exp(-.5 * ((frequency - 800e6) / 110e6) ** 2)
    measured_w = profile.mean_w * 10 ** ((hump_db + variation) / 10) + nuisance_w
    frame = replace(trace, powers_dbm=tuple(10 * np.log10(measured_w) + 30))
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(30000)
    page.start_live_average.click()
    page._result("fetch_current_trace_fast", frame)
    page.correction_controls.stripes.toggle.setChecked(True)
    wait_until(app, lambda: page._display_state.primary_key == "stripe_corrected")
    page.stripe_controls.view.setCurrentIndex(page.stripe_controls.view.findData(view))
    source = page._display_state.selected
    expected = clean_display_spectrum(
        source.values, unit="dB", modes=modes, frequencies_hz=source.frequencies_hz,
        parameters=page._analysis_parameters, allow_gaps=True,
    )
    assert not np.allclose(source.values, expected.values, equal_nan=True)

    page._open_spectrum_window()
    floating = page._spectrum_window
    try:
        page.overlay_analysis_source.setChecked(False)
        for name in modes:
            page.cleanup_filters[name].setChecked(True)
        key = f"analysis:stripe_{view}"
        wait_until(app, lambda: page._display_state.primary_key == key)
        assert page._display_state.selected.unit == "dB"
        assert "Filtered" in page._display_state.selected.label
        assert page._manual_trace_payload(key)[0] is page._rolling_average_trace
        for plot in (page.spectrum_plot, floating.spectrum):
            np.testing.assert_allclose(plot._traces["Analysis"][1], expected.values, equal_nan=True)
        np.testing.assert_array_equal(np.isnan(page._display_state.selected.values), np.isnan(source.values))
        page.overlay_analysis_source.setChecked(True)
        np.testing.assert_allclose(page._display_state.by_key[source.key].values, source.values, equal_nan=True)

        first_filtered = page._display_state.selected
        floating.show_live_frame.click()
        next_frame = replace(frame, powers_dbm=tuple(np.asarray(frame.powers_dbm) + 0.4))
        page._result("fetch_current_trace_fast", next_frame)
        wait_until(app, lambda: page._display_state.primary_key == key
                   and page._analysis_raw_snapshot is page._rolling_average_trace)
        displayed = page._display_state
        assert not np.allclose(displayed.selected.values, first_filtered.values, equal_nan=True)
        assert displayed.by_key[source.key].frame_id == displayed.selected.frame_id
        assert displayed.by_key["live_frame"].unit == "dB"
        for plot in (page.spectrum_plot, floating.spectrum):
            np.testing.assert_allclose(plot._traces["Analysis"][1], displayed.selected.values, equal_nan=True)
        page.cleanup_filters["denoise"].setChecked(False)
        page.cleanup_filters["narrow_reject"].setChecked(False)
        assert page._display_state.primary_key == source.key
    finally:
        floating.close()


def test_live_frame_controls_fit_the_correction_strip_at_different_widths(shared_page):
    app, page, _, _, trace, _, _, _ = shared_page
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(30000)
    page.start_live_average.click()
    page._result("fetch_current_trace_fast", trace)
    page.show_live_frame.click()
    page.resize(1450, 900)
    page.show()
    app.processEvents()
    page._open_spectrum_window()
    floating = page._spectrum_window
    try:
        for width, height in ((1450, 900), (950, 720), (820, 460), (1300, 800)):
            page.resize(width, height)
            floating.resize(860, 580)
            QTest.qWait(50)
            if page.compact_plot_settings.isVisible():
                page.compact_plot_settings.click()
                QTest.qWait(50)
            for strip, controls in (
                (page.correction_controls, (page.correction_controls.live_average, page.show_live_frame,
                                            page.correction_controls.stripes, page.correction_controls.reference)),
                (page.presentation_controls, (page.show_absolute, page.overlay_analysis_source, page.peak_settings)),
                (floating.show_live_frame.parentWidget(), (floating.live_average, floating.show_live_frame,
                                                           floating.stripes, floating.show_absolute)),
            ):
                rects = []
                for control in controls:
                    assert control.isVisibleTo(strip) and strip.isAncestorOf(control)
                    bounds = control.rect().translated(control.mapTo(strip, QPoint()))
                    assert strip.rect().contains(bounds)
                    assert not any(bounds.intersects(previous) for previous in rects)
                    rects.append(bounds)
                    popup_view = page._presentation_popup_view
                    if popup_view.isAncestorOf(control):
                        assert popup_view.rect().contains(control.rect().translated(control.mapTo(popup_view, QPoint())))
            assert floating.show_live_frame.isChecked()
            page._presentation_popup.hide()
    finally:
        floating.close()


def test_live_stripes_fall_back_to_input_after_incompatible_settings(shared_page):
    app, page, _, _, trace, _, requests, _ = shared_page
    page.stripe_controls.mode.setCurrentIndex(page.stripe_controls.mode.findData("manual"))
    page.stripe_controls.bands.setPlainText("799 MHz .. 802 MHz")
    page.stripe_controls.apply.click()
    wait_until(app, lambda: page._display_state.primary_key == "stripe_corrected")

    page._show_trace(replace(trace, frequencies_hz=tuple(x + 1e6 for x in trace.frequencies_hz)), update_controls=False)
    assert page._display_state.primary_key == "stripe_input"
    assert "frequency grid" in page.stripe_controls.status.text()
    np.testing.assert_array_equal(page._display_state.selected.values, dbm_to_w(trace.powers_dbm))
    assert len(requests) == 1


def test_stripes_switch_finds_bands_and_corrects_live_without_form_entries(shared_page):
    app, page, _, profile, trace, _, requests, _ = shared_page
    nuisance_w = np.zeros(len(trace.frequencies_hz))
    nuisance_w[600] = 3e-9
    page.correction_workspace._profile = replace(profile, mean_w=profile.mean_w + nuisance_w)
    page._shared_background_changed()
    signal_w = dbm_to_w(trace.powers_dbm)
    live = replace(trace, powers_dbm=tuple(10 * np.log10((signal_w + nuisance_w) / 1e-3)))
    page._show_trace(live, update_controls=False)
    page.resize(1440, 900)
    page.show()
    page._open_spectrum_window()
    floating = page._spectrum_window
    floating.resize(860, 580)
    app.processEvents()
    main_switch = page.correction_controls.stripes.toggle
    floating_switch = floating.stripes.toggle
    assert main_switch.isVisibleTo(page)
    for control in (floating_switch, floating.show_absolute, floating.tools_toggle):
        assert control.isVisibleTo(floating)
        assert floating.rect().contains(control.mapTo(floating, control.rect().bottomRight()))
    assert not main_switch.isChecked() and not floating_switch.isChecked()

    QTest.mouseClick(main_switch.indicator, Qt.MouseButton.LeftButton)
    wait_until(app, lambda: page._display_state.primary_key == "stripe_corrected")
    assert main_switch.isChecked() and floating_switch.isChecked()
    assert not page.stripe_dialog.isVisible()
    assert not page.stripe_controls.bands.toPlainText()
    assert "1/1" in page.stripe_controls.status.text()
    np.testing.assert_allclose(page._display_state.selected.values, signal_w, rtol=1e-12)
    assert len(requests) == 1

    page._show_trace(trace, update_controls=False)
    assert "0/1" in page.stripe_controls.status.text()
    np.testing.assert_array_equal(page._display_state.selected.values, signal_w)
    QTest.mouseClick(floating_switch.indicator, Qt.MouseButton.LeftButton)
    assert not main_switch.isChecked() and not floating_switch.isChecked()
    assert page._display_state.primary_key == "raw"
    assert len(requests) == 1
    floating.close()


def test_automatic_stripes_offer_background_setup_and_return_off_on_cancel(shared_page):
    app, page, _, _, _, _, requests, _ = shared_page
    page.correction_workspace._profile = None
    page._shared_background_changed()
    page.resize(1440, 900)
    page.show()
    page._open_spectrum_window()
    floating = page._spectrum_window
    app.processEvents()

    switch = page.correction_controls.stripes.toggle
    QTest.mouseClick(floating.stripes.toggle.indicator, Qt.MouseButton.LeftButton)
    app.processEvents()
    assistant = page._background_assistant
    assert assistant is not None and assistant.isVisible()
    assert assistant.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert "signal of interest" in assistant.explanation.text()
    assert switch.isChecked()
    assert not requests

    assistant.reject()
    assert not switch.isChecked() and not floating.stripes.toggle.isChecked()
    assert page._display_state.primary_key == "raw"
    assert not requests
    floating.close()


def test_automatic_stripes_use_loaded_background_during_live(shared_page, tmp_path):
    app, page, context, profile, trace, _, requests, _ = shared_page
    nuisance_w = np.zeros(len(trace.frequencies_hz))
    nuisance_w[600] = 1e-9
    path = tmp_path / "background.h5"
    BackgroundProfileHdf5Store.save(path, context, replace(profile, mean_w=profile.mean_w + nuisance_w))
    page.correction_workspace._profile = None
    page._shared_background_changed()
    signal_w = dbm_to_w(trace.powers_dbm)
    live = replace(trace, powers_dbm=tuple(10 * np.log10((signal_w + nuisance_w) / 1e-3)))
    page._show_trace(live, update_controls=False)
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(30000)

    switch = page.correction_controls.stripes.toggle
    switch.setChecked(True)
    assistant = page._background_assistant
    assert assistant is not None
    app.processEvents()
    assert assistant.record_available
    assert assistant.background_source.currentData() == "load"
    assert assistant.load_fields.isVisibleTo(assistant)
    assert not assistant.record_fields.isVisibleTo(assistant)
    assert assistant.next.text() == "Load background"
    assert not assistant.next.isEnabled()
    assert page._timer.isActive()
    assert not requests
    assistant.profile_path.setText(str(path))
    assistant.next.click()
    wait_until(app, lambda: page._display_state.primary_key == "stripe_corrected")
    assert page._timer.isActive()
    assert page._background_assistant is None
    assert switch.isChecked()
    assert len(requests) == 1  # Same readback check as a pre-loaded background.
    np.testing.assert_allclose(page._display_state.selected.values, signal_w, rtol=1e-12)


@pytest.mark.parametrize("problem", ["missing", "grid", "fingerprint", "age"])
def test_incompatible_background_is_reported_and_never_subtracted(shared_page, problem):
    app, page, context, profile, trace, _, requests, _ = shared_page
    if problem == "missing":
        page.correction_workspace._profile = None
    elif problem == "grid":
        page.correction_workspace._context = replace(context, frequencies_hz=context.frequencies_hz + 1e6)
    elif problem == "fingerprint":
        wrong = replace(context, configuration_fingerprint="different RBW and RF path")
        page.correction_workspace._context = wrong
        page.correction_workspace._profile = replace(profile, context_id=wrong.context_id)
    else:
        page.correction_workspace._profile = replace(profile, started_at_s=1, completed_at_s=2)
        config = page._station_settings.anritsu.spectrum_correction
        updated = config.model_copy(update={
            "reference_policy": config.reference_policy.model_copy(update={"maximum_age": "1 s"}),
        })
        settings = page._station_settings
        page._station_settings = settings.model_copy(update={
            "devices": settings.devices.model_copy(update={
                "anritsu": settings.anritsu.model_copy(update={"spectrum_correction": updated}),
            }),
        })
    page.cleanup_filters["background"].setChecked(True)
    QTest.qWait(80)
    app.processEvents()
    if problem == "missing":
        assert page._background_assistant is not None and page._pending_correction == "background"
        assert not page.cleanup_filters["background"].isChecked()
        page._background_assistant.reject()
        return
    assert page._cleanup_result is None
    assert "unavailable" in page.analysis_status.text()
    assert page._active_spectrum_unit == "dBm"
    assert page.spectrum_host.currentWidget() is page.spectrum_empty
    assert not page.spectrum_plot._traces
    assert page._latest_trace is trace
    assert page._analysis_error in page.spectrum_empty_text.text()
    assert all(item.startswith("read_background_filter_configuration:") for item in requests)


def test_spectrogram_uses_same_filters_and_preserves_signed_rows(shared_page):
    app, page, context, profile, trace, _, _, _ = shared_page
    page.analysis_tabs.setCurrentIndex(1)
    assert page.signal_analysis_card.isHidden() is False
    page._spectrogram_buffer.clear()
    page._spectrogram_buffer.append(trace, now=10)
    second = replace(trace, powers_dbm=tuple(np.asarray(trace.powers_dbm) + .01))
    page._spectrogram_buffer.append(second, now=11)
    raw_rows = page._spectrogram_buffer.snapshot(30)[2].copy()
    for key in ("background", "narrow_reject", "denoise"):
        page.cleanup_filters[key].setChecked(True)
    wait_until(app, lambda: page._spectrogram_filter_outcome is not None and "denoise" in page._spectrogram_filter_outcome.method)
    outcome = page._spectrogram_filter_outcome
    assert outcome.unit == "W" and outcome.matrix.shape == raw_rows.shape
    assert not outcome.matrix.flags.writeable
    for raw, filtered in zip(raw_rows, outcome.matrix, strict=True):
        expected = clean_spectrum_pipeline(raw, unit="dBm", modes=("background", "narrow_reject", "denoise"),
                                          frequencies_hz=trace.frequencies_hz, parameters=page._analysis_parameters,
                                          background_profile=profile, background_context=context)
        np.testing.assert_allclose(filtered, expected.values, rtol=1e-6, atol=1e-20)
    assert np.any(outcome.matrix < 0)
    np.testing.assert_array_equal(page._spectrogram_buffer.snapshot(30)[2], raw_rows)
    assert page.spectrogram_plot.color_bar.axis.labelUnits == "W"
    assert not page.spectrogram_plot.color_bar.getAxis("left").labelText
    assert outcome.color_levels[0] == -outcome.color_levels[1]
    old = outcome
    page.cleanup_filters["background"].setChecked(False)
    page._spectrogram_filters_completed(old)
    assert page._spectrogram_filter_outcome is None
    wait_until(app, lambda: page._spectrogram_filter_outcome is not None and page._spectrogram_filter_outcome.unit == "dBm")


@pytest.mark.parametrize("modes", [("denoise",), ("emi_reject", "denoise")])
def test_spectrogram_cache_filters_only_new_rows_off_gui_thread(shell_qt_application, modes):
    controller = SpectrumAnalysisController()
    outcomes, threads = [], []
    controller.result.connect(outcomes.append)
    from PySide6.QtCore import QThread
    real = clean_spectrum_pipeline

    def cleanup(*args, **kwargs):
        threads.append(QThread.currentThread())
        return real(*args, **kwargs)

    rows = tuple(np.full(101, -60, dtype=np.float32) for _ in range(3))
    params = SpectrumAnalysisParameters()
    key = ("raw", modes, params)
    try:
        with patch("app.devices.anritsu_ms2830a.ui.analysis_worker.clean_spectrum_pipeline", side_effect=cleanup) as mocked:
            controller.submit(SpectrogramAnalysisRequest(1, tuple(np.arange(101)), (1., 2.), rows[:2], "dBm", modes, params, key))
            wait_until(shell_qt_application, lambda: len(outcomes) == 1)
            controller.submit(SpectrogramAnalysisRequest(2, tuple(np.arange(101)), (1., 2., 3.), rows, "dBm", modes, params, key))
            wait_until(shell_qt_application, lambda: len(outcomes) == 2)
            assert mocked.call_count == 3
            assert all(thread is not shell_qt_application.thread() for thread in threads)
            assert outcomes[-1].matrix.shape == (3, 101)
            controller.submit(SpectrogramAnalysisRequest(3, tuple(np.arange(101)), (2., 3.), rows[1:], "dBm", modes, params, key))
            wait_until(shell_qt_application, lambda: len(outcomes) == 3)
            assert mocked.call_count == (5 if "emi_reject" in modes else 3)
    finally:
        controller.close()
    assert not controller._thread.isRunning()


@pytest.mark.parametrize("theme,size", [("light", (1450, 900)), ("dark", (950, 720))])
def test_shared_filters_render_on_spectrogram_and_spectrum(shared_page, theme, size):
    app, page, _, _, _, _, _, _ = shared_page
    apply_application_theme(app, theme)
    page.resize(*size)
    page.show()
    page.cleanup_filters["background"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "W")
    artifacts = Path("artifacts/shared-background")
    artifacts.mkdir(parents=True, exist_ok=True)
    for tab, name in ((0, "spectrum"), (1, "spectrogram")):
        page.analysis_tabs.setCurrentIndex(tab)
        QTest.qWait(80)
        app.processEvents()
        for control in page.cleanup_filters.values():
            assert control.isVisibleTo(page)
            origin = control.mapTo(page, QPoint(0, 0))
            assert page.rect().contains(origin + QPoint(control.width()-1, control.height()-1))
        widgets = list(page.cleanup_filters.values())
        for index, control in enumerate(widgets):
            assert all(not control.rect().translated(control.mapTo(page, QPoint())).intersects(
                other.rect().translated(other.mapTo(page, QPoint()))) for other in widgets[index+1:])
        plot = page.spectrum_plot if tab == 0 else page.spectrogram_plot
        assert plot.height() > 200
        assert page.grab().save(str(artifacts / f"{name}-{theme}-{size[0]}.png"))


def test_validation_dispatch_only_reads_analyzer_settings(shared_page):
    _, _, _, _, _, _, _, adapter = shared_page
    session = adapter._require_session()
    before = len(session.commands)
    result = MODULE.dispatch(adapter, "read_background_filter_configuration:fixture", None)
    assert len(result) == 2
    commands = session.commands[before:]
    assert commands and all(command.strip().endswith("?") for command in commands)



def test_hidden_tabs_buffer_frames_without_submitting_filters_or_repainting(shared_page):
    app, page, _, _, trace, _, _, _ = shared_page
    page.cleanup_filters["background"].setChecked(True)
    page.cleanup_filters["denoise"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "W")
    newer = replace(trace, acquired_at_utc=datetime.now(UTC), powers_dbm=tuple(np.asarray(trace.powers_dbm)+.02))
    with patch.object(page, "_preview_page_active", return_value=False), \
         patch.object(page._analysis_controller, "submit") as spectrum_job, \
         patch.object(page._spectrogram_analysis_controller, "submit") as spectrogram_job, \
         patch.object(page.spectrum_plot, "set_trace") as spectrum_paint, \
         patch.object(page.spectrogram_plot, "set_data") as spectrogram_paint:
        page._show_trace(newer, update_controls=False)
        assert page._latest_trace is newer and page._spectrogram_buffer.row_count > 0
        spectrum_job.assert_not_called()
        spectrogram_job.assert_not_called()
        spectrum_paint.assert_not_called()
        spectrogram_paint.assert_not_called()
    page.analysis_tabs.setCurrentIndex(1)
    wait_until(app, lambda: page._spectrogram_filter_outcome is not None)
    assert page._spectrogram_filter_outcome.unit == "W"
    page.analysis_tabs.setCurrentIndex(0)
    wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "W")
    expected = clean_spectrum_pipeline(newer.powers_dbm, unit="dBm", modes=("background", "denoise"),
                                      parameters=page._analysis_parameters, frequencies_hz=newer.frequencies_hz,
                                      background_profile=page.correction_workspace._profile,
                                      background_context=page.correction_workspace._context)
    np.testing.assert_array_equal(page._cleanup_result.values, expected.values)


def test_background_toggle_revalidates_settings_and_cannot_replay_old_watt_result(shared_page):
    app, page, _, _, _, _, requests, _ = shared_page
    outcomes = []
    page._analysis_controller.result.connect(outcomes.append)
    page.cleanup_filters["background"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "W")
    previous = outcomes[-1]
    page.cleanup_filters["background"].setChecked(False)
    page._analysis_completed(previous)
    assert page._cleanup_result is None
    wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "dBm")
    page.cleanup_filters["background"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "W")
    assert len(requests) == 2
