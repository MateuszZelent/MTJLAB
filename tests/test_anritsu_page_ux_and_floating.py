"""Focused regression tests for AnritsuPage UX, layout geometry, floating taskbar windows, Live pipeline, and state safety."""

# ruff: noqa: F811
from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import numpy as np
from PySide6.QtCore import QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox
from qfluentwidgets import PrimaryPushButton, PushButton, TransparentToolButton
from shiboken6 import isValid

from app.devices.anritsu_ms2830a import (
    AdvancedSpectrumSnapshot,
    AnritsuConfigurationSnapshot,
    AnritsuFullConfigurationReadback,
    InstrumentAverageTarget,
    SpectrumTrace,
)
from app.devices.anritsu_ms2830a.ui.page import (
    AnritsuAdvancedSpectrumPanel,
    AnritsuPage,
    AnritsuPageState,
    _SpectrogramBuffer,
)
from app.spectrum import SpectrumCleanupResult
from app.storage.reference_store import ReferenceHdf5Store
from app.ui.shell import MainWindow
from tests.helpers import loaded_settings
from tests.shell_test_isolation import (  # noqa: F401
    isolated_shell_persistence,
    shell_qt_application,
)
from tests.test_main_window import SETTINGS_TEMPLATE, synthetic_anritsu_peaks


def _make_controller(*, connected: bool = False) -> MagicMock:
    controller = MagicMock()
    controller.visa_address = "SIM::ANRITSU::INSTR"
    controller.is_connected = connected
    return controller


def _sample_trace(offset_db: float = 0.0, points: int = 201) -> SpectrumTrace:
    freqs = tuple(np.linspace(1.0e9, 1.02e9, points).tolist())
    powers = tuple((-60.0 + offset_db + 20.0 * np.exp(-(((np.linspace(-1.0, 1.0, points)) / 0.15) ** 2))).tolist())
    return SpectrumTrace(
        frequencies_hz=freqs,
        powers_dbm=powers,
        acquired_at_utc=datetime(2026, 11, 24, 12, 34, 56, tzinfo=UTC),
        trace_name="TRAC1",
    )


def _sample_readback() -> AnritsuFullConfigurationReadback:
    return AnritsuFullConfigurationReadback(
        start_hz=1e6,
        stop_hz=10e6,
        center_hz=5.5e6,
        span_hz=9e6,
        reference_level_dbm=0.0,
        points=1001,
        rbw_auto=True,
        rbw_hz=30e3,
        vbw_auto=True,
        vbw_mode="VID",
        vbw_hz=30e3,
        detector="NORM",
        attenuation_auto=True,
        attenuation_db=10.0,
        preamplifier_enabled=False,
        sweep_time_auto=True,
        sweep_time_s=0.05,
        average_count=1,
        continuous_sweep=True,
        instrument_mode="SPECT",
    )


def _mapped_rect(widget, root) -> QRect:
    return QRect(widget.mapTo(root, QPoint(0, 0)), widget.size())


def _process(app: QApplication, rounds: int = 8) -> None:
    for _ in range(rounds):
        app.processEvents()


def _wait_until(app: QApplication, predicate, *, timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def test_floating_windows_are_independent_taskbar_windows_and_cleanup_on_close(
    shell_qt_application,
) -> None:
    window = MainWindow(SETTINGS_TEMPLATE, simulation=True)
    window.show()
    window.resize(1440, 900)
    window._navigate_to("anritsu")
    page = window.anritsu_page
    page._device_state_changed("verified")
    page._show_trace(synthetic_anritsu_peaks(), update_controls=True)
    _process(shell_qt_application)

    # Open floating spectrum, spectrogram, diagnostics, advanced, and readback windows.
    page._open_spectrum_window()
    page._open_spectrogram_window()
    page._show_anritsu_hardware_info()
    page._show_advanced_spectrum_dialog()
    page._show_full_readback_dialog(_sample_readback())
    _process(shell_qt_application)

    floating_windows = [
        page._spectrum_window,
        page._spectrogram_window,
        page._trace_diagnostics_dialog,
        page._advanced_dialog_instance,
        page._readback_dialog,
    ]
    assert all(w is not None for w in floating_windows)

    assert page._spectrum_window.windowTitle() == "Anritsu MS2830A — Spectrum"
    assert page._spectrogram_window.windowTitle() == "Anritsu MS2830A — Spectrogram"
    assert page._trace_diagnostics_dialog.windowTitle() == "Anritsu MS2830A — Trace diagnostics"
    # UI-M4: no "read-only display" contradiction beside Start Live button.
    assert "read-only" not in page._spectrum_window.status.text().lower()

    for floating in floating_windows:
        assert floating.parentWidget() is None
        flags = floating.windowFlags()
        assert bool(flags & Qt.WindowType.Window)
        assert bool(flags & Qt.WindowType.WindowMinimizeButtonHint)
        assert bool(flags & Qt.WindowType.WindowMaximizeButtonHint)
        assert bool(flags & Qt.WindowType.WindowCloseButtonHint)
        if sys.platform == "win32" and QApplication.platformName() == "windows":
            import win32con
            import win32gui

            hwnd = int(floating.winId())
            assert win32gui.GetWindow(hwnd, win32con.GW_OWNER) == 0
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            assert bool(ex_style & win32con.WS_EX_APPWINDOW)
            assert not bool(ex_style & win32con.WS_EX_TOOLWINDOW)

    # Minimize and restore via clicking/re-opening the floating spectrum window.
    spec = page._spectrum_window
    spec.showMinimized()
    _process(shell_qt_application)
    page._open_spectrum_window()
    _process(shell_qt_application)
    assert not spec.isMinimized()
    assert spec.isVisible()

    # Close the plot scene before Qt destroys its parentless floating window.
    floating_workbench = spec.spectrum
    native_plot = floating_workbench.plot
    assert window.close()
    assert floating_workbench._cleaned_up
    shell_qt_application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(native_plot)
    _process(shell_qt_application)
    for floating in floating_windows:
        try:
            assert not floating.isVisible()
        except RuntimeError:
            # Already deleted via deleteLater — also proves clean teardown.
            pass


def test_layout_geometry_reclaims_plot_area_and_eliminates_overlap(
    shell_qt_application,
) -> None:
    window = MainWindow(SETTINGS_TEMPLATE, simulation=True)
    try:
        window.show()
        window._set_theme_mode("light", persist=False)
        window._navigate_to("anritsu")
        page = window.anritsu_page
        page._device_state_changed("verified")
        page._show_trace(synthetic_anritsu_peaks(), update_controls=True)

        # 1440x900 drawer closed -> plot area >= 55% of page area, zero overlaps.
        window.resize(1440, 900)
        page.toggle_acquisition_controls.setChecked(False)
        _process(shell_qt_application, 12)

        page_area = page.width() * page.height()
        plot_rect = _mapped_rect(page.spectrum_plot, page)
        plot_ratio = (plot_rect.width() * plot_rect.height()) / max(1, page_area)
        assert plot_ratio >= 0.55, f"Expected plot >= 55% of page, got {plot_ratio:.1%}"

        # Drawer open at 1440x900 -> plot remains generous and drawer has no horizontal scroll.
        page.toggle_acquisition_controls.setChecked(True)
        _process(shell_qt_application, 12)
        drawer_plot_rect = _mapped_rect(page.spectrum_plot, page)
        assert drawer_plot_rect.width() >= 600
        assert drawer_plot_rect.height() >= 280
        assert page.control_scroll.horizontalScrollBar().maximum() == 0

        # 1024x700 -> zero widget overlaps inside hero_card and single-row Correction card.
        page.toggle_acquisition_controls.setChecked(False)
        window.resize(1024, 700)
        _process(shell_qt_application, 12)

        hero_children = [
            page.simulation_badge,
            page.live_indicator,
            page.quick_controls_button,
            page.live,
            page.single,
            page.abort_button,
            page.toggle_acquisition_controls,
            page.record_spectra,
        ]
        visible_rects = [
            (w.objectName() or w.text(), _mapped_rect(w, page.hero_card))
            for w in hero_children
            if w.isVisibleTo(page.hero_card)
        ]
        for i in range(len(visible_rects)):
            for j in range(i + 1, len(visible_rects)):
                name_a, rect_a = visible_rects[i]
                name_b, rect_b = visible_rects[j]
                inter = rect_a.intersected(rect_b)
                assert inter.width() <= 1 or inter.height() <= 1, (
                    f"Overlap in hero_card at 1024x700: {name_a} vs {name_b} ({inter})"
                )

        # Narrow labels disambiguated (UI-M2).
        assert page.correction_controls.configure_background.text() != page.correction_controls.configure_reference.text()
        assert "BG" in page.correction_controls.configure_background.text()
        assert "Ref" in page.correction_controls.configure_reference.text()

        # Switching to Spectrogram tab hides spectrum-only controls (UI-M2).
        page.analysis_tabs.setCurrentIndex(1)
        _process(shell_qt_application)
        assert not page.quick_comparison_group.isVisibleTo(page)
        assert not page.show_absolute.isVisibleTo(page)
        assert not page.auto_peak_detection.isVisibleTo(page)
        assert not page.open_peak_table.isVisibleTo(page)
    finally:
        window.close()
        _process(shell_qt_application)


def test_fluent_ux_states_hierarchy_tab_order_and_copy(shell_qt_application) -> None:
    page = AnritsuPage(_make_controller(connected=False), loaded_settings(), single_sweep_available=True)
    try:
        # UI-H3: starts DISCONNECTED and disables hardware acquisition controls.
        assert page._page_state == AnritsuPageState.DISCONNECTED
        assert not page.live.isEnabled()
        assert not page.single.isEnabled()
        assert not page.abort_button.isEnabled()
        assert not page.configure_button.isEnabled()
        assert not page.read_configuration.isEnabled()
        assert not page.acquire_average.isEnabled()

        # Unlocks cleanly on verified connect.
        page._device_state_changed("verified")
        assert page._page_state == AnritsuPageState.IDLE
        assert page.live.isEnabled()
        assert page.single.isEnabled()
        assert page.abort_button.isEnabled()

        # UI-H4: Single solid-blue primary CTA in top/plot chrome.
        assert isinstance(page.live, PrimaryPushButton)
        assert isinstance(page.toggle_acquisition_controls, PushButton)
        assert not isinstance(page.toggle_acquisition_controls, PrimaryPushButton)
        assert isinstance(page.open_peak_table, PushButton)
        assert not isinstance(page.open_peak_table, PrimaryPushButton)
        assert isinstance(page.acquire_average, PushButton)
        assert not isinstance(page.acquire_average, PrimaryPushButton)

        # UI-M1: Fluent INFO button with >= 28x28 target.
        assert isinstance(page.hardware_info_button, TransparentToolButton)
        assert page.hardware_info_button.width() >= 28
        assert page.hardware_info_button.height() >= 28

        # UI-M2: Disambiguated setup labels at full desktop width.
        assert page.correction_controls.configure_background.text() == "BG setup…"
        assert page.correction_controls.configure_reference.text() == "Ref setup…"

        # UI-M3: AnritsuAdvancedSpectrumPanel margins restored.
        panel = AnritsuAdvancedSpectrumPanel()
        margins = panel.layout().contentsMargins()
        assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (16, 14, 16, 14)

        # UI-L1: SIMULATED badge visibility, clean UTC footer, clean protocol lock copy.
        assert not page.simulation_badge.isVisible()
        page.set_manual_archive_context(simulation=True)
        assert not page.simulation_badge.isHidden()
        page.set_manual_archive_context(simulation=False)
        assert page.simulation_badge.isHidden()

        page._show_trace(_sample_trace())
        assert "12:34:56 UTC" in page.info.text()
        assert "2026-11-24T" not in page.info.text()

        _ = page._advanced_dialog
        assert "WRITE LOCKED — protocol=" not in page.advanced_protocol_status.text()

        # UI-L2: standard form typography (not 16px) & accessibleName on average_count.
        assert page.start.font().pixelSize() != 16
        assert page.average_count.accessibleName()
        assert page.live.toolTip()
        assert page.open_floating_spectrum.toolTip()

        # UI-H5: Explicit focus chain connects acquisition -> setup -> filters -> plot.
        assert page.quick_controls_button.nextInFocusChain() is page.live
        assert page.live.nextInFocusChain() is page.single
        assert page.single.nextInFocusChain() is page.abort_button
    finally:
        assert page.shutdown_analysis()
        page.close()
        page.deleteLater()
        _process(shell_qt_application)


def test_lazy_dialogs_and_single_redraw_raw_live_pipeline(shell_qt_application) -> None:
    page = AnritsuPage(_make_controller(connected=True), loaded_settings(), single_sweep_available=True)
    try:
        # PERF-C2: Heavy dialogs are NOT constructed during __init__.
        assert page._advanced_dialog_instance is None
        assert page._sweep_timing_dialog_instance is None
        assert page._reference_dialog_instance is None
        assert page._live_average_dialog_instance is None
        assert page._stripe_dialog_instance is None
        assert page._recording_dialog_instance is None
        assert page._correction_workspace is None

        # PERF-C4: Raw Live frame performs exactly 1 redraw and 0 worker submits.
        page.auto_peak_detection.setChecked(False)
        trace = _sample_trace()
        with (
            patch.object(page._analysis_controller, "submit") as submit,
            patch.object(page.spectrum_plot, "set_trace", wraps=page.spectrum_plot.set_trace) as set_trace,
        ):
            page._show_trace(trace, update_controls=False)
            _process(shell_qt_application)
            submit.assert_not_called()
            assert set_trace.call_count == 1
            assert page._analysis_raw_snapshot is trace

        # PERF-H3: _SpectrogramBuffer.frame_snapshot returns read-only ndarray without tuple copy.
        buf = _SpectrogramBuffer()
        buf.append(_sample_trace(0.0))
        buf.append(_sample_trace(1.0))
        snap1 = buf.frame_snapshot(30)
        snap2 = buf.frame_snapshot(30)
        assert snap1 is not None and snap2 is not None
        assert isinstance(snap1[0], np.ndarray)
        assert not snap1[0].flags.writeable
        assert snap1[0] is snap2[0]
        assert buf.grid_id == 1

        # Accessing lazy attribute builds dialog cleanly on demand.
        dlg = page.reference_dialog
        assert dlg is page._reference_dialog_instance
    finally:
        assert page.shutdown_analysis()
        page.close()
        page.deleteLater()
        _process(shell_qt_application)


def test_reference_hdf5_io_off_thread_and_simulation_provenance(
    shell_qt_application, tmp_path: Path
) -> None:
    page = AnritsuPage(_make_controller(connected=True), loaded_settings(), single_sweep_available=True)
    try:
        page.set_manual_archive_context(simulation=True)
        trace = _sample_trace()
        page._show_trace(trace, update_controls=False)
        ref = page._build_reference(trace, kind="single", count=1)
        page._set_reference(ref)
        target = tmp_path / "sim_reference.h5"

        with patch(
            "app.devices.anritsu_ms2830a.ui.page.QFileDialog.getSaveFileName",
            return_value=(str(target), "HDF5 measurement (*.h5)"),
        ):
            page.save_reference_file()
            assert _wait_until(shell_qt_application, lambda: not page._reference_io_busy)

        assert target.is_file()
        assert ReferenceHdf5Store.read_simulation_enabled(target) is True

        # Loading simulated reference while simulation=False must fail cleanly (BUG-C4).
        page.set_manual_archive_context(simulation=False)
        with patch(
            "app.devices.anritsu_ms2830a.ui.page.QFileDialog.getOpenFileName",
            return_value=(str(target), "HDF5 measurement (*.h5)"),
        ):
            page.load_reference_file()
            assert _wait_until(shell_qt_application, lambda: not page._reference_io_busy)
        assert "failed" in page.banner.last_message.lower()

        # Loading simulated reference while simulation=True succeeds off-thread.
        page.set_manual_archive_context(simulation=True)
        with (
            patch(
                "app.devices.anritsu_ms2830a.ui.page.QFileDialog.getOpenFileName",
                return_value=(str(target), "HDF5 measurement (*.h5)"),
            ),
            patch(
                "app.devices.anritsu_ms2830a.ui.page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
        ):
            page.load_reference_file()
            assert _wait_until(shell_qt_application, lambda: not page._reference_io_busy)
        assert page._reference_spectrum is not None
        assert page._reference_spectrum.kind == "imported"
    finally:
        assert page.shutdown_analysis()
        page.close()
        page.deleteLater()
        _process(shell_qt_application)


def test_state_safety_bug_fixes(shell_qt_application) -> None:
    page = AnritsuPage(_make_controller(connected=True), loaded_settings(), single_sweep_available=True)
    try:
        # BUG-M7: _build_reference leaves reference_level_dbm=None when _last_configuration is None.
        page.reference.setText("-42 dBm")
        ref_unconfirmed = page._build_reference(_sample_trace(), kind="single", count=1)
        assert ref_unconfirmed.reference_level_dbm is None

        # BUG-H1: configure clears stale _last_advanced_configuration.
        page._last_advanced_configuration = AdvancedSpectrumSnapshot(
            rbw_auto=False,
            rbw_hz=10e3,
            vbw_mode="manual",
            vbw_hz=10e3,
            detector="NORM",
            attenuation_auto=True,
            attenuation_db=10.0,
            preamplifier_enabled=False,
            sweep_time_auto=True,
            sweep_time_s=0.1,
            instrument_mode="SPECT",
        )
        page._result("configure", AnritsuConfigurationSnapshot(1e6, 10e6, -5.0, 1001, "SPECT"))
        assert page._last_advanced_configuration is None
        ref_confirmed = page._build_reference(_sample_trace(), kind="single", count=1)
        assert ref_confirmed.reference_level_dbm == -5.0

        # BUG-H2: changing simulation resets _manual_archive.
        fake_archive = SimpleNamespace(active_path=Path("dummy.h5"), close=Mock())
        page._manual_archive = fake_archive
        page.set_manual_archive_context(simulation=True)
        fake_archive.close.assert_called_once()
        assert page._manual_archive is None

        # BUG-H4: custom points does not raise; invalid points <= 0 sets ERROR cleanly.
        page._result("read_configuration", AnritsuConfigurationSnapshot(1e6, 10e6, 0.0, 777, "SPECT"))
        assert page.points.currentData() == 777
        assert page._page_state == AnritsuPageState.IDLE
        page._result("read_configuration", AnritsuConfigurationSnapshot(1e6, 10e6, 0.0, 0, "SPECT"))
        assert page._page_state == AnritsuPageState.ERROR
        page._device_state_changed("verified")

        # BUG-H4: invalid form text does not crash _show_full_readback_dialog.
        page.start.setText("not-a-frequency")
        page.reference.setText("bad-ref")
        page._show_full_readback_dialog(_sample_readback())
        dialog = page._readback_dialog
        assert dialog is not None
        assert dialog._form_values["start_hz"] == "invalid"
        assert dialog._form_values["reference_level_dbm"] == "invalid"
        # BUG-H7: closing readback dialog clears _readback_dialog to None.
        dialog.close()
        _process(shell_qt_application)
        assert page._readback_dialog is None

        # BUG-M1: explicit unit-bearing CSV column headers (never "value").
        page.start.setText("1 GHz")
        page.stop.setText("1.02 GHz")
        page.reference.setText("0 dBm")
        page._show_trace(_sample_trace(0.0), update_controls=False)
        assert page.spectrum_plot._csv_value_column == "power_dbm"
        ref_trace = _sample_trace(-5.0)
        page._set_reference(page._build_reference(ref_trace, kind="single", count=1))
        page.reference_operation.setCurrentIndex(page.reference_operation.findData("difference_db"))
        page._refresh_spectrum_display()
        assert page.spectrum_plot._csv_value_column == "delta_db"
        page.reference_operation.setCurrentIndex(page.reference_operation.findData("ratio_linear"))
        page._refresh_spectrum_display()
        assert page.spectrum_plot._csv_value_column == "ratio_linear"

        # BUG-L2: out-of-range removed_peak_indices guarded in _sync_peak_markers.
        page.highlight_replacements.setChecked(True)
        page.cleanup_filters["narrow_reject"].blockSignals(True)
        page.cleanup_filters["narrow_reject"].setChecked(True)
        page.cleanup_filters["narrow_reject"].blockSignals(False)
        page._cleanup_result = SpectrumCleanupResult(
            values_dbm=tuple(np.zeros(201).tolist()),
            unit="dBm",
            method="narrow_reject",
            noise_sigma_db=0.1,
            stationary_interference_indices=(),
            removed_peak_indices=(0, 5, 9999),
        )
        page._sync_peak_markers()

        # BUG-L2: _manual_archive_finished with None job/result is safe.
        page._manual_archive_job = ("save", {})
        page._manual_archive_result = None
        page._manual_archive_finished()

        # BUG-H6: clear_all_spectra discards in-flight fetches & clears targets.
        page._fetch_pending = True
        page._instrument_live_target = InstrumentAverageTarget(frame_count=5)
        page._paused_instrument_live_target = InstrumentAverageTarget(frame_count=3)
        page._preview_statistics = object()
        page.clear_all_spectra(confirm=False)
        assert page._pending_fetch_discard == "stopped"
        assert page._instrument_live_target is None
        assert page._paused_instrument_live_target is None
        assert page._preview_statistics is None
        assert page._analysis_source_snapshot is None
        assert page._analysis_raw_snapshot is None

        # BUG-H7: two-phase closeEvent rejects close without killing workers while busy.
        page._open_spectrum_window()
        page._reference_io_busy = True
        close_ev = QCloseEvent()
        page.closeEvent(close_ev)
        assert not close_ev.isAccepted()
        assert not page._analysis_controller._closed
        assert page._spectrum_window is not None and page._spectrum_window.isVisible()
        page._reference_io_busy = False
    finally:
        assert page.shutdown_analysis()
        page.close()
        page.deleteLater()
        _process(shell_qt_application)
