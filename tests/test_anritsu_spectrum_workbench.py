"""Scientific controls remain stable while completed Live frames change."""

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage, _AnritsuSpectrumWindow
from app.devices.anritsu_ms2830a.ui.spectrum_workbench import SpectrumWorkbench
from app.settings import SettingsRepository
from app.ui.design_system import apply_application_theme
from tests.helpers import SETTINGS_TEMPLATE


@pytest.fixture
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def workbench(application):
    widget = SpectrumWorkbench()
    widget.set_trace("Raw", [1e6, 2e6, 3e6, 4e6, 5e6], [-90, -40, -10, -40, -90], primary=True)
    widget.auto_range()
    yield widget
    widget.tools.close()
    widget.tools.deleteLater()
    widget.close()
    widget.deleteLater()
    application.processEvents()


def apply_range(widget, axis, lower, upper):
    _, minimum, maximum = widget.axis_controls[axis]
    minimum.setText(lower)
    maximum.setText(upper)
    return widget.apply_axis_range(axis)


@pytest.mark.parametrize("unit,expected", [("W", "W"), ("V", "V"), ("dBm", "dB"), ("dB", "dB"), ("ratio", "ratio")])
def test_marker_delta_preserves_physical_unit(workbench, application, unit, expected):
    workbench.resize(1000, 700)
    workbench.show()
    workbench.set_trace_unit("Raw", unit)
    workbench.set_trace("Raw", [1e6, 2e6, 3e6], [1., 2., 4.], primary=True)
    workbench.add_frequency_marker(1e6)
    workbench.add_frequency_marker(3e6)
    application.processEvents()
    assert f"ΔA 3 {expected}" in workbench.marker_readout.text()
    assert workbench.plot.isVisible() and workbench.plot.width() > 0


def test_fixed_axes_survive_new_frames_reset_and_outlying_markers(workbench):
    assert apply_range(workbench, "x", "2 MHz", "4e6 Hz")
    assert apply_range(workbench, "y", "-80 dBm", "-5 dBm")
    workbench.add_frequency_marker(9e9)
    for amplitude in (-120, 30, -10):
        workbench.set_trace("Raw", [1e6, 2e6, 8e6], [amplitude] * 3, primary=True)
        workbench.auto_range()
        assert workbench.plot.viewRange()[0] == pytest.approx([2e6, 4e6])
        assert workbench.plot.viewRange()[1] == pytest.approx([-80, -5])
    assert workbench.plot.getViewBox().state["mouseEnabled"] == [False, False]
    workbench.plot.getViewBox().setYRange(-200, 100, padding=0)
    assert workbench.plot.viewRange()[1] == pytest.approx([-80, -5])
    workbench.axis_controls["x"][0].setChecked(False)
    workbench.auto_range()
    assert workbench.plot.viewRange()[0][1] < 9e9
    assert workbench.plot.viewRange()[1] == pytest.approx([-80, -5])


@pytest.mark.parametrize("axis,lower,upper", [
    ("x", "3 MHz", "2 MHz"), ("x", "2", "4 MHz"),
    ("x", "2 mV", "4 MHz"), ("x", "nan Hz", "4 MHz"),
    ("y", "-80 dB", "0 dBm"),
    ("y", "-80 dBm", "inf dBm"), ("y", "0 dBm", "0 dBm"),
])
def test_invalid_ranges_leave_previous_limits_intact(workbench, axis, lower, upper):
    workbench.fixed_ranges[axis] = (1e6, 4e6) if axis == "x" else (-80, 0)
    previous = workbench.fixed_ranges.copy()
    assert not apply_range(workbench, axis, lower, upper)
    assert workbench.fixed_ranges == previous
    assert workbench.feedback.text()


def test_amplitude_dimension_change_unlocks_y_only(workbench):
    assert apply_range(workbench, "x", "1 MHz", "5 MHz")
    assert apply_range(workbench, "y", "-80 dBm", "0 dBm")
    workbench.set_labels(y_unit="dB")
    assert "y" not in workbench.fixed_ranges
    assert "x" in workbench.fixed_ranges
    assert not workbench.axis_controls["y"][0].isChecked()
    assert not apply_range(workbench, "y", "-80 dBm", "0 dBm")
    assert apply_range(workbench, "y", "-10 dB", "20 dB")
    workbench.set_labels(y_unit="linear ratio")
    assert apply_range(workbench, "y", "0", "2 linear ratio")


def test_shown_fixed_frequency_range_accepts_negative_display_margin(application):
    window = _AnritsuSpectrumWindow(None)
    try:
        window.resize(1280, 820)
        window.show()
        plot = window.spectrum
        plot.set_labels(x="Frequency", x_unit="Hz", y="Signed residual", y_unit="W")
        plot.set_trace("Analysis", [1e8, 3e9, 6e9], [1e-12, 8e-12, -1e-13], primary=True)
        plot.auto_range()
        application.processEvents()
        assert apply_range(plot, "x", "-89.9097745 MHz", "3.28938815 GHz")
        assert apply_range(plot, "y", "-1 pW", "20.0469206 pW")
        for width in (1280, 900):
            window.resize(width, 820)
            plot.set_trace("Analysis", [1e8, 4e9, 6e9], [2e-12, 4e-12, 1e-12], primary=True)
            plot.auto_range()
            application.processEvents()
            assert plot.axis_controls["x"][0].isChecked()
            assert plot.plot.viewRange()[0] == pytest.approx([-89.9097745e6, 3.28938815e9])
            assert plot.plot.viewRange()[1] == pytest.approx([-1e-12, 20.0469206e-12])
        assert not apply_range(plot, "x", "4 GHz", "2 GHz")
        assert plot.axis_feedback["x"].isVisibleTo(window)
        assert plot.plot.viewRange()[0] == pytest.approx([-89.9097745e6, 3.28938815e9])
        assert apply_range(plot, "x", "1 GHz", "3 GHz")
        assert not plot.axis_feedback["x"].isVisible()
        application.processEvents()
        artifacts = Path("artifacts/anritsu-spectrum-workbench")
        artifacts.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(artifacts / "fixed-frequency-regression.png"))
    finally:
        window.close()
        window.deleteLater()
        application.processEvents()


def test_markers_sample_nearest_bins_update_deltas_and_remove(workbench):
    first = workbench.add_frequency_marker(2.1e6)
    second = workbench.add_frequency_marker(3e6)
    assert "-40 dBm" in workbench.marker_readout.text()
    assert "ΔA 30 dB" in workbench.marker_readout.text()
    workbench._finish_marker_drag(first)
    assert workbench.frequency_markers[first].value() == 2e6
    workbench.frequency_markers[second].setPos(6e6)
    assert "Outside measured trace" in workbench.marker_readout.text()
    workbench.frequency_markers[second].setPos(3e6)
    workbench.set_trace("Raw", [1e6, 2e6, 3e6], [-90, -20, -15], primary=True)
    assert "ΔA 5 dB" in workbench.marker_readout.text()
    workbench.remove_selected_marker()
    assert second not in workbench.frequency_markers
    workbench.remove_selected_marker()
    assert not workbench.remove_marker_button.isEnabled()
    assert workbench.marker_readout.text() == "No frequency markers."


def test_visible_peak_search_uses_selected_trace(workbench):
    workbench.set_trace("Processed", [1e6, 2e6, 3e6, 4e6, 5e6], [40, 0, 10, 20, 0])
    workbench.set_trace_unit("Processed", "dB")
    workbench.trace_selector.setCurrentText("Processed")
    assert apply_range(workbench, "x", "2 MHz", "5 MHz")
    workbench.add_peak_marker()
    assert workbench.frequency_markers["M1"].value() == 4e6
    assert "20 dB" in workbench.marker_readout.text()
    workbench.peak_search()
    assert workbench.marker.value() == 4e6


def test_band_extrema_and_interpolated_three_db_lobe_width(workbench):
    workbench.band_start.setText("1 MHz")
    workbench.band_stop.setText("5 MHz")
    assert workbench.apply_band()
    text = workbench.band_readout.text()
    assert "Samples: 5" in text
    assert "Peak: -10 dBm" in text
    assert "−3 dB width: 200 kHz" in text
    workbench.band_start.setText("2 MHz")
    workbench.band_stop.setText("3 MHz")
    assert workbench.apply_band()
    assert "incomplete lobe" in workbench.band_readout.text()
    previous = workbench.band.getRegion()
    workbench.band_stop.setText("1 MHz")
    assert not workbench.apply_band()
    assert workbench.band.getRegion() == previous
    workbench.band.setRegion((8e6, 9e6))
    assert "No measured samples" in workbench.band_readout.text()


def test_markers_keep_positions_after_frequency_grid_changes(workbench):
    name = workbench.add_frequency_marker(3e6)
    workbench.set_trace("Raw", [1e6, 2e6], [-10, -20], primary=True)
    assert workbench.frequency_markers[name].value() == 3e6
    assert "Outside measured trace" in workbench.marker_readout.text()
    workbench.clear()
    assert workbench.frequency_markers[name].value() == 3e6
    assert workbench.trace_selector.count() == 0
    assert "no data" in workbench.marker_readout.text()


def test_hold_resets_when_frequency_bins_change_with_same_point_count(workbench):
    workbench.toggle_max_hold()
    workbench.toggle_min_hold()
    workbench.set_trace("Raw", [1e6, 2e6, 3e6, 4e6, 5e6], [-100] * 5, primary=True)
    assert workbench._traces["Max hold"][1].tolist() == [-90, -40, -10, -40, -90]
    workbench.set_trace("Raw", [2e6, 3e6, 4e6, 5e6, 6e6], [-70] * 5, primary=True)
    assert workbench._traces["Max hold"][1].tolist() == [-70] * 5
    assert workbench._traces["Min hold"][1].tolist() == [-70] * 5


def test_holds_use_selected_trace_and_do_not_mix_absolute_and_relative_power(workbench):
    workbench.toggle_max_hold()
    workbench.set_trace_unit("Processed", "dB")
    workbench.set_trace("Processed", [1e6, 2e6, 3e6, 4e6, 5e6], [40] * 5, primary=True)
    assert workbench._traces["Max hold"][1].tolist() == [-90, -40, -10, -40, -90]
    assert workbench._hold_source == "Raw"
    workbench.trace_selector.setCurrentText("Processed")
    assert "Max hold" not in workbench._traces
    workbench.toggle_max_hold()
    assert workbench._traces["Max hold"][1].tolist() == [40] * 5
    workbench.set_trace_unit("Processed", "linear ratio")
    assert "Max hold" not in workbench._traces


def test_removing_selected_trace_resets_holds_and_measurement_source(workbench):
    workbench.set_trace("Processed", [1e6, 2e6, 3e6, 4e6, 5e6], [20] * 5)
    workbench.toggle_max_hold()
    workbench.clear_trace("Raw")
    assert workbench.trace_selector.currentText() == "Processed"
    assert workbench._hold_source == "Processed"
    assert "Max hold" not in workbench._traces


def test_freeze_preview_keeps_acquisition_display_current_and_resumes_latest(application):
    controller = MagicMock()
    controller.is_connected = False
    controller.visa_address = "SIM::ANRITSU"
    settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
    page = AnritsuPage(controller, settings, single_sweep_available=True)
    try:
        def frame(power):
            return SpectrumTrace(frequencies_hz=(1e6, 2e6), powers_dbm=(power, power),
                                 trace_name="TRAC1", acquired_at_utc=datetime.now(UTC))
        page._latest_trace = frame(-50)
        page._refresh_spectrum_display()
        page.resize(1280, 820)
        page.show()
        application.processEvents()
        assert page.open_floating_spectrum.isVisibleTo(page)
        assert not page.analysis_details.isVisible()
        QTest.mouseClick(page.open_floating_spectrum, Qt.MouseButton.LeftButton)
        floating = page._spectrum_window
        assert floating is not None
        floating.spectrum.freeze.setChecked(True)
        page._latest_trace = frame(-20)
        page._refresh_spectrum_display(auto_range=True)
        assert floating.spectrum._traces["Raw"][1].tolist() == [-50, -50]
        assert page.spectrum_plot._traces["Raw"][1].tolist() == [-20, -20]
        floating.spectrum.freeze.setChecked(False)
        assert floating.spectrum._traces["Raw"][1].tolist() == [-20, -20]
        controller.call.assert_not_called()
    finally:
        if page._spectrum_window:
            page._spectrum_window.close()
        page.close()
        page.deleteLater()
        application.processEvents()


def test_quick_comparisons_units_and_return_to_current_view(application):
    controller = MagicMock()
    controller.is_connected = False
    controller.visa_address = "SIM::ANRITSU"
    page = AnritsuPage(controller, SettingsRepository(SETTINGS_TEMPLATE).load().settings,
                       single_sweep_available=True)
    try:
        raw = SpectrumTrace((1e6, 2e6, 3e6, 4e6, 5e6), (0, -10, -20, -30, -40), datetime.now(UTC), "TRAC1")
        page._latest_trace = raw
        page._reference_trace = SpectrumTrace(raw.frequencies_hz, (-10,) * 5, datetime.now(UTC), "TRAC1")
        page._background_for_filter = MagicMock(return_value=(None, SimpleNamespace(
            mean_w=(.002, .0001, .000001, .000001, .000001), profile_id="test-profile", content_hash="test-hash", context_id="test-context")))
        page.resize(1600, 1000)
        page.show()
        application.processEvents()
        for checkbox in page.quick_curves.values():
            assert checkbox.isVisibleTo(page)
            checkbox.setChecked(True)
        assert len(page._display_state.traces) == 3
        assert page._display_state.by_key["background_difference"].values[0] < 0
        exported_raw, derived, unit, operation = page._manual_trace_payload("background_difference")
        assert exported_raw is raw
        assert derived[0] < 0 and unit == "W"
        assert "test-hash" in operation
        artifact_dir = Path("artifacts/spectrum-layout")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        application.processEvents()
        assert page.grab().save(str(artifact_dir / "three-power-comparisons.png"))
        page.quick_power_unit.setCurrentIndex(page.quick_power_unit.findData("dBm"))
        assert "non-positive" in page.info.text()
        assert all(trace.unit == "dBm" for trace in page._display_state.traces)
        page.quick_power_unit.setCurrentIndex(page.quick_power_unit.findData("W"))
        assert page._display_state.by_key["background_difference"].values[0] < 0
        page._background_for_filter.side_effect = ValueError("Background is stale")
        page._refresh_spectrum_display()
        assert "background_difference" not in page._display_state.by_key
        assert "Background is stale" in page.info.text()
        page.quick_power_unit.setCurrentIndex(0)
        for checkbox in page.quick_curves.values():
            checkbox.setChecked(False)
        assert page._display_state.available_keys == ("raw",)
        assert page._display_state.selected.unit == "dBm"
        assert "Background_difference" not in page.spectrum_plot._traces
        assert "Reference_difference" not in page.spectrum_plot._traces
        assert raw.powers_dbm == (0, -10, -20, -30, -40)
        controller.call.assert_not_called()
    finally:
        page.close()
        page.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(1280, 820), (900, 620)])
def test_rendered_live_workbench_tools_and_plot_are_accessible(application, theme, size):
    apply_application_theme(application, theme)
    window = _AnritsuSpectrumWindow(None)
    try:
        window.resize(*size)
        plot = window.spectrum
        x = np.linspace(200e6, 250e6, 1001)
        y = -85 + 60 * np.exp(-((x - 225e6) / 1e6) ** 2)
        plot.set_trace("Raw", x, y, primary=True)
        plot.set_title("Current spectrum")
        plot.auto_range()
        assert apply_range(plot, "y", "-100 dBm", "-10 dBm")
        plot.add_frequency_marker(225e6)
        plot.add_frequency_marker(227e6)
        plot.band_start.setText("220 MHz")
        plot.band_stop.setText("230 MHz")
        plot.apply_band()
        window.show()
        application.processEvents()
        assert plot.isVisible() and plot.width() > 300 and plot.height() > 200
        assert plot.tools.isVisible() and plot.tools.width() >= 300
        assert not plot.geometry().intersects(plot.tools.geometry())
        for button in plot.toolbar_buttons:
            assert button.isVisible()
            assert plot.rect().contains(button.geometry().center())
        plot.tools.ensureWidgetVisible(plot.apply_band_button)
        application.processEvents()
        point = plot.apply_band_button.mapTo(plot.tools.viewport(), plot.apply_band_button.rect().center())
        assert plot.tools.viewport().rect().contains(point)
        plot.tools.verticalScrollBar().setValue(0)
        application.processEvents()
        artifacts = Path("artifacts/anritsu-spectrum-workbench")
        artifacts.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(artifacts / f"live-{theme}-{size[0]}.png"))
        plot.tools.ensureWidgetVisible(plot.band_readout)
        application.processEvents()
        assert window.grab().save(str(artifacts / f"analysis-{theme}-{size[0]}.png"))
        window.tools_toggle.setChecked(False)
        application.processEvents()
        assert not plot.tools.isVisible()
        assert plot.isVisible()
    finally:
        window.close()
        window.deleteLater()
        application.processEvents()
