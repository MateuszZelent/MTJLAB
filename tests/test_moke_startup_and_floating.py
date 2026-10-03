"""Readback initializes independent drafts; floating controls keep one writer."""

import time
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QPoint, QRect, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QVBoxLayout, QWidget

from app.bootstrap import StationComposition
from app.devices.moke_box.protocol import MokeCommandType, MokeFrame
from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
from app.devices.moke_box.ui.page import MokeBoxPage
from app.domain.quantities import DIMENSION_VOLTAGE, parse_quantity
from app.ui.design_system import apply_application_theme
from app.ui.quick_controls import QuickControlsWindow
from app.ui.workers import DeviceController
from tests.test_fluent_moke_field_workflow import application as application, workspace as workspace, wait_for
from tests.test_moke_dual_outputs import dual_settings
from tests.test_moke_quick_controls import coordinator_for


@pytest.fixture
def existing_outputs(application, tmp_path, monkeypatch):
    initial = {channel: .03 * (channel - 4) for channel in range(8)}
    initial.update({0: .75, 2: -.2})
    sent, transports = [], []
    connect, send = SimulatedMokeBoxTransport.connect, SimulatedMokeBoxTransport.send

    def connect_existing(transport, endpoint, timeout):
        connect(transport, endpoint, timeout)
        transport._vouts.update(initial)
        transports.append(transport)

    def record_send(transport, raw):
        sent.append(MokeFrame.decode(raw))
        send(transport, raw)

    monkeypatch.setattr(SimulatedMokeBoxTransport, "connect", connect_existing)
    monkeypatch.setattr(SimulatedMokeBoxTransport, "send", record_send)
    settings = dual_settings(tmp_path)
    host = QWidget()
    layout = QVBoxLayout(host)
    controllers = StationComposition(settings, simulation=True).create_controllers(("moke_box",), host)
    page = MokeBoxPage(controllers["moke_box"], settings, host)
    layout.addWidget(page)
    workflow = page.field_workflow
    workflow._simulation = True
    coordinator = coordinator_for(host, workflow, settings)
    host.resize(1360, 880)
    host.show()
    page.views.setCurrentIndex(2)
    yield host, page, controllers["moke_box"], initial, sent, transports, coordinator
    if workflow.busy:
        workflow.stop()
        wait_for(application, lambda: not workflow.busy)
    page._dock_controls()
    assert DeviceController.close_all(controllers.values())
    host.close()
    application.processEvents()


def test_first_readback_populates_all_channels_and_retains_safety_limits(existing_outputs, application):
    _, page, controller, _, sent, _, coordinator = existing_outputs
    workflow = page.field_workflow
    controller.call("connect")
    wait_for(application, lambda: len(workflow._initialized_voltage_channels) == 8)
    for channel in (0, 2, 1, 3, 4, 5, 6, 7, 0):
        workflow.channel_selector.setCurrentIndex(workflow.channel_selector.findData(channel))
        application.processEvents()
        actual_v = workflow._last_voltages[channel]
        assert workflow._voltage(workflow.target) == actual_v
        assert workflow._slider_mapping.value_for_position(workflow.voltage_slider.value()) == pytest.approx(
            actual_v, abs=workflow._slider_mapping.step_si)
        assert f"{actual_v:+.6f} V" in workflow.voltage_readout.text()
        target = f"moke_box.vout{channel}.voltage"
        assert parse_quantity(coordinator.draft_text(target), DIMENSION_VOLTAGE).si_value == actual_v
        assert coordinator._confirmed_values[target] == actual_v
        assert workflow.target.isEnabled() == (channel in (0, 2))
    assert workflow.configuration_panel.minimum_text == "-0.5 V"
    assert workflow.configuration_panel.maximum_text == "0.5 V"
    assert workflow.initial_readback_note.isVisible()
    assert not workflow.set_button.isEnabled()  # .75 V was read, never approved as an operator target.
    assert not workflow.live_control_switch.isChecked()
    assert all(frame.record_type == MokeCommandType.READBACK_VOUT for frame in sent)
    settings = QSettings("LabControl", "LabControl")
    keys = ("quick_controls/targets", "quick_controls/outputs", "quick_controls/geometry")
    saved = {key: (settings.contains(key), settings.value(key)) for key in keys}
    window = QuickControlsWindow(coordinator, page)
    try:
        window.set_output_targets(())
        window.set_targets(("moke_box.vout0.voltage", "moke_box.vout2.voltage"))
        window.show()
        application.processEvents()
        for channel in (0, 2):
            row = window._rows[f"moke_box.vout{channel}.voltage"]
            actual_v = workflow._last_voltages[channel]
            assert row.slider.value_si() == actual_v
            assert row.slider._mapping.value_for_position(row.slider.slider.value()) == pytest.approx(
                actual_v, abs=(row.slider._mapping.maximum_si - row.slider._mapping.minimum_si) / 10000)
        row = window._rows["moke_box.vout0.voltage"]
        assert "outside working range" in row.confirmed_dac.text()
        assert coordinator.bound("moke_box.vout0.voltage").maximum_si == .5
        row.slider.slider.setValue(row.slider.slider.maximum() - 1)
        assert row.slider.value_si() <= .5
        assert all(frame.record_type == MokeCommandType.READBACK_VOUT for frame in sent)
    finally:
        window.close()
        for key, (existed, value) in saved.items():
            settings.setValue(key, value) if existed else settings.remove(key)
    # A deliberate slider edit is bounded by working limits even when the
    # display extends to the actual out-of-range initial DAC.
    workflow.voltage_slider.setValue(workflow.voltage_slider.maximum() - 1)
    assert workflow._voltage(workflow.target) <= .5
    assert all(frame.record_type == MokeCommandType.READBACK_VOUT for frame in sent)


def test_late_first_readback_preserves_edits_and_channel_drafts(existing_outputs, application, monkeypatch):
    _, page, controller, _, sent, _, _ = existing_outputs
    workflow = page.field_workflow
    entered, release = Event(), Event()
    original = SimulatedMokeBoxTransport.recv_exact
    reads = [0]

    def delayed_initial_readback(transport, count):
        reads[0] += 1
        if reads[0] == 2:  # First is connect qualification, second is the UI snapshot.
            entered.set()
            release.wait(4)
        return original(transport, count)

    monkeypatch.setattr(SimulatedMokeBoxTransport, "recv_exact", delayed_initial_readback)
    try:
        controller.call("connect")
        wait_for(application, entered.is_set)
        assert not workflow.set_button.isEnabled()
        workflow.target.setText("200 mV")
        workflow.channel_selector.setCurrentIndex(workflow.channel_selector.findData(2))
        workflow.target.setText("120 mV")
        workflow.channel_selector.setCurrentIndex(workflow.channel_selector.findData(0))
        release.set()
        wait_for(application, lambda: len(workflow._initialized_voltage_channels) == 8)
        assert workflow.target.text() == "200 mV"
        workflow.channel_selector.setCurrentIndex(workflow.channel_selector.findData(2))
        assert workflow.target.text() == "120 mV"
        assert workflow._last_voltages[2] < 0
        assert all(frame.record_type == MokeCommandType.READBACK_VOUT for frame in sent)
    finally:
        release.set()


def test_subsequent_readback_and_late_quick_binding_do_not_replace_drafts(existing_outputs, application):
    host, page, controller, _, sent, transports, _ = existing_outputs
    workflow = page.field_workflow
    controller.call("connect")
    wait_for(application, lambda: len(workflow._initialized_voltage_channels) == 8)
    initial_drafts = dict(workflow.quick_control_drafts())
    coordinator = coordinator_for(host, workflow, page._settings)
    for channel in (0, 2):
        target = f"moke_box.vout{channel}.voltage"
        assert coordinator.draft_text(target) == initial_drafts[target]
        assert coordinator._confirmed_values[target] == workflow._last_voltages[channel]
    transports[0]._vouts.update({0: .4, 2: .05})  # Simulate externally changed hardware.
    controller.call("read_vouts")
    wait_for(application, lambda: workflow._last_voltages[0] < .5)
    for channel in (0, 2):
        workflow.channel_selector.setCurrentIndex(workflow.channel_selector.findData(channel))
        assert workflow.target.text() == initial_drafts[f"moke_box.vout{channel}.voltage"]
    assert all(frame.record_type == MokeCommandType.READBACK_VOUT for frame in sent)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_floating_controls_geometry_plot_toggle_docking_and_live(workspace, application, theme, monkeypatch):
    host, page, _, _ = workspace
    workflow = page.field_workflow
    wait_for(application, lambda: len(workflow._initialized_voltage_channels) == 8)
    apply_application_theme(application, theme)
    page.views.setCurrentIndex(2)
    original = SimulatedMokeBoxTransport.send
    writes = []

    def slow_send(transport, raw):
        if MokeFrame.decode(raw).record_type == MokeCommandType.SET_VOUT:
            writes.append(raw)
            time.sleep(.08)
        original(transport, raw)

    monkeypatch.setattr(SimulatedMokeBoxTransport, "send", slow_send)
    target, slider, history = workflow.target, workflow.voltage_slider, workflow.voltage_history
    workflow.open_floating_button.click()
    window = page._control_window
    assert window is not None and not window.isModal()
    QTest.qWait(200)
    assert workflow.control_page.isVisibleTo(window)
    assert page._control_placeholder.isVisibleTo(host)
    assert workflow.target is target and workflow.voltage_slider is slider and workflow.voltage_history is history
    assert not writes
    artifacts = Path("artifacts/moke-floating-controls")
    artifacts.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(artifacts / f"plot-{theme}.png"))
    window.resize(760, 720)
    QTest.qWait(150)
    assert workflow.workspace_splitter.orientation() == Qt.Orientation.Horizontal
    for button in (workflow.live_control_switch, workflow.set_button, workflow.zero_button):
        assert button.isVisibleTo(window)
        assert window.rect().contains(QRect(button.mapTo(window, QPoint()), button.size()))
    assert window.grab().save(str(artifacts / f"narrow-{theme}.png"))
    window.resize(540, 840)
    QTest.qWait(150)
    assert workflow.workspace_splitter.orientation() == Qt.Orientation.Vertical
    assert workflow.workspace_splitter.sizes()[0] > workflow.workspace_splitter.sizes()[1]
    assert workflow.compact_field_note.isVisibleTo(window)
    assert not workflow.field_note.isVisible()
    assert window.grab().save(str(artifacts / f"stacked-{theme}.png"))
    window.resize(1120, 840)
    window.show_history.setChecked(False)
    QTest.qWait(200)
    assert not history.isVisible()
    assert workflow.field_card.isVisibleTo(window)
    assert workflow.compact_field_note.isVisibleTo(window)
    assert window.width() < 650
    assert not writes
    for button in (workflow.live_control_switch, workflow.set_button, workflow.zero_button):
        assert button.isVisibleTo(window)
        assert window.rect().contains(QRect(button.mapTo(window, QPoint()), button.size()))
    assert window.grab().save(str(artifacts / f"compact-{theme}.png"))
    workflow.live_control_switch.setChecked(True)
    workflow.target.setText("400 mV")
    wait_for(application, lambda: workflow.busy and bool(writes))
    thread = workflow._thread
    window.show_history.setChecked(True)
    QTest.qWait(100)
    assert workflow._thread is thread and history.isVisibleTo(window)
    workflow.target.setText("-100 mV")
    wait_for(application, lambda: not workflow.busy)
    QTest.qWait(550)
    assert workflow._last_voltages[2] == pytest.approx(-.1, abs=.001)
    assert len(workflow._test_voltage_completions) == 1
    assert history.points and history.requested_points
    write_count = len(writes)
    window.titleBar.closeBtn.click()
    QTest.qWait(150)
    assert page._control_window is None
    assert workflow.control_page.isVisibleTo(host)
    assert workflow.field_card.parent() is page.field_workflow._field_scroll.widget()
    assert history.isVisibleTo(host)
    assert workflow.target is target and workflow.target.text() == "-100 mV"
    assert workflow.live_control_switch.isChecked()
    assert len(writes) == write_count
    workflow.open_floating_button.click()
    assert page._control_window is not window
    page._control_window.show_history.setChecked(False)
    page._control_window.dock_button.click()
    assert page._control_window is None and history.isVisibleTo(host)
