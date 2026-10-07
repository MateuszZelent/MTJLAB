"""Station limits cannot be bypassed through ROI, YAML or direct adapter calls."""
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from app.domain.quantities import DIMENSION_VOLTAGE
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.safety.moke_box import MokeVoltagePlan
from app.settings.models import StationSettings
from app.ui.recipes.sweep_editor import SweepGeneratorDialog
from tests.helpers import simulation_settings
from tests.test_moke_voltage_control import controlled_adapter, mutations, plan_for


def settings_for_channel_two(maximum="1 V"):
    raw = simulation_settings().model_dump(mode="python")
    # Synthetic qualification for the shipped recipe's fixed Keithley A bias.
    # Real station limits are never changed by these tests.
    keithley = raw["devices"]["keithley"]["safety"]
    keithley["allow_output_enable"] = True
    channel_a = keithley["channels"]["A"]
    channel_a["enabled"] = True
    channel_a["lab_limits"]["source_current"].update(min="-2 mA", max="2 mA", max_abs="2 mA")
    channel_a["lab_limits"]["voltage_compliance"]["max"] = "670 mV"
    channel_a["lab_limits"]["measured_voltage_trip"].update(min="-675 mV", max="675 mV")
    channel_a["lab_limits"]["measured_current_trip"].update(min="-3 mA", max="3 mA")
    channel_a["lab_limits"]["max_abs_power"] = "10 mW"
    settings = StationSettings.model_validate(raw)
    profile = settings.moke_box.voltage_control.model_copy(update={
        "channel": 2, "maximum": maximum,
    })
    return settings.model_copy(update={"devices": settings.devices.model_copy(update={
        "moke_box": settings.moke_box.model_copy(update={
            "endpoint": "SIM::MOKE::INSTR", "voltage_control": profile,
        }),
    })})


@pytest.mark.parametrize("stop", ["10000 mV", "10 V", "1000.001 mV", "-1000.001 mV", "10 mA", "10"])
def test_yaml_invalid_endpoint_is_rejected(stop):
    source = Path("recipes/anritsu_background_reference_smoke_test.yml").read_text(encoding="utf-8")
    source = source.replace("stop: 10 mV", f"stop: {stop}")
    with pytest.raises(RuntimeError):
        RecipeCompiler(settings_for_channel_two()).compile(parse_recipe_text(source))


@pytest.mark.parametrize("stop", ["10000 mV", "10 V", "1000.001 mV"])
def test_ui_blocks_invalid_points_and_accept_even_if_invoked_directly(stop, monkeypatch):
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    owner._settings = settings_for_channel_two()
    dialog = SweepGeneratorDialog({
        "device": "MOKE Box", "label": "VOUT 2", "target": "moke_box.vout2.voltage",
        "dimension": DIMENSION_VOLTAGE,
    }, owner, initial_segments=[{"start": "0 mV", "stop": stop, "points": 10, "spacing": "linear"}])
    try:
        dialog.show()
        app.processEvents()
        assert dialog.isVisible() and dialog.width() >= 640
        assert not dialog.create_button.isEnabled()
        assert "outside" in dialog.preview.text()
        monkeypatch.setattr("app.ui.recipes.sweep_editor.QMessageBox.warning", lambda *args: None)
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
    finally:
        dialog.close()
        owner.close()


def test_ui_uses_independent_channel_limits():
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    settings = settings_for_channel_two()
    independent = settings.moke_box.voltage_control.model_copy(update={
        "channel": 1, "approved": True, "binding_id": "test", "qualification_reference": "test",
        "minimum": "-10 mV", "maximum": "10 mV",
    })
    owner._settings = settings.model_copy(update={"devices": settings.devices.model_copy(update={
        "moke_box": settings.moke_box.model_copy(update={"channel_profiles": {"1": independent}}),
    })})
    dialog = SweepGeneratorDialog({
        "device": "MOKE Box", "label": "VOUT 1", "target": "moke_box.vout1.voltage",
        "dimension": DIMENSION_VOLTAGE,
    }, owner, initial_segments=[{"start": "0 mV", "stop": "20 mV", "points": 10, "spacing": "linear"}])
    try:
        app.processEvents()
        assert dialog._safety_bound.maximum_si == 0.01
        assert not dialog.create_button.isEnabled()
    finally:
        dialog.close()
        owner.close()


def test_ui_accept_rechecks_tightened_limits(monkeypatch):
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    owner._settings = settings_for_channel_two()
    dialog = SweepGeneratorDialog({
        "device": "MOKE Box", "label": "VOUT 2", "target": "moke_box.vout2.voltage",
        "dimension": DIMENSION_VOLTAGE,
    }, owner, initial_segments=[{"start": "0 mV", "stop": "10 mV", "points": 10, "spacing": "linear"}])
    try:
        app.processEvents()
        assert dialog.create_button.isEnabled()
        owner._settings = settings_for_channel_two("5 mV")
        monkeypatch.setattr("app.ui.recipes.sweep_editor.QMessageBox.warning", lambda *args: None)
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert dialog._safety_bound.maximum_si == 0.005
        dialog._refresh_preview()
        assert not dialog.create_button.isEnabled()
    finally:
        dialog.close()
        owner.close()


def test_ui_channel_switch_changes_target_and_uses_new_limits():
    app = QApplication.instance() or QApplication([])
    owner = QWidget()
    settings = settings_for_channel_two()
    independent = settings.moke_box.voltage_control.model_copy(update={
        "channel": 0, "approved": True, "binding_id": "test", "qualification_reference": "test",
        "minimum": "-5 mV", "maximum": "5 mV",
    })
    owner._settings = settings.model_copy(update={"devices": settings.devices.model_copy(update={
        "moke_box": settings.moke_box.model_copy(update={"channel_profiles": {"0": independent}}),
    })})
    dialog = SweepGeneratorDialog({
        "device": "MOKE Box", "label": "VOUT 0", "target": "moke_box.vout0.voltage",
        "dimension": DIMENSION_VOLTAGE,
    }, owner, initial_segments=[{"start": "0 mV", "stop": "10 mV", "points": 10, "spacing": "linear"}])
    try:
        dialog.show()
        app.processEvents()
        assert not dialog.create_button.isEnabled()
        dialog.channel_selector.setCurrentIndex(2)
        app.processEvents()
        assert dialog.definition["target"] == "moke_box.vout2.voltage"
        assert dialog._safety_bound.maximum_si == 1
        assert dialog.create_button.isEnabled()
        assert not dialog._moke_channel_available(1)
    finally:
        dialog.close()
        owner.close()


def test_shipped_recipe_points_fit_both_working_and_station_limits():
    source = Path("recipes/anritsu_background_reference_smoke_test.yml").read_text(encoding="utf-8")
    plan = RecipeCompiler(settings_for_channel_two()).compile(parse_recipe_text(source))
    configure, = [action for action in plan.actions if action.kind == "configure_moke_box" and action.source_node_id == "moke-voltage-sweep"]
    trajectory = configure.payload["plan"]
    assert len(trajectory.targets_v) == 10
    assert trajectory.minimum_v == 0 and trajectory.maximum_v == 0.01
    assert trajectory.targets_v[0] == 0 and trajectory.targets_v[-1] == 0.01
    assert all(0 <= trajectory.applied_voltage(value) <= 0.01 for value in trajectory.targets_v)
    preparation, arming = [action for action in plan.actions if action.kind in {"configure_moke_box", "arm_moke_voltage"} and action.source_node_id == "moke-voltage-sweep"]
    assert preparation.source_node_id == arming.source_node_id == "moke-voltage-sweep"
    assert preparation.semantic_id == arming.semantic_id
    assert preparation.payload["plan"] == arming.payload["plan"]
    with pytest.raises(RuntimeError):
        RecipeCompiler(settings_for_channel_two("5 mV")).compile(parse_recipe_text(source))


def test_repeated_automatic_sweep_gets_new_one_shot_permission_each_time():
    recipe = parse_recipe_text("""
schema_version: 1
name: repeated MOKE
root:
  id: repeat
  type: repeat
  count: 2
  children:
    - id: voltage
      type: sweep
      target: moke_box.vout2.voltage
      segments: [{start: 0 mV, stop: 10 mV, points: 10, spacing: linear}]
      children: []
finally: [{id: zero, type: stop_moke_voltage}]
""")
    plan = RecipeCompiler(settings_for_channel_two()).compile(recipe)
    preparations = [action for action in plan.actions if action.kind == "configure_moke_box"]
    arming = [action for action in plan.actions if action.kind == "arm_moke_voltage"]
    assert len(preparations) == len(arming) == 2
    assert all(len(action.payload["plan"].targets_v) == 10 for action in preparations)


@pytest.mark.parametrize("target", [1.000001, -1.000001, 10, float("nan"), float("inf")])
def test_adapter_rejects_invalid_plan_without_set_command(target):
    adapter, transport, profile = controlled_adapter()
    transport.sent.clear()
    plan = MokeVoltagePlan(profile.fingerprint, 2, -1, 1, (target,))
    with pytest.raises(RuntimeError):
        adapter.configure_voltage_plan(plan)
    assert not mutations(transport)


def test_armed_adapter_rejects_unplanned_value_and_changed_profile_without_set():
    adapter, transport, profile = controlled_adapter()
    plan = plan_for(profile, targets=(0.01,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    transport.sent.clear()
    with pytest.raises(RuntimeError):
        adapter.set_vout(2, 10)
    assert not mutations(transport)
    plan.validate(profile)
    with pytest.raises(RuntimeError):
        plan.validate(replace(profile, maximum_v=0.005))
    assert not mutations(transport)


@pytest.mark.parametrize("target", [-1, 1, -0.01, 0.01])
def test_dac_rounding_never_expands_authorized_envelope(target):
    _, _, profile = controlled_adapter()
    lower, upper = (-abs(target), abs(target))
    plan = MokeVoltagePlan(profile.fingerprint, 2, lower, upper, (target,))
    plan.validate(profile)
    applied = plan.applied_voltage(target)
    assert lower <= applied <= upper
    assert abs(applied - target) <= 10 / 32767
