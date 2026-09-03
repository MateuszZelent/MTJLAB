"""Comprehensive audit tests for the wait function in the sweep tree."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import threading
import time
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.devices.registry import built_in_device_registry
from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.quantities import QuantityError
from app.engine.compiler import RecipeCompiler
from app.engine.policy import ExecutionPolicy
from app.engine.runner import RecipeRunner
from app.recipes import parse_recipe_text
from app.recipes.models import RecipeNode
from app.recipes.semantic_tree import (
    SemanticNodeKind,
    normalize_recipe_tree,
)
from app.ui.measurement_tree.model import MeasurementTreeModel, MeasurementTreeRole
from app.ui.recipes.common_dialogs import ActionNodeEditorDialog
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
    app = QApplication.instance() or QApplication([])
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
        settle_time: 40 ms
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
    compiler = RecipeCompiler(simulation_settings(approved=True))
    plan = compiler.compile(parse_recipe_text(source))

    events = []
    def on_event(name, data):
        events.append((name, data))

    registry = built_in_device_registry()
    keithley_mod = registry.get("keithley")
    anritsu_mod = registry.get("anritsu")
    rigol_mod = registry.get("rigol")

    runner = RecipeRunner(
        settings=simulation_settings(approved=True),
        writer=MemoryWriter(),
        keithley=keithley_mod.create_adapter(None, simulation=True),
        anritsu=anritsu_mod.create_adapter(None, simulation=True),
        rigol=rigol_mod.create_adapter(None, simulation=True),
        event_sink=on_event,
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
    compiler = RecipeCompiler(simulation_settings(approved=True))
    plan = compiler.compile(parse_recipe_text(source))

    registry = built_in_device_registry()
    keithley_mod = registry.get("keithley")
    anritsu_mod = registry.get("anritsu")
    rigol_mod = registry.get("rigol")

    runner = RecipeRunner(
        settings=simulation_settings(approved=True),
        writer=MemoryWriter(),
        keithley=keithley_mod.create_adapter(None, simulation=True),
        anritsu=anritsu_mod.create_adapter(None, simulation=True),
        rigol=rigol_mod.create_adapter(None, simulation=True),
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
    assert runner.state.value in {"safe", "aborted", "stopping"}


def test_action_node_editor_dialog_validates_wait_duration():
    """Verify that the ActionNodeEditorDialog modal edits and validates wait duration."""
    app = QApplication.instance() or QApplication([])
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
