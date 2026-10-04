"""Rendered controls and real worker integration for unified power processing."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import SpectrumAnalysisSettingsDialog
from app.settings.models import SpectrumPreviewSettings
from app.spectrum.streaming_statistics import dbm_to_w
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_shared_background_filter import shared_page as shared_page  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


def test_live_coalescing_does_not_discard_averaging_samples_and_spectrogram_matches(shared_page):
    app, page, _, profile, trace, _, _, _ = shared_page
    page._spectrogram_buffer.clear()
    page.correction_controls.power_average.setCurrentIndex(page.correction_controls.power_average.findData(4))
    page.cleanup_filters["background"].setChecked(True)
    rows = []
    for index in range(12):
        powers = tuple(np.asarray(trace.powers_dbm) + index*.005)
        rows.append(powers)
        page._show_trace(replace(trace, powers_dbm=powers,
                                acquired_at_utc=trace.acquired_at_utc + timedelta(seconds=index)), update_controls=False)
    wait_until(app, lambda: page._cleanup_result is not None and page._preview_statistics is not None
               and page._preview_statistics.frame_count == 4
               and page._applied_analysis_generation == page._analysis_generation)
    expected = np.mean([dbm_to_w(row) for row in rows[-4:]], axis=0) - profile.mean_w
    np.testing.assert_allclose(page._cleanup_result.values, expected, atol=4e-24, rtol=1e-9)
    assert "Power average" in page._cleanup_result.method
    assert "frames=4" in page._cleanup_result.input_provenance
    raw_saved, values_saved, unit_saved, operation_saved = page._manual_trace_payload("analysis:raw")
    assert unit_saved == "W" and raw_saved.powers_dbm == rows[-1]
    np.testing.assert_allclose(values_saved, expected, atol=4e-24, rtol=1e-9)
    assert '"temporal_average_frames": 4' in operation_saved
    assert profile.content_hash in operation_saved
    page._open_spectrum_window()
    app.processEvents()
    assert page._spectrum_window.isVisible()
    np.testing.assert_allclose(page._spectrum_window.spectrum._curves["Analysis"].getData()[1], expected,
                               atol=4e-24, rtol=1e-9)
    page._spectrum_window.close()
    page.analysis_tabs.setCurrentIndex(1)
    wait_until(app, lambda: page._spectrogram_filter_outcome is not None
               and page._spectrogram_filter_outcome.statistics.frame_count == 4)
    np.testing.assert_allclose(page._spectrogram_filter_outcome.matrix[-1], expected, atol=1e-23, rtol=1e-6)
    page._reset_preview_average()
    wait_until(app, lambda: page._spectrogram_filter_outcome is not None
               and page._spectrogram_filter_outcome.statistics.frame_count == 1)


def test_processing_history_preserves_precision_and_resets_on_context(shared_page):
    _, page, _, _, trace, _, _, _ = shared_page
    buffer = page._spectrogram_buffer
    buffer.clear()
    for index in range(10):
        buffer.append(replace(trace, powers_dbm=tuple(np.asarray(trace.powers_dbm) + index*1e-8)), now=1+index*.01)
    times, rows = buffer.processing_tail(64)
    assert len(rows) == 10 and len(times) == 10
    assert rows[-1].dtype == np.float64 and not rows[-1].flags.writeable
    buffer.append(replace(trace, configuration_generation=trace.configuration_generation+1), now=2.)
    assert len(buffer.processing_tail(64)[0]) == 1


def test_power_average_precedes_reference_math_in_both_views(shared_page):
    app, page, _, _, trace, _, _, _ = shared_page
    page._spectrogram_buffer.clear()
    reference = replace(trace, powers_dbm=tuple(np.full(len(trace.powers_dbm), -60.)))
    page._set_reference(page._build_reference(reference, kind="single", count=1))
    page.reference_operation.setCurrentIndex(page.reference_operation.findData("ratio_linear"))
    page.correction_controls.power_average.setCurrentIndex(page.correction_controls.power_average.findData(4))
    for level in (-30., -60.):
        page._show_trace(replace(trace, powers_dbm=tuple(np.full(len(trace.powers_dbm), level))), update_controls=False)
    wait_until(app, lambda: page._preview_statistics is not None and page._preview_statistics.frame_count == 2
               and page._applied_analysis_generation == page._analysis_generation)
    np.testing.assert_allclose(page._cleanup_result.values, 500.5, rtol=1e-12)
    assert page._cleanup_result.unit == "ratio"
    page.analysis_tabs.setCurrentIndex(1)
    wait_until(app, lambda: page._spectrogram_filter_outcome is not None
               and page._spectrogram_filter_outcome.statistics.frame_count == 2)
    np.testing.assert_allclose(page._spectrogram_filter_outcome.matrix[-1], 500.5, rtol=1e-6)


def test_short_spectrogram_window_retains_average_warmup(shared_page):
    _, page, _, _, trace, _, _, _ = shared_page
    buffer = page._spectrogram_buffer
    buffer.clear()
    for index in range(40):
        buffer.append(replace(trace), now=1+index*10.)
    _, times, rows = buffer.frame_snapshot(window_s=20., processing=True, warmup_frames=31)
    assert len(rows) == 34 and len(times) == 34
    assert times[-1] == 391. and times[-32] == 81.


def test_stationary_line_history_is_shared_even_when_display_rows_are_coalesced(shared_page):
    app, page, _, _, trace, _, _, _ = shared_page
    page._spectrogram_buffer.clear()
    values = np.full(len(trace.powers_dbm), -80.)
    values[250] = -60.
    page.cleanup_filters["emi_reject"].setChecked(True)
    for _ in range(8):
        page._show_trace(replace(trace, powers_dbm=tuple(values)), update_controls=False)
    wait_until(app, lambda: page._cleanup_result is not None
               and page._applied_analysis_generation == page._analysis_generation)
    assert 250 in page._cleanup_result.stationary_interference_indices
    expected = page._cleanup_result.values
    page.analysis_tabs.setCurrentIndex(1)
    wait_until(app, lambda: page._spectrogram_filter_outcome is not None)
    np.testing.assert_allclose(page._spectrogram_filter_outcome.matrix[-1], expected, atol=1e-5)


def test_peak_measurements_use_corrected_input_until_filtered_measurements_are_selected(shared_page):
    app, page, _, _, _, _, _, _ = shared_page
    page._analysis_parameters_applied(replace(page._analysis_parameters, peak_fit_models=False))
    page.auto_peak_detection.setChecked(True)
    page.cleanup_filters["background"].setChecked(True)
    page.cleanup_filters["narrow_reject"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None and 250 in page._cleanup_result.removed_peak_indices
               and any(peak.index == 250 for peak in page._detected_peaks))
    assert "before display filters" in page._peak_measurement_method()
    np.testing.assert_array_equal(page._analysis_values()[1], page._cleanup_result.input_values)
    page._analysis_parameters_applied(replace(page._analysis_parameters, peak_measure_filtered=True))
    wait_until(app, lambda: page._cleanup_result is not None
               and page._applied_analysis_generation == page._analysis_generation)
    assert all(peak.index != 250 for peak in page._detected_peaks)
    np.testing.assert_array_equal(page._analysis_values()[1], page._cleanup_result.values)


def test_derived_hdf5_preserves_average_provenance_and_canonical_raw(shared_page, tmp_path):
    import h5py

    from app.storage import (
        ManualSpectrumArchive,
        ManualSpectrumSaveMode,
        ThatecCompatibilityValidator,
    )

    app, page, _, _, trace, _, _, _ = shared_page
    page.correction_controls.power_average.setCurrentIndex(page.correction_controls.power_average.findData(4))
    page.cleanup_filters["background"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None and page._cleanup_result.unit == "W")
    raw, values, unit, operation = page._manual_trace_payload("analysis:raw")
    archive = ManualSpectrumArchive()
    try:
        result = archive.save(raw, destination=tmp_path / "preview.h5", mode=ManualSpectrumSaveMode.TIMESTAMPED,
            trace_variant="analysis:raw", processed_values=values, processed_unit=unit, processing_operation=operation)
    finally:
        archive.close()
    report = ThatecCompatibilityValidator().validate(result.path, require_pythat=True)
    assert report.valid, report.errors
    with h5py.File(result.path, "r") as stored:
        spectra = stored["spectra"]
        group = spectra[next(iter(spectra))]
        assert "power_average" in group.attrs["processing_operation"]
        assert '"temporal_average_frames": 4' in group.attrs["processing_operation"]
        np.testing.assert_array_equal(group["power_dbm"][:], trace.powers_dbm)


def test_processing_dialog_and_protected_band_settings_render_and_roundtrip(shared_page):
    app, page, _, _, _, _, _, _ = shared_page
    page.resize(1450, 900)
    page.show()
    app.processEvents()
    page.correction_controls.background_tools.click()
    dialog = page._processing_quality_dialog
    app.processEvents()
    assert dialog.isVisible() and dialog.model.isVisibleTo(dialog)
    assert "not qualified" in dialog.evidence.text()
    artifacts = Path("artifacts/spectrum-processing")
    artifacts.mkdir(parents=True, exist_ok=True)
    assert dialog.grab().save(str(artifacts / "quality.png"))
    dialog.close()
    settings = SpectrumAnalysisSettingsDialog(page, current_parameters=page._analysis_parameters, section="filters")
    try:
        settings.show()
        app.processEvents()
        settings.temporal_frames.setValue(7)
        settings.temporal_gap.setText("45 s")
        settings.narrow_protect.setChecked(True)
        settings.narrow_protected_start.setText("450 MHz")
        settings.narrow_protected_stop.setText("510 MHz")
        settings.additional_bands.setText("780 MHz .. 950 MHz")
        parameters = settings.get_parameters()
        assert parameters.narrow_protected_regions_hz == ((450e6, 510e6), (780e6, 950e6))
        page._analysis_parameters_applied(parameters)
        saved = SpectrumPreviewSettings.model_validate(page.preview_settings_snapshot())
        assert saved.average_frames == 7
        assert saved.analysis_parameters().narrow_protected_regions_hz == parameters.narrow_protected_regions_hz
        settings.additional_bands.setText("500 MHz .. 200 MHz")
        with pytest.raises(ValueError, match="protected"):
            settings.get_parameters()
        settings.additional_bands.setText("780 MHz .. 950 MHz")
        assert settings.grab().save(str(artifacts / "filters.png"))
    finally:
        settings.close()
        settings.deleteLater()


def test_protected_band_roundtrip_preserves_sub_hertz_edges_at_gigahertz(shared_page):
    _, page, _, _, _, _, _, _ = shared_page
    bands = ((6000000000.25, 6000000001.25), (6010000000.125, 6010000001.125))
    parameters = replace(page._analysis_parameters, narrow_protected_regions_hz=bands)
    page._analysis_parameters_applied(parameters)
    saved = SpectrumPreviewSettings.model_validate(page.preview_settings_snapshot())
    assert saved.analysis_parameters().narrow_protected_regions_hz == bands
    dialog = SpectrumAnalysisSettingsDialog(page, current_parameters=parameters, section="filters")
    try:
        assert dialog.get_parameters().narrow_protected_regions_hz == bands
    finally:
        dialog.close()
        dialog.deleteLater()


@pytest.mark.parametrize("button,attribute", [(0, "_diagnostic_dialog"),
    (1, "_training_dialog"), (2, "_validation_dialog")])
def test_quality_tools_are_visible_and_close_without_orphan_workers(shared_page, button, attribute):
    app, page, _, _, _, _, _, _ = shared_page
    page.resize(1450, 900)
    page.show()
    page._open_processing_quality()
    quality = page._processing_quality_dialog
    quality.buttons[button].click()
    app.processEvents()
    tool = getattr(page.correction_workspace, attribute)
    assert tool.isVisible() and tool.parentWidget() is page
    assert tool.width() >= 400 and tool.height() >= 300
    quality.close()
    app.processEvents()
    assert tool.isVisible()
    tool.close()
    wait_until(app, lambda: getattr(page.correction_workspace, attribute) is None)


@pytest.mark.parametrize("value", [{"average_frames": 0}, {"reset_gap": "0 s"},
                                  {"protected_bands": [("2 GHz", "1 GHz")]},
                                  {"protected_bands": [("2 V", "3 V")]}])
def test_invalid_saved_processing_settings_are_rejected(value):
    with pytest.raises(ValueError):
        SpectrumPreviewSettings.model_validate(value)
