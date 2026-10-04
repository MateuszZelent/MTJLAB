"""Recorded backgrounds must reach the displayed pipeline, including old archives."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from app.spectrum import clean_spectrum_pipeline
from app.storage.background_profile_store import BackgroundProfileHdf5Store
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_background_correction_assistant import setup as setup  # noqa: PLC0414
from tests.test_shared_background_filter import shared_page as shared_page  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


def legend_labels(plot):
    return [label.text for _, label in plot.plot.plotItem.legend.items]


@pytest.mark.parametrize("reload_archive", [False, True])
def test_recorded_background_is_verified_for_preview_without_rewriting_qualification(setup, reload_archive):
    app, page, requests, errors = setup
    page.resize(1450, 900)
    page.show()
    page.correction_controls.configure_background.click()
    assistant = page._background_assistant
    archive_path = assistant.reference_path
    assistant.next.click()
    wait_until(app, lambda: page._background_assistant is None or bool(errors), timeout=15)
    assert not errors
    context, profile = BackgroundProfileHdf5Store.load(archive_path)
    assert not context.settings_verified and not context.independent_sweeps_qualified
    assert not profile.signal_free_qualified
    original_archive = archive_path.read_bytes()

    if reload_archive:
        page.cleanup_filters["background"].setChecked(False)
        page.correction_controls.configure_background.click()
        assistant = page._background_assistant
        assistant.background_source.setCurrentIndex(assistant.background_source.findData("load"))
        assistant.profile_path.setText(str(archive_path))
        assistant.next.click()
        wait_until(app, lambda: page._background_assistant is None or bool(errors), timeout=15)
        assert not errors

    page.single.click()
    wait_until(app, lambda: page._latest_trace is not None and not page._fetch_pending)
    raw = page._latest_trace
    frequencies = np.asarray(raw.frequencies_hz)
    residual = (profile.mean_w * .05 + np.max(profile.mean_w) * (
                    .03 + np.random.default_rng(16).normal(0, .005, frequencies.size))
                + np.max(profile.mean_w) * .1 * np.exp(-.5 * ((frequencies-frequencies.mean()) / np.ptp(frequencies) * 15) ** 2))
    spike = len(residual)//3
    residual[spike] += np.max(profile.mean_w)
    signal = replace(raw, powers_dbm=tuple(10*np.log10(profile.mean_w + residual)+30),
                     acquired_at_utc=datetime.now(UTC))
    page._analysis_parameters_applied(replace(page._analysis_parameters,
        narrow_max_width_hz=5 * float(np.median(np.diff(frequencies)))))
    page.cleanup_filters["narrow_reject"].setChecked(True)
    page.cleanup_filters["denoise"].setChecked(True)
    page._show_trace(signal, update_controls=False)
    wait_until(app, lambda: page._analysis_raw_snapshot is signal or page._analysis_error is not None)
    assert page._analysis_error is None, page._analysis_error
    assert page._cleanup_result.unit == "W"
    assert spike in page._cleanup_result.removed_peak_indices
    verified, _ = page._background_for_filter(signal.frequencies_hz)
    assert verified.settings_verified and not verified.independent_sweeps_qualified
    assert verified.context_id == context.context_id
    assert not page.correction_workspace._context.settings_verified
    expected = clean_spectrum_pipeline(signal.powers_dbm, unit="dBm",
        modes=("background", "narrow_reject", "denoise"), frequencies_hz=signal.frequencies_hz,
        parameters=page._analysis_parameters, background_profile=profile, background_context=verified)
    np.testing.assert_allclose(page.spectrum_plot._traces["Analysis"][1], expected.values, rtol=1e-12)
    assert page.spectrum_host.currentWidget() is page.spectrum_plot
    assert page._active_spectrum_unit == "W" and "Raw" not in page.spectrum_plot._traces
    assert page.overlay_analysis_source.isVisibleTo(page) and page.overlay_analysis_source.isChecked()
    np.testing.assert_allclose(page.spectrum_plot._traces["Corrected"][1], residual, rtol=1e-10)
    assert not np.array_equal(page.spectrum_plot._traces["Corrected"][1], page.spectrum_plot._traces["Analysis"][1])
    labels = ["Raw − background [W]", "Filtered · Raw − background [W]"]
    assert legend_labels(page.spectrum_plot) == labels
    page.open_floating_spectrum.click()
    floating = page._spectrum_window.spectrum
    assert legend_labels(floating) == labels
    page.overlay_analysis_source.setChecked(False)
    assert legend_labels(page.spectrum_plot) == labels[1:] == legend_labels(floating)
    page.overlay_analysis_source.setChecked(True)
    assert legend_labels(page.spectrum_plot) == labels == legend_labels(floating)
    # An old hidden display checkbox must not bypass the enabled correction.
    page.show_analysis.setChecked(False)
    assert "Raw" not in page.spectrum_plot._traces and page._active_spectrum_unit == "W"
    page.show_analysis.setChecked(True)
    assert archive_path.read_bytes() == original_archive
    assert any(operation.startswith("read_background_filter_configuration:") for operation in requests)
    assert not any(operation in {"configure", "set_signal_generator_output"} for operation in requests)
    directory = Path("artifacts/background-preview-fix")
    directory.mkdir(parents=True, exist_ok=True)
    assert page.grab().save(str(directory / f"spectrum-{'loaded' if reload_archive else 'recorded'}.png"))
    assert page._spectrum_window.grab().save(str(directory / f"floating-{'loaded' if reload_archive else 'recorded'}.png"))
    page._spectrum_window.close()

    page.analysis_tabs.setCurrentIndex(1)
    wait_until(app, lambda: page._spectrogram_filter_outcome is not None or page._spectrogram_filter_error is not None)
    assert page._spectrogram_filter_error is None
    outcome = page._spectrogram_filter_outcome
    assert outcome.unit == "W"
    np.testing.assert_allclose(outcome.matrix[-1], expected.values, rtol=2e-5, atol=1e-23)
    assert page.grab().save(str(directory / f"spectrogram-{'loaded' if reload_archive else 'recorded'}.png"))
    page.analysis_tabs.setCurrentIndex(0)
    page.live.click()
    wait_until(app, lambda: page._timer.isActive())
    wait_until(app, lambda: page._live_frame_count >= 2 and page._analysis_raw_snapshot is not None
               and page._analysis_raw_snapshot is not signal and page._cleanup_result is not None)
    assert page._analysis_error is None and page._cleanup_result.unit == "W"
    assert legend_labels(page.spectrum_plot) == labels and "Raw" not in page.spectrum_plot._traces
    page.live.click()
    wait_until(app, lambda: not page._timer.isActive() and not page._live_transition_pending)
    assert archive_path.read_bytes() == original_archive


def test_missing_readback_and_incompatible_settings_do_not_display_raw_as_a_correction(shared_page):
    app, page, context, profile, raw, _, _, adapter = shared_page
    page.correction_workspace._context = replace(context, settings_verified=False)
    with patch.object(page._controller, "call") as request:
        page.cleanup_filters["background"].setChecked(True)
        assert page.spectrum_host.currentWidget() is page.spectrum_empty
        assert "Preparing" in page.spectrum_empty_heading.text()
        assert "checking" in page.correction_controls.summary.text().lower()
        assert not page.spectrum_plot._traces and not legend_labels(page.spectrum_plot)
        assert page._cleanup_result is None
        operation = request.call_args.args[0]
        full, advanced = adapter.read_full_configuration(), adapter.read_advanced_spectrum_configuration()
        page._result(operation, (replace(full, reference_level_dbm=full.reference_level_dbm+1), advanced))
        assert page._cleanup_result is None
        assert page.spectrum_host.currentWidget() is page.spectrum_empty
        assert "differ" in page.spectrum_empty_text.text()
        assert "unavailable" in page.correction_controls.summary.text().lower()
        assert not page.spectrum_plot._traces
        assert page._latest_trace is raw and page.correction_workspace._profile is profile
    page.cleanup_filters["background"].setChecked(False)
    assert legend_labels(page.spectrum_plot) == ["Raw [dBm]"]
    page.cleanup_filters["background"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None)
    assert legend_labels(page.spectrum_plot) == ["Raw − background [W]"]
    assert not page.correction_workspace._context.settings_verified


def test_labels_and_comparison_follow_filter_changes_without_duplicate_legend_items(shared_page):
    app, page, _, _, _, _, _, _ = shared_page
    page.cleanup_filters["background"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None)
    assert legend_labels(page.spectrum_plot) == ["Raw − background [W]"]
    page.cleanup_filters["denoise"].setChecked(True)
    wait_until(app, lambda: page._cleanup_result is not None)
    assert legend_labels(page.spectrum_plot) == ["Raw − background [W]", "Filtered · Raw − background [W]"]
    page.cleanup_filters["background"].setChecked(False)
    wait_until(app, lambda: page._cleanup_result is not None)
    assert legend_labels(page.spectrum_plot) == ["Raw [dBm]", "Filtered · Raw [dBm]"]
    page.cleanup_filters["denoise"].setChecked(False)
    assert legend_labels(page.spectrum_plot) == ["Raw [dBm]"]


def test_reading_settings_recovers_a_failed_background_check_without_another_sweep(shared_page):
    app, page, _, _, raw, _, requests, adapter = shared_page
    with patch.object(page._controller, "call") as request:
        page.cleanup_filters["background"].setChecked(True)
        page._error(request.call_args.args[0], "Readback timed out")
    assert page.spectrum_host.currentWidget() is page.spectrum_empty
    assert "Readback timed out" in page.spectrum_empty_text.text()
    page._result("read_configuration", adapter.read_current_configuration())
    wait_until(app, lambda: page._analysis_raw_snapshot is raw)
    assert page._analysis_error is None and page._cleanup_result.unit == "W"
    assert legend_labels(page.spectrum_plot) == ["Raw − background [W]"]
    assert requests and all(operation.startswith("read_background_filter_configuration:") for operation in requests)
