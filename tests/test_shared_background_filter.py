"""Shared background subtraction for passive spectrum and spectrogram previews."""

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtTest import QTest

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.anritsu_ms2830a.acquisition_context import spectrum_configuration_fingerprint
from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.devices.anritsu_ms2830a.module import MODULE
from app.devices.anritsu_ms2830a.ui.analysis_worker import (
    SpectrogramAnalysisRequest,
    SpectrumAnalysisController,
)
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from app.devices.simulators import SimulatedVisaFactory
from app.domain.spectrum_correction import BackgroundProfile, SpectrumAcquisitionContext
from app.spectrum.analysis import SpectrumAnalysisParameters, clean_spectrum_pipeline
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
