"""Capture the unified Sweeps/Execution release-gate views offscreen."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.engine import RecipeCompiler
from app.recipes import load_recipe, parse_recipe_text
from app.recipes.semantic_tree import SemanticNodeKind
from app.ui.shell import MainWindow


SINGLE_AXIS_SOURCE = """\
schema_version: 1
name: Unified tree visual qualification
root:
  id: root
  type: sequence
  children:
    - id: configuration
      type: configure_keithley
      channel: B
      mode: current
      level: 0 A
      compliance: 67 mV
    - id: current-axis
      type: sweep
      target: keithley.B.current
      start: 0 A
      stop: 10 mA
      points: 10
      children:
        - id: wait-point
          type: wait
          duration: 2 s
        - id: acquire
          type: acquire_spectrum
          trace: TRAC1
finally:
  - id: output-off
    type: set_keithley_output
    channel: B
    enabled: false
"""

SAME_DEVICE_TWO_AXIS_SOURCE = """\
schema_version: 1
name: Same-device nested axes visual qualification
root:
  id: root
  type: sequence
  children:
    - id: configuration
      type: configure_keithley
      channel: B
      mode: current
      level: 0 A
      compliance: 10 mV
    - id: outer
      type: sweep
      target: keithley.B.current
      start: 0 A
      stop: 1 mA
      points: 2
      children:
        - id: outer-update
          type: update_keithley_level
          channel: B
          mode: current
          level: "${keithley.B.current}"
        - id: inner
          type: sweep
          target: keithley.B.compliance_voltage
          start: 10 mV
          stop: 20 mV
          points: 3
          children:
            - id: inner-update
              type: update_keithley_compliance
              channel: B
              mode: current
              level: "${keithley.B.current}"
              compliance: "${keithley.B.compliance_voltage}"
finally: []
"""


def _reference_sources() -> tuple[tuple[str, str], ...]:
    two_device = load_recipe(
        Path("recipes/keithley_b_rigol_frequency_anritsu_reference_10x100.yml")
    ).source_text
    return (
        ("one-axis", SINGLE_AXIS_SOURCE),
        ("two-device", two_device),
        ("same-device", SAME_DEVICE_TWO_AXIS_SOURCE),
    )


def _representative_event(snapshot: object, plan: object) -> dict[str, object]:
    set_rows = [
        node
        for node in snapshot.by_id.values()
        if node.kind is SemanticNodeKind.SET_ROI_VALUE
    ]
    if not set_rows:
        raise RuntimeError("Reference recipe has no Set ROI value operation.")
    semantic_id = set_rows[-1].semantic_id
    actions = [action for action in plan.actions if action.semantic_id == semantic_id]
    if not actions:
        raise RuntimeError(f"No compiled action for {semantic_id}.")
    action = actions[min(len(actions) - 1, max(0, len(actions) // 3))]
    context = action.axis_context
    if context is None:
        raise RuntimeError(f"No axis context for {semantic_id}.")
    kind = action.kind
    device = (
        "keithley"
        if "keithley" in kind
        else "rigol"
        if "rigol" in kind
        else "anritsu"
    )
    event: dict[str, object] = {
        "semantic_id": semantic_id,
        "node_id": action.node_id,
        "kind": kind,
        "device": device,
        "requested_si": float(action.payload.get("requested_si", context.value_si)),
        "action_index": plan.actions.index(action),
        "total_actions": len(plan.actions),
        "axis_context": {
            "axis_id": context.axis_id,
            "point_index": context.point_index,
            "point_count": context.point_count,
            "stage_index": context.stage_index,
            "value_si": context.value_si,
            "active_setpoints_si": dict(context.active_setpoints_si),
            "loop_path": list(context.loop_path),
        },
    }
    channel = action.payload.get("channel")
    if channel is not None:
        event["channel"] = channel
    return event


def _settle(application: QApplication, count: int = 8) -> None:
    for _ in range(count):
        application.processEvents()


def main() -> int:
    application = QApplication.instance() or QApplication([])
    output = Path("docs/qualification/artifacts/unified-sweep-tree")
    output.mkdir(parents=True, exist_ok=True)
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.show()
        selected_scenario = os.environ.get("SWEEP_CAPTURE_SCENARIO", "").strip()
        for scenario, source in _reference_sources():
            if selected_scenario and scenario != selected_scenario:
                continue
            window.recipe_page._apply_builder_source(
                source, f"Loaded {scenario} visual qualification recipe"
            )
            snapshot = window.recipe_page.semantic_tree_snapshot()
            plan = RecipeCompiler(window._settings).compile(
                parse_recipe_text(source, origin=f"{scenario} visual qualification")
            )
            active_event = _representative_event(snapshot, plan)
            for theme in ("light", "dark"):
                window._set_theme_mode(theme, persist=False)
                for width, height, size_name in (
                    (1440, 900, "desktop"),
                    (1024, 720, "narrow"),
                ):
                    window.resize(width, height)
                    window._navigate_to("sweeps")
                    _settle(application)
                    if not window.recipe_page.measurement_tree.isVisibleTo(window):
                        raise RuntimeError("Sweeps semantic tree is not rendered.")
                    if not window.grab().save(
                        str(output / f"{scenario}-sweeps-{size_name}-{theme}.png")
                    ):
                        raise RuntimeError("Could not save Sweeps qualification image.")

                    window._navigate_to("execution")
                    _settle(application)
                    monitor = window.run_monitor
                    monitor.run_started(
                        len(plan.actions),
                        20.0,
                        plan_actions=plan.actions,
                        recipe_source=source,
                        semantic_tree=snapshot,
                    )
                    _settle(application)
                    monitor.append_event(
                        "semantic_operation_started", active_event
                    )
                    monitor._flush_semantic_states()
                    monitor.measurement_tree.follow_semantic_id(
                        str(active_event["semantic_id"]), force=True
                    )
                    _settle(application)
                    if not monitor.measurement_tree.isVisibleTo(window):
                        raise RuntimeError("Execution semantic tree is not rendered.")
                    if not window.grab().save(
                        str(output / f"{scenario}-execution-{size_name}-{theme}.png")
                    ):
                        raise RuntimeError("Could not save Execution qualification image.")
        return 0
    finally:
        window.recipe_page._close_discard_confirmed = True
        window._set_theme_mode("system", persist=False)
        window.close()
        _settle(application, 2)


if __name__ == "__main__":
    raise SystemExit(main())
