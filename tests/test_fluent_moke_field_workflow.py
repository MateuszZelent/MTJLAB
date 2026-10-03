import os
import time
from pathlib import Path
from unittest.mock import patch
from threading import Event
from types import SimpleNamespace

import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QWidget
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from qfluentwidgets import ScrollArea, Theme

from app.bootstrap import StationComposition
from app.devices.moke_box.ui.page import MokeBoxPage
from app.devices.moke_box.adapter import MokeBoxAdapter
from app.devices.moke_box.models import MokeBoxConfig
from app.devices.moke_box.protocol import MokeCommandType, MokeFrame, decode_voltage
from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
from app.devices.simulation import SimulationContext
from app.devices.simulators import simulated_station_settings
from app.ui.recipes.page import RecipePage
from app.devices.moke_box.ui.voltage_history import MokeVoltageHistory
from app.settings.models import StationSettings
from app.settings import SettingsRepository
from app.engine.compiler import RecipeCompiler
from app.domain.errors import ConfigurationError
from app.recipes import parse_recipe_text
from app.ui.shell import MainWindow
from app.ui.workers import DeviceController
from app.ui.design_system import apply_application_theme
from app.ui.common.precision_stepper import install_precision_arrow_stepper
from app.ui.recipes.sweep_editor import SweepGeneratorDialog
from app.ui.recipes.common_dialogs import FixedValueDialog
from tests.helpers import loaded_settings
from tests.test_main_window import TEST_ENGINEER, write_engineer_settings


@pytest.fixture(scope="module")
def application():
    application = QApplication.instance() or QApplication([])
    # Qt's offscreen plugin has no Windows font discovery in the sandbox.
    if not QFontDatabase.families():
        for name in ("segoeui.ttf", "seguisb.ttf"):
            font = Path("C:/Windows/Fonts") / name
            if font.is_file():
                QFontDatabase.addApplicationFont(str(font))
    apply_application_theme(application, "light")
    return application


def wait_for(application, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while not predicate():
        application.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Qt workflow did not reach the expected state")
        time.sleep(0.01)
    application.processEvents()


def test_voltage_history_keeps_last_three_minutes_and_expires_idle_data(application, monkeypatch):
    clock_s = [1000.0]
    monkeypatch.setattr("app.devices.moke_box.ui.voltage_history.time",
                        SimpleNamespace(monotonic=lambda: clock_s[0]))
    history = MokeVoltageHistory()
    history.resize(900, 500)
    history.show()
    model = SimpleNamespace(
        calibration_id="test-three-minute-calibration",
        ascending=SimpleNamespace(estimate=lambda voltage: voltage * 0.04),
        descending=SimpleNamespace(estimate=lambda voltage: voltage * 0.035),
    )
    try:
        history.append(0.1, model)
        clock_s[0] += 60
        history.append(0.2, model)
        clock_s[0] = 1180
        QTest.qWait(350)
        assert len(history.points) == 2  # The boundary at exactly 180 s remains visible.
        assert history.voltage_curve.xData == pytest.approx([-180, -120])
        assert history.ascending_curve.yData == pytest.approx([0.004, 0.008])
        assert history.descending_curve.yData == pytest.approx([0.0035, 0.007])
        assert history.item.vb.viewRange()[0] == pytest.approx([-180, 0])
        # Linked ViewBoxes account for their border in screen coordinates.
        # Both traces align to the same time axis within one rendered pixel.
        seconds_per_pixel = history.WINDOW_S / history.item.vb.width()
        assert history.field_view.viewRange()[0] == pytest.approx([-180, 0], abs=seconds_per_pixel)
        clock_s[0] = 1180.1
        QTest.qWait(350)  # No new append: the visible window still moves and prunes.
        assert len(history.points) == 1
        assert history.voltage_curve.xData == pytest.approx([-120.1])
        clock_s[0] = 1240.1
        QTest.qWait(350)
        assert not history.points
        for curve in (history.voltage_curve, history.ascending_curve, history.descending_curve):
            assert curve.xData is None or not len(curve.xData)
        assert history.item.vb.viewRange()[0] == pytest.approx([-180, 0])
    finally:
        history.close()


def test_voltage_history_prunes_on_append_and_keeps_full_window_after_clear(application, monkeypatch):
    clock_s = [1000.0]
    monkeypatch.setattr("app.devices.moke_box.ui.voltage_history.time",
                        SimpleNamespace(monotonic=lambda: clock_s[0]))
    history = MokeVoltageHistory()
    try:
        history.append(0.1)
        clock_s[0] += 181
        history.append(0.2)
        assert len(history.points) == 1
        history.show()
        application.processEvents()
        assert history.voltage_curve.xData == pytest.approx([0])
        assert history.voltage_curve.yData == pytest.approx([0.2])
        assert history.item.vb.viewRange()[0] == pytest.approx([-180, 0])
        history.clear()
        assert not history.points
        assert history.item.vb.viewRange()[0] == pytest.approx([-180, 0])
        history.append(0.3)
        assert history.voltage_curve.xData == pytest.approx([0])
    finally:
        history.close()


@pytest.fixture
def workspace(application, tmp_path):
    raw = loaded_settings().model_dump(mode="python")
    raw["devices"]["moke_box"]["calibration_directory"] = str(tmp_path / "calibrations")
    settings = StationSettings.model_validate(raw)
    host = QWidget()
    layout = QVBoxLayout(host)
    composition = StationComposition(settings, simulation=True)
    controllers = composition.create_controllers(("moke_box", "lakeshore_gaussmeter"), host)
    page = MokeBoxPage(controllers["moke_box"], settings, host)
    layout.addWidget(page)
    page.field_workflow.bind_reference(
        controllers["lakeshore_gaussmeter"], simulation=True, authorize=None,
        pause_live=lambda: page.stop_live("Paused for calibration"),
    )
    host.resize(1360, 880)
    host.show()
    for controller in controllers.values():
        controller.call("connect")
    workflow = page.field_workflow
    wait_for(application, lambda: workflow._profile is not None and workflow._reference_connected)
    yield host, page, controllers, tmp_path
    workflow.stop() if workflow.busy else None
    wait_for(application, lambda: not workflow.busy)
    assert DeviceController.close_all(controllers.values())
    host.close()
    application.processEvents()


@pytest.mark.parametrize("width,height,theme", [(1360, 880, Theme.LIGHT), (980, 720, Theme.DARK)])
def test_fluent_control_and_calibration_render_with_visible_geometry(workspace, application, width, height, theme):
    host, page, _, tmp_path = workspace
    apply_application_theme(application, "dark" if theme == Theme.DARK else "light")
    host.resize(width, height)
    workflow = page.field_workflow
    for index, child in ((2, workflow.control_page), (3, workflow.calibration_page)):
        page.views.setCurrentIndex(index)
        wait_for(application, lambda: child.isVisible())
        # Let the native Fluent selection transition reach its final position.
        transition_end = time.monotonic() + 0.25
        wait_for(application, lambda: time.monotonic() >= transition_end)
        assert host.width() == width
        assert host.height() == height
        assert child.isVisible()
        assert child.width() > 500 and child.height() > 300
        assert child.parent() is page.views.stack
        if index == 2:
            assert workflow.live_control_switch.isVisibleTo(host)
            assert workflow.live_control_switch.width() > 80
            for control in (workflow.live_control_switch, workflow.set_button, workflow.zero_button):
                assert child.rect().contains(control.mapTo(child, control.rect().center()))
        scroll = child if isinstance(child, ScrollArea) else child.findChild(ScrollArea)
        assert scroll.viewport().width() >= scroll.widget().width()
        assert scroll.horizontalScrollBar().maximum() == 0
        path = tmp_path / f"moke-field-{index}-{width}.png"
        assert host.grab().save(str(path))


def test_manual_control_range_permission_slider_and_zero_use_one_controller_lease(workspace, application):
    _, page, controllers, _ = workspace
    workflow = page.field_workflow
    page.views.setCurrentIndex(2)
    assert workflow.set_button.isEnabled()
    assert not hasattr(workflow, "arm_button")
    workflow.target.setText("200 mV")
    assert workflow.set_button.isEnabled()
    workflow.target.setText("250 mV")
    assert workflow.set_button.isEnabled()
    workflow.set_button.click()
    wait_for(application, lambda: not workflow.busy)
    actual = controllers["moke_box"].adapter_for_run().read_vouts()[2]
    assert abs(actual - 0.25) < 0.001
    assert f"{actual:+.6f} V" in workflow.voltage_readout.text()
    exported = {value.key: value for value in page.manual_metadata_values()}
    assert exported["moke_box.vout.2_v"].value_si == actual
    assert exported["moke_box.vout.2_v"].unit == "V"
    assert page.vout_values[2].text() == f"{actual:+.6f} V"
    wait_for(application, lambda: workflow.set_button.isEnabled())
    workflow.voltage_slider.setValue(7500)
    assert abs(workflow._voltage(workflow.target) - 0.25) < 0.001
    workflow.voltage_slider.setValue(8000)
    # Live OFF: neither release nor Enter applies the draft.
    assert abs(controllers["moke_box"].adapter_for_run().read_vouts()[2] - actual) < 0.001
    workflow.voltage_slider.sliderReleased.emit()
    workflow.target.returnPressed.emit()
    QTest.qWait(550)
    assert not workflow.busy
    assert abs(controllers["moke_box"].adapter_for_run().read_vouts()[2] - actual) < 0.001
    workflow.set_button.click()
    wait_for(application, lambda: not workflow.busy)
    assert abs(controllers["moke_box"].adapter_for_run().read_vouts()[2] - 0.3) < 0.001
    assert len(workflow.voltage_history.points) == 2
    workflow.configuration_panel.set_operator_limits("-0.4 V", "0.5 V")
    assert workflow.set_button.isEnabled()
    workflow.target.setText("2 V")
    assert not workflow.set_button.isEnabled()
    workflow.target.setText("200 mV")
    assert workflow.set_button.isEnabled()
    workflow.channel_selector.setCurrentIndex(3)
    assert not workflow.live_control_switch.isEnabled()
    assert not workflow.set_button.isEnabled()
    assert not workflow.voltage_history.points
    workflow.channel_selector.setCurrentIndex(2)
    assert workflow.set_button.isEnabled()
    workflow.zero_button.click()
    wait_for(application, lambda: not workflow.busy)
    assert "DAC zero confirmed" in workflow.manual_status.text()
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0
    exported = {value.key: value for value in page.manual_metadata_values()}
    assert exported["moke_box.vout.2_v"].value_si == 0


def test_sweep_confirmed_voltage_updates_manual_export_without_device_query(workspace, application):
    _, page, controllers, _ = workspace
    before = controllers["moke_box"].adapter_for_run().read_vouts()[2]
    page.apply_execution_event(
        "action_finished", {"kind": "update_moke_voltage"},
        {"voltage_control": {"actual": {"channel": 2, "actual_v": 0.19989623706778162}}}, {},
    )
    application.processEvents()
    exported = {value.key: value for value in page.manual_metadata_values()}
    assert exported["moke_box.vout.2_v"].value_si == 0.19989623706778162
    assert page.vout_values[2].text() == "+0.199896 V"
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == before
    page.apply_execution_event(
        "moke_dac_shutdown", {},
        {"dac_shutdown": {"actual": {"channel": 2, "actual_v": 0.0, "safe_target_confirmed": True}}}, {},
    )
    exported = {value.key: value for value in page.manual_metadata_values()}
    assert exported["moke_box.vout.2_v"].value_si == 0


def test_zero_button_cancels_pending_live_target_and_keeps_confirmed_zero(workspace, application):
    _, page, controllers, _ = workspace
    workflow = page.field_workflow
    page.views.setCurrentIndex(2)
    workflow.target.setText("400 mV")
    workflow.set_button.click()
    wait_for(application, lambda: not workflow.busy and workflow.live_control_switch.isEnabled())
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == pytest.approx(0.4, abs=0.001)
    workflow.live_control_switch.setChecked(True)
    workflow.target.setText("-300 mV")
    assert workflow._live_pending
    workflow.zero_button.click()
    wait_for(application, lambda: not workflow.busy)
    QTest.qWait(700)
    assert not workflow.live_control_switch.isChecked()
    assert not workflow._live_pending
    assert not workflow.busy
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0
    assert "DAC zero confirmed" in workflow.manual_status.text()
    assert "unknown" in workflow.manual_status.text()
    assert {value.key: value.value_si for value in page.manual_metadata_values()}["moke_box.vout.2_v"] == 0


def test_live_voltage_debounces_edits_and_applies_slider_without_apply(workspace, application):
    _, page, controllers, _ = workspace
    workflow = page.field_workflow
    page.views.setCurrentIndex(2)
    assert not workflow.live_control_switch.isChecked()
    workflow.live_control_switch.setChecked(True)
    assert workflow._manual_envelope is not None
    QTest.qWait(550)
    assert not workflow.voltage_history.points  # Enabling Live does not send the old draft.
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0
    for text in ("100 mV", "200 mV", "300 mV"):
        workflow.target.setText(text)
        QTest.qWait(80)
    assert not workflow.busy
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0
    wait_for(application, lambda: len(workflow.voltage_history.points) == 1 and not workflow.busy)
    assert abs(controllers["moke_box"].adapter_for_run().read_vouts()[2] - 0.3) < 0.001
    # No release or Apply is required while Live is ON.
    workflow.voltage_slider.setValue(7000)
    wait_for(application, lambda: len(workflow.voltage_history.points) == 2 and not workflow.busy)
    assert abs(controllers["moke_box"].adapter_for_run().read_vouts()[2] - 0.2) < 0.001
    QTest.qWait(550)
    assert len(workflow.voltage_history.points) == 2  # Completion does not resubmit.


def test_apply_validates_current_settings_and_authorization_without_separate_arm(workspace, application):
    _, page, controllers, _ = workspace
    workflow = page.field_workflow
    requests = []

    def approve(operation, request):
        requests.append((operation, request))

    workflow._authorize = approve
    workflow.target.setText("200 mV")
    workflow.configuration_panel.set_operator_limits("-250 mV", "250 mV")
    workflow.manual_settling.setText("3 s")
    assert not requests  # Editing never authorizes or writes an output.
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0
    workflow.set_button.click()
    wait_for(application, lambda: not workflow.busy)
    assert [operation for operation, _ in requests] == ["arm_voltage_plan", "ramp_vout"]
    for _, request in requests:
        assert request["minimum_v"] == -0.25
        assert request["maximum_v"] == 0.25
        assert request["settling_s"] == 3
        assert request["targets_v"] == (0.2,)
    actual = controllers["moke_box"].adapter_for_run().read_vouts()[2]
    assert actual == pytest.approx(0.2, abs=0.001)

    def deny(operation, request):
        raise ConfigurationError("injected output authorization denial")

    workflow._authorize = deny
    workflow.target.setText("100 mV")
    workflow.set_button.click()
    application.processEvents()
    assert not workflow.busy
    assert "authorization denial" in workflow.manual_status.text()
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == actual


def test_manual_readback_failure_stops_live_and_allows_deliberate_apply_after_zero(workspace, application, monkeypatch):
    _, page, controllers, _ = workspace
    workflow = page.field_workflow
    adapter = controllers["moke_box"].adapter_for_run()
    send = SimulatedMokeBoxTransport.send
    ignore_nonzero = [True]

    def ignore_write(transport, raw):
        frame = MokeFrame.decode(raw)
        if ignore_nonzero[0] and frame.record_type == MokeCommandType.SET_VOUT and decode_voltage(frame.msb, frame.lsb) != 0:
            return
        send(transport, raw)

    monkeypatch.setattr(SimulatedMokeBoxTransport, "send", ignore_write)
    workflow.live_control_switch.setChecked(True)
    workflow.target.setText("100 mV")
    wait_for(application, lambda: "VOUT2 readback differs" in workflow.manual_status.text() and not workflow.busy)
    assert not workflow.live_control_switch.isChecked()
    assert "approved DAC zero was confirmed" in workflow.manual_status.text()
    assert adapter.read_vouts()[2] == 0
    QTest.qWait(550)
    assert adapter.read_vouts()[2] == 0  # A failed Live target never restarts itself.
    assert workflow.set_button.isEnabled()
    ignore_nonzero[0] = False
    workflow.set_button.click()
    wait_for(application, lambda: not workflow.busy)
    assert adapter.read_vouts()[2] == pytest.approx(0.1, abs=0.001)


def test_moke_voltage_arrows_preserve_units_precision_and_live_apply(workspace, application):
    _, page, controllers, _ = workspace
    workflow = page.field_workflow
    page.views.setCurrentIndex(2)
    install_precision_arrow_stepper(application)
    assert workflow.target.text() == "0 mV"
    for draft, increased, decreased in (
        ("0 mV", "100 mV", "0 mV"),
        ("0 V", "0.1 V", "0.0 V"),
        ("0.00 V", "0.01 V", "0.00 V"),
        ("0,00 V", "0,01 V", "0,00 V"),
        ("0.00 mV", "0.01 mV", "0.00 mV"),
        ("1e-2 V", "2e-2 V", "1e-2 V"),
    ):
        workflow.target.setText(draft)
        QTest.keyClick(workflow.target, Qt.Key.Key_Up)
        assert workflow.target.text() == increased
        QTest.keyClick(workflow.target, Qt.Key.Key_Down)
        assert workflow.target.text() == decreased
    workflow.target.setText("0 mV")
    QTest.keyClick(workflow.voltage_slider, Qt.Key.Key_Right)
    assert workflow.target.text() == "100 mV"
    QTest.qWait(550)
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0
    workflow.set_button.click()
    wait_for(application, lambda: not workflow.busy)
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == pytest.approx(0.1, abs=0.001)
    workflow.target.setText("0.00 V")
    workflow.live_control_switch.setChecked(True)
    QTest.keyClick(workflow.target, Qt.Key.Key_Up)
    wait_for(application, lambda: len(workflow.voltage_history.points) == 2 and not workflow.busy)
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == pytest.approx(0.01, abs=0.001)
    workflow.target.setText("0.5 V")
    QTest.keyClick(workflow.target, Qt.Key.Key_Up)
    assert workflow.target.text() == "0.5 V"  # Shared Keithley range editor clamps at MAX.
    wait_for(application, lambda: len(workflow.voltage_history.points) == 3 and not workflow.busy)
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == pytest.approx(0.5, abs=0.001)
    workflow.target.setText("2 V")
    QTest.qWait(550)
    assert not workflow._live_timer.isActive()
    assert not workflow.set_button.isEnabled()
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == pytest.approx(0.5, abs=0.001)


def test_live_off_and_invalid_drafts_cancel_pending_voltage(workspace, application):
    _, page, controllers, _ = workspace
    workflow = page.field_workflow
    workflow.live_control_switch.setChecked(True)
    for invalid in ("2 V", "nan V", "100 mA", ""):
        workflow.target.setText("100 mV")
        workflow.target.setText(invalid)
        QTest.qWait(550)
        assert not workflow.busy
        assert not workflow.voltage_history.points
        assert not workflow.set_button.isEnabled()
    workflow.target.setText("200 mV")
    assert workflow._live_timer.isActive()
    workflow.live_control_switch.setChecked(False)
    QTest.qWait(550)
    workflow.target.returnPressed.emit()
    workflow.voltage_slider.sliderReleased.emit()
    assert not workflow.busy
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0
    assert workflow.set_button.isEnabled()
    workflow.set_button.click()
    wait_for(application, lambda: not workflow.busy)
    assert abs(controllers["moke_box"].adapter_for_run().read_vouts()[2] - 0.2) < 0.001


@pytest.mark.parametrize("change", ["channel", "range", "timing", "recipe", "disconnect", "settings", "calibration"])
def test_live_permission_changes_cancel_pending_output(workspace, application, change):
    _, page, controllers, _ = workspace
    workflow = page.field_workflow
    workflow.live_control_switch.setChecked(True)
    workflow.target.setText("200 mV")
    assert workflow._live_timer.isActive()
    if change == "channel":
        workflow.channel_selector.setCurrentIndex(3)
    elif change == "range":
        workflow.configuration_panel.set_operator_limits("-0.4 V", "0.4 V")
    elif change == "timing":
        workflow.manual_settling.setText("3 s")
    elif change == "recipe":
        workflow.set_execution_controlled(True)
    elif change == "disconnect":
        workflow._state_changed("disconnected")
    elif change == "settings":
        workflow.set_settings(workflow._settings)
    else:
        workflow.probe.setText("SIM live cancellation")
        workflow.orientation.setText("+z")
        workflow.geometry.setText("fixed gap")
        workflow.uncertainty.setText("10 uT")
        workflow.arm_calibration_button.click()
        assert workflow.start_calibration_button.isEnabled()
    assert not workflow.live_control_switch.isChecked()
    assert not workflow._live_timer.isActive()
    assert workflow._manual_envelope is None
    QTest.qWait(550)
    assert not workflow.busy
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0


@pytest.mark.parametrize("disable_live", [False, True])
def test_live_keeps_only_latest_draft_while_one_worker_waits(workspace, application, disable_live):
    _, page, controllers, _ = workspace
    workflow = page.field_workflow
    adapter = controllers["moke_box"].adapter_for_run()
    ramp = MokeBoxAdapter.ramp_vout
    requests = []
    release = Event()

    def delayed_ramp(device, channel, voltage, **kwargs):
        requests.append(voltage)
        result = ramp(device, channel, voltage, **kwargs)
        if len(requests) == 1:
            release.wait(5)
        return result

    with patch.object(MokeBoxAdapter, "ramp_vout", new=delayed_ramp):
        workflow.live_control_switch.setChecked(True)
        workflow.target.setText("100 mV")
        wait_for(application, lambda: bool(requests))
        assert workflow.busy
        assert workflow.target.isEnabled()
        assert workflow.voltage_slider.isEnabled()
        assert not workflow.configuration_panel.level_field.edit_button.isEnabled()
        workflow.target.setText("300 mV")
        workflow.target.setText("200 mV")
        QTest.qWait(550)
        assert requests == [0.1]
        if disable_live:
            workflow.live_control_switch.setChecked(False)
        release.set()
        count = 1 if disable_live else 2
        wait_for(application, lambda: not workflow.busy and len(workflow.voltage_history.points) == count)
        QTest.qWait(550)
        assert requests == ([0.1] if disable_live else [0.1, 0.2])
    actual = adapter.read_vouts()[2]
    assert abs(actual - (0.1 if disable_live else 0.2)) < 0.001


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_live_mouse_drag_survives_worker_start_finish_and_readback(workspace, application, theme):
    host, page, controllers, tmp_path = workspace
    workflow = page.field_workflow
    page.views.setCurrentIndex(2)
    apply_application_theme(application, theme)
    QTest.qWait(300)
    slider = workflow.voltage_slider
    handle = slider.handle
    ramp = MokeBoxAdapter.ramp_vout
    requests = []
    release = Event()
    disabled_inputs = []

    class InputObserver(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.EnabledChange and not watched.isEnabled():
                disabled_inputs.append(watched)
            return False

    observer = InputObserver(host)
    for widget in (slider, handle, workflow.target):
        widget.installEventFilter(observer)

    def delayed_ramp(device, channel, voltage, **kwargs):
        requests.append(voltage)
        result = ramp(device, channel, voltage, **kwargs)
        if len(requests) == 1:
            release.wait(5)
        return result

    def move_handle(fraction):
        position = QPoint(round(handle.width() / 2 + slider.grooveLength * fraction),
                          slider.height() // 2)
        QTest.mouseMove(handle, handle.mapFrom(slider, position))
        application.processEvents()
        return workflow._voltage(workflow.target)

    with patch.object(MokeBoxAdapter, "ramp_vout", new=delayed_ramp):
        workflow.live_control_switch.setChecked(True)
        QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=handle.rect().center())
        try:
            first_target = move_handle(0.6)
            assert first_target > 0.05  # A real pointer move must change the Fluent slider.
            wait_for(application, lambda: bool(requests))
            assert workflow.busy
            middle_target = move_handle(0.8)
            assert middle_target > first_target
            QTest.qWait(550)
            assert requests == [first_target]
            assert host.grab().save(str(tmp_path / f"moke-live-held-slider-{theme}.png"))
            assert not disabled_inputs, "A Live refresh disabled the pressed control mid-gesture"
            release.set()
            wait_for(application, lambda: not workflow.busy and len(workflow.voltage_history.points) == 1)
            # Keep the mouse pressed through completion and the profile/readback update.
            latest_target = move_handle(0.7)
            assert first_target < latest_target < middle_target
            wait_for(application, lambda: not workflow.busy and len(requests) == 2)
            assert requests == [first_target, latest_target]
            assert not disabled_inputs
            assert workflow.live_control_switch.isChecked()
            assert workflow._voltage(workflow.target) == latest_target
            assert slider.value() == pytest.approx(workflow._slider_mapping.position_for_value(latest_target), abs=12)
            assert slider.isVisibleTo(host)
            assert slider.rect().contains(handle.geometry().center())
            assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == pytest.approx(latest_target, abs=0.001)
        finally:
            release.set()
            QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=handle.rect().center())
        QTest.qWait(550)
        assert requests == [first_target, latest_target]


def test_live_worker_preserves_voltage_editor_focus_caret_and_selection(workspace, application):
    _, page, _, _ = workspace
    workflow = page.field_workflow
    page.views.setCurrentIndex(2)
    QTest.qWait(300)
    ramp = MokeBoxAdapter.ramp_vout
    release = Event()
    started = Event()

    def delayed_ramp(device, channel, voltage, **kwargs):
        result = ramp(device, channel, voltage, **kwargs)
        started.set()
        release.wait(5)
        return result

    with patch.object(MokeBoxAdapter, "ramp_vout", new=delayed_ramp):
        workflow.live_control_switch.setChecked(True)
        workflow.target.setText("100 mV")
        workflow.target.setFocus()
        workflow.target.setSelection(0, 3)
        application.processEvents()
        assert workflow.target.hasFocus()
        try:
            wait_for(application, started.is_set)
            assert workflow.target.hasFocus()
            assert workflow.target.selectedText() == "100"
            release.set()
            wait_for(application, lambda: not workflow.busy)
            QTest.qWait(300)
            assert workflow.target.hasFocus()
            assert workflow.target.selectedText() == "100"
            assert workflow.target.cursorPosition() == 3
            assert workflow.target.text() == "100 mV"
        finally:
            release.set()


def test_operator_settling_time_uses_seconds_and_cannot_shorten_station_floor(workspace, application):
    _, page, controllers, _ = workspace
    workflow = page.field_workflow
    assert workflow.manual_settling.text() == "2 s"
    assert workflow._profile.minimum_settling_s == 2
    for invalid in ("1 s", "200 ms", "2 V", "nan s", "30 s"):
        workflow.manual_settling.setText(invalid)
        assert workflow._manual_envelope is None
        assert not workflow.set_button.isEnabled()
    workflow.manual_settling.setText("3000 ms")
    workflow.live_control_switch.setChecked(True)
    assert workflow._manual_plan.settling_s == 3
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0


def test_live_completion_preserves_saved_calibration_selection_and_review(workspace, application):
    _, page, _, _ = workspace
    workflow = page.field_workflow
    page.views.setCurrentIndex(3)
    workflow.grid_points.setValue(3)
    workflow.samples.setValue(2)
    workflow.repetitions.setValue(1)
    workflow.orientation.setText("+z")
    workflow.geometry.setText("fixed simulated gap")
    workflow.uncertainty.setText("10 uT")
    for index in range(2):
        workflow.probe.setText(f"SIM saved-selection probe {index}")
        workflow.arm_calibration_button.click()
        workflow.start_calibration_button.click()
        wait_for(application, lambda count=index + 1: not workflow.busy and workflow.saved_models.count() == count)
    workflow.saved_models.setCurrentIndex(1)
    selected = workflow.saved_models.currentData()
    workflow.load_model_button.click()
    workflow.reviewed.setChecked(True)
    reviewed_id = workflow._review_model.calibration_id
    page.views.setCurrentIndex(2)
    workflow.clear_history_button.click()
    workflow.live_control_switch.setChecked(True)
    workflow.target.setText("100 mV")
    wait_for(application, lambda: not workflow.busy and len(workflow.voltage_history.points) == 1)
    QTest.qWait(300)  # Include the queued get_control_profile reply after completion.
    assert workflow.saved_models.currentData() == selected
    assert workflow._review_model.calibration_id == reviewed_id
    assert workflow.reviewed.isChecked()
    assert workflow.live_control_switch.isChecked()
    assert workflow.target.text() == "100 mV"


def test_coupled_calibration_reviews_activates_and_previews_field(workspace, application):
    host, page, controllers, tmp_path = workspace
    workflow = page.field_workflow
    page.views.setCurrentIndex(3)
    workflow.grid_points.setValue(3)
    workflow.samples.setValue(2)
    workflow.repetitions.setValue(1)
    workflow.probe.setText("SIM-probe-475")
    workflow.orientation.setText("+z")
    workflow.geometry.setText("synthetic fixed gap")
    workflow.uncertainty.setText("10 uT")
    workflow.arm_calibration_button.click()
    assert workflow.start_calibration_button.isEnabled(), workflow.calibration_status.text()
    workflow.start_calibration_button.click()
    assert workflow.busy
    assert not workflow.live_control_switch.isEnabled()
    wait_for(application, lambda: not workflow.busy, timeout=35)
    assert workflow._last_result is not None, workflow.calibration_status.text()
    assert len(workflow._last_result.points) == 6
    assert "DAC zero confirmed" in workflow.calibration_status.text()
    assert not workflow.activate_button.isEnabled()
    workflow.reviewed.setChecked(True)
    workflow.activate_button.click()
    assert workflow._active_model is not None
    page.views.setCurrentIndex(2)
    workflow.show_field_preview(0)
    assert "B↑" in workflow.field_readout.text()
    assert "B↓" in workflow.field_readout.text()
    assert (tmp_path / "calibrations" / "active_simulation.json").exists()
    assert not (tmp_path / "calibrations" / "active_hardware.json").exists()
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0
    wait_for(application, lambda: workflow.saved_models.count() == 1)
    workflow._review_model = None
    workflow.load_model_button.click()
    assert workflow._review_model is not None
    assert not workflow.reviewed.isChecked()
    assert not workflow.activate_button.isEnabled()
    workflow.target.setText("100 mV")
    expected_preview = workflow.field_readout.text()
    for target in ("100 mV", "250 mV", "-200 mV", "0 V"):
        workflow.target.setText(target)
        assert workflow.set_button.isEnabled()
        workflow.set_button.click()
        wait_for(application, lambda: not workflow.busy and workflow.set_button.isEnabled())
    assert expected_preview != workflow.field_readout.text()
    assert len(workflow.voltage_history.points) == 4
    assert all(abs(point[2]) < 1 for point in workflow.voltage_history.points)
    apply_application_theme(application, "light")
    host.resize(1360, 880)
    application.processEvents()
    paint_end = time.monotonic() + 0.25
    wait_for(application, lambda: time.monotonic() >= paint_end)
    assert host.grab().save(str(tmp_path / "moke-voltage-calibrated-control.png"))
    workflow.zero_button.click()
    wait_for(application, lambda: not workflow.busy)


def test_close_requests_stop_and_keeps_active_worker_alive(workspace, application):
    _, page, _, _ = workspace
    workflow = page.field_workflow
    # An externally controlled recipe revokes manual permissions immediately.
    workflow.target.setText("100 mV")
    workflow.set_execution_controlled(True)
    assert not workflow.set_button.isEnabled()
    assert not workflow.arm_calibration_button.isEnabled()
    assert workflow.prepare_application_shutdown()
    workflow.set_execution_controlled(False)
    workflow.grid_points.setValue(101)
    workflow.probe.setText("SIM stop probe")
    workflow.orientation.setText("+z")
    workflow.geometry.setText("fixed synthetic gap")
    workflow.uncertainty.setText("10 uT")
    workflow.arm_calibration_button.click()
    assert workflow.start_calibration_button.isEnabled()
    workflow.start_calibration_button.click()
    assert workflow.busy
    assert not workflow.prepare_application_shutdown()
    wait_for(application, lambda: not workflow.busy)
    assert workflow._last_result is None
    assert workflow.set_button.isEnabled()


def test_copied_keithley_edit_button_changes_operator_range_without_hardware_write(workspace, application):
    _, page, controllers, _ = workspace
    workflow = page.field_workflow
    workflow.target.setText("100 mV")
    assert workflow.set_button.isEnabled()
    workflow.probe.setText("Editing calibration metadata leaves manual voltage control independent")
    assert workflow.set_button.isEnabled()
    with patch("app.devices.moke_box.ui.configuration_panel.LimitEditDialog") as dialog_class:
        dialog = dialog_class.return_value
        dialog.DialogCode = QDialog.DialogCode
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.minimum.text.return_value = "-200 mV"
        dialog.maximum.text.return_value = "300 mV"
        workflow.configuration_panel.level_field.edit_button.click()
        dialog_class.assert_called_once()
    assert workflow.configuration_panel.minimum_text == "-200 mV"
    assert workflow.configuration_panel.maximum_text == "300 mV"
    assert workflow.set_button.isEnabled()
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0
    assert not hasattr(workflow.configuration_panel, "mode")
    labels = [workflow.configuration_panel.form.itemAt(row, workflow.configuration_panel.form.ItemRole.LabelRole).widget().text()
              for row in range(workflow.configuration_panel.form.rowCount())]
    assert "Source mode" not in labels
    workflow.read_voltage_button.click()
    wait_for(application, lambda: bool(workflow.voltage_history.points))
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0


def test_full_fluent_shell_binds_control_calibration_and_voltage_sweep(application, tmp_path):
    settings_path = tmp_path / "settings.yml"
    write_engineer_settings(settings_path)
    repository = SettingsRepository(settings_path)
    raw = repository.load().raw
    raw["storage"]["output_directory"] = str(tmp_path / "measurements")
    raw["storage"]["catalogue_directory"] = str(tmp_path / "catalogue")
    raw["devices"]["moke_box"]["calibration_directory"] = str(tmp_path / "calibrations")
    repository.save_raw(raw)
    window = MainWindow(settings_path, simulation=True, authenticated_username=TEST_ENGINEER)
    try:
        window.recipe_page.path.setText(str(tmp_path / "draft.yml"))
        window.resize(1360, 880)
        window.show()
        window._navigate_to("keithley")
        application.processEvents()
        reference_end = time.monotonic() + 0.25
        wait_for(application, lambda: time.monotonic() >= reference_end)
        assert window.keithley_page.source_scroll.grab().save(str(tmp_path / "keithley-source-reference.png"))
        window._navigate_to("moke_box")
        workflow = window.moke_box_page.field_workflow
        window.moke_box_page.views.setCurrentIndex(2)
        for key in ("moke_box", "lakeshore_gaussmeter"):
            window._controllers[key].call("connect")
        wait_for(application, lambda: workflow._profile is not None and workflow._reference_connected)
        assert workflow.control_page.isVisibleTo(window)
        assert workflow.channel_selector.isVisibleTo(window)
        assert workflow.voltage_history.isVisibleTo(window)
        assert workflow.voltage_history.width() > 240
        viewport = window.navigation_routes["moke_box"].scroll_area.viewport()
        for control in (workflow.live_control_switch, workflow.set_button, workflow.zero_button):
            assert viewport.rect().contains(QRect(control.mapTo(viewport, QPoint()), control.size()))
        workflow.target.setText("100 mV")
        assert workflow.set_button.isEnabled(), workflow.manual_status.text()
        workflow.set_button.click()
        wait_for(application, lambda: not workflow.busy)
        assert "readback confirmed" in workflow.manual_status.text()
        assert "unknown" in window.safety_strip.outputs.text()
        reference = window.keithley_page.configuration_panel
        copied = workflow.configuration_panel
        assert type(reference.level_field) is type(copied.level_field)
        assert type(reference.level_field.range_pill) is type(copied.level_field.range_pill)
        assert type(reference.level_field.edit_button) is type(copied.level_field.edit_button)
        assert reference.form.horizontalSpacing() == copied.form.horizontalSpacing()
        assert reference.form.verticalSpacing() == copied.form.verticalSpacing()
        assert reference.layout().contentsMargins() == copied.layout().contentsMargins()
        assert type(workflow.live_control_switch) is type(window.keithley_page.live_control_switch)
        definition = {"target": "moke_box.vout2.voltage", "dimension": "voltage"}
        from PySide6.QtCore import QTimer
        modal_observations = []

        class AcceptedMokeDialog(SweepGeneratorDialog):
            def __init__(self, definition, parent, **kwargs):
                super().__init__(definition, parent, initial_segments=kwargs.get("initial_segments") or
                                 [{"start": "-0.5 V", "stop": "0.5 V", "points": 3}])

            def exec(self):
                if modal_observations:
                    self.resize(980, 680)
                    apply_application_theme(application, "dark")
                def accept_visible():
                    assert self.isVisible()
                    assert self.channel_selector.count() == 8
                    assert self.channel_selector.currentData() == 2
                    assert [item.isEnabled for item in self.channel_selector.items] == [False, False, True, False, False, False, False, False]
                    assert self.channel_selector.width() > 100
                    assert self.rect().contains(self.channel_selector.mapTo(self, self.channel_selector.rect().center()))
                    assert self.rect().contains(self.create_button.mapTo(self, self.create_button.rect().center()))
                    assert self.plot_panel.rect().contains(self.preview.geometry())
                    assert self.plot_panel.rect().contains(self.field_preview.geometry())
                    modal_observations.append(self.channel_selector.currentData())
                    assert self.grab().save(str(tmp_path / f"moke-sweep-modal-{len(modal_observations)}.png"))
                    self.accept()
                QTimer.singleShot(200, accept_visible)
                result = super().exec()
                apply_application_theme(application, "light")
                return result

        empty_source = yaml.safe_dump({"schema_version": 1, "name": "MOKE library", "root":
                                       {"id": "main", "type": "sequence", "children": []}})
        window.recipe_page._apply_builder_source(empty_source, "New integration draft")
        with patch("app.ui.recipes.page.SweepGeneratorDialog.exec", return_value=QDialog.DialogCode.Rejected):
            window.recipe_page._library_add_device("moke_box", parent_id="main", branch="children", index=0)
        assert window.recipe_page._builder_source() == empty_source
        with patch("app.ui.recipes.page.SweepGeneratorDialog", AcceptedMokeDialog):
            window.recipe_page._library_add_device("moke_box", parent_id="main", branch="children", index=0)
            library_recipe = parse_recipe_text(window.recipe_page._builder_source())
            module_node = library_recipe.root.children[0]
            assert module_node.data["device_module"] == "moke_box"
            before_children = [child.id for child in module_node.children]
            window.recipe_page._edit_moke_module_node(module_node)
            edited = parse_recipe_text(window.recipe_page._builder_source()).root.children[0]
            assert [child.id for child in edited.children] == before_children
            assert [child.id for child in edited.children[2].children] == [child.id for child in module_node.children[2].children]
        assert modal_observations == [2, 2]
        library_plan = RecipeCompiler(window._settings).compile(parse_recipe_text(window.recipe_page._builder_source()))
        assert library_plan.total_points == 3
        assert library_plan.required_devices == frozenset({"moke_box"})
        assert window.recipe_page._drop_library_block("safety:moke_zero", "__finally__", "children", 0)
        safety_recipe = parse_recipe_text(window.recipe_page._builder_source())
        assert safety_recipe.finally_nodes[0].type == "stop_moke_voltage"
        fixed_definition = dict(next(item for item in window.recipe_page._recipe_parameter_definitions
                                     if item["target"] == "moke_box.vout2.voltage"))
        fixed_dialog = FixedValueDialog(fixed_definition, window.recipe_page)
        fixed_dialog.value.setText("100 mV")
        assert fixed_dialog.channel_selector.currentData() == 2
        fixed_node = window.recipe_page._fixed_node_from_dialog(fixed_definition, fixed_dialog)
        fixed_dialog.close()
        fixed_source = yaml.safe_dump({"schema_version": 1, "name": "Fixed MOKE", "root": fixed_node})
        window.recipe_page._apply_builder_source(fixed_source, "Fixed MOKE integration")
        fixed_recipe = parse_recipe_text(fixed_source)
        with patch("app.ui.recipes.page.FixedValueDialog.exec", return_value=QDialog.DialogCode.Accepted):
            window.recipe_page._edit_moke_module_node(fixed_recipe.root)
        fixed_plan = RecipeCompiler(window._settings).compile(parse_recipe_text(window.recipe_page._builder_source()))
        assert [action.kind for action in fixed_plan.actions] == [
            "configure_moke_box", "arm_moke_voltage", "update_moke_voltage", "stop_moke_voltage"]
        assert fixed_plan.actions[2].payload["voltage_v"] == 0.1
        node = window.recipe_page._sweep_node_from_generator(
            definition, [{"start": "-0.5 V", "stop": "0.5 V", "points": 3}])
        moke_definitions = [item for item in window.recipe_page._recipe_parameter_definitions
                            if item["target"].startswith("moke_box.")]
        assert [item["target"] for item in moke_definitions] == ["moke_box.vout2.voltage"]
        window._navigate_to("sweeps")
        tree_source = yaml.safe_dump({"schema_version": 1, "name": "MOKE tree integration", "root": node})
        window.recipe_page._apply_builder_source(tree_source, "MOKE integration test")
        # This test owns the temporary draft; acknowledge discarding it on close.
        window.recipe_page._close_discard_confirmed = True
        tree = window.recipe_page.measurement_tree
        tree.expandAll()
        application.processEvents()
        axes = [item for item in window.recipe_page.tree_model.tree.by_id.values()
                if item.axis is not None and item.axis.device_module == "moke_box"]
        assert axes
        axis = axes[0]
        assert axis.axis.target == "moke_box.vout2.voltage"
        assert axis.axis.parameter_id == "output.voltage"
        index = window.recipe_page.tree_model.index_for_semantic_id(axis.semantic_id)
        window.navigation_routes["sweeps"].scroll_area.ensureWidgetVisible(tree)
        page_scroll = window.navigation_routes["sweeps"].scroll_area
        page_scroll.verticalScrollBar().setValue(page_scroll.verticalScrollBar().maximum())
        tree.scrollTo(index)
        QTest.qWait(300)
        assert tree.isVisibleTo(window)
        assert tree.width() > 200 and tree.height() > 200
        assert tree.visualRect(index).intersects(tree.viewport().rect())
        axis_rect = tree.visualRect(index)
        assert page_scroll.viewport().rect().contains(
            tree.viewport().mapTo(page_scroll.viewport(), axis_rect.center()))
        assert window.grab().save(str(tmp_path / "moke-sweeps-tree.png"))
        tree_plan = RecipeCompiler(window._settings).compile(parse_recipe_text(tree_source))
        assert tree_plan.total_points == 3
        assert tree_plan.required_devices == frozenset({"moke_box"})
        window._navigate_to("moke_box")
        document = {"schema_version": 1, "name": "Generated MOKE sweep", "root": node}
        plan = RecipeCompiler(window._settings).compile(parse_recipe_text(yaml.safe_dump(document)))
        assert plan.actions[0].kind == "configure_moke_box"
        assert plan.actions[1].kind == "arm_moke_voltage"
        assert sum(action.kind == "update_moke_voltage" for action in plan.actions) == 3
        assert plan.actions[-1].kind == "stop_moke_voltage"
        window.moke_box_page.views.setCurrentIndex(3)
        workflow.grid_points.setValue(3)
        workflow.samples.setValue(2)
        workflow.repetitions.setValue(1)
        workflow.probe.setText("SIM full-shell probe")
        workflow.orientation.setText("+z")
        workflow.geometry.setText("fixed synthetic gap")
        workflow.uncertainty.setText("10 uT")
        workflow.arm_calibration_button.click()
        assert workflow.start_calibration_button.isEnabled(), workflow.calibration_status.text()
        workflow.start_calibration_button.click()
        wait_for(application, lambda: not workflow.busy, timeout=35)
        assert workflow._last_result is not None, workflow.calibration_status.text()
        workflow.reviewed.setChecked(True)
        workflow.activate_button.click()
        assert workflow._active_model is not None
        window.moke_box_page.views.setCurrentIndex(2)
        workflow.target.setText("100 mV")
        assert workflow.set_button.isEnabled()
        workflow.set_button.click()
        wait_for(application, lambda: not workflow.busy)
        assert "B↑" in workflow.field_readout.text()
        assert "B↓" in workflow.field_readout.text()
        workflow.live_control_switch.setChecked(True)
        workflow.target.setText("250 mV")
        wait_for(application, lambda: len(workflow.voltage_history.points) == 2 and not workflow.busy)
        assert abs(window._controllers["moke_box"].adapter_for_run().read_vouts()[2] - 0.25) < 0.001
        paint_end = time.monotonic() + 0.25
        wait_for(application, lambda: time.monotonic() >= paint_end)
        assert window.grab().save(str(tmp_path / "moke-voltage-full-shell.png"))
        workflow.zero_button.click()
        wait_for(application, lambda: not workflow.busy)
    finally:
        window.recipe_page._close_discard_confirmed = True
        window.close()
        application.processEvents()


def test_single_point_moke_generator_uses_station_envelope(application, tmp_path):
    raw = simulated_station_settings(loaded_settings()).model_dump(mode="python")
    raw["devices"]["moke_box"]["calibration_directory"] = str(tmp_path / "calibrations")
    settings = StationSettings.model_validate(raw)
    page = RecipePage(settings)
    try:
        definition = dict(next(item for item in page._recipe_parameter_definitions
                               if item["target"] == "moke_box.vout2.voltage"))
        node = page._sweep_node_from_generator(definition, [{"value": "200 mV"}])
        recipe = parse_recipe_text(yaml.safe_dump({"schema_version": 1, "name": "One MOKE point", "root": node}))
        plan = RecipeCompiler(settings).compile(recipe)
        assert plan.total_points == 1
        assert plan.actions[0].payload["plan"].targets_v == (0.2,)
        assert plan.actions[0].payload["plan"].minimum_v == -1
        assert plan.actions[0].payload["plan"].maximum_v == 1
    finally:
        page._close_discard_confirmed = True
        page.close()


@pytest.mark.parametrize("width,height,theme", [(1360, 880, "light"), (980, 880, "dark")])
def test_readonly_moke_explains_control_lock_and_never_claims_kepco_off(application, tmp_path, width, height, theme):
    settings_path = tmp_path / "settings.yml"
    write_engineer_settings(settings_path)
    repository = SettingsRepository(settings_path)
    raw = repository.load().raw
    raw["storage"]["output_directory"] = str(tmp_path / "measurements")
    raw["storage"]["catalogue_directory"] = str(tmp_path / "catalogue")
    raw["devices"]["moke_box"]["calibration_directory"] = str(tmp_path / "calibrations")
    repository.save_raw(raw)
    window = MainWindow(settings_path, simulation=True, authenticated_username=TEST_ENGINEER)
    transport = SimulatedMokeBoxTransport(SimulationContext(seed=0))
    commands = []
    original_send = transport.send

    def record_send(frame):
        commands.append(frame)
        original_send(frame)

    transport.send = record_send
    adapter = MokeBoxAdapter(MokeBoxConfig("SIM::MOKE::INSTR"), transport)
    controller = window._controllers["moke_box"]
    try:
        controller.reconfigure(adapter)
        window.resize(width, height)
        window.show()
        window._set_theme_mode(theme, persist=False)
        window._navigate_to("moke_box")
        page = window.moke_box_page
        workflow = page.field_workflow
        page.views.setCurrentIndex(2)
        controller.call("connect")
        wait_for(application, lambda: workflow._connected and window._device_states["moke_box"] == "verified")
        assert workflow._profile is None
        assert "Read-only connection" in page.safety_note.text()
        assert "no approved control profile" in page.safety_note.text()
        assert "no approved voltage-control profile" in workflow.manual_status.text()
        for control in (workflow.set_button, workflow.zero_button, workflow.live_control_switch):
            assert not control.isEnabled()
            assert "no approved voltage-control profile" in control.toolTip()
        assert not workflow.arm_calibration_button.isEnabled()
        assert "unknown" in window.safety_strip.outputs.text()
        assert "off" not in window.safety_strip.outputs.text().lower()
        assert workflow.read_voltage_button.text() == "Read selected VOUT"
        workflow.read_voltage_button.click()
        wait_for(application, lambda: len(workflow.voltage_history.points) == 1)
        assert page.views.currentIndex() == 2
        assert "Confirmed DAC: +0.000000 V" in workflow.voltage_readout.text()
        workflow.read_configuration_button.click()
        wait_for(application, lambda: len(workflow.voltage_history.points) == 2)
        assert page.views.currentIndex() == 0
        assert all("+0.000000 V" in value.text() for value in page.vout_values.values())
        assert commands and all(command == bytes.fromhex("18000018") for command in commands)
        page.views.setCurrentIndex(2)
        paint_end = time.monotonic() + 0.25
        wait_for(application, lambda: time.monotonic() >= paint_end)
        assert workflow.read_voltage_button.isVisibleTo(window)
        viewport = window.navigation_routes["moke_box"].scroll_area.viewport()
        for control in (workflow.live_control_switch, workflow.set_button, workflow.zero_button):
            assert viewport.rect().contains(QRect(control.mapTo(viewport, QPoint()), control.size()))
        assert window.grab().save(str(tmp_path / f"moke-readonly-{theme}.png"))
    finally:
        window.close()
        application.processEvents()
