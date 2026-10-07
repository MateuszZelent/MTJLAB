"""A typed axis may not relabel an unrelated instrument operation."""
from dataclasses import replace

import pytest

from app.devices.anritsu_ms2830a.sweep_provider import PROVIDER as ANRITSU
from app.devices.keithley_2600.sweep_provider import PROVIDER as KEITHLEY
from app.domain.errors import ConfigurationError
from app.recipes.models import RecipeNode
from app.recipes import parse_recipe_text
from app.recipes.semantic_tree import normalize_recipe_tree
from app.devices.registry import built_in_device_registry
from app.engine.compiler import RecipeCompiler
from tests.helpers import loaded_settings


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("target,mode", [
    ("current", "current"), ("level", "current"), ("voltage", "voltage"),
    ("compliance_voltage", "current"), ("compliance_current", "voltage"),
    ("settling_time", "measure_only"),
])
def test_keithley_binding_agrees_with_parameter_dimension_and_mode(channel, target, mode):
    owner = RecipeNode("owner", "configure_keithley", {"channel": channel, "mode": mode})
    binding = KEITHLEY.binding_for_target(owner, f"keithley.{channel}.{target}")
    KEITHLEY.validate_binding(owner, binding)
    for change in ({"parameter_id": "unsupported"}, {"dimension": "frequency"},
                   {"endpoint": "B" if channel == "A" else "A"}):
        with pytest.raises(ConfigurationError):
            KEITHLEY.validate_binding(owner, replace(binding, **change))
    if target != "settling_time":
        wrong_mode = "voltage" if mode == "current" else "current"
        with pytest.raises(ConfigurationError, match="source mode"):
            KEITHLEY.validate_binding(replace(owner, data={"channel": channel, "mode": wrong_mode}), binding)


@pytest.mark.parametrize("target", [
    "spectrum.start_frequency", "spectrum.stop_frequency", "spectrum.reference_level",
    "sg.frequency", "sg.power",
])
def test_anritsu_binding_cannot_name_another_parameter(target):
    owner = RecipeNode("owner", "configure_anritsu", {})
    binding = ANRITSU.binding_for_target(owner, f"anritsu.{target}")
    ANRITSU.validate_binding(owner, binding)
    with pytest.raises(ConfigurationError, match="parameter"):
        ANRITSU.validate_binding(owner, replace(binding, parameter_id="spectrum.unknown"))
    if target.startswith("sg."):
        ANRITSU.validate_binding(owner, replace(binding, parameter_id=target.replace("sg.", "signal_generator.")))


@pytest.mark.parametrize("target,module,endpoint,parameter", [
    ("keithley.A.current", "keithley", "A", "source.compliance"),
    ("keithley.B.current", "keithley", "A", "source.level"),
    ("anritsu.spectrum.start_frequency", "anritsu", "SPECTRUM", "spectrum.stop_frequency"),
    ("anritsu.sg.frequency", "anritsu", "SPECTRUM", "sg.frequency"),
])
def test_explicit_yaml_identity_is_rejected_before_compilation(target, module, endpoint, parameter):
    value = "0.1 mA" if module == "keithley" else "1 MHz"
    recipe = parse_recipe_text(f"""schema_version: 1
name: invalid-binding
root:
  id: axis
  type: sweep
  target: {target}
  binding:
    owner_node_id: axis
    device_module: {module}
    endpoint: {endpoint}
    parameter_id: {parameter}
  segments:
    - value: {value}
  children: []
""")
    with pytest.raises(ConfigurationError, match="binding endpoint/parameter"):
        normalize_recipe_tree(recipe, built_in_device_registry().sweep_providers())
    with pytest.raises(ConfigurationError, match="binding endpoint/parameter"):
        RecipeCompiler(loaded_settings()).compile(recipe)
