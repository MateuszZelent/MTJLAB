"""One Live session, independent peak histories, and frequency-safe exports."""
import csv
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import replace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QWidget

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage, AnritsuPageState
from app.devices.anritsu_ms2830a.ui.analysis_worker import SpectrumAnalysisController, SpectrumAnalysisRequest
from tests.helpers import loaded_settings
from tests.test_spectrum_correction_controller import wait_until
from app.ui.design_system import apply_application_theme


@pytest.fixture
def application():
    app = QApplication.instance() or QApplication([])
    if not QFontDatabase.families() and Path("C:/Windows/Fonts/arial.ttf").exists():
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/arial.ttf")
    app.setFont(QFont("Arial", 10))
    return app


def frame(shift=0.):
    x = np.linspace(1e6, 10e6, 1001)
    y = -85 + 50 * np.exp(-((x - 3e6 - shift) / 100e3)**2) + 40 * np.exp(-((x - 7e6 - shift) / 130e3)**2)
    return SpectrumTrace(tuple(x), tuple(y), datetime.now(timezone.utc), "TRAC1")


@pytest.fixture
def page(application):
    controller = MagicMock()
    controller.is_connected = False
    controller.visa_address = "SIM::ANRITSU"
    widget = AnritsuPage(controller, loaded_settings(), single_sweep_available=True)
    widget.resize(1360, 900)
    widget.show()
    yield widget, controller
    if widget._spectrum_window is not None:
        widget._spectrum_window.close()
    widget.close()
    widget.deleteLater()
    application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()


def test_floating_live_buttons_share_state_and_respect_execution_ownership(page, application):
    widget, controller = page
    widget._set_page_state(AnritsuPageState.DISCONNECTED)
    widget._open_spectrum_window()
    floating = widget._spectrum_window
    assert not floating.start_live.isEnabled()
    widget._set_page_state(AnritsuPageState.IDLE)
    assert floating.start_live.isEnabled() and not floating.stop_live.isEnabled()
    floating.start_live.click()
    assert controller.call.call_args.args[0] == "start_live"
    assert not floating.start_live.isEnabled() and not floating.stop_live.isEnabled()
    widget._live_transition_pending = False
    widget._timer.start()
    widget._set_page_state(AnritsuPageState.LIVE)
    assert floating.stop_live.isEnabled() and not floating.start_live.isEnabled()
    floating.stop_live.click()
    assert controller.call.call_args.args == ("stop_live",)
    assert not widget._timer.isActive()
    widget.set_execution_controlled(True)
    assert not floating.start_live.isEnabled() and not floating.stop_live.isEnabled()


@pytest.mark.parametrize("operation", ["start_live", "stop_live"])
def test_failed_live_transition_releases_buttons_without_erasing_spectrum(page, operation):
    widget, _ = page
    widget._latest_trace = frame()
    widget._set_page_state(AnritsuPageState.IDLE)
    widget._open_spectrum_window()
    widget._live_transition_pending = True
    widget._set_page_state(AnritsuPageState.STOPPING)
    widget._error(operation, "injected failure")
    assert not widget._live_transition_pending
    assert widget._spectrum_window.start_live.isEnabled()
    assert not widget._spectrum_window.stop_live.isEnabled()
    assert widget._latest_trace is not None


@pytest.mark.parametrize("width,theme", [(1280, "light"), (860, "dark")])
def test_multiple_peak_windows_preserve_history_pause_gate_and_export(page, application, tmp_path, width, theme):
    widget, controller = page
    apply_application_theme(application, theme)
    widget._show_trace(frame())
    wait_until(application, lambda: len(widget._detected_peaks) >= 2)
    widget._start_peak_tracking(0)
    first = widget._peak_tracking_window
    widget._start_peak_tracking(1)
    second = widget._peak_tracking_window
    assert first is not second and first in widget._additional_peak_trackers
    assert first.point_count == second.point_count == 1
    first.gate.setText("1 MHz")
    first.apply_gate.click()
    assert widget._additional_peak_trackers[first]["context"][2] == 1e6
    context = widget._additional_peak_trackers[first]["context"]
    first.gate.setText("100 mV")
    first.apply_gate.click()
    assert "Invalid" in first.status.text()
    assert widget._additional_peak_trackers[first]["context"] == context
    first.paused.setChecked(True)
    assert context not in [s["context"] for w, s in widget._additional_peak_trackers.items() if not w.paused.isChecked()]
    first.paused.setChecked(False)
    widget._show_trace(frame(30e3))
    wait_until(application, lambda: second.point_count >= 2 and first.point_count >= 3)
    assert first._frequencies_hz[-1] == pytest.approx(3.03e6, abs=15e3)
    assert second._frequencies_hz[-1] == pytest.approx(7.03e6, abs=15e3)
    first.export_csv(tmp_path / "tracked.csv")
    with (tmp_path / "tracked.csv").open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert any(row["status"] == "gap" and row["frequency_hz"] == "" for row in rows)
    assert rows[-1]["amplitude_unit"] == "dBm" and rows[-1]["source"]
    assert float(rows[-1]["frequency_hz"]) == first._frequencies_hz[-1]
    first.clear_history.click()
    assert first.point_count == 0 and second.point_count >= 2
    first.close()
    assert first not in widget._additional_peak_trackers and widget._peak_tracking_window is second
    widget._open_spectrum_window()
    floating = widget._spectrum_window
    floating.resize(width, 780)
    second.resize(620, 540)
    application.processEvents()
    assert floating.peak_table.isVisibleTo(floating)
    assert floating.start_live.isVisibleTo(floating)
    assert second.plot.height() > 100 and second.apply_gate.isVisibleTo(second)
    assert floating.grab().save(str(tmp_path / f"floating-{theme}.png"))
    assert second.grab().save(str(tmp_path / f"tracking-{theme}.png"))
    controller.call.assert_not_called()
    apply_application_theme(application, "light")


def test_worker_matches_multiple_tracks_independently(application):
    controller = SpectrumAnalysisController()
    results = []
    controller.result.connect(results.append)
    trace = frame()
    request = SpectrumAnalysisRequest(1, trace.frequencies_hz, trace.powers_dbm, "raw", (), False,
        tracking_context=(1, 3e6, 500e3), additional_tracking_contexts=((2, 7e6, 500e3), (3, 9e6, 100e3)))
    try:
        controller.submit(request)
        wait_until(application, lambda: bool(results))
        assert results[0].tracked_peak.frequency_hz == pytest.approx(3e6, abs=15e3)
        matches = dict(results[0].additional_tracked_peaks)
        assert matches[(2, 7e6, 500e3)].frequency_hz == pytest.approx(7e6, abs=15e3)
        assert matches[(3, 9e6, 100e3)] is None
        controller.submit(replace(request, generation=2, additional_tracking_contexts=()))
        wait_until(application, lambda: len(results) == 2)
        assert not results[-1].additional_tracked_peaks
    finally:
        assert controller.close()


def test_closing_latest_tracker_keeps_older_tracker_updating(page, application):
    widget, _ = page
    widget._show_trace(frame())
    wait_until(application, lambda: len(widget._detected_peaks) >= 2)
    widget._open_peak_table()
    table = widget._peak_table_dialog
    original = widget._detected_peaks
    table.table.selectRow(0)
    table.set_peaks(tuple(reversed(original)), method="Raw")
    assert table.selected_peak_index() == 1
    widget._start_peak_tracking(0)
    older = widget._peak_tracking_window
    widget._start_peak_tracking(1)
    widget._peak_tracking_window.close()
    assert widget._peak_tracking_window is None
    widget._show_trace(frame(20e3))
    wait_until(application, lambda: older.point_count >= 2)
    assert older._frequencies_hz[-1] == pytest.approx(3.02e6, abs=15e3)
    older.close()
    assert not widget._additional_peak_trackers


def test_tracking_export_keeps_linear_units_and_does_not_bridge_lost_peak(page, application, tmp_path):
    widget, _ = page
    widget._show_trace(frame())
    wait_until(application, lambda: len(widget._detected_peaks) >= 2)
    widget._start_peak_tracking(0)
    tracking = widget._peak_tracking_window
    tracking.clear()
    peak = replace(widget._detected_peaks[0], frequency_hz=5e9, amplitude_dbm=1e-9, amplitude_unit="W")
    tracking.append(0, peak, source="Raw - background")
    tracking.mark_lost(target_hz=5e9, gate_hz=1e6, elapsed_s=1)
    tracking.append(2, replace(peak, frequency_hz=5.001e9), source="Raw - background")
    assert np.isnan(tracking.curve.yData[1])
    output = tmp_path / "power.csv"
    tracking.export_csv(output)
    with output.open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert float(rows[0]["frequency_hz"]) == 5e9
    assert float(rows[0]["amplitude"]) == 1e-9 and rows[0]["amplitude_unit"] == "W"
    assert rows[1]["status"] == "gap" and rows[1]["frequency_hz"] == ""
    assert datetime.fromisoformat(rows[0]["analysis_time_utc"]).utcoffset().total_seconds() == 0
    assert tracking._initial_frequency_hz == 5e9


def test_results_navigation_registers_first_route_before_selection_signal(application, capfd):
    from app.ui.results.page import _FluentResultSections
    sections = _FluentResultSections()
    sections.addTab(QWidget(), "Overview")
    application.processEvents()
    assert sections.stack.currentIndex() == 0
    assert "RouteKeyError" not in capfd.readouterr().err
    sections.close()
    sections.deleteLater()
    application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()
