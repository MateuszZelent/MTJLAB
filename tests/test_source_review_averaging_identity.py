from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from PySide6.QtCore import QThread
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication
from qfluentwidgets import isDarkTheme

from app.devices.anritsu_ms2830a import AnritsuAdapter, InstrumentAverageResult, SpectrumTrace
from app.devices.anritsu_ms2830a.adapter import AnritsuConfigurationSnapshot
from app.devices.anritsu_ms2830a.module import MODULE
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage, AnritsuPageState
from app.devices.simulators import SimulatedVisaFactory
from app.domain.errors import DeviceError
from app.domain.spectrum_averaging import InstrumentAverageProvenance
from app.storage.reference_store import ReferenceHdf5Store
from app.ui.design_system import apply_application_theme, plot_theme, tokens_for
from app.ui.workers import DeviceController
from tests.helpers import loaded_settings, simulation_settings
from tests.test_spectrum_correction_controller import wait_until


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        application.setFont(QFont("Segoe UI", 10))
    return application


@pytest.fixture
def page(app):
    widget = AnritsuPage(Mock(), loaded_settings(), single_sweep_available=True)
    yield widget
    widget.cancel_averaging()
    assert widget.shutdown_analysis()
    widget.close()
    widget.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("destination", ["spectrum", "reference"])
@pytest.mark.parametrize("change", ["grid", "generation", "trace", "invalid_grid"])
def test_averaging_rejects_changed_identity_without_replacing_result(page, destination, change):
    original = SpectrumTrace((1e6, 2e6, 3e6), (-70., -60., -50.), datetime.now(UTC), "TRAC1", configuration_generation=4)
    page._averaged_trace = original
    old_reference = page._reference_spectrum
    page.average_count.setValue(2)
    page.reference_average_count.setValue(2)
    page._start_temporal_averaging(destination)
    operation = "unaveraged_sweep"
    if destination == "reference":
        page._result("start_live", AnritsuConfigurationSnapshot(1e6, 3e6, -10., 1001, "SPECT"))
        operation = "fetch_current_trace_fast"
    page._result(operation, original)
    changed = {
        "grid": replace(original, frequencies_hz=(1e6, 2.1e6, 3e6)),
        "generation": replace(original, configuration_generation=5),
        "trace": replace(original, trace_name="TRAC2"),
        "invalid_grid": replace(original, frequencies_hz=(1e6, float("nan"), 3e6)),
    }[change]
    page._result(operation, changed)
    assert not page._averaging_active
    assert page._averager.count == 0
    assert page._averaging_source_trace is None
    assert page._averaged_trace is original
    assert page._reference_spectrum is old_reference
    assert "Averaging stopped" in page.info.text()


def test_new_average_can_use_new_grid_and_generation_after_failed_series(page):
    first = SpectrumTrace((1e6, 2e6), (-60., -60.), datetime.now(UTC), "TRAC1", configuration_generation=1)
    second = replace(first, frequencies_hz=(2e6, 3e6), configuration_generation=2, powers_dbm=(-50., -50.))
    page.average_count.setValue(2)
    page.start_averaging()
    page._result("unaveraged_sweep", first)
    page._result("unaveraged_sweep", second)
    page.start_averaging()
    page._result("unaveraged_sweep", second)
    page._result("unaveraged_sweep", second)
    assert not page._averaging_active
    assert page._averaged_trace.frequencies_hz == second.frequencies_hz
    assert page._averaged_trace.configuration_generation == 2
    assert page._averaged_trace.powers_dbm == pytest.approx((-50., -50.))


def test_live_average_ignores_inflight_poll_and_requests_qualified_sweep(page):
    sample = SpectrumTrace((1e6, 2e6), (-60., -60.), datetime.now(UTC), "TRAC1")
    page._timer.start(10000)
    page._fetch_pending = True
    page.start_averaging()
    assert page._resume_live_after_averaging
    page._controller.call.reset_mock()
    page._result("fetch_current_trace_fast", sample)
    assert page._averager.count == 0
    page._controller.call.assert_called_once_with("unaveraged_sweep", "TRAC1")
    page._result("unaveraged_sweep", sample)
    assert page._averager.count == 1


def test_cancel_restart_discards_old_inflight_sweep(page):
    old = SpectrumTrace((1e6, 2e6), (-10., -10.), datetime.now(UTC), "TRAC1")
    fresh = replace(old, powers_dbm=(-60., -60.))
    page.average_count.setValue(1)
    page.start_averaging()
    page.cancel_averaging()
    page._controller.call.reset_mock()
    page.start_averaging()
    page._controller.call.assert_not_called()
    page._result("unaveraged_sweep", old)
    assert page._averager.count == 0
    assert page._averaging_active
    assert page._latest_trace is not old
    page._controller.call.assert_called_once_with("unaveraged_sweep", "TRAC1")
    page._result("unaveraged_sweep", fresh)
    assert not page._averaging_active
    assert page._pending_fetch_discard is None
    assert page._averaged_trace.powers_dbm == pytest.approx(fresh.powers_dbm)


def test_continuous_reference_skips_duplicate_buffer_reads_and_resumes_live(page):
    page._timer.start(10000)
    page.reference_average_count.setValue(2)
    page.start_reference_averaging()
    page._controller.call.assert_called_once_with("fetch_current_trace_fast", "TRAC1")
    baseline = SpectrumTrace((1e6, 2e6), (-81., -81.), datetime.now(UTC), "TRAC1")
    first = replace(baseline, powers_dbm=(-80., -80.))
    second = replace(baseline, powers_dbm=(-70., -70.))
    for sample in (baseline, baseline, first, first):
        page._result("fetch_current_trace_fast", sample)
    assert page.average_progress.value() == 1
    assert page._reference_spectrum is None
    page._result("fetch_current_trace_fast", second)
    assert page._reference_spectrum.average_count == 2
    assert page._reference_spectrum.trace.powers_dbm == pytest.approx((-72.596373, -72.596373))
    assert page._timer.isActive()
    assert all(call.args[0] == "fetch_current_trace_fast" for call in page._controller.call.call_args_list)


def test_live_average_shares_floating_controls_and_uses_existing_live_stream(page, app):
    page.resize(1440, 900)
    page.show()
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10000)
    page._open_spectrum_window()
    floating = page._spectrum_window
    floating.resize(860, 580)
    app.processEvents()
    assert floating.live_average.isVisibleTo(floating) and floating.live_average.width() > 90
    assert page.show_live_frame.isVisibleTo(page)
    assert floating.show_live_frame.isVisibleTo(floating)
    assert floating.rect().contains(floating.show_live_frame.mapTo(floating, floating.show_live_frame.rect().bottomRight()))
    assert not page.show_live_frame.isEnabled() and not floating.show_live_frame.isEnabled()
    assert not floating.show_live_frame.isChecked()
    floating.live_average.click()
    app.processEvents()
    assert page.live_average_dialog.isVisible() and page.live_average_target.frames.isVisibleTo(page.live_average_dialog)
    page.live_average_target.frames.setValue(2)
    page.start_live_average.click()
    base = SpectrumTrace((1e6, 2e6), (-81., -81.), datetime.now(UTC), "TRAC1")
    for level in (-81., -80., -70.):
        page._result("fetch_current_trace_fast", replace(base, powers_dbm=(level, level)))
    page._controller.call.assert_not_called()
    assert not page._averaging_active and page._timer.isActive()
    assert page._latest_trace.powers_dbm == (-70., -70.)
    assert page._display_state.by_key["averaged"].values == pytest.approx((-72.596373,) * 2)
    assert floating.show_live_frame.isEnabled()
    assert "live_frame" not in page._display_state.by_key
    floating.show_live_frame.click()
    assert page.show_live_frame.isChecked()
    assert page._display_state.primary_key == "averaged"
    assert page._display_state.by_key["live_frame"].values == (-70., -70.)
    palette = plot_theme(tokens_for("light"))
    for plot in (page.spectrum_plot, floating.spectrum):
        average_curve = plot._curves["Averaged"]
        live_curve = plot._curves["Live frame"]
        assert average_curve.opts["pen"].color().name() == palette.measurement
        assert live_curve.opts["pen"].color().name() == palette.reference
        assert live_curve.zValue() < average_curve.zValue()
        assert 0 < live_curve.opacity() < average_curve.opacity()
    page._result("fetch_current_trace_fast", replace(base, powers_dbm=(-50., -50.)))
    assert page._display_state.by_key["averaged"].values == pytest.approx((-52.967086,) * 2)
    assert page._display_state.by_key["live_frame"].values == (-50., -50.)
    assert page._manual_trace_payload("live_frame") == (page._latest_trace, None, None, "none")
    assert page._manual_trace_payload("averaged")[0] is page._display_average_trace()
    page._set_reference(page._build_reference(base, kind="single", count=1))
    page.correction_controls.reference.setChecked(True)
    assert page._display_state.by_key["processed"].values == pytest.approx((28.032914,) * 2)
    overlay = page._display_state.by_key["live_frame"]
    assert overlay.unit == "dB" and overlay.values == (31., 31.)
    assert overlay.label == "Live − reference"
    assert page._manual_trace_payload("live_frame")[:3] == (page._latest_trace, (31., 31.), "dB")
    assert page._manual_trace_payload("processed")[0] is page._display_average_trace()
    assert page._display_state.primary_key == "processed"
    assert floating.spectrum.trace_selector.currentText() == "Processed"

    floating.spectrum.freeze.setChecked(True)
    page.show_live_frame.click()
    assert not floating.show_live_frame.isChecked()
    assert "live_frame" not in page._display_state.by_key
    assert floating.spectrum.trace_point_count("Live frame") == 2
    floating.spectrum.freeze.setChecked(False)
    assert floating.spectrum.trace_point_count("Live frame") == 0
    page.cancel_live_average.click()
    assert page._display_state.by_key["processed"].values == (31., 31.)
    assert page._timer.isActive()
    assert not page.show_live_frame.isEnabled() and not floating.show_live_frame.isEnabled()
    page._controller.call.assert_not_called()


def test_device_live_averaging_uses_one_result_per_block_and_discards_cancelled_result(page, app):
    page.resize(1440, 900)
    page.show()
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10_000)
    raw = SpectrumTrace((1e6, 2e6), (-80.0, -75.0), datetime.now(UTC), "TRAC1")
    page._show_trace(raw)
    page._show_anritsu_hardware_info()
    assert "trace_name: TRAC1" in page._trace_diagnostics_dialog.raw_text.toPlainText()
    page._open_spectrum_window()
    floating = page._spectrum_window
    floating.live_average.click()
    app.processEvents()
    target = page.live_average_target
    assert target.backend.isVisibleTo(page.live_average_dialog)
    assert page.live_average_dialog.rect().contains(
        target.backend.mapTo(page.live_average_dialog, target.backend.rect().bottomRight())
    )
    target.mode.setCurrentIndex(target.mode.findData("seconds"))
    target.seconds.setValue(120)
    target.backend.setCurrentIndex(target.backend.findData("instrument"))
    assert target.target() == (0, 5)
    target.backend.setCurrentIndex(target.backend.findData("application"))
    assert target.target() == (0, 120)
    target.backend.setCurrentIndex(target.backend.findData("instrument"))
    target.mode.setCurrentIndex(target.mode.findData("frames"))
    assert target.mode.currentText() == "Sweeps"
    target.frames.setValue(50)
    page.start_live_average.click()
    page.fetch_live()
    operation, payload = page._controller.call.call_args.args
    assert operation == "instrument_average_live" and payload.frame_count == 50
    assert page._controller.call.call_count == 1

    mean = replace(raw, powers_dbm=(-66.0, -64.0), trace_name="TRAC1_ANRITSU_LAV50",
                   instrument_average=InstrumentAverageProvenance(50, .006, requested_sweep_count=50))
    page._result(operation, InstrumentAverageResult(mean, 50, 0.006))
    assert page._display_state.selected.values == mean.powers_dbm
    assert page._latest_trace is raw
    assert "individual TRAC1 frames" in page._trace_diagnostics_dialog.raw_text.toPlainText()
    page._trace_diagnostics_dialog.close()
    page._show_anritsu_hardware_info()
    assert "individual TRAC1 frames" in page._trace_diagnostics_dialog.raw_text.toPlainText()
    assert page._manual_trace_payload("averaged")[0] is mean
    assert not floating.show_live_frame.isEnabled()
    assert not page.correction_controls.power_average.isEnabled()
    assert not page.cleanup_filters["emi_reject"].isEnabled()
    page._set_reference(page._build_reference(raw, kind="single", count=1))
    page.correction_controls.reference.setChecked(True)
    assert page._display_state.by_key["processed"].values == (14.0, 11.0)
    assert not page.cleanup_filters["emi_reject"].isEnabled()

    page.fetch_live()
    assert page._controller.call.call_args.args[0] == operation
    page.cancel_live_average.click()
    late = replace(mean, powers_dbm=(-80., -79.))
    page._result(operation, InstrumentAverageResult(late, 50, 0.006))
    assert page._rolling_average_trace is mean
    assert page._latest_trace is raw
    page.fetch_live()
    assert page._controller.call.call_args.args[0] == "fetch_current_trace_fast"
    fresh = replace(raw, powers_dbm=(-70., -68.))
    page._result("fetch_current_trace_fast", fresh)
    assert page._rolling_average_trace is None
    assert page._latest_trace is fresh


def test_pending_anritsu_block_does_not_offer_an_earlier_raw_or_averaged_trace(page):
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10_000)
    raw = SpectrumTrace((1e6, 2e6), (-81., -80.), datetime.now(UTC), "TRAC1")
    page._show_trace(raw)
    page._averaged_trace = replace(raw, powers_dbm=(-75., -75.), trace_name="TRAC1_AVG2")
    page.show_average.setChecked(True)
    page.fetch_live()

    target = page.live_average_target
    target.backend.setCurrentIndex(target.backend.findData("instrument"))
    page.start_live_average.click()
    assert page._display_state.selected is None
    assert page.analysis_source.findData("raw") == -1
    assert page.analysis_source.findData("averaged") == -1
    assert not page.use_current_reference.isEnabled()

    page._result("fetch_current_trace_fast", replace(raw, powers_dbm=(-78., -78.)))
    assert page._display_state.selected is None
    page.fetch_live()
    assert page._controller.call.call_args.args[0] == "instrument_average_live"


@pytest.mark.parametrize("theme,window_size,mode,expected_count", [
    ("light", (1440, 900), "frames", 4), ("dark", (860, 620), "seconds", 10),
])
def test_rendered_live_and_floating_share_native_worker_results(
    app, tmp_path, theme, window_size, mode, expected_count,
):
    settings = simulation_settings()
    adapter = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
    captures = []

    def dispatch(device, operation, payload):
        if operation == "instrument_average_live":
            captures.append((QThread.currentThread(), payload))
        return MODULE.dispatch(device, operation, payload)

    controller = DeviceController(adapter, dispatcher=dispatch)
    widget = AnritsuPage(controller, settings, single_sweep_available=True)
    original_theme = "dark" if isDarkTheme() else "light"
    apply_application_theme(app, theme)
    try:
        widget.resize(*window_size)
        widget.show()
        controller.call("connect")
        wait_until(app, lambda: widget._page_state is AnritsuPageState.IDLE)
        widget.refresh.setValue(5_000)
        widget.live.click()
        wait_until(app, widget._timer.isActive)
        widget._open_spectrum_window()
        floating = widget._spectrum_window
        floating.resize(820, 560)
        floating.live_average.click()
        dialog = widget.live_average_dialog
        if theme == "dark":
            dialog.resize(440, 340)
        controls = widget.live_average_target
        controls.backend.setCurrentIndex(controls.backend.findData("instrument"))
        controls.mode.setCurrentIndex(controls.mode.findData(mode))
        controls.frames.setValue(4)
        controls.seconds.setValue(1)
        app.processEvents()

        editor = controls.frames if mode == "frames" else controls.seconds
        for control in (controls.backend, editor, widget.start_live_average, widget.cancel_live_average):
            assert control.isVisibleTo(dialog) and control.height() > 0
            assert dialog.rect().contains(control.mapTo(dialog, control.rect().bottomRight()))
        assert dialog.grab().save(str(tmp_path / f"anritsu-target-{theme}.png"))

        widget.start_live_average.click()
        wait_until(app, lambda: widget._rolling_average_trace is not None, timeout=15)
        mean = widget._rolling_average_trace
        assert mean.instrument_average.sweep_count == expected_count
        assert widget._display_state.selected.values == mean.powers_dbm
        assert floating.spectrum.trace_point_count("Averaged") == len(mean.powers_dbm)
        assert "Anritsu block" in floating.status.text()
        assert "raw spectrum" not in widget.correction_controls.summary.text()
        assert widget.grab().save(str(tmp_path / f"anritsu-live-{theme}.png"))
        assert floating.grab().save(str(tmp_path / f"anritsu-floating-{theme}.png"))
        thread, request = captures[0]
        assert thread is controller._thread and thread is not app.thread()
        if mode == "frames":
            assert request.frame_count == expected_count and request.duration_s is None
        else:
            assert request.duration_s == 1 and request.frame_count is None
        floating.stop_live.click()
        wait_until(app, lambda: widget._page_state is AnritsuPageState.IDLE)
        assert not widget._timer.isActive()
    finally:
        assert widget.shutdown_analysis()
        widget.close()
        widget.deleteLater()
        assert controller.close()
        apply_application_theme(app, original_theme)
        app.processEvents()


def test_stopping_live_keeps_the_last_completed_anritsu_mean_available_to_save(page):
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10_000)
    raw = SpectrumTrace((1e6, 2e6), (-81., -80.), datetime.now(UTC), "TRAC1")
    page._show_trace(raw)
    target = page.live_average_target
    target.backend.setCurrentIndex(target.backend.findData("instrument"))
    target.frames.setValue(4)
    page.start_live_average.click()
    mean = replace(raw, powers_dbm=(-74., -73.), trace_name="TRAC1_ANRITSU_LAV4")
    page._result("instrument_average_live", InstrumentAverageResult(mean, 4, .025))
    page.fetch_live()
    page.toggle_live()
    page._result("instrument_average_live",
                 InstrumentAverageResult(replace(mean, powers_dbm=(-80., -80.)), 4, .025))
    page._result("stop_live", None)

    assert page._display_state.by_key["averaged"].values == mean.powers_dbm
    assert page._manual_trace_payload("averaged")[0].powers_dbm == mean.powers_dbm
    with pytest.raises(ValueError, match="individual spectra"):
        page._manual_trace_payload("raw")
    assert not page._timer.isActive()
    assert page._page_state is AnritsuPageState.IDLE

    page.toggle_live()
    page._result("start_live", AnritsuConfigurationSnapshot(1e6, 2e6, -10., 1001, "SPECT"))
    page.fetch_live()
    page._result("fetch_current_trace_fast", raw)
    assert page._display_state.by_key["raw"].values == raw.powers_dbm


def test_stopping_raw_live_discards_pending_poll_before_stop_acknowledgement(page):
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10_000)
    raw = SpectrumTrace((1e6, 2e6), (-81., -80.), datetime.now(UTC), "TRAC1")
    page._show_trace(raw)
    page.fetch_live()
    page.toggle_live()
    page._result("fetch_current_trace_fast", replace(raw, powers_dbm=(-70., -70.)))
    assert page._latest_trace is raw
    assert page._page_state is AnritsuPageState.STOPPING
    page._result("stop_live", None)
    page.fetch_live()
    assert [call.args[0] for call in page._controller.call.call_args_list] == [
        "fetch_current_trace_fast", "stop_live",
    ]


@pytest.mark.parametrize("completion", ["success", "error"])
def test_recipe_preview_survives_a_late_manual_response_after_release(page, completion):
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10_000)
    page.fetch_live()
    page.set_execution_controlled(True)
    page._show_execution_trace({"frequency_hz": (1e6, 2e6), "power_dbm": (-50., -50.)})
    page.set_execution_controlled(False)
    if completion == "success":
        page._result("fetch_current_trace_fast",
                     SpectrumTrace((1e6, 2e6), (-81., -80.), datetime.now(UTC), "TRAC1"))
    else:
        page._error("fetch_current_trace_fast", "Manual read rejected during recipe ownership")
    assert page._latest_trace_is_execution_preview
    assert page._display_state.selected.values == (-50., -50.)
    assert "Run Engine confirmed" in page.info.text()
    assert page._page_state is AnritsuPageState.IDLE
    assert not page.use_current_reference.isEnabled()
    assert page._controller.call.call_count == 1


@pytest.mark.parametrize("method,source_label", [
    ({"acquisition_mode": "single_sweep"}, "PyLab triggered average"),
    ({"acquisition_mode": "continuous_live"}, "PyLab Live average"),
    ({"averaging_source": "instrument", "instrument_average": {
        "trace_storage_mode": "LAV", "sweep_count": 4, "sweep_time_s": .025,
    }}, "Anritsu average"),
])
def test_recipe_read_only_device_preview_names_acquisition_source(page, method, source_label):
    page._set_page_state(AnritsuPageState.IDLE)
    page.cleanup_filters["emi_reject"].setChecked(True)
    page.set_execution_controlled(True)
    page._show_execution_trace({
        "frequency_hz": (1e6, 2e6), "power_dbm": (-50., -50.), "average_count": 4, **method,
    })
    assert source_label in page.info.text() and "4 sweeps" in page.info.text()
    assert not page.use_current_reference.isEnabled()
    assert "emi_reject" not in page._selected_cleanup_modes()
    if source_label == "Anritsu average":
        assert page._display_state.selected.label == source_label


def test_shell_emergency_stop_discards_an_inflight_anritsu_block_before_worker_ack(page):
    from app.ui.shell.main_window import MainWindow

    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10_000)
    target = page.live_average_target
    target.backend.setCurrentIndex(target.backend.findData("instrument"))
    page.start_live_average.click()
    operation, payload = page._controller.call.call_args.args
    mean = SpectrumTrace((1e6, 2e6), (-70., -70.), datetime.now(UTC),
                         f"TRAC1_ANRITSU_LAV{payload.frame_count}")
    station = SimpleNamespace(
        anritsu_page=page, _settings=loaded_settings(), _simulation=True,
        _controllers={"anritsu": page._controller},
        _run_controller=Mock(), quick_control_coordinator=Mock(), _audit_record=Mock(),
        _log=Mock(), _refresh_safety_strip=Mock(),
    )

    MainWindow._emergency_off_all(station)
    station._run_controller.request_emergency_stop.assert_called_once_with(station._settings, simulation=True)
    assert not page._timer.isActive()
    page._result(operation, InstrumentAverageResult(mean, payload.frame_count, .025))
    assert page._display_state.selected is None
    assert page._page_state is AnritsuPageState.STOPPING
    page._result("emergency_off", True)
    assert page._page_state is AnritsuPageState.IDLE


def test_device_reference_stores_actual_count_without_double_averaging(page, tmp_path):
    target = page.reference_average_target
    target.backend.setCurrentIndex(target.backend.findData("instrument"))
    target.frames.setValue(50)
    page.start_reference_averaging()
    operation, payload = page._controller.call.call_args.args
    assert operation == "instrument_average" and payload.frame_count == 50

    trace = SpectrumTrace((1e6, 2e6), (-66.0, -64.0), datetime.now(UTC), "TRAC1_ANRITSU_LAV50",
                          instrument_average=InstrumentAverageProvenance(50, .006, requested_sweep_count=50))
    page._result(operation, InstrumentAverageResult(trace, 50, 0.006))
    assert page._reference_spectrum.average_count == 50
    assert page._reference_spectrum.trace is trace
    assert page._averager.count == 0
    destination = tmp_path / "instrument-reference.h5"
    ReferenceHdf5Store.save(destination, page._reference_spectrum)
    reopened = ReferenceHdf5Store.load(destination)
    assert reopened.average_count == 50
    assert reopened.trace.trace_name == "TRAC1_ANRITSU_LAV50"
    assert reopened.trace.instrument_average == trace.instrument_average


def test_device_reference_retains_confirmed_configuration_and_waits_for_compatible_signal(page):
    settings = simulation_settings()
    adapter = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
    adapter.connect()
    try:
        full, advanced = adapter.read_acquisition_configuration()
        page._set_page_state(AnritsuPageState.IDLE)
        controls = page.reference_average_target
        controls.backend.setCurrentIndex(controls.backend.findData("instrument"))
        controls.frames.setValue(4)
        page.start_reference_averaging()
        operation, request = page._controller.call.call_args.args
        capture = MODULE.dispatch(adapter, operation, request)
    finally:
        adapter.disconnect()

    page._last_configuration = AnritsuConfigurationSnapshot(full.start_hz, full.stop_hz, 5., full.points, "SPECT")
    page._last_advanced_configuration = replace(advanced, rbw_hz=advanced.rbw_hz * 2)
    sample = replace(capture.trace, powers_dbm=tuple(value + 3 for value in capture.trace.powers_dbm),
                     trace_name="TRAC1", instrument_average=None)
    page._show_trace(sample)
    page._result(operation, capture)
    reference = page._reference_spectrum
    assert reference.reference_level_dbm == full.reference_level_dbm
    assert reference.rbw_hz == advanced.rbw_hz
    assert reference.instrument_average.sweep_count == 4
    assert page._preview_input_trace is None
    assert "4 sweeps" in page.reference_status.text()

    page._show_trace(sample)
    page.correction_controls.reference.setChecked(True)
    assert page._display_state.selected.key == "processed"
    assert page._display_state.selected.values == pytest.approx((3.,) * full.points)


def test_device_live_resumes_after_device_reference_without_mixing_pending_blocks(page):
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10_000)
    for target, frames in ((page.live_average_target, 50), (page.reference_average_target, 32)):
        target.backend.setCurrentIndex(target.backend.findData("instrument"))
        target.frames.setValue(frames)
    page.start_live_average.click()
    page.start_reference_averaging()
    trace = SpectrumTrace((1e6, 2e6), (-66., -64.), datetime.now(UTC), "TRAC1_ANRITSU_LAV50")
    page._result("instrument_average_live", InstrumentAverageResult(trace, 50, .006))
    operation, target = page._controller.call.call_args.args
    assert operation == "instrument_average" and target.frame_count == 32
    reference_trace = replace(trace, trace_name="TRAC1_ANRITSU_LAV32",
                              instrument_average=InstrumentAverageProvenance(32, .006, requested_sweep_count=32))
    page._result(operation, InstrumentAverageResult(reference_trace, 32, .006))
    assert page._reference_spectrum.trace is reference_trace
    assert page._timer.isActive() and page._rolling_average_trace is None
    page.fetch_live()
    operation, target = page._controller.call.call_args.args
    assert operation == "instrument_average_live" and target.frame_count == 50


def test_abort_failure_after_pending_hardware_reference_releases_ui_without_restarting_live(page):
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10_000)
    target = page.reference_average_target
    target.backend.setCurrentIndex(target.backend.findData("instrument"))
    target.frames.setValue(50)
    page.start_reference_averaging()
    page.abort_button.click()
    trace = SpectrumTrace((1e6, 2e6), (-66., -64.), datetime.now(UTC), "TRAC1_ANRITSU_LAV50")
    page._result("instrument_average", InstrumentAverageResult(trace, 50, .006))
    page._controller.call.reset_mock()
    page.fetch_live()
    page._controller.call.assert_not_called()
    page._error("abort_acquisition", "Abort verification timed out")
    assert page._page_state is AnritsuPageState.ERROR
    assert not page._averaging_active and not page._timer.isActive()
    assert page._reference_spectrum is None
    assert page.capture_reference.isEnabled()


def test_abort_ignores_start_live_result_already_in_flight(page):
    page.toggle_live()
    assert page._page_state is AnritsuPageState.STARTING_LIVE
    page.abort_button.click()
    page._result("start_live", AnritsuConfigurationSnapshot(1e6, 2e6, -10.0, 1001, "SPECT"))
    assert page._page_state is AnritsuPageState.STOPPING
    assert not page._timer.isActive()
    page._result("abort_acquisition", True)
    assert page._page_state is AnritsuPageState.IDLE


def test_abort_single_reference_clears_intent_for_next_spectrum(page):
    page.acquire_reference_once()
    page.abort_button.click()
    trace = SpectrumTrace((1e6, 2e6), (-66., -64.), datetime.now(UTC), "TRAC1")
    page._result("unaveraged_sweep", trace)
    page._result("abort_acquisition", True)
    page.average_count.setValue(1)
    page.start_averaging()
    page._result("unaveraged_sweep", trace)
    assert page._reference_spectrum is None
    assert page._averaged_trace.powers_dbm == trace.powers_dbm


def test_timed_reference_uses_elapsed_seconds_and_records_actual_count(page, tmp_path):
    destination = "reference"
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10000)
    target = page.reference_average_target
    target.mode.setCurrentIndex(target.mode.findData("seconds"))
    target.seconds.setValue(180)
    base = SpectrumTrace((1e6, 2e6), (-81., -81.), datetime.now(UTC), "TRAC1")
    with patch("app.devices.anritsu_ms2830a.ui.page.time") as clock:
        clock.monotonic.return_value = 20
        page._start_temporal_averaging(destination)
        page._result("fetch_current_trace_fast", base)
        assert not page._averaging_deadline_timer.isActive()
        clock.monotonic.return_value = 25
        page._result("fetch_current_trace_fast", replace(base, powers_dbm=(-80., -80.)))
        assert page._averaging_deadline_timer.interval() == 180_000
        clock.monotonic.return_value = 84
        page._result("fetch_current_trace_fast", replace(base, powers_dbm=(-70., -70.)))
        assert page._averaging_active and page._averager.count == 2
        clock.monotonic.return_value = 205
        page._finish_timed_average()
        assert not page._averaging_active and page._timer.isActive()
    reference = page._reference_spectrum
    assert reference.average_count == 2
    path = tmp_path / "reference.h5"
    ReferenceHdf5Store.save(path, reference)
    restored = ReferenceHdf5Store.load(path)
    assert restored.average_count == 2
    trace = restored.trace
    assert trace.trace_name.endswith("2_180s")
    assert trace.powers_dbm == pytest.approx((-72.596373,) * 2)


def test_live_average_evicts_old_reads_and_keeps_rolling_past_duration(page):
    page._set_page_state(AnritsuPageState.LIVE)
    page._timer.start(10000)
    page.live_average_target.mode.setCurrentIndex(page.live_average_target.mode.findData("seconds"))
    page.live_average_target.seconds.setValue(180)
    base = SpectrumTrace((1e6, 2e6), (-80., -80.), datetime.now(UTC), "TRAC1")
    page.start_live_average.click()
    with patch("app.devices.anritsu_ms2830a.ui.page.time") as clock:
        for received_at, level in ((20, -80.), (30, -80.), (100, -70.), (205, -60.)):
            clock.monotonic.return_value = received_at
            page._result("fetch_current_trace_fast", replace(base, powers_dbm=(level, level)))
        # The identical read at 30 s is still in the window, the one at 20 s has expired.
        assert page._display_state.by_key["averaged"].values == pytest.approx((-64.317983,) * 2)
        assert "3" in page.live_average_status.text()
        clock.monotonic.return_value = 400
        new_context = replace(base, powers_dbm=(-50., -50.), configuration_generation=1)
        page._result("fetch_current_trace_fast", new_context)
        assert page._display_state.by_key["averaged"].values == pytest.approx((-50., -50.))
    assert page._timer.isActive()
    assert not page._averaging_deadline_timer.isActive()


def test_cancelling_reference_during_start_closes_temporary_live_without_polling(page):
    page.start_reference_averaging()
    page.cancel_averaging()
    page._result("start_live", AnritsuConfigurationSnapshot(1e6, 3e6, -10., 1001, "SPECT"))
    page._result("stop_live", None)
    assert [call.args[0] for call in page._controller.call.call_args_list] == ["start_live", "stop_live"]
    assert page._reference_spectrum is None and not page._timer.isActive()


def test_cancel_does_not_replace_completed_trace_or_queue_manual_reference(page):
    old = SpectrumTrace((1e6, 2e6), (-10., -10.), datetime.now(UTC), "TRAC1")
    page._latest_trace = replace(old, powers_dbm=(-60., -60.))
    completed = page._latest_trace
    page.start_averaging()
    page.cancel_averaging()
    page._controller.call.reset_mock()
    page.read_once()
    page.acquire_reference_once()
    page._controller.call.assert_not_called()
    page._result("unaveraged_sweep", old)
    assert page._latest_trace is completed
    assert not page._fetch_pending
    page.read_once()
    page._controller.call.assert_called_once_with("single_sweep", "TRAC1")


def test_failed_cancelled_request_clears_discard_state_and_stops_new_series(page):
    page.start_averaging()
    page.cancel_averaging()
    page.start_averaging()
    page._error("unaveraged_sweep", "transport timeout")
    assert page._pending_fetch_discard is None
    assert not page._fetch_pending
    assert not page._averaging_active
    # Communication failure is reported, not silently retried in a new series.
    assert "Averaging stopped" in page.info.text()
    page.average_count.setValue(1)
    page.start_averaging()
    sample = SpectrumTrace((1e6, 2e6), (-60., -60.), datetime.now(UTC), "TRAC1")
    page._result("unaveraged_sweep", sample)
    assert page._averaged_trace.powers_dbm == pytest.approx(sample.powers_dbm)


def test_cancel_waits_for_sweep_restoration_before_restarting_live(page):
    page._timer.start(10000)
    page.start_averaging()
    page._controller.call.reset_mock()

    page.cancel_averaging()

    assert not page._timer.isActive()
    page._controller.call.assert_not_called()
    sample = SpectrumTrace((1e6, 2e6), (-60., -60.), datetime.now(UTC), "TRAC1")
    page._result("unaveraged_sweep", sample)
    page._controller.call.assert_called_once_with("start_live", True)
    assert not page._timer.isActive()


def test_live_keeps_polling_while_analyser_is_building_the_first_trace(page):
    adapter = AnritsuAdapter(loaded_settings())
    page._timer.start(10000)
    page._fetch_pending = True

    with pytest.raises(DeviceError) as error:
        adapter._decode_binary_trace("TRAC1", [-999.0, -999.0], (2, 1e6, 2e6))
    page._error("fetch_current_trace_fast", str(error.value))

    assert page._timer.isActive()
    assert not page._fetch_pending
    assert page._stale_frame_count == 1
