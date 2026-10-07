from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from app.engine.compiler import RecipeCompiler
from app.engine.runner import RecipeRunner
from app.domain.models import DeviceState
from app.recipes import parse_recipe_text
from app.ui.recipes.moke_voltage_dialog import MokeVoltageSetDialog
from tests.test_adapters_and_runner import MemoryWriter
from tests.test_moke_smoke_sweep_limits import settings_for_channel_two
from tests.test_moke_voltage_control import controlled_adapter, plan_for


@pytest.mark.parametrize("voltage", ["0 mV", "5 mV", "-5 mV"])
def test_fixed_operating_point_changes_nonzero_output_and_holds_until_measurement(voltage):
    settings = settings_for_channel_two()
    adapter, _, profile = controlled_adapter(minimum_settling_s=0)
    # Use the same authoritative profile as the adapter, not an unrelated binding.
    settings = settings.model_copy(update={"devices": settings.devices.model_copy(update={
        "moke_box": settings.moke_box.model_copy(update={"voltage_control": settings.moke_box.voltage_control.model_copy(update={
            "minimum_settling_time": "0 s",
        })}),
    })})
    from app.safety.moke_box import control_profile_from_settings
    adapter._active_profile = None
    adapter._config = adapter._config.__class__(
        "SIM::MOKE::INSTR", allow_vout_control=True, allowed_vout_channels=(2,),
        control_profile=control_profile_from_settings(settings, simulation=True, channel=2),
    )
    profile = adapter.get_control_profile(2)
    initial = plan_for(profile, targets=(0.2,))
    adapter.configure_voltage_plan(initial)
    adapter.arm_voltage_plan(initial)
    adapter.ramp_vout(2, 0.2)
    recipe = parse_recipe_text(f"""
schema_version: 1
name: operating point
root:
  id: root
  type: sequence
  children:
    - {{id: operating, type: set_moke_voltage, channel: 2, voltage: '{voltage}'}}
    - {{id: measurement, type: checkpoint}}
finally: [{{id: zero, type: stop_moke_voltage}}]
""")
    plan = RecipeCompiler(settings).compile(recipe)
    seen = []
    def event(name, data):
        if name == "action_started" and data.get("node_id") == "measurement":
            seen.append(adapter.read_vouts()[2])
    unused = SimpleNamespace(state=DeviceState.DISCONNECTED, connected=False)
    runner = RecipeRunner(rigol=unused, keithley=unused, anritsu=unused, moke_box=adapter,
                          writer=MemoryWriter(), on_event=event)
    result = runner.run(plan)
    assert result.error is None, result.error
    assert seen, "Operating point must be confirmed before the measurement"
    from app.domain.quantities import parse_quantity
    assert seen[0] == pytest.approx(parse_quantity(voltage, "voltage").si_value, abs=10 / 32767)
    assert adapter.read_vouts()[2] == 0


def test_recipe_explicit_baseline_precedes_background_and_reference():
    recipe = parse_recipe_text(Path("recipes/anritsu_background_reference_smoke_test.yml").read_text(encoding="utf-8"))
    plan = RecipeCompiler(settings_for_channel_two()).compile(recipe)
    assert [action.kind for action in plan.actions[:3]] == ["configure_moke_box", "arm_moke_voltage", "update_moke_voltage"]
    assert plan.actions[2].payload["channel"] == 2
    assert plan.actions[2].payload["voltage_v"] == 0
    assert recipe.root.children[0].data["device_module"] == "moke_box"
    assert recipe.root.children[0].children[0].type == "set_moke_voltage"


def test_dialog_renders_and_rejects_invalid_voltage(tmp_path):
    app = QApplication.instance() or QApplication([])
    from PySide6.QtGui import QFont, QFontDatabase
    if not QFontDatabase.families() and Path("C:/Windows/Fonts/arial.ttf").exists():
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/arial.ttf")
    app.setFont(QFont("Arial", 10))
    dialog = MokeVoltageSetDialog(settings_for_channel_two(), channel=2, voltage="5 mV")
    try:
        dialog.show()
        app.processEvents()
        assert dialog.isVisible() and dialog.voltage.width() > 0
        assert dialog.save.isEnabled()
        assert dialog.validated_data()[0] == {"channel": 2, "voltage": "5 mV"}
        dialog.grab().save(str(tmp_path / "moke-operating-point.png"))
        dialog.voltage.setText("10000 mV")
        assert not dialog.save.isEnabled()
        dialog.accept()
        assert dialog.result() == 0
    finally:
        dialog.close()


def test_library_operating_point_can_be_inserted_and_edited(monkeypatch):
    from PySide6.QtWidgets import QDialog
    from app.ui.recipes.page import RecipePage
    app = QApplication.instance() or QApplication([])
    page = RecipePage(settings_for_channel_two())
    try:
        page._apply_builder_source("""
schema_version: 1
name: library
root: {id: root, type: sequence, children: []}
""", "test draft")
        def accepted(dialog):
            dialog.voltage.setText("5 mV")
            return QDialog.DialogCode.Accepted
        monkeypatch.setattr(MokeVoltageSetDialog, "exec", accepted)
        page._library_add_basic("set_moke_voltage", parent_id="root", branch="children", index=0)
        recipe = parse_recipe_text(page._builder_source())
        device, = recipe.root.children
        node, = device.children
        assert node.type == "set_moke_voltage"
        assert node.data["channel"] == 2 and node.data["voltage"] == "5 mV"
        def edit(dialog):
            dialog.voltage.setText("7 mV")
            return QDialog.DialogCode.Accepted
        monkeypatch.setattr(MokeVoltageSetDialog, "exec", edit)
        page._edit_action_node(node)
        assert parse_recipe_text(page._builder_source()).root.children[0].children[0].data["voltage"] == "7 mV"
        app.processEvents()
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_device_library_offers_fixed_voltage_and_edit_keeps_it(monkeypatch):
    from PySide6.QtWidgets import QDialog
    from app.ui.recipes.page import RecipePage
    app = QApplication.instance() or QApplication([])
    page = RecipePage(settings_for_channel_two())
    try:
        page._apply_builder_source("""
schema_version: 1
name: device library
root: {id: root, type: sequence, children: []}
""", "test draft")
        def accepted(dialog):
            assert dialog.mode.count() == 2
            dialog.voltage.setText("5 mV")
            return QDialog.DialogCode.Accepted
        monkeypatch.setattr(MokeVoltageSetDialog, "exec", accepted)
        page._library_add_device("moke_box", parent_id="root", branch="children", index=0)
        recipe = parse_recipe_text(page._builder_source())
        device, = recipe.root.children
        fixed, = device.children
        assert fixed.type == "set_moke_voltage" and fixed.data["voltage"] == "5 mV"
        plan = RecipeCompiler(settings_for_channel_two()).compile(recipe)
        assert [action.kind for action in plan.actions] == ["configure_moke_box", "arm_moke_voltage", "update_moke_voltage"]
        def edited(dialog):
            dialog.voltage.setText("7 mV")
            return QDialog.DialogCode.Accepted
        monkeypatch.setattr(MokeVoltageSetDialog, "exec", edited)
        page._edit_moke_module_node(device)
        assert parse_recipe_text(page._builder_source()).root.children[0].children[0].data["voltage"] == "7 mV"
        app.processEvents()
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_shipped_yaml_uses_the_same_device_structure_as_visual_factories(tmp_path):
    from app.recipes.moke_nodes import moke_fixed_node, moke_device_node
    from app.ui.recipes.page import RecipePage
    app = QApplication.instance() or QApplication([])
    source = Path("recipes/anritsu_background_reference_smoke_test.yml").read_text(encoding="utf-8")
    recipe = parse_recipe_text(source)
    fixed = recipe.root.children[0]
    assert RecipePage._node_to_mapping(fixed) == moke_fixed_node(
        fixed.id, fixed.children[0].id, 2, "0 mV")
    roi = recipe.root.children[-1]
    assert RecipePage._node_to_mapping(roi) == moke_device_node(
        roi.id, 2, [RecipePage._node_to_mapping(roi.children[0])])
    from dataclasses import replace
    bare = replace(recipe, root=replace(recipe.root, children=tuple(
        child.children[0] if child.data.get("device_module") == "moke_box" else child
        for child in recipe.root.children)))
    canonical_plan = RecipeCompiler(settings_for_channel_two()).compile(recipe)
    bare_plan = RecipeCompiler(settings_for_channel_two()).compile(bare)
    assert [(action.kind, action.payload) for action in canonical_plan.actions] == [
        (action.kind, action.payload) for action in bare_plan.actions]
    assert canonical_plan.total_points == canonical_plan.total_spectra == 10
    bad_fixed = replace(fixed, data={**fixed.data, "channel": 0})
    bad_recipe = replace(recipe, root=replace(recipe.root, children=(bad_fixed, *recipe.root.children[1:])))
    with pytest.raises(RuntimeError, match="channel differs"):
        RecipeCompiler(settings_for_channel_two()).compile(bad_recipe)
    page = RecipePage(settings_for_channel_two())
    try:
        page.resize(1360, 880)
        page.show()
        page._apply_builder_source(source, "canonical recipe")
        app.processEvents()
        assert page.measurement_tree.isVisible() and page.measurement_tree.width() > 0
        assert page.grab().save(str(tmp_path / "canonical-moke-tree.png"))
        reloaded = parse_recipe_text(page._builder_source())
        assert RecipePage._node_to_mapping(reloaded.root) == RecipePage._node_to_mapping(recipe.root)
    finally:
        page._close_discard_confirmed = True
        page.close()
