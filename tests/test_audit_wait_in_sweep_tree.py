"""Comprehensive audit tests for the wait function in the sweep tree."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import threading
import time
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.devices.registry import built_in_device_registry
from app.domain.errors import SafetyViolation
from app.domain.quantities import QuantityError
from app.engine.compiler import RecipeCompiler
from app.engine.policy import ExecutionPolicy
from app.engine.runner import ExecutionMode, RecipeRunner
from app.recipes import parse_recipe_text
from app.recipes.models import RecipeNode
from app.recipes.semantic_tree import (
    SemanticNodeKind,
    normalize_recipe_tree,
)
from app.settings import SettingsRepository
from app.storage.hdf5_writer import Hdf5RunWriter
from app.ui.measurement_tree.model import MeasurementTreeModel, MeasurementTreeRole
from app.ui.recipes.common_dialogs import ActionNodeEditorDialog
from app.ui.shell.main_window import simulated_station_settings
from tests.helpers import simulation_settings


@dataclass
class MemoryWriter:
    points: list[object] = field(default_factory=list)
    events: list[tuple[str, dict[str, object], str]] = field(default_factory=list)
    status: str | None = None

    def append(self, point: object, trace: object = None) -> int:
        self.points.append((point, trace))
        return len(self.points) - 1

    def close(self, status: str) -> None:
        self.status = status

    def append_event(self, name: str, data: dict[str, object], *, severity: str = "info") -> None:
        self.events.append((name, data, severity))


def _providers():
    return built_in_device_registry().sweep_providers()


SINGLE_SWEEP_WAIT_YAML = """\
schema_version: 1
name: single-sweep-wait
root:
  id: root
  type: sequence
  children:
    - id: keithley-cfg
      type: configure_keithley
      channel: B
      mode: current
      level: 0 A
      compliance: 67 mV
    - id: current-sweep
      type: sweep
      target: keithley.B.current
      start: 0 A
      stop: 2 mA
      points: 3
      children:
        - id: settle-wait
          type: wait
          duration: 250 ms
        - id: measure-point
          type: measure_keithley
          channel: B
finally:
  - id: keithley-off
    type: set_keithley_output
    channel: B
    enabled: false
"""

NESTED_SWEEP_WAIT_YAML = """\
schema_version: 1
name: nested-sweep-wait
root:
  id: root
  type: sequence
  children:
    - id: keithley-cfg
      type: configure_keithley
      channel: B
      mode: current
      level: 0 A
      compliance: 67 mV
    - id: outer-sweep
      type: sweep
      target: keithley.B.compliance_voltage
      start: 50 mV
      stop: 65 mV
      points: 2
      children:
        - id: outer-wait
          type: wait
          duration: 100 ms
        - id: inner-sweep
          type: sweep
          target: keithley.B.current
          start: 0 A
          stop: 1 mA
          points: 3
          children:
            - id: inner-wait
              type: wait
              duration: 50 ms
            - id: measure-point
              type: measure_keithley
              channel: B
finally:
  - id: keithley-off
    type: set_keithley_output
    channel: B
    enabled: false
"""


def test_single_sweep_wait_semantic_tree_structure():
    """Verify how a wait action inside a sweep node is structured in the semantic tree."""
    recipe = parse_recipe_text(SINGLE_SWEEP_WAIT_YAML)
    tree = normalize_recipe_tree(recipe, _providers())

    # The sweep axis node
    axis = tree.require("current-sweep")
    assert axis.kind is SemanticNodeKind.SWEEP_AXIS
    assert axis.axis is not None
    assert axis.axis.target == "keithley.B.current"
    assert len(axis.children) == 1

    # The loop body
    loop_body = axis.children[0]
    assert loop_body.kind is SemanticNodeKind.LOOP_BODY
    assert loop_body.label == "For each source-current point"
    # Children of loop body: [ROI, settle-wait, measure-point]
    assert len(loop_body.children) == 3
    assert loop_body.children[0].kind is SemanticNodeKind.SET_ROI_VALUE
    assert loop_body.children[1].semantic_id == "settle-wait"
    assert loop_body.children[1].kind is SemanticNodeKind.ACTION
    assert loop_body.children[1].label == "Wait · 250 ms"
    assert loop_body.children[1].source_node_id == "settle-wait"
    assert loop_body.children[1].data["duration"] == "250 ms"
    assert loop_body.children[2].semantic_id == "measure-point"

    # Index lookups
    assert tree.parent_by_id["settle-wait"] == loop_body.semantic_id
    assert "settle-wait" in tree.children_by_id[loop_body.semantic_id]
    assert tree.require("settle-wait") is loop_body.children[1]


def test_measurement_tree_model_wait_presentation_and_lifecycle():
    """Verify MeasurementTreeModel display roles, text, progress, and state transitions for wait in sweep."""
    _ = QApplication.instance() or QApplication([])
    recipe = parse_recipe_text(SINGLE_SWEEP_WAIT_YAML)
    tree = normalize_recipe_tree(recipe, _providers())
    model = MeasurementTreeModel(tree)

    wait_idx = model.index_for_semantic_id("settle-wait")
    assert wait_idx.isValid()

    # Pre-run state
    assert model.data(wait_idx, int(Qt.ItemDataRole.DisplayRole)) == "Wait · 250 ms"
    assert model.data(wait_idx.siblingAtColumn(1), int(Qt.ItemDataRole.DisplayRole)) == "250 ms"
    assert model.data(wait_idx.siblingAtColumn(2), int(Qt.ItemDataRole.DisplayRole)) == "—"
    assert model.data(wait_idx.siblingAtColumn(3), int(Qt.ItemDataRole.DisplayRole)) == "READY"
    assert model.data(wait_idx, int(MeasurementTreeRole.NODE_KIND)) == "action"
    assert model.data(wait_idx, int(MeasurementTreeRole.SEMANTIC_ID)) == "settle-wait"

    # Running state at point 1 (index 0 of 3)
    axis_context = {
        "axis_id": "current-sweep",
        "point_index": 0,
        "point_count": 3,
        "stage_index": 0,
        "value_si": 0.0,
        "active_setpoints_si": {"keithley.B.current": 0.0},
        "loop_path": ("current-sweep",),
    }
    running_state = {
        "semantic_id": "settle-wait",
        "kind": "wait",
        "phase": "running",
        "duration_s": 0.25,
        "action_index": 2,
        "total_actions": 9,
        "axis_context": axis_context,
    }
    model.apply_state(running_state)

    assert model.data(wait_idx.siblingAtColumn(1), int(Qt.ItemDataRole.DisplayRole)) == "250 ms"
    assert model.data(wait_idx.siblingAtColumn(2), int(Qt.ItemDataRole.DisplayRole)) == "1/3 · ROI 1"
    assert model.data(wait_idx.siblingAtColumn(3), int(Qt.ItemDataRole.DisplayRole)) == "RUNNING"

    # Applied state at point 1
    applied_state = {
        **running_state,
        "phase": "applied",
        "applied_si": 0.25,
        "readback_si": 0.25,
    }
    model.apply_state(applied_state)
    assert model.data(wait_idx.siblingAtColumn(2), int(Qt.ItemDataRole.DisplayRole)) == "1/3 · ROI 1"
    assert model.data(wait_idx.siblingAtColumn(3), int(Qt.ItemDataRole.DisplayRole)) == "APPLIED"
    foreground = model.data(wait_idx, int(Qt.ItemDataRole.ForegroundRole))
    assert foreground is not None

    # Running state at point 2 (index 1 of 3)
    axis_context_pt2 = {**axis_context, "point_index": 1, "value_si": 0.001}
    running_pt2 = {**running_state, "action_index": 5, "axis_context": axis_context_pt2}
    model.apply_state(running_pt2)
    assert model.data(wait_idx.siblingAtColumn(2), int(Qt.ItemDataRole.DisplayRole)) == "2/3 · ROI 1"
    assert model.data(wait_idx.siblingAtColumn(3), int(Qt.ItemDataRole.DisplayRole)) == "RUNNING"


def test_single_sweep_wait_compilation():
    """Verify that RecipeCompiler compiles a wait inside a sweep once per sweep point with exact context."""
    recipe = parse_recipe_text(SINGLE_SWEEP_WAIT_YAML)
    compiler = RecipeCompiler(simulation_settings())
    plan = compiler.compile(recipe)

    # 3 points: For each point: Set ROI / update -> wait -> measure
    waits = [a for a in plan.actions if a.kind == "wait"]
    assert len(waits) == 3

    for idx, wait_action in enumerate(waits):
        assert wait_action.node_id == "settle-wait"
        assert wait_action.semantic_id == "settle-wait"
        assert wait_action.payload["duration_s"] == 0.25
        assert wait_action.axis_context is not None
        assert wait_action.axis_context.point_index == idx
        assert wait_action.axis_context.point_count == 3
        expected_current = [0.0, 0.001, 0.002][idx]
        assert math.isclose(wait_action.axis_context.value_si, expected_current, abs_tol=1e-9)
        assert math.isclose(wait_action.setpoints_si["keithley.B.current"], expected_current, abs_tol=1e-9)

    action_kinds = [a.kind for a in plan.actions]
    assert action_kinds.count("wait") == 3
    assert action_kinds.count("measure_keithley") == 3


def test_nested_sweep_wait_compilation_and_cartesian_expansion():
    """Verify nested sweep axes with waits at both outer and inner levels."""
    recipe = parse_recipe_text(NESTED_SWEEP_WAIT_YAML)
    compiler = RecipeCompiler(simulation_settings())
    plan = compiler.compile(recipe)

    # Outer sweep: 2 points (50 mV, 65 mV)
    # Inner sweep: 3 points (0 A, 0.5 mA, 1 mA)
    # Total outer waits: 2
    # Total inner waits: 2 * 3 = 6
    outer_waits = [a for a in plan.actions if a.node_id == "outer-wait"]
    inner_waits = [a for a in plan.actions if a.node_id == "inner-wait"]

    assert len(outer_waits) == 2
    assert len(inner_waits) == 6

    for idx, ow in enumerate(outer_waits):
        assert ow.payload["duration_s"] == 0.1
        assert ow.axis_context.point_index == idx
        assert ow.axis_context.point_count == 2
        expected_v = [0.05, 0.065][idx]
        assert math.isclose(ow.axis_context.value_si, expected_v, abs_tol=1e-9)

    for idx, iw in enumerate(inner_waits):
        assert iw.payload["duration_s"] == 0.05
        outer_pt = idx // 3
        inner_pt = idx % 3
        assert iw.axis_context.point_index == inner_pt
        assert iw.axis_context.point_count == 3
        expected_v = [0.05, 0.065][outer_pt]
        expected_i = [0.0, 0.0005, 0.001][inner_pt]
        assert math.isclose(iw.axis_context.value_si, expected_i, abs_tol=1e-9)
        assert math.isclose(iw.setpoints_si["keithley.B.compliance_voltage"], expected_v, abs_tol=1e-9)
        assert math.isclose(iw.setpoints_si["keithley.B.current"], expected_i, abs_tol=1e-9)


def test_sweep_wait_safety_bounds_and_validation():
    """Verify safety boundaries: 0s, 3600s, negative, >3600s, wrong unit."""
    compiler = RecipeCompiler(simulation_settings())

    def make_recipe(dur_text: str) -> str:
        return f"""\
schema_version: 1
name: bounds-test
root:
  id: root
  type: sequence
  children:
    - id: swp
      type: sweep
      target: keithley.B.current
      start: 0 A
      stop: 1 mA
      points: 2
      children:
        - id: w
          type: wait
          duration: {dur_text}
        - id: m
          type: measure_keithley
          channel: B
"""

    # 0 s allowed
    plan_0 = compiler.compile(parse_recipe_text(make_recipe("0 s")))
    waits_0 = [a for a in plan_0.actions if a.kind == "wait"]
    assert len(waits_0) == 2
    assert waits_0[0].payload["duration_s"] == 0.0

    # 3600 s allowed
    plan_3600 = compiler.compile(parse_recipe_text(make_recipe("3600 s")))
    waits_3600 = [a for a in plan_3600.actions if a.kind == "wait"]
    assert waits_3600[0].payload["duration_s"] == 3600.0

    # >3600 s rejected
    with pytest.raises(SafetyViolation, match="Wait duration must be in the range 0–3600 s"):
        compiler.compile(parse_recipe_text(make_recipe("3601 s")))

    # Negative rejected
    with pytest.raises(SafetyViolation, match="Wait duration must be in the range 0–3600 s"):
        compiler.compile(parse_recipe_text(make_recipe("-10 ms")))

    # Incompatible unit rejected
    with pytest.raises(QuantityError):
        compiler.compile(parse_recipe_text(make_recipe("5 V")))


def test_sweep_wait_variable_substitution():
    """Verify wait duration referencing a swept variable context."""
    source = """\
schema_version: 1
name: sweep-variable-wait
root:
  id: root
  type: sequence
  children:
    - id: settle-sweep
      type: sweep
      target: keithley.B.settling_time
      start: 50 ms
      stop: 150 ms
      points: 3
      children:
        - id: dynamic-wait
          type: wait
          duration: ${keithley.B.settling_time}
        - id: m
          type: measure_keithley
          channel: B
"""
    compiler = RecipeCompiler(simulation_settings())
    plan = compiler.compile(parse_recipe_text(source))
    waits = [a for a in plan.actions if a.node_id == "dynamic-wait"]
    assert len(waits) == 3
    assert [math.isclose(w.payload["duration_s"], exp, abs_tol=1e-9) for w, exp in zip(waits, [0.05, 0.10, 0.15])]


def test_legacy_device_sweep_auto_settle_coexistence():
    """Verify automatic settling wait from legacy Keithley sweep coexists cleanly with explicit wait."""
    source = """\
schema_version: 1
name: auto-and-explicit-settle
root:
  id: root
  type: sequence
  children:
    - id: keithley-axis
      type: sequence
      device_module: keithley
      operation: configure_selected_parameters
      channel: B
      source_mode: current
      configuration:
        channel: B
        source_mode: current
        source_level: 0 A
        compliance: 67 mV
        settling_time: 40 ms
      parameter_actions:
        - parameter_id: source.level
          mode: sweep
          segments:
            - start: 0 A
              stop: 1 mA
              points: 2
      children:
        - id: user-wait
          type: wait
          duration: 60 ms
        - id: m
          type: measure_keithley
          channel: B
"""
    compiler = RecipeCompiler(simulation_settings())
    plan = compiler.compile(parse_recipe_text(source))
    
    # Each legacy sweep point has an auto-settle wait (node_id="keithley-axis.settle") and user wait (node_id="user-wait")
    waits = [a for a in plan.actions if a.kind == "wait"]
    assert len(waits) == 4
    auto_waits = [w for w in waits if w.node_id == "keithley-axis.settle"]
    user_waits = [w for w in waits if w.node_id == "user-wait"]
    assert len(auto_waits) == 2
    assert len(user_waits) == 2
    assert all(w.payload["duration_s"] == 0.04 for w in auto_waits)
    assert all(w.payload["duration_s"] == 0.06 for w in user_waits)
    assert all(w.semantic_id is None for w in auto_waits)
    assert all(w.semantic_id == "user-wait" for w in user_waits)


def test_sweep_wait_simulated_runner_execution():
    """Verify simulated Runner execution of a sweep with wait actions and event emission."""
    source = """\
schema_version: 1
name: simulated-sweep-wait
root:
  id: root
  type: sequence
  children:
    - id: keithley-cfg
      type: configure_keithley
      channel: B
      mode: current
      level: 0 A
      compliance: 67 mV
    - id: current-sweep
      type: sweep
      target: keithley.B.current
      start: 0 A
      stop: 1 mA
      points: 2
      children:
        - id: step-wait
          type: wait
          duration: 30 ms
        - id: measure-point
          type: measure_keithley
          channel: B
finally:
  - id: keithley-off
    type: set_keithley_output
    channel: B
    enabled: false
"""
    settings = simulation_settings(approved=True)
    compiler = RecipeCompiler(settings)
    plan = compiler.compile(parse_recipe_text(source))

    events = []
    def on_event(name, data):
        events.append((name, data))

    registry = built_in_device_registry()
    keithley = registry.get("keithley").create_adapter(settings, simulation=True)
    anritsu = registry.get("anritsu").create_adapter(settings, simulation=True)
    rigol = registry.get("rigol").create_adapter(settings, simulation=True)
    for dev in (keithley, anritsu, rigol):
        dev.connect()

    runner = RecipeRunner(
        writer=MemoryWriter(),
        keithley=keithley,
        anritsu=anritsu,
        rigol=rigol,
        on_event=on_event,
        policy=ExecutionPolicy(heartbeat_interval_s=0.01),
    )

    t0 = time.monotonic()
    result = runner.run(plan)
    elapsed = time.monotonic() - t0

    assert result.state.value == "safe"
    # 2 points * 30 ms = 60 ms minimum wait
    assert elapsed >= 0.055

    wait_started_events = [
        data for name, data in events
        if name == "semantic_operation_started" and data.get("semantic_id") == "step-wait"
    ]
    wait_applied_events = [
        data for name, data in events
        if name == "semantic_operation_applied" and data.get("semantic_id") == "step-wait"
    ]

    assert len(wait_started_events) == 2
    assert len(wait_applied_events) == 2

    assert wait_started_events[0]["axis_context"]["point_index"] == 0
    assert wait_started_events[1]["axis_context"]["point_index"] == 1
    assert wait_applied_events[0]["applied_si"] == 0.03
    assert wait_applied_events[1]["applied_si"] == 0.03


def test_sweep_wait_prompt_cancellation():
    """Verify that stopping during a wait inside a sweep terminates within <= 250 ms without blocking."""
    source = """\
schema_version: 1
name: interruptible-wait
root:
  id: root
  type: sequence
  children:
    - id: keithley-cfg
      type: configure_keithley
      channel: B
      mode: current
      level: 0 A
      compliance: 67 mV
    - id: current-sweep
      type: sweep
      target: keithley.B.current
      start: 0 A
      stop: 1 mA
      points: 2
      children:
        - id: long-wait
          type: wait
          duration: 10 s
        - id: m
          type: measure_keithley
          channel: B
finally:
  - id: keithley-off
    type: set_keithley_output
    channel: B
    enabled: false
"""
    settings = simulation_settings(approved=True)
    compiler = RecipeCompiler(settings)
    plan = compiler.compile(parse_recipe_text(source))

    registry = built_in_device_registry()
    keithley = registry.get("keithley").create_adapter(settings, simulation=True)
    anritsu = registry.get("anritsu").create_adapter(settings, simulation=True)
    rigol = registry.get("rigol").create_adapter(settings, simulation=True)
    for dev in (keithley, anritsu, rigol):
        dev.connect()

    runner = RecipeRunner(
        writer=MemoryWriter(),
        keithley=keithley,
        anritsu=anritsu,
        rigol=rigol,
        policy=ExecutionPolicy(heartbeat_interval_s=0.01),
    )

    t0 = time.monotonic()
    thread = threading.Thread(target=runner.run, args=(plan,))
    thread.start()

    # Allow wait to start
    time.sleep(0.08)
    runner.request_stop()
    thread.join(timeout=1.0)
    stop_duration = time.monotonic() - t0

    assert not thread.is_alive()
    # It must NOT wait 10 s; abort should complete promptly
    assert stop_duration < 0.6
    assert runner.state.value == "safe"


def test_action_node_editor_dialog_validates_wait_duration():
    """Verify that the ActionNodeEditorDialog modal edits and validates wait duration."""
    _ = QApplication.instance() or QApplication([])
    wait_node = RecipeNode("my-wait", "wait", {"duration": "100 ms"})
    dialog = ActionNodeEditorDialog(wait_node)
    try:
        assert "duration" in dialog._editors
        editor, kind = dialog._editors["duration"]
        assert kind == "str"
        assert editor.text() == "100 ms"

        # Valid edit
        editor.setText("2.5 s")
        fields = dialog.node_fields()
        dialog._validate_fields(fields)
        assert fields["duration"] == "2.5 s"

        # Invalid edit
        editor.setText("not-a-time")
        invalid_fields = dialog.node_fields()
        with pytest.raises(QuantityError):
            dialog._validate_fields(invalid_fields)
    finally:
        dialog.close()


def test_multiple_wait_nodes_in_single_sweep():
    """Verify multiple distinct wait nodes in the same sweep loop compile and update independently."""
    source = """\
schema_version: 1
name: multi-wait-sweep
root:
  id: root
  type: sequence
  children:
    - id: keithley-cfg
      type: configure_keithley
      channel: B
      mode: current
      level: 0 A
      compliance: 67 mV
    - id: current-sweep
      type: sweep
      target: keithley.B.current
      start: 0 A
      stop: 1 mA
      points: 2
      children:
        - id: pre-wait
          type: wait
          duration: 40 ms
        - id: measure-1
          type: measure_keithley
          channel: B
        - id: post-wait
          type: wait
          duration: 80 ms
        - id: measure-2
          type: measure_keithley
          channel: B
"""
    recipe = parse_recipe_text(source)
    tree = normalize_recipe_tree(recipe, _providers())
    assert "pre-wait" in tree.by_id
    assert "post-wait" in tree.by_id

    compiler = RecipeCompiler(simulation_settings())
    plan = compiler.compile(recipe)

    pre_waits = [a for a in plan.actions if a.node_id == "pre-wait"]
    post_waits = [a for a in plan.actions if a.node_id == "post-wait"]
    assert len(pre_waits) == 2
    assert len(post_waits) == 2
    assert all(w.payload["duration_s"] == 0.04 for w in pre_waits)
    assert all(w.payload["duration_s"] == 0.08 for w in post_waits)
    assert all(w.semantic_id == "pre-wait" for w in pre_waits)
    assert all(w.semantic_id == "post-wait" for w in post_waits)


def test_sweep_containing_repeat_with_wait():
    """Verify a repeat loop inside a sweep preserves axis context on child wait actions."""
    source = """\
schema_version: 1
name: sweep-repeat-wait
root:
  id: root
  type: sequence
  children:
    - id: keithley-cfg
      type: configure_keithley
      channel: B
      mode: current
      level: 0 A
      compliance: 67 mV
    - id: current-sweep
      type: sweep
      target: keithley.B.current
      start: 0 A
      stop: 1 mA
      points: 2
      children:
        - id: rep
          type: repeat
          count: 3
          children:
            - id: loop-wait
              type: wait
              duration: 15 ms
            - id: m
              type: measure_keithley
              channel: B
"""
    recipe = parse_recipe_text(source)
    compiler = RecipeCompiler(simulation_settings())
    plan = compiler.compile(recipe)

    # 2 sweep points * 3 repeat count = 6 wait actions
    waits = [a for a in plan.actions if a.node_id == "loop-wait"]
    assert len(waits) == 6
    assert all(w.payload["duration_s"] == 0.015 for w in waits)
    # The axis context should be preserved from the outer sweep
    assert [w.axis_context.point_index for w in waits] == [0, 0, 0, 1, 1, 1]


def test_sweep_containing_if_condition_with_wait():
    """Verify conditional branches inside a sweep compile the chosen branch wait."""
    source = """\
schema_version: 1
name: sweep-if-wait
root:
  id: root
  type: sequence
  children:
    - id: keithley-cfg
      type: configure_keithley
      channel: B
      mode: current
      level: 0 A
      compliance: 67 mV
    - id: current-sweep
      type: sweep
      target: keithley.B.current
      start: 0 A
      stop: 2 mA
      points: 3
      children:
        - id: branch-if
          type: if
          left: ${keithley.B.current}
          operator: ">"
          right: 0.5 mA
          children:
            - id: long-wait
              type: wait
              duration: 100 ms
          else:
            - id: short-wait
              type: wait
              duration: 20 ms
        - id: m
          type: measure_keithley
          channel: B
"""
    recipe = parse_recipe_text(source)
    compiler = RecipeCompiler(simulation_settings())
    plan = compiler.compile(recipe)

    # Points: 0 mA (short), 1 mA (long), 2 mA (long)
    waits = [a for a in plan.actions if a.kind == "wait"]
    assert len(waits) == 3
    assert waits[0].node_id == "short-wait"
    assert waits[0].payload["duration_s"] == 0.02
    assert waits[1].node_id == "long-wait"
    assert waits[1].payload["duration_s"] == 0.10
    assert waits[2].node_id == "long-wait"
    assert waits[2].payload["duration_s"] == 0.10


def test_keithley_sweep_node_from_generator_creates_explicit_wait_child():
    """Verify RecipePage generator creates explicit wait child node inside sweep."""
    from app.ui.recipes.page import RecipePage

    _ = QApplication.instance() or QApplication([])
    settings = simulation_settings(approved=True)
    page = RecipePage(settings)
    try:
        definition = {"target": "keithley.B.current", "label": "Keithley B current"}
        segments = [{"start": "0 A", "stop": "1 mA", "points": 10, "spacing": "linear"}]
        keithley_options = {
            "compliance": "67 mV",
            "nplc": 1.0,
            "settle_time": "150 ms",
            "sense_mode": "2wire",
        }
        node = page._sweep_node_from_generator(
            definition,
            segments,
            keithley_options=keithley_options,
        )
        assert node["type"] == "sweep"
        children = node["children"]
        assert len(children) == 2
        assert children[0]["type"] == "configure_keithley"
        assert children[1]["type"] == "wait"
        assert children[1]["duration"] == "150 ms"

        # Zero settling time should not generate a dummy wait
        keithley_options_zero = dict(keithley_options, settle_time="0 s")
        node_zero = page._sweep_node_from_generator(
            definition,
            segments,
            keithley_options=keithley_options_zero,
        )
        assert len(node_zero["children"]) == 1
        assert node_zero["children"][0]["type"] == "configure_keithley"
    finally:
        page.close()


def test_configure_keithley_accepts_settling_time_alias():
    """Verify configure_keithley compiles settling_time parameter correctly."""
    source = """\
schema_version: 1
name: settling-time-alias-test
root:
  id: root
  type: sequence
  children:
    - id: keithley-cfg
      type: configure_keithley
      channel: B
      mode: current
      level: 100 uA
      compliance: 67 mV
      settling_time: 50 ms
"""
    recipe = parse_recipe_text(source)
    compiler = RecipeCompiler(simulation_settings(approved=True))
    plan = compiler.compile(recipe)

    cfg_action = next(a for a in plan.actions if a.kind == "configure_keithley")
    assert cfg_action.payload["request"].settle_time_s == 0.05


def test_standard_recipes_with_explicit_waits_compile_successfully():
    """Verify standard sweep recipes with explicit wait steps compile cleanly."""
    from copy import deepcopy
    from pathlib import Path
    from app.settings.models import StationSettings

    raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
    raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    raw["devices"]["rigol"]["safety"]["channels"]["1"]["lab_limits"]["frequency"]["max"] = "30 MHz"
    raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
    limits = raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]
    limits["source_current"] = {
        "min": "0 A",
        "max": "150 mA",
        "max_abs": "150 mA",
    }
    limits["measured_current_trip"] = {"min": "-1 mA", "max": "151 mA"}
    limits["max_abs_power"] = "10.05 mW"
    settings = StationSettings.model_validate(raw)

    recipe_files = [
        "recipes/keithley_b_rigol_frequency_anritsu_reference_10x100.yml",
        "recipes/rigol_frequency_anritsu_reference_10.yml",
    ]
    compiler = RecipeCompiler(settings)
    for rel_path in recipe_files:
        path = Path(rel_path)
        assert path.exists(), f"Recipe file missing: {rel_path}"
        recipe = parse_recipe_text(path.read_text(encoding="utf-8"))
        plan = compiler.compile(recipe)
        assert plan is not None
        # Verify that wait actions were generated
        wait_actions = [a for a in plan.actions if a.kind == "wait"]
        assert len(wait_actions) > 0, f"Expected wait actions in {rel_path}"
        assert all(a.payload["duration_s"] > 0 for a in wait_actions)


def test_output_on_off_drag_and_drop_in_sweep_and_tree_presentation() -> None:
    """Verify that all library output ON/OFF blocks are droppable into sweeps and correctly presented in tree."""
    from app.recipes import add_recipe_node
    from app.ui.recipes.page import RecipePage
    from PySide6.QtCore import Qt

    _ = QApplication.instance() or QApplication([])
    settings = simulation_settings(approved=True)
    page = RecipePage(settings)
    try:
        # Create and add a sweep node to the page
        defn = {"target": "keithley.B.current"}
        segs = [{"start": "0 A", "stop": "1 mA", "points": 5, "spacing": "linear"}]
        sweep_node = page._sweep_node_from_generator(defn, segs)
        root_id = parse_recipe_text(page.editor.toPlainText()).root.id
        src = add_recipe_node(page.editor.toPlainText(), parent_id=root_id, branch="children", node=sweep_node)
        page._apply_builder_source(src, "added sweep")
        sweep_id = str(sweep_node["id"])

        # Drop Keithley OUTPUT ON and OUTPUT OFF into the sweep
        assert page._drop_library_block("output:keithley_b", sweep_id, "children", 0)
        assert page._drop_library_block("safety:keithley_b_off", sweep_id, "children", 1)

        # Drop Rigol OUTPUT ON and OUTPUT OFF into the sweep
        assert page._drop_library_block("output:rigol_1", sweep_id, "children", 2)
        assert page._drop_library_block("safety:rigol_1", sweep_id, "children", 3)

        # Drop Anritsu SG OUTPUT ON and OUTPUT OFF into the sweep
        assert page._drop_library_block("output:anritsu_sg", sweep_id, "children", 4)
        assert page._drop_library_block("safety:anritsu_sg", sweep_id, "children", 5)

        # Verify parsed recipe structure
        recipe = parse_recipe_text(page.editor.toPlainText())
        sweep = next(c for c in recipe.root.children if c.id == sweep_id)
        child_types = [c.type for c in sweep.children]
        assert "set_keithley_output" in child_types
        assert "enable_rigol_output" in child_types or "set_rigol_output" in child_types
        assert "enable_anritsu_sg_output" in child_types or "set_anritsu_sg_output" in child_types

        # Verify SemanticTree labels
        tree = page.tree_model.tree
        labels = [node.label for node in tree.by_id.values()]
        assert "Keithley B · OUTPUT ON" in labels
        assert "Keithley B · OUTPUT OFF" in labels
        assert "Rigol CH1 · OUTPUT ON" in labels
        assert "Rigol CH1 · OUTPUT OFF" in labels
        assert "Anritsu SG · OUTPUT ON" in labels
        assert "Anritsu SG · OUTPUT OFF" in labels

        # Verify MeasurementTreeModel value text (Column 1 shows ON / OFF)
        model = page.tree_model
        for node in tree.by_id.values():
            if "OUTPUT ON" in node.label:
                idx = model.index_for_semantic_id(node.semantic_id)
                col1 = model.index(idx.row(), 1, idx.parent())
                assert model.data(col1, Qt.ItemDataRole.DisplayRole) == "ON"
            elif "OUTPUT OFF" in node.label and "Finally" not in node.label:
                idx = model.index_for_semantic_id(node.semantic_id)
                col1 = model.index(idx.row(), 1, idx.parent())
                assert model.data(col1, Qt.ItemDataRole.DisplayRole) == "OFF"
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_device_configuration_output_policy_on_and_off_surfaced_in_tree() -> None:
    """Device configuration sequence nodes with output_policy on/off surface explicit tree rows."""
    recipe_yaml = """
schema_version: 1
name: Device Output Policy Test
root:
  id: sequence-main
  type: sequence
  children:
  - id: keithley-node
    type: sequence
    device_module: keithley
    channel: B
    source_mode: current
    output_policy: on
    parameter_actions:
    - parameter_id: source.level
      mode: sweep
      value: 1 mA
      segments:
      - start: 0 A
        stop: 5 mA
        spacing: linear
        points: 5
    configuration:
      channel: B
      source_mode: current
      source_level: 1 mA
      compliance: 670 mV
    children:
    - id: wait-sub
      type: wait
      duration: 100 ms
  - id: rigol-node
    type: sequence
    device_module: rigol
    channel: 1
    output_policy: off
    configuration:
      waveform: SIN
      frequency: 1 kHz
    children: []
finally: []
"""
    recipe = parse_recipe_text(recipe_yaml)
    tree = normalize_recipe_tree(recipe, built_in_device_registry().sweep_providers())
    model = MeasurementTreeModel(tree)

    # 1. Keithley output_policy: on surfaces child node keithley-node.output-on
    k_on = tree.require("keithley-node.output-on")
    assert k_on.label == "Keithley B · OUTPUT ON"
    assert k_on.kind is SemanticNodeKind.ACTION
    assert tree.parent_by_id["keithley-node.output-on"] == "keithley-node"
    # Column 1 shows ON
    k_on_idx = model.index_for_semantic_id("keithley-node.output-on")
    assert k_on_idx.isValid()
    col1 = model.index(k_on_idx.row(), 1, k_on_idx.parent())
    assert model.data(col1, Qt.ItemDataRole.DisplayRole) == "ON"

    # 2. Rigol output_policy: off surfaces child node rigol-node.output-off
    r_off = tree.require("rigol-node.output-off")
    assert r_off.label == "Rigol CH1 · OUTPUT OFF"
    assert r_off.kind is SemanticNodeKind.ACTION
    assert tree.parent_by_id["rigol-node.output-off"] == "rigol-node"
    # Column 1 shows OFF
    r_off_idx = model.index_for_semantic_id("rigol-node.output-off")
    assert r_off_idx.isValid()
    r_col1 = model.index(r_off_idx.row(), 1, r_off_idx.parent())
    assert model.data(r_col1, Qt.ItemDataRole.DisplayRole) == "OFF"


def test_expandable_finally_branch_with_guaranteed_and_authored_actions() -> None:
    """Finally — safe shutdown is an expandable branch exposing device-level shutdown actions."""
    # Case A: Empty finally: [] populates guaranteed safe shutdown actions
    recipe_empty_finally = parse_recipe_text("""
schema_version: 1
name: Empty Finally Recipe
root:
  id: sequence-main
  type: sequence
  children:
  - id: wait-1
    type: wait
    duration: 50 ms
finally: []
""")
    tree_a = normalize_recipe_tree(recipe_empty_finally, built_in_device_registry().sweep_providers())
    model_a = MeasurementTreeModel(tree_a)
    finally_a = tree_a.require("__finally__")
    assert len(finally_a.children) == 4
    labels_a = [c.label for c in finally_a.children]
    assert "Keithley A + B · OUTPUT OFF" in labels_a
    assert "Rigol CH1 + CH2 · OUTPUT OFF" in labels_a
    assert "Anritsu RF · OUTPUT OFF + abort" in labels_a
    assert "Flush measurement checkpoints" in labels_a

    # Verify column 1 displays OFF for output off rows
    k_off_idx = model_a.index_for_semantic_id("__finally__.keithley_outputs_off")
    assert model_a.data(model_a.index(k_off_idx.row(), 1, k_off_idx.parent()), Qt.ItemDataRole.DisplayRole) == "OFF"
    flush_idx = model_a.index_for_semantic_id("__finally__.storage_flush_checkpoint")
    assert model_a.data(model_a.index(flush_idx.row(), 1, flush_idx.parent()), Qt.ItemDataRole.DisplayRole) in {"Action", "—"}

    # Case B: Authored finally nodes populate under Finally
    recipe_authored_finally = parse_recipe_text("""
schema_version: 1
name: Authored Finally Recipe
root:
  id: sequence-main
  type: sequence
  children:
  - id: wait-1
    type: wait
    duration: 50 ms
finally:
- id: keithley-off-auth
  type: set_keithley_output
  channel: B
  enabled: false
- id: keithley-ramp-auth
  type: ramp_keithley_to_zero
  channel: B
""")
    tree_b = normalize_recipe_tree(recipe_authored_finally, built_in_device_registry().sweep_providers())
    finally_b = tree_b.require("__finally__")
    assert len(finally_b.children) == 2
    assert finally_b.children[0].semantic_id == "keithley-off-auth"
    assert finally_b.children[0].label == "Keithley B · OUTPUT OFF"
    assert finally_b.children[1].semantic_id == "keithley-ramp-auth"
    assert finally_b.children[1].label == "Ramp Keithley B to zero"


def test_untitled_sweep_yml_audit_and_dry_run_simulation(tmp_path: Path) -> None:
    """Audit recipes/untitled_sweep.yml for safety, tree presentation, and dry run execution."""
    recipe_path = Path("recipes/untitled_sweep.yml")
    assert recipe_path.exists()
    recipe = parse_recipe_text(recipe_path.read_text(encoding="utf-8"))

    # Verify semantic tree presentation
    tree = normalize_recipe_tree(recipe, built_in_device_registry().sweep_providers())
    model = MeasurementTreeModel(tree)

    # 1. Keithley B OUTPUT ON is explicitly surfaced in tree
    assert "keithley-81c50119.output-on" in tree.by_id
    k_on = tree.require("keithley-81c50119.output-on")
    assert k_on.label == "Keithley B · OUTPUT ON"
    k_idx = model.index_for_semantic_id("keithley-81c50119.output-on")
    assert model.data(model.index(k_idx.row(), 1, k_idx.parent()), Qt.ItemDataRole.DisplayRole) == "ON"

    # 2. Finally is expandable with all guaranteed shutdown rows
    finally_node = tree.require("__finally__")
    assert len(finally_node.children) == 4
    k_off = tree.require("__finally__.keithley_outputs_off")
    assert k_off.label == "Keithley A + B · OUTPUT OFF"
    k_off_idx = model.index_for_semantic_id("__finally__.keithley_outputs_off")
    assert model.data(model.index(k_off_idx.row(), 1, k_off_idx.parent()), Qt.ItemDataRole.DisplayRole) == "OFF"

    # 3. Compile against station settings and execute dry run
    raw_settings = SettingsRepository(".config/settings.yml").load().settings
    settings = simulated_station_settings(raw_settings)
    plan = RecipeCompiler(settings, outputs_forced_off=True).compile(recipe)
    assert len(plan.actions) > 0

    # Ensure compiler generated output-on action matching tree semantic_id
    output_on_actions = [a for a in plan.actions if a.kind == "set_keithley_output"]
    assert any(a.semantic_id == "keithley-81c50119.output-on" for a in output_on_actions)

    # Execute in DRY_RUN mode
    reg = built_in_device_registry()
    k_adapter = reg.get("keithley").create_adapter(settings, simulation=True)
    an_adapter = reg.get("anritsu").create_adapter(settings, simulation=True)
    rg_adapter = reg.get("rigol").create_adapter(settings, simulation=True)
    k_adapter.connect()
    an_adapter.connect()
    rg_adapter.connect()

    h5_target = tmp_path / "dry_run_audit.h5"
    writer = Hdf5RunWriter(
        h5_target,
        recipe_source=recipe.source_text,
        settings_source="",
        plan_hash=plan.sha256,
        device_idn={},
    )
    events: list[tuple[str, dict]] = []
    runner = RecipeRunner(
        writer=writer,
        keithley=k_adapter,
        anritsu=an_adapter,
        rigol=rg_adapter,
        execution_mode=ExecutionMode.DRY_RUN,
        on_event=lambda name, data: events.append((name, data)),
    )
    result = runner.run(plan)
    assert result.state.value == "safe"
    assert result.stored_points == 10
    # Confirm physical outputs were never enabled
    assert not k_adapter._output_states["B"]
    assert not rg_adapter._output_states[1]
    # Confirm dry run suppressed events were emitted
    assert any(name == "dry_run_output_action_suppressed" for name, _ in events)




