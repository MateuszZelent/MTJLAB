"""Rigol binding identities must agree before any plan can execute."""

from dataclasses import replace

import pytest

from app.devices.registry import built_in_device_registry
from app.devices.rigol_dg1000z.sweep_provider import PROVIDER
from app.domain.errors import ConfigurationError
from app.domain.quantities import parse_quantity
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.recipes.models import RecipeNode
from app.recipes.semantic_tree import normalize_recipe_tree
from tests.helpers import loaded_settings


def recipe(endpoint="1", parameter="carrier.frequency", target="rigol.1.frequency"):
    return parse_recipe_text(f"""
schema_version: 1
name: binding-check
root:
  id: root
  type: sequence
  children:
    - id: owner
      type: configure_rigol
      channel: 1
      waveform: SIN
      frequency: 1 MHz
      high_level: 10 mV
      low_level: -10 mV
    - id: axis
      type: sweep
      target: {target}
      binding:
        owner_node_id: owner
        device_module: rigol
        endpoint: '{endpoint}'
        parameter_id: {parameter}
      start: 1 MHz
      stop: 2 MHz
      points: 2
      children:
        - {{id: settle, type: wait, duration: '1 ms'}}
""")


@pytest.mark.parametrize("endpoint,parameter,target", [
    ("2", "carrier.frequency", "rigol.1.frequency"),
    ("1", "carrier.frequency", "rigol.2.frequency"),
    ("1", "carrier.offset", "rigol.1.frequency"),
    ("1", "carrier.unknown", "rigol.1.frequency"),
])
def test_inconsistent_yaml_rejected_in_tree_and_compiler(endpoint, parameter, target):
    source = recipe(endpoint, parameter, target)
    with pytest.raises(ConfigurationError, match="binding endpoint/parameter"):
        normalize_recipe_tree(source, built_in_device_registry().sweep_providers())
    with pytest.raises(ConfigurationError, match="binding endpoint/parameter"):
        RecipeCompiler(loaded_settings()).compile(source)


def test_valid_yaml_compiles_to_same_channel_and_frequency():
    plan = RecipeCompiler(loaded_settings()).compile(recipe())
    updates = [a for a in plan.actions if a.kind == "update_rigol_frequency"]
    assert [a.payload["frequency_hz"] for a in updates] == [1e6, 2e6]
    assert all(a.payload["channel"] == 1 for a in updates)


@pytest.mark.parametrize("change", [
    {"endpoint": "2"}, {"target": "rigol.2.frequency"},
    {"parameter_id": "carrier.offset"}, {"dimension": "voltage"},
])
def test_provider_rejects_inconsistent_typed_binding(change):
    node = RecipeNode("owner", "configure_rigol", {"channel": 1})
    binding = PROVIDER.binding_for_target(node, "rigol.1.frequency")
    with pytest.raises(ConfigurationError):
        PROVIDER.compile_point(node, replace(binding, **change), parse_quantity("1 MHz"), {}, loaded_settings())


def test_level_baseline_is_never_taken_from_other_channel():
    node = RecipeNode("axis", "sweep", {"channel": 1}, children=(
        RecipeNode("ch2", "configure_rigol", {"channel": 2, "high_level": "90 mV", "low_level": "-90 mV"}),
        RecipeNode("ch1", "configure_rigol", {"channel": 1, "high_level": "10 mV", "low_level": "-10 mV"}),
    ))
    binding = PROVIDER.binding_for_target(node, "rigol.1.high_level")
    update = PROVIDER.compile_point(node, binding, parse_quantity("20 mV"), {}, loaded_settings())
    assert update.payload == {"channel": 1, "high_level_v": .02, "low_level_v": -.01}
    with pytest.raises(ConfigurationError, match="explicit .* baseline"):
        PROVIDER.compile_point(replace(node, children=node.children[:1]), binding,
                               parse_quantity("20 mV"), {}, loaded_settings())
