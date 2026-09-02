from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.engine import ExecutionMode, RecipeCompiler
from app.recipes import load_recipe, parse_recipe_text
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

    def _tick(self) -> None:
        now = time.monotonic()
        self.maximum_gap_s = max(self.maximum_gap_s, now - self._last)
        self._last = now
        self.ticks += 1


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
    tree = page.tree

    def walk(item: object) -> list[str]:
        return [item.text(0), *(label for i in range(item.childCount()) for label in walk(item.child(i)))]

    return [label for i in range(tree.topLevelItemCount()) for label in walk(tree.topLevelItem(i))]


def build_recipe_page(source: str) -> object:
    from app.ui.recipes.page import RecipePage

    page = RecipePage(simulation_settings())
    page.editor.setPlainText(source)
    return page


def compile_source(source: str):
    return RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))


def build_simulated_window(source: Path, *, seed: int) -> MainWindow:
    window = MainWindow(".config/settings.yml", simulation=True)
    window._simulation_seed = seed
    recipe = load_recipe(source)
    plan = RecipeCompiler(window._settings).compile(recipe)
    window._characterization_plan = plan
    return window


def start_and_wait_for_run(window: MainWindow, *, expected_points: int) -> None:
    plan = window._characterization_plan
    monitor: RunMonitorPage = window.run_monitor
    monitor.run_started(
        len(plan.actions), 1.0, plan_actions=plan.actions, recipe_source=plan.recipe_source
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
    deadline = time.monotonic() + 180.0
    while window._run_controller.running and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    assert not window._run_controller.running, "simulated run did not finish"
    assert monitor._stored_points == expected_points


@pytest.mark.qualification
def test_execution_tree_keeps_qt_event_loop_live_for_1000_points(qtbot) -> None:
    window = build_simulated_window(SIMULATED_10_BY_100_SOURCE, seed=17)
    probe = GuiGapProbe()
    try:
        window.show()
        probe.timer.start()
        start_and_wait_for_run(window, expected_points=1000)
        assert probe.ticks > 20
        assert probe.maximum_gap_s < 0.250
    finally:
        window.close()
