"""Final-state editing validates its output without approving the whole recipe."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from app.engine import RecipeCompiler
from app.recipes import RecipeNode, parse_recipe_text
from app.settings.models import StationSettings
from app.ui.recipes.final_state_dialog import FinalStateDialog
from app.ui.recipes.final_state_validation import validate_final_state_edit
from app.ui.recipes.page import RecipePage
from tests.test_final_output_state import qualified_settings, recipe_for


@pytest.fixture
def settings():
    raw = qualified_settings().model_dump(mode="python")
    moke = raw["devices"]["moke_box"]
    moke["voltage_control"]["channel"] = 0
    moke["allowed_vout_channels"] = [0]
    moke["channel_profiles"] = {}
    for channel in raw["devices"]["keithley"]["safety"]["channels"].values():
        channel["enabled"] = True
        limits = channel["lab_limits"]
        limits["source_current"].update(min="-20 mA", max="20 mA", max_abs="20 mA")
        limits["voltage_compliance"]["max"] = "1 V"
        limits["measured_voltage_trip"].update(min="-1 V", max="1 V")
        limits["measured_current_trip"].update(min="-30 mA", max="30 mA")
        limits["max_abs_power"] = "100 mW"
    return StationSettings.model_validate(raw)


@pytest.fixture
def actual_source():
    raw = yaml.safe_load(Path("recipes/example_nested_keithley_ab_fixed_moke2.yml").read_text(encoding="utf-8"))
    # Keep the complete real example and pin the regression inputs in memory:
    # the shared example may be repaired independently of this UI regression.
    initial = next(node for node in raw["root"]["children"] if node["id"] == "keithley-a-initial")
    initial["source_range"] = "10 mA"
    final = next(node for node in raw["finally"] if node["id"] == "final-moke-hold")
    final["voltage"] = "321.7 mV"
    return yaml.safe_dump(raw, sort_keys=False)


@pytest.fixture
def page(settings, tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    from app.recipes.repository import RecipeRepository
    autosave = RecipeRepository.autosave
    monkeypatch.setattr(RecipeRepository, "autosave", lambda self, path, source:
                        autosave(self, tmp_path / "recipe.yml", source))
    widget = RecipePage(settings)
    widget.resize(1280, 900)
    widget.show()
    app.processEvents()
    yield widget
    widget._autosave_timer.stop()
    widget._close_discard_confirmed = True
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_actual_invalid_recipe_can_save_moke_final_hold(page, settings, actual_source, tmp_path, monkeypatch):
    recipe = parse_recipe_text(actual_source)
    with pytest.raises(RuntimeError, match="(?i)range"):
        RecipeCompiler(settings).compile(recipe)
    page._apply_builder_source(actual_source, "Load actual example")
    before = parse_recipe_text(page._builder_source())

    def edit(dialog):
        dialog.show()
        QApplication.processEvents()
        assert dialog.channel.currentData() == 0
        assert dialog.output.currentData() == "hold"
        for control in (dialog.fields["voltage"], dialog.save, dialog.status):
            assert control.isVisible()
            assert dialog.rect().contains(control.mapTo(dialog, QPoint(0, 0)))
            assert dialog.rect().contains(control.mapTo(dialog, control.rect().bottomRight()))
        dialog.fields["voltage"].setText("14 mV")
        assert dialog.grab().save(str(tmp_path / "moke-final-local-validation.png"))
        for invalid in ("14", "14 mA", "nan V", "inf V", "1000.001 mV", "-1000.001 mV"):
            dialog.fields["voltage"].setText(invalid)
            QTest.mouseClick(dialog.save, Qt.MouseButton.LeftButton)
            assert dialog.result() != QDialog.DialogCode.Accepted
            assert dialog.status.text()
        dialog.fields["voltage"].setText("14 mV")
        QTest.mouseClick(dialog.save, Qt.MouseButton.LeftButton)
        assert dialog.result() == QDialog.DialogCode.Accepted, dialog.status.text()
        return dialog.result()

    monkeypatch.setattr(FinalStateDialog, "exec", edit)
    page._edit_final_state(before.finally_nodes[-1])
    saved = parse_recipe_text(page._builder_source())
    assert saved.root == before.root
    assert saved.finally_nodes[:-1] == before.finally_nodes[:-1]
    assert saved.finally_nodes[-1].id == "final-moke-hold"
    assert saved.finally_nodes[-1].data == {**before.finally_nodes[-1].data, "voltage": "14 mV"}
    assert not page.run_button.isEnabled()
    with pytest.raises(RuntimeError, match="(?i)range"):
        RecipeCompiler(settings).compile(saved)


@pytest.mark.parametrize("prerequisite", ["missing", "disabled", "wrong-channel"])
def test_moke_final_requires_main_plan_same_channel(page, actual_source, monkeypatch, prerequisite):
    source = actual_source
    if prerequisite == "missing":
        source = source.replace("type: set_moke_voltage", "type: comment").replace(
            "    channel: 0\n    voltage: -14 mV\n    block_type: recipe.set_moke_voltage", "    text: removed")
    elif prerequisite == "disabled":
        source = source.replace("voltage: -14 mV", "voltage: -14 mV\n    disabled: true")
    page._apply_builder_source(source, "Missing prerequisite")
    before = page._builder_source()

    def edit(dialog):
        dialog.show()
        QApplication.processEvents()
        if prerequisite == "wrong-channel":
            dialog.channel.setCurrentIndex(dialog.channel.findData(1))
        dialog.fields["voltage"].setText("14 mV")
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert "main measurement plan" in dialog.status.text(), dialog.status.text()
        dialog.reject()
        return dialog.result()

    monkeypatch.setattr(FinalStateDialog, "exec", edit)
    page._edit_final_state(parse_recipe_text(before).finally_nodes[-1])
    assert page._builder_source() == before


@pytest.mark.parametrize("voltage", ["14", "14 mA", "nan V", "inf V", "1000.001 mV", "-1000.001 mV"])
def test_local_validation_rejects_moke_units_and_limits(settings, actual_source, voltage):
    recipe = parse_recipe_text(actual_source)
    final = replace(recipe.finally_nodes[-1], data={**recipe.finally_nodes[-1].data, "voltage": voltage})
    recipe = replace(recipe, finally_nodes=(*recipe.finally_nodes[:-1], final))
    with pytest.raises((RuntimeError, ValueError)) as error:
        validate_final_state_edit(settings, recipe, final.id)
    assert "source range" not in str(error.value)


@pytest.mark.parametrize("voltage", ["-1 V", "1 V", "999.999 mV", "1.4e-2 V"])
def test_local_validation_accepts_moke_boundary_and_explicit_exponent(settings, actual_source, voltage):
    recipe = parse_recipe_text(actual_source)
    final = replace(recipe.finally_nodes[-1], data={**recipe.finally_nodes[-1].data, "voltage": voltage})
    validate_final_state_edit(settings, replace(recipe, finally_nodes=(final,)), final.id)


@pytest.mark.parametrize("violation", ["tight-profile", "unapproved", "duplicate", "off-nonzero"])
def test_local_validation_preserves_moke_profile_and_endpoint_semantics(settings, actual_source, violation):
    recipe = parse_recipe_text(actual_source)
    final = replace(recipe.finally_nodes[-1], data={**recipe.finally_nodes[-1].data, "voltage": "14 mV"})
    recipe = replace(recipe, finally_nodes=(final,))
    raw = settings.model_dump(mode="python")
    if violation == "tight-profile":
        raw["devices"]["moke_box"]["voltage_control"]["maximum"] = "10 mV"
    elif violation == "unapproved":
        raw["devices"]["moke_box"].update(endpoint="TEST::NO-CONNECTION", allow_vout_control=False,
                                          allowed_vout_channels=[])
        raw["devices"]["moke_box"]["voltage_control"]["approved"] = False
    elif violation == "duplicate":
        recipe = replace(recipe, finally_nodes=(replace(final, id="duplicate"), final))
    else:
        recipe = replace(recipe, finally_nodes=(replace(final, data={**final.data, "output": "off"}),))
    with pytest.raises(RuntimeError):
        validate_final_state_edit(StationSettings.model_validate(raw), recipe, final.id)


@pytest.mark.parametrize("device", ["keithley", "rigol"])
@pytest.mark.parametrize("prerequisite", ["on", "missing-on", "off-after-on", "wrong-channel", "conditional", "disabled"])
def test_local_hold_preserves_source_and_output_continuity(device, prerequisite):
    recipe = recipe_for(device, enabled=prerequisite != "missing-on")
    children = list(recipe.root.children)
    if prerequisite == "off-after-on":
        children.append(RecipeNode("off", f"set_{device}_output", {"channel": "A" if device == "keithley" else 1, "enabled": False}))
    elif prerequisite == "wrong-channel":
        children = [replace(node, data={**node.data, "channel": "B" if device == "keithley" else 2})
                    if "channel" in node.data else node for node in children]
    elif prerequisite == "conditional":
        children = [RecipeNode("not-executed", "if", {"condition": False}, tuple(children))]
    elif prerequisite == "disabled":
        children[0] = replace(children[0], data={**children[0].data, "disabled": True})
    # An invalid configuration on the other endpoint must not block this edit.
    unrelated = RecipeNode("unrelated", f"configure_{device}", {
        **deepcopy(recipe.root.children[0].data),
        "channel": "B" if device == "keithley" else 2,
        **({"source_range": "nonsense"} if device == "keithley" else {"frequency": "nonsense"}),
    })
    recipe = replace(recipe, root=replace(recipe.root, children=(unrelated, *children)))
    if prerequisite == "on":
        validate_final_state_edit(qualified_settings(), recipe, "finish")
        with pytest.raises((RuntimeError, ValueError)):
            RecipeCompiler(qualified_settings()).compile(recipe)
    else:
        with pytest.raises(RuntimeError):
            validate_final_state_edit(qualified_settings(), recipe, "finish")


def test_same_channel_sweep_range_remains_a_local_prerequisite(settings, actual_source):
    recipe = parse_recipe_text(actual_source)
    final = RecipeNode("keithley-final", "final_state", {"device": "keithley", "channel": "A", "output": "hold", "level": "1 mA"})
    with pytest.raises(RuntimeError, match="(?i)range"):
        validate_final_state_edit(settings, replace(recipe, finally_nodes=(final,)), final.id)
    # The enclosing B axis and its source configuration survive projection.
    repaired = parse_recipe_text(actual_source.replace("source_range: 10 mA", "source_range: 100 mA"))
    validate_final_state_edit(settings, replace(repaired, finally_nodes=(final,)), final.id)


def test_local_moke_validation_preserves_implicit_arm_across_other_vout_sweep(settings, actual_source):
    raw = settings.model_dump(mode="python")
    moke = raw["devices"]["moke_box"]
    moke["channel_profiles"]["1"] = {**moke["voltage_control"], "channel": 1}
    moke["allowed_vout_channels"] = [0, 1]
    recipe = parse_recipe_text(actual_source)
    recipe = replace(recipe, root=RecipeNode("main", "sequence", {}, (
        recipe.root.children[0],
        RecipeNode("other-vout", "sweep", {"target": "moke_box.vout1.voltage",
                   "segments": [{"start": "0 mV", "stop": "5 mV", "points": 2, "spacing": "linear"}]}),
        RecipeNode("update-original", "update_moke_voltage", {"channel": 0, "voltage": "5 mV"}),
    )), finally_nodes=(recipe.finally_nodes[-1],))
    qualified = StationSettings.model_validate(raw)
    with pytest.raises(RuntimeError, match="channel differs"):
        RecipeCompiler(qualified).compile(recipe)
    with pytest.raises(RuntimeError, match="channel differs"):
        validate_final_state_edit(qualified, recipe, "final-moke-hold")


def native_moke_case(settings, device):
    """An actual selected-parameter module enclosing the edited MOKE output."""
    if device in {"keithley", "rigol"}:
        baseline = recipe_for(device).root.children[0]
        configuration = {"channel": baseline.data["channel"]}
        if device == "keithley":
            configuration["source_mode"] = "current"
        action = ({"parameter_id": "source.level", "mode": "set", "value": "0.2 mA"}
                  if device == "keithley" else
                  {"parameter_id": "carrier.frequency", "mode": "set", "value": "2 kHz"})
    elif device == "anritsu":
        configuration = {"start_frequency": "1 MHz", "stop_frequency": "10 MHz",
                         "reference_level": "0 dBm", "points": 101}
        baseline = RecipeNode("source", "configure_anritsu", configuration)
        action = {"parameter_id": "spectrum.reference_level", "mode": "set", "value": "-10 dBm"}
    else:
        raw = settings.model_dump(mode="python")
        raw["devices"]["anritsu"]["signal_generator"].update(
            control_protocol="basic_scpi", frequency={"min": "100 MHz", "max": "6 GHz"},
            power={"min": "-100 dBm", "max": "0 dBm"})
        raw["devices"]["anritsu"]["safety"]["signal_generator_output_allowed"] = True
        settings = StationSettings.model_validate(raw)
        configuration = {"frequency": "1 GHz", "power": "-30 dBm"}
        baseline = RecipeNode("source", "configure_anritsu_sg", configuration)
        action = {"parameter_id": "sg.power", "mode": "set", "value": "-20 dBm"}
    native = RecipeNode("selected", "sequence", {
        "device_module": device, "operation": "configure_selected_parameters",
        "configuration": configuration, "parameter_actions": [action],
    }, (RecipeNode("moke", "set_moke_voltage", {"channel": 0, "voltage": "5 mV"}),))
    final = RecipeNode("finish", "final_state", {
        "device": "moke_box", "channel": 0, "output": "hold", "voltage": "14 mV"})
    recipe = replace(recipe_for("moke_box"), root=RecipeNode("main", "sequence", {}, (baseline, native)),
                     finally_nodes=(final,))
    return settings, recipe


@pytest.mark.parametrize("device", ["keithley", "rigol", "anritsu", "anritsu_sg"])
def test_local_moke_final_keeps_retained_native_module_baseline(settings, device):
    settings, recipe = native_moke_case(settings, device)
    assert RecipeCompiler(settings).compile(recipe).retained_outputs == {"moke_box.vout0"}
    validate_final_state_edit(settings, recipe, "finish")


@pytest.mark.parametrize("device", ["keithley", "rigol", "anritsu", "anritsu_sg"])
@pytest.mark.parametrize("prerequisite", ["missing", "disabled", "after-module"])
def test_retained_native_module_still_requires_preceding_baseline(settings, device, prerequisite):
    settings, recipe = native_moke_case(settings, device)
    baseline, native = recipe.root.children
    children = ((native,) if prerequisite == "missing" else (native, baseline)
                if prerequisite == "after-module" else
                (replace(baseline, data={**baseline.data, "disabled": True}), native))
    recipe = replace(recipe, root=replace(recipe.root, children=children))
    for validate in (lambda: RecipeCompiler(settings).compile(recipe),
                     lambda: validate_final_state_edit(settings, recipe, "finish")):
        with pytest.raises(RuntimeError, match="explicit.*baseline"):
            validate()


@pytest.mark.parametrize("device", ["keithley", "rigol", "anritsu_sg"])
@pytest.mark.parametrize("turn_off", [False, True])
def test_retained_native_module_preserves_output_continuity_order(settings, device, turn_off):
    settings, recipe = native_moke_case(settings, device)
    baseline, native = recipe.root.children
    native = replace(native, data={**native.data, "output_policy": "continue"})
    if device == "keithley":
        native = replace(native, data={**native.data, "parameter_actions": [{
            "parameter_id": "source.level", "mode": "sweep",
            "segments": [{"start": "0.2 mA", "stop": "0.3 mA", "points": 2}],
        }]})
    output = {"enabled": True}
    if device in {"keithley", "rigol"}:
        output["channel"] = baseline.data["channel"]
    on = RecipeNode("on", f"set_{device}_output", output)
    children = (baseline, on)
    if turn_off:
        # A retained sibling native module can switch this very output OFF.
        off = replace(native, id="native-off", data={**native.data, "output_policy": "off"}, children=())
        children += (off,)
    recipe = replace(recipe, root=replace(recipe.root, children=(*children, native)))
    if turn_off:
        for validate in (lambda: RecipeCompiler(settings).compile(recipe),
                         lambda: validate_final_state_edit(settings, recipe, "finish")):
            with pytest.raises(RuntimeError, match="OUTPUT ON"):
                validate()
    else:
        RecipeCompiler(settings).compile(recipe)
        validate_final_state_edit(settings, recipe, "finish")


def test_native_dependencies_close_through_nested_devices(settings):
    settings, keithley = native_moke_case(settings, "keithley")
    settings, rigol = native_moke_case(settings, "rigol")
    keithley_baseline, outer = keithley.root.children
    rigol_baseline, inner = rigol.root.children
    inner = replace(inner, id="inner-rigol")
    outer = replace(outer, children=(inner,))
    recipe = replace(keithley, root=replace(keithley.root, children=(
        keithley_baseline, replace(rigol_baseline, id="rigol-baseline"), outer)))
    RecipeCompiler(settings).compile(recipe)
    validate_final_state_edit(settings, recipe, "finish")


@pytest.mark.parametrize("device", ["keithley", "rigol", "anritsu", "anritsu_sg"])
def test_native_dependencies_preserve_intervening_same_endpoint_validation(settings, device):
    settings, recipe = native_moke_case(settings, device)
    baseline, native = recipe.root.children
    bad_action = {
        "keithley": {"parameter_id": "source.level", "mode": "set", "value": "100 A"},
        "rigol": {"parameter_id": "carrier.frequency", "mode": "set", "value": "1000 GHz"},
        "anritsu": {"parameter_id": "spectrum.start_frequency", "mode": "set", "value": "20 MHz"},
        "anritsu_sg": {"parameter_id": "sg.power", "mode": "set", "value": "100 dBm"},
    }[device]
    intervening = replace(native, id="invalid-intervening", children=(),
                          data={**native.data, "parameter_actions": [bad_action]})
    recipe = replace(recipe, root=replace(recipe.root, children=(baseline, intervening, native)))
    for validate in (lambda: RecipeCompiler(settings).compile(recipe),
                     lambda: validate_final_state_edit(settings, recipe, "finish")):
        with pytest.raises(RuntimeError):
            validate()


def test_native_sg_dependencies_preserve_spectrum_mode_transition(settings):
    settings, recipe = native_moke_case(settings, "anritsu_sg")
    _, spectrum = native_moke_case(settings, "anritsu")
    baseline, native = recipe.root.children
    native = replace(native, data={**native.data, "output_policy": "continue"})
    spectrum_baseline = replace(spectrum.root.children[0], id="switch-to-spectrum")
    recipe = replace(recipe, root=replace(recipe.root, children=(baseline,
        RecipeNode("sg-on", "set_anritsu_sg_output", {"enabled": True}), spectrum_baseline, native)))
    for validate in (lambda: RecipeCompiler(settings).compile(recipe),
                     lambda: validate_final_state_edit(settings, recipe, "finish")):
        with pytest.raises(RuntimeError, match="OFF explicitly"):
            validate()
