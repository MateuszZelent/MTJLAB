"""Every compiler-generated local output transition has a visible tree row."""
import pytest

from app.devices.registry import built_in_device_registry
from app.recipes import parse_recipe_text
from app.recipes.semantic_tree import normalize_recipe_tree
from tests.test_sweep_audit_contracts import audit_settings, compile_source


@pytest.mark.parametrize("device,channel", [("keithley", "A"), ("keithley", "B"), ("rigol", 1), ("rigol", 2)])
@pytest.mark.parametrize("policy", ["on", "on_keep", "off"])
@pytest.mark.parametrize("sweep", [False, True])
def test_local_output_rows_match_compiler_order_and_configuration_channel(tmp_path, device, channel, policy, sweep):
    settings = audit_settings(tmp_path)
    raw = settings.model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    raw["devices"]["rigol"]["safety"]["channels"][str(channel) if device == "rigol" else "1"]["enabled"] = True
    settings = type(settings).model_validate(raw)
    if device == "keithley":
        baseline = f"{{id: baseline, type: configure_keithley, channel: {channel}, mode: current, level: '100 uA', compliance: '20 mV', source_range: '10 mA'}}"
        config = f"{{channel: {channel}, source_mode: current}}"
        action = "{parameter_id: source.level, mode: set, value: '200 uA'}"
    else:
        baseline = f"{{id: baseline, type: configure_rigol, channel: {channel}, waveform: SIN, frequency: '1 MHz', high_level: '10 mV', low_level: '-10 mV'}}"
        config = f"{{channel: {channel}}}"
        action = "{parameter_id: carrier.frequency, mode: set, value: '200 kHz'}"
    if sweep:
        parameter, start, stop = (("source.level", "200 uA", "300 uA") if device == "keithley"
                                  else ("carrier.frequency", "200 kHz", "300 kHz"))
        action = f"{{parameter_id: {parameter}, mode: sweep, segments: [{{start: '{start}', stop: '{stop}', points: 2}}]}}"
    plan = compile_source(settings, f"""    - {baseline}
    - id: local
      type: sequence
      device_module: {device}
      operation: configure_selected_parameters
      configuration: {config}
      output_policy: {policy}
      parameter_actions: [{action}]
      children: [{{id: measurement_wait, type: wait, duration: '1 ms'}}]
""")
    tree = normalize_recipe_tree(parse_recipe_text(plan.recipe_source), built_in_device_registry().sweep_providers())
    children = tree.require("local").children
    transitions = [a for a in plan.actions if a.kind == f"set_{device}_output"]
    for action in transitions:
        row = tree.require(action.node_id)
        assert action.semantic_id == row.semantic_id
        assert row.data["channel"] == channel
        assert row.data["enabled"] == action.payload["enabled"]
        assert str(channel) in row.label
    ids = [row.semantic_id for row in children]
    body_id = children[1].semantic_id if sweep else "measurement_wait"
    if sweep:
        assert children[1].kind.value == "sweep_axis"
    if policy == "on":
        assert ids == ["local.output-on", body_id, "local.output-off"]
    elif policy == "on_keep":
        assert ids == ["local.output-on", body_id]
    else:
        assert ids == ["local.output-off", body_id]
