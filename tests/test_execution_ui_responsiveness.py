from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import h5py
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.engine import ExecutionMode, RecipeCompiler
from app.domain.execution_state import SemanticOperationState
from app.devices.registry import built_in_device_registry
from app.recipes import load_recipe, parse_recipe_text
from app.recipes.semantic_tree import normalize_recipe_tree
from app.ui.execution import RunMonitorPage
from app.ui.shell import MainWindow
from tests.helpers import simulation_settings


class GuiGapProbe(QObject):
    def __init__(self, interval_ms: int = 20) -> None:
        super().__init__()
        self._last = time.monotonic()
        self.maximum_gap_s = 0.0
        self.ticks = 0
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self._tick)

    def start(self) -> None:
        """Begin a fresh measured interval after unmeasured setup work."""

        self.maximum_gap_s = 0.0
        self.ticks = 0
        self._last = time.monotonic()
        self.timer.start()

    def _tick(self) -> None:
        now = time.monotonic()
        self.maximum_gap_s = max(self.maximum_gap_s, now - self._last)
        self._last = now
        self.ticks += 1


def test_gui_gap_probe_excludes_time_before_monitoring_starts() -> None:
    """A disabled timer must not report Fluent startup as a run-time stall."""

    app = QApplication.instance() or QApplication([])
    probe = GuiGapProbe(interval_ms=5)
    time.sleep(0.08)
    probe.start()
    deadline = time.monotonic() + 0.06
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    probe.timer.stop()

    assert probe.ticks > 0
    assert probe.maximum_gap_s < 0.04


SINGLE_AXIS_SOURCE = """\
schema_version: 1
name: one-axis
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
      stop: 1 mA
      points: 2
      children:
        - id: acquire
          type: acquire_spectrum
          trace: TRAC1
        - id: wait
          type: wait
          duration: 2 s
finally:
  - id: shutdown
    type: set_keithley_output
    channel: B
    enabled: false
"""

SAME_DEVICE_TWO_AXIS_SOURCE = """\
schema_version: 1
name: same-device-nested
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

SIMULATED_10_BY_100_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "recipes"
    / "keithley_b_rigol_frequency_anritsu_reference_10x100.yml"
)


def semantic_labels(page: object) -> list[str]:
    model = getattr(page, "tree_model", None)
    if model is not None and hasattr(model, "tree"):
        return [node.label for node in model.tree.by_id.values()]
    tree = page.tree

    def walk(item: object) -> list[str]:
        return [item.text(0), *(label for i in range(item.childCount()) for label in walk(item.child(i)))]

    return [label for i in range(tree.topLevelItemCount()) for label in walk(tree.topLevelItem(i))]


def build_recipe_page(source: str) -> object:
    from app.ui.recipes.page import RecipePage

    page = RecipePage(simulation_settings())
    page._apply_builder_source(source, "Loaded semantic responsiveness fixture")
    return page


def compile_source(source: str):
    return RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))


def build_simulated_window(
    source: Path, *, seed: int, spectrum_points: int | None = None
) -> MainWindow:
    window = MainWindow(".config/settings.yml", simulation=True)
    window._simulation_seed = seed
    recipe = load_recipe(source)
    if spectrum_points is not None:
        configured = "      points: 1001\n"
        replacement = f"      points: {spectrum_points}\n"
        if recipe.source_text.count(configured) != 1:
            raise AssertionError("Qualification recipe spectrum point field changed.")
        recipe = parse_recipe_text(
            recipe.source_text.replace(configured, replacement),
            origin=str(source),
        )
    plan = RecipeCompiler(window._settings).compile(recipe)
    window._characterization_plan = plan
    return window


def start_and_wait_for_run(window: MainWindow, *, expected_points: int) -> Path:
    plan = window._characterization_plan
    monitor: RunMonitorPage = window.run_monitor
    semantic_tree = window.recipe_page.semantic_tree_snapshot(plan.recipe_source, plan)
    monitor.run_started(
        len(plan.actions),
        1.0,
        plan_actions=plan.actions,
        recipe_source=plan.recipe_source,
        semantic_tree=semantic_tree,
    )
    window._run_controller.start(
        window._settings,
        window._repository.path,
        plan,
        simulation=True,
        execution_mode=ExecutionMode.DRY_RUN.value,
        output_dir_override=str(Path(".tmp-characterization")),
        file_stem_override="responsiveness",
    )
    deadline = time.monotonic() + 900.0
    while window._run_controller.running and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    assert not window._run_controller.running, "simulated run did not finish"
    for _ in range(4):
        QApplication.processEvents()
    assert monitor._stored_points == expected_points
    result_path = Path(monitor.completion_path.text())
    assert result_path.is_file(), "completed run path was not published"
    return result_path


def test_1000_semantic_events_coalesce_to_bounded_model_flushes() -> None:
    """The visible model receives the latest state, not 1000 widget passes."""

    app = QApplication.instance() or QApplication([])
    page = RunMonitorPage()
    try:
        for index in range(1000):
            page.queue_semantic_state(
                SemanticOperationState(
                    "axis-current.set-roi-value",
                    "applied",
                    index / 1000,
                    index / 1000,
                    index / 1000,
                    "simulated_ack",
                    index,
                    1000,
                    None,
                )
            )
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)
        assert page.ui_metrics.semantic_events_received == 1000
        assert page.ui_metrics.model_flushes <= 8
        assert page.ui_metrics.tree_rebuilds == 0
        assert page.ui_metrics.semantic_events_coalesced >= 999
        assert page.ui_metrics.max_tree_update_duration_s < 0.250
    finally:
        page.close()
        app.processEvents()


def test_wait_semantic_projection_preserves_duration_while_running() -> None:
    """A queued WAIT event keeps its operation label and 2 s value visible."""

    app = QApplication.instance() or QApplication([])
    recipe = parse_recipe_text(SINGLE_AXIS_SOURCE)
    tree = normalize_recipe_tree(recipe, built_in_device_registry().sweep_providers())
    page = RunMonitorPage()
    try:
        page.run_started(
            actions=1,
            estimated_duration_s=2.0,
            semantic_tree=tree,
            execution_mode=ExecutionMode.DRY_RUN.value,
        )
        event = {
            "semantic_id": "wait",
            "kind": "wait",
            "device": "keithley",
            "duration_s": 2.0,
            "action_index": 0,
            "total_actions": 1,
        }
        page.queue_semantic_event("semantic_operation_started", event)
        page.flush_semantic_states()

        assert page.current_operation_parameter.text() == "Wait · 2 s"
        assert page.current_operation_value.text() == "2 s"
        assert page.current_operation_si.text() == "SI 2 s"
        assert page.current_operation_phase.text() == "WAITING"
        assert page.current_operation_state.text() == "WAITING"
        assert "2 s remaining" in page.current_operation_detail.text()

        page.update_heartbeat(
            {
                "node_id": "wait",
                "kind": "wait",
                "attempt": 1,
                "elapsed_s": 0.75,
                "remaining_s": 1.75,
            }
        )
        assert "1.25 s remaining of 2 s" in page.current_operation_detail.text()

        page.queue_semantic_event(
            "semantic_operation_applied",
            {
                **event,
                "applied_si": 2.0,
                "readback_si": 2.0,
                "verification": "simulated_ack",
            },
        )
        page.flush_semantic_states()
        assert page.current_operation_value.text() == "2 s"
        assert page.current_operation_state.text() == "CONFIRMED"
        assert "WAIT completed · 2 s elapsed" in page.current_operation_detail.text()
    finally:
        page.close()
        app.processEvents()


def test_semantic_execution_renders_one_tree_without_legacy_overlap() -> None:
    """The production projection has one visible model/view child only."""

    app = QApplication.instance() or QApplication([])
    page = RunMonitorPage()
    try:
        page.resize(1200, 760)
        page.show()
        recipe = parse_recipe_text(SINGLE_AXIS_SOURCE)
        plan = RecipeCompiler(simulation_settings()).compile(recipe)
        tree = normalize_recipe_tree(
            recipe,
            built_in_device_registry().sweep_providers(),
        )
        page.run_started(
            len(plan.actions),
            1.0,
            plan_actions=plan.actions,
            recipe_source=plan.recipe_source,
            semantic_tree=tree,
            execution_mode=ExecutionMode.DRY_RUN.value,
        )
        app.processEvents()

        assert page.activity_splitter.widget(0) is page.measurement_tree
        assert page.measurement_tree.isVisibleTo(page)
        assert not hasattr(page, "steps")
        assert page.measurement_tree.geometry().height() >= 220
    finally:
        page.close()
        app.processEvents()


def test_execution_workspace_is_bounded_while_tree_keeps_internal_scroll() -> None:
    """Desktop layout keeps the active tree in view instead of reserving a
    construction-time splitter height larger than the page viewport."""

    app = QApplication.instance() or QApplication([])
    page = RunMonitorPage()
    try:
        page.resize(1200, 760)
        page.show()
        app.processEvents()

        assert page.workspace_card.maximumHeight() == 440
        assert page.workspace_card.height() <= 440
        assert page.measurement_tree.minimumHeight() >= 260
        # QFluent's SmoothScrollDelegate owns the visible overlay scrollbar;
        # the native QAbstractScrollArea policy intentionally remains hidden.
        assert getattr(page.measurement_tree, "scrollDelagate", None) is not None
    finally:
        page.close()
        app.processEvents()


def test_large_spectrum_preview_is_decimated_before_plotting() -> None:
    app = QApplication.instance() or QApplication([])
    page = RunMonitorPage()
    try:
        page.resize(1200, 760)
        page.show()
        app.processEvents()
        values = tuple(float(index) for index in range(10_001))
        page.update_spectrum_preview(
            {
                "point_index": 1,
                "frequency_hz": values,
                "power_dbm": tuple(-80.0 for _ in values),
                "source_points": 10_001,
            }
        )

        rendered = page.spectrum_preview.trace_point_count("Stored spectrum")
        assert 512 <= rendered <= page.spectrum_preview.width() * 2
        assert "10001 source values" in page.spectrum_preview.plot.plotItem.titleLabel.text
    finally:
        page.close()
        app.processEvents()


def test_execution_page_owns_only_the_semantic_measurement_tree() -> None:
    """Execution must never install an item-based fallback measurement tree."""

    app = QApplication.instance() or QApplication([])
    page = RunMonitorPage()
    try:
        page.resize(1200, 760)
        page.show()
        app.processEvents()

        assert page.activity_splitter.widget(0) is page.measurement_tree
        assert page.measurement_tree.isVisibleTo(page)
        assert not hasattr(page, "steps")
    finally:
        page.close()
        app.processEvents()


@pytest.mark.qualification
def test_execution_tree_keeps_qt_event_loop_live_for_1000_points() -> None:
    app = QApplication.instance() or QApplication([])
    window = build_simulated_window(
        SIMULATED_10_BY_100_SOURCE,
        seed=17,
        spectrum_points=10_001,
    )
    probe = GuiGapProbe()
    try:
        window.show()
        window._navigate_to("execution")
        # Flush the initial Fluent/pyqtgraph layout before measuring the run;
        # deferred first-paint work is startup cost, not sweep-time latency.
        for _ in range(8):
            app.processEvents()
            time.sleep(0.01)
        probe.start()
        result_path = start_and_wait_for_run(window, expected_points=1000)
        assert probe.ticks > 20
        assert probe.maximum_gap_s < 0.350
        metrics = window.run_monitor.ui_metrics
        assert metrics.tree_rebuilds == 0
        assert metrics.semantic_events_received > 1000
        assert metrics.model_flushes < metrics.semantic_events_received
        assert metrics.max_tree_update_duration_s < 0.250
        assert metrics.preview_flushes > 0
        assert metrics.max_preview_update_duration_s < 0.250
        with h5py.File(result_path, "r") as run:
            assert len(run["spectra"]) == 1000
            assert len(run["reference/power_dbm"]) == 10_001
            assert all(
                len(run[f"spectra/{index}/power_dbm"]) == 10_001
                for index in range(1000)
            )
    finally:
        window.close()
        app.processEvents()
