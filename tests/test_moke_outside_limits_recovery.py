"""An existing test DAC voltage can return to zero without widening normal limits."""
from dataclasses import replace
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontDatabase

from app.devices.moke_box.protocol import set_vout, decode_voltage, encode_voltage
from app.devices.moke_box.ui.configuration_panel import MokeVoltageConfigurationPanel
from app.ui.widgets import LimitEditDialog
from app.domain.errors import DeviceError
from tests.test_moke_voltage_control import controlled_adapter, plan_for, mutations


def recovery_adapter(initial):
    adapter, transport, profile = controlled_adapter(minimum_settling_s=0)
    profile = replace(profile, kepco_mode="dac_test")
    adapter._config = replace(adapter._config, control_profile=profile)
    transport.send(set_vout(2, initial))
    transport.sent.clear()
    return adapter, transport, profile


@pytest.mark.parametrize("initial", [2.0, -2.0])
@pytest.mark.parametrize("operation", ["stop", "apply_zero"])
def test_existing_test_dac_outside_limits_returns_monotonically_to_zero(initial, operation):
    adapter, transport, profile = recovery_adapter(initial)
    start = adapter.read_vouts()[2]
    if operation == "stop":
        result = adapter.stop_vout(2)
        assert result.safe_target_confirmed
    else:
        plan = plan_for(profile, targets=(0,))
        adapter.configure_voltage_plan(plan)
        adapter.arm_voltage_plan(plan)
        result = adapter.ramp_vout(2, 0)
    values = [decode_voltage(frame.msb, frame.lsb) for frame in mutations(transport)]
    assert len(values) > 30 and values[-1] == 0
    previous = start
    for value in values:
        assert abs(value) < abs(previous)
        assert value * start >= 0
        assert abs(value - previous) <= profile.maximum_step_v
        previous = value
    assert all(frame.channel == 2 for frame in mutations(transport))
    assert result.actual_v == 0
    assert profile.minimum_v == -1 and profile.maximum_v == 1


def test_existing_outside_voltage_does_not_authorize_nonzero_target():
    adapter, transport, profile = recovery_adapter(2)
    plan = plan_for(profile, targets=(0.2,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    with pytest.raises(DeviceError, match="outside the qualified station envelope"):
        adapter.ramp_vout(2, 0.2)
    assert not mutations(transport)


def test_recovery_waits_for_fresh_confirmation_without_repeating_set(monkeypatch):
    adapter, transport, _ = recovery_adapter(2)
    original = adapter._read_vouts_from_transport
    count = 0
    def read():
        nonlocal count
        values = original()
        count += 1
        if count == 2:
            values[2] = decode_voltage(*encode_voltage(2))
        return values
    monkeypatch.setattr(adapter, "_read_vouts_from_transport", read)
    assert adapter.stop_vout(2).safe_target_confirmed
    values = [decode_voltage(frame.msb, frame.lsb) for frame in mutations(transport)]
    assert len(values) == len(set(values))  # Confirmation is retried; SET is not.


def test_recovery_honors_physical_slew_timing():
    import time
    adapter, transport, profile = controlled_adapter(simulation=False, minimum_settling_s=0)
    profile = replace(profile, kepco_mode="dac_test")
    adapter._config = replace(adapter._config, control_profile=profile)
    transport.send(set_vout(2, 2))
    start = adapter.read_vouts()[2]
    began = time.monotonic()
    assert adapter.stop_vout(2).safe_target_confirmed
    assert time.monotonic() - began >= abs(start) / profile.maximum_slew_v_s


@pytest.mark.parametrize("completion", ["hold", "automatic"])
def test_sweep_initial_zero_recovers_two_volts_then_applies_completion_policy(completion):
    import yaml
    from app.engine import RecipeCompiler, RecipeRunner
    from app.recipes import parse_recipe_text
    from app.ui.recipes.page import RecipePage
    from app.safety.moke_box import control_profile_from_settings
    from tests.test_final_output_state import rig, moke_completion_recipe
    from tests.test_adapters_and_runner import MemoryWriter
    settings, adapter, transport, devices = rig("moke_box")
    primary = settings.moke_box.voltage_control.model_copy(update={"kepco_mode": "dac_test"})
    settings = settings.model_copy(update={"devices": settings.devices.model_copy(update={
        "moke_box": settings.moke_box.model_copy(update={"voltage_control": primary})})})
    adapter._config = replace(adapter._config, control_profile=control_profile_from_settings(settings, simulation=True))
    transport.send(set_vout(2, 2))
    recipe = moke_completion_recipe(completion)
    root = RecipePage._node_to_mapping(recipe.root)
    root["children"].insert(0, {"id": "initial-zero", "type": "set_moke_voltage", "channel": 2, "voltage": "0 V"})
    source = yaml.safe_dump({"schema_version": 1, "name": "recover-and-measure", "root": root,
                            "finally": [RecipePage._node_to_mapping(node) for node in recipe.finally_nodes]})
    plan = RecipeCompiler(settings).compile(parse_recipe_text(source))
    result = RecipeRunner(**devices, writer=MemoryWriter()).run(plan)
    assert result.error is None
    assert adapter.read_vouts()[2] == pytest.approx(0.2 if completion == "hold" else 0, abs=.00031)


def test_coil_profile_does_not_inherit_test_dac_recovery():
    adapter, transport, _ = controlled_adapter(minimum_settling_s=0)
    transport.send(set_vout(2, 2))
    transport.sent.clear()
    with pytest.raises(DeviceError, match="outside the qualified station envelope"):
        adapter.stop_vout(2)
    assert not mutations(transport)


def test_recovery_fault_never_reports_zero(monkeypatch):
    adapter, transport, _ = recovery_adapter(2)
    original = adapter._read_vouts_from_transport
    count = 0
    def read():
        nonlocal count
        values = original()
        count += 1
        if count > 1:
            values[2] = 2.5
        return values
    monkeypatch.setattr(adapter, "_read_vouts_from_transport", read)
    with pytest.raises(DeviceError, match="did not move toward zero"):
        adapter.stop_vout(2)
    assert len(mutations(transport)) == 1
    assert not adapter._safe_target_confirmed


def test_limits_modal_reports_invalid_range_and_applies_valid_range(tmp_path):
    application = QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/arial.ttf")
    application.setFont(QFont("Arial", 10))
    panel = MokeVoltageConfigurationPanel()
    _, _, profile = recovery_adapter(2)
    panel.profile = profile
    changes = []
    panel.range_changed.connect(lambda low, high: changes.append((low, high)))
    dialogs = []
    def edit(dialog):
        dialogs.append(dialog)
        dialog.show()
        application.processEvents()
        from qfluentwidgets import PrimaryPushButton
        save = next(button for button in dialog.findChildren(PrimaryPushButton) if button.text() == "Save limits")
        dialog.minimum.setText("-3 V")
        dialog.maximum.setText("3 V")
        QTest.mouseClick(save, Qt.MouseButton.LeftButton)
        assert dialog.result() == 0
        assert dialog.validation_error.isVisible()
        assert "[-1, 1] V" in dialog.validation_error.text()
        assert not changes
        assert dialog.grab().save(str(tmp_path / "moke-limit-error.png"))
        dialog.minimum.setText("-900 mV")
        dialog.maximum.setText("900 mV")
        QTest.mouseClick(save, Qt.MouseButton.LeftButton)
        assert dialog.result() == 1
        return dialog.result()
    try:
        panel.show()
        with patch.object(LimitEditDialog, "exec", edit):
            panel.level_field.edit_button.click()
        assert changes == [("-900 mV", "900 mV")]
        assert panel.minimum_text == "-900 mV" and panel.maximum_text == "900 mV"
        assert profile.minimum_v == -1 and profile.maximum_v == 1
    finally:
        for dialog in dialogs:
            dialog.close()
            dialog.deleteLater()
        panel.close()
        panel.deleteLater()
        application.processEvents()



def test_page_zero_button_recovers_existing_test_channel_and_range_edits_work(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QWidget, QVBoxLayout
    from app.bootstrap import StationComposition
    from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
    from app.devices.moke_box.ui.page import MokeBoxPage
    from app.ui.workers import DeviceController
    from tests.test_moke_dual_outputs import dual_settings
    from tests.test_fluent_moke_field_workflow import wait_for
    application = QApplication.instance() or QApplication([])
    transports = []
    connect = SimulatedMokeBoxTransport.connect
    def existing(transport, endpoint, timeout):
        connect(transport, endpoint, timeout)
        transport._vouts[2] = 2.0
        transports.append(transport)
    monkeypatch.setattr(SimulatedMokeBoxTransport, "connect", existing)
    settings = dual_settings(tmp_path)
    host = QWidget()
    layout = QVBoxLayout(host)
    controllers = StationComposition(settings, simulation=True).create_controllers(("moke_box",), host)
    page = MokeBoxPage(controllers["moke_box"], settings, host)
    layout.addWidget(page)
    workflow = page.field_workflow
    workflow._simulation = True
    try:
        host.resize(1360, 880)
        host.show()
        page.views.setCurrentIndex(2)
        controllers["moke_box"].call("connect")
        wait_for(application, lambda: workflow._profile is not None and len(workflow._initialized_voltage_channels) == 8)
        workflow.configuration_panel.channel.setCurrentIndex(2)
        assert workflow.zero_button.isEnabled()
        assert workflow.configuration_panel.profile.kepco_mode == "dac_test"
        workflow.configuration_panel.set_operator_limits("-250 mV", "250 mV")
        assert workflow.configuration_panel.maximum_text == "250 mV"
        workflow.zero_button.click()
        wait_for(application, lambda: not workflow.busy)
        assert transports[0]._vouts[2] == 0
        assert workflow._last_voltages[2] == 0
        assert workflow.configuration_panel.maximum_text == "250 mV"
        assert host.grab().save(str(tmp_path / "moke-page-recovered.png"))
    finally:
        if workflow.busy:
            workflow.stop()
            wait_for(application, lambda: not workflow.busy)
        wait_for(application, lambda: workflow._catalog_thread is None)
        DeviceController.close_all(controllers.values())
        host.close()
        host.deleteLater()
        application.processEvents()
