from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.domain.errors import ConfigurationError
from app.recipes import parse_recipe_text
from app.recipes.semantic_tree import (
    SemanticNodeKind,
    SweepBindingDraft,
    normalize_recipe_tree,
)


class _KeithleyResolver:
    module_key = "keithley"

    def bind_legacy_action(self, node, action):
        parameter_id = str(action["parameter_id"])
        endpoint = str(node.data["configuration"]["channel"])
        target = f"keithley.{endpoint}.current"
        return SweepBindingDraft(
            owner_node_id=node.id,
            device_module=self.module_key,
            endpoint=endpoint,
            parameter_id=parameter_id,
            target=target,
            dimension="current",
            stages=tuple(action.get("segments", ())),
        )


class _WrongDimensionResolver(_KeithleyResolver):
    def bind_legacy_action(self, node, action):
        draft = super().bind_legacy_action(node, action)
        return SweepBindingDraft(
            owner_node_id=draft.owner_node_id,
            device_module=draft.device_module,
            endpoint=draft.endpoint,
            parameter_id=draft.parameter_id,
            target=draft.target,
            dimension="voltage",
            stages=draft.stages,
        )


def _providers():
    return {"keithley": _KeithleyResolver()}


LEGACY_KEITHLEY_SWEEP = """\
schema_version: 1
name: legacy-local-axis
root:
  id: measurement
  type: sequence
  children:
    - id: keithley-b
      type: sequence
      device_module: keithley
      operation: configure_selected_parameters
      configuration: {channel: B, source_mode: current}
      parameter_actions:
        - parameter_id: source.level
          mode: sweep
          segments:
            - {start: 0 A, stop: 5 mA, points: 2}
            - {start: 5 mA, stop: 10 mA, points: 2}
      children:
        - {id: checkpoint, type: checkpoint}
"""


def test_legacy_device_sweep_normalizes_to_one_axis_and_loop() -> None:
    tree = normalize_recipe_tree(parse_recipe_text(LEGACY_KEITHLEY_SWEEP), _providers())

    axis = tree.require("keithley-b.axis.source-level")
    assert axis.kind is SemanticNodeKind.SWEEP_AXIS
    assert axis.axis is not None
    assert axis.axis.target == "keithley.B.current"
    assert [child.kind for child in axis.children] == [SemanticNodeKind.LOOP_BODY]
    assert axis.children[0].children[0].kind is SemanticNodeKind.SET_ROI_VALUE
    assert axis.children[0].children[1].source_node_id == "checkpoint"
    assert tree.parent_by_id[axis.semantic_id] == "keithley-b"
    assert tree.children_by_id[axis.semantic_id] == (axis.children[0].semantic_id,)


def test_shared_stage_boundary_is_deduplicated_once_and_stages_remain_visible() -> None:
    tree = normalize_recipe_tree(parse_recipe_text(LEGACY_KEITHLEY_SWEEP), _providers())
    binding = tree.require("keithley-b.axis.source-level").axis
    assert binding is not None

    assert tuple(point.si_value for point in binding.points).count(0.005) == 1
    assert tuple(len(stage.points) for stage in binding.stages) == (2, 1)


def test_nested_axes_generate_cartesian_context_with_outer_setpoint_active() -> None:
    source = """\
schema_version: 1
name: nested
root:
  id: keithley-b
  type: sequence
  device_module: keithley
  children:
    - id: current-axis
      type: sweep
      target: keithley.B.current
      binding:
        owner_node_id: keithley-b
        device_module: keithley
        endpoint: B
        parameter_id: source.level
      start: 0 A
      stop: 1 mA
      points: 2
      children:
        - id: compliance-axis
          type: sweep
          target: keithley.B.compliance_voltage
          binding:
            owner_node_id: keithley-b
            device_module: keithley
            endpoint: B
            parameter_id: source.compliance
          start: 1 V
          stop: 3 V
          points: 3
          children:
            - {id: point, type: checkpoint}
"""
    tree = normalize_recipe_tree(parse_recipe_text(source), _providers())

    contexts = tree.point_contexts["compliance-axis"]
    assert len(contexts) == 6
    assert [context.point_index for context in contexts] == [0, 1, 2, 0, 1, 2]
    assert contexts[3].active_setpoints_si == {
        "keithley.B.current": 0.001,
        "keithley.B.compliance_voltage": 1.0,
    }
    assert contexts[3].loop_path == ("current-axis", "compliance-axis")


def test_nested_axes_reject_duplicate_active_binding_to_same_target() -> None:
    source = """\
schema_version: 1
name: duplicate-active-target
root:
  id: first
  type: sweep
  target: keithley.B.current
  start: 0 A
  stop: 1 mA
  points: 2
  children:
    - id: second
      type: sweep
      target: keithley.B.current
      start: 1 mA
      stop: 2 mA
      points: 2
      children:
        - {id: point, type: checkpoint}
"""
    with pytest.raises(ConfigurationError, match="duplicate active sweep binding"):
        normalize_recipe_tree(parse_recipe_text(source), _providers())


def test_legacy_normalization_rejects_ambiguous_sweeps_and_dimension_mismatch() -> None:
    ambiguous = LEGACY_KEITHLEY_SWEEP.replace(
        "      children:",
        "        - parameter_id: source.level\n"
        "          mode: sweep\n"
        "          segments: [{start: 0 A, stop: 1 mA, points: 2}]\n"
        "      children:",
    )
    with pytest.raises(ConfigurationError, match="multiple legacy local sweeps"):
        normalize_recipe_tree(parse_recipe_text(ambiguous), _providers())
    with pytest.raises(ConfigurationError, match="dimension"):
        normalize_recipe_tree(
            parse_recipe_text(LEGACY_KEITHLEY_SWEEP),
            {"keithley": _WrongDimensionResolver()},
        )


@pytest.mark.parametrize(
    "binding",
    [
        "binding: []",
        "binding: {owner_node_id: '', device_module: keithley, endpoint: B, parameter_id: source.level}",
        "binding: {owner_node_id: keithley-b, device_module: keithley, endpoint: B}",
        "binding: {owner_node_id: keithley-b, device_module: keithley, endpoint: B, parameter_id: source.level, extra: no}",
    ],
)
def test_canonical_binding_rejects_malformed_shape(binding: str) -> None:
    source = f"""\
schema_version: 1
name: malformed-binding
root:
  id: axis
  type: sweep
  target: keithley.B.current
  {binding}
  start: 0 A
  stop: 1 mA
  points: 2
  children:
    - {{id: point, type: checkpoint}}
"""
    with pytest.raises(ConfigurationError, match="binding"):
        parse_recipe_text(source)


def test_canonical_binding_rejects_unknown_owner_and_unknown_target() -> None:
    unknown_owner = """\
schema_version: 1
name: unknown-owner
root:
  id: axis
  type: sweep
  target: keithley.B.current
  binding:
    owner_node_id: missing
    device_module: keithley
    endpoint: B
    parameter_id: source.level
  start: 0 A
  stop: 1 mA
  points: 2
  children: [{id: point, type: checkpoint}]
"""
    with pytest.raises(ConfigurationError, match="owner_node_id"):
        normalize_recipe_tree(parse_recipe_text(unknown_owner), _providers())

    unknown_target = unknown_owner.replace("owner_node_id: missing", "owner_node_id: axis").replace(
        "keithley.B.current", "keithley.B.not_registered"
    )
    with pytest.raises(ConfigurationError, match="Unknown sweep target"):
        normalize_recipe_tree(parse_recipe_text(unknown_target), _providers())


def test_snapshot_preserves_source_and_is_immutable() -> None:
    recipe = parse_recipe_text(LEGACY_KEITHLEY_SWEEP)
    tree = normalize_recipe_tree(recipe, _providers())

    assert tree.source_text == LEGACY_KEITHLEY_SWEEP
    with pytest.raises(TypeError):
        tree.by_id["new"] = tree.roots[0]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        tree.roots[0].label = "changed"  # type: ignore[misc]
    with pytest.raises(ConfigurationError, match="Unknown semantic node"):
        tree.require("missing")
