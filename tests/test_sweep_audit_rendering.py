"""Shown Fluent shell and live instrument projection for the sweep audit."""

import os
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QTabWidget
from PySide6.QtTest import QTest

from app.devices.registry import built_in_device_registry
from app.domain.quantities import DIMENSION_CURRENT, parse_quantity
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.recipes.semantic_tree import normalize_recipe_tree
from app.ui.shell import MainWindow
from tests.shell_test_isolation import (  # noqa: F401
    isolated_shell_persistence,
    shell_qt_application,
)
from tests.test_sweep_audit_contracts import AXES, SETUP, audit_settings

ARTIFACTS = Path("docs/audits/2026-10-05-sweep-fixes")


def settle():
    QTest.qWait(300)
    for _ in range(8):
        QApplication.processEvents()
    # Initial layout can delay the first animation tick. Wait for the native
    # transition to finish instead of assuming its 600 ms clock started early.
    for _ in range(100):
        indicators = [window.navigationInterface.panel.indicator
                      for window in QApplication.topLevelWidgets()
                      if getattr(window, "navigationInterface", None) is not None]
        if all(not indicator.isVisible() for indicator in indicators):
            return
        QTest.qWait(50)
    pytest.fail("Native Fluent navigation did not settle within five seconds")


@pytest.mark.parametrize("width,height,theme", [(1360, 880, "light"), (1360, 880, "dark"), (1000, 760, "light")])
def test_visible_sweep_and_execution_pages(width, height, theme, tmp_path):
    window = MainWindow(".config/settings.yml", simulation=True)
    window.resize(width, height)
    window.show()
    window._set_theme_mode(theme, persist=False)
    settings = audit_settings(tmp_path)
    plan = RecipeCompiler(settings).compile(parse_recipe_text(
        "schema_version: 1\nname: MOKE 0 x Keithley B x Keithley A\nroot:\n  id: root\n  type: sequence\n  children:\n" + SETUP + AXES))
    recipe = parse_recipe_text(plan.recipe_source)
    tree = normalize_recipe_tree(recipe, built_in_device_registry().sweep_providers())
    window.recipe_page._restore_tree_history_source(plan.recipe_source, "Audit simulation")
    # This generated test document is intentionally disposable at teardown.
    window.recipe_page._close_discard_confirmed = True
    window._navigate_to("sweeps")
    settle()
    indicator = window.navigationInterface.panel.indicator
    animation = indicator.scaleSlideAni.currentAni
    assert not indicator.isVisible(), (animation.state(), animation.currentTime(), animation.duration())
    page = window.recipe_page
    assert page.isVisibleTo(window)
    assert page.workspace_splitter.width() > 400 and page.workspace_splitter.height() > 180
    assert not page.findChildren(QTabWidget)
    scroll_bar = window.navigation_routes["sweeps"].scroll_area.verticalScrollBar()
    scroll_bar.setValue(scroll_bar.maximum())
    settle()
    assert page.measurement_tree.visibleRegion().boundingRect().height() > 180
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(ARTIFACTS / f"sweeps-{width}-{theme}.png"))
    window._navigate_to("execution")
    monitor = window.run_monitor
    monitor.run_started(len(plan.actions), 60.0, plan_actions=plan.actions,
                        recipe_source=plan.recipe_source, semantic_tree=tree)
    settle()
    assert monitor.isVisibleTo(window)
    assert monitor.measurement_tree.isVisibleTo(window)
    assert monitor.measurement_tree.width() > 250 and monitor.measurement_tree.height() > 150
    assert monitor.tree_model.tree is tree
    assert window.grab().save(str(ARTIFACTS / f"execution-{width}-{theme}.png"))


def test_keithley_current_readback_is_visible_without_device_commands(tmp_path):
    window = MainWindow(".config/settings.yml", simulation=True)
    window.resize(1360, 880)
    window.show()
    window._navigate_to("keithley")
    page = window.keithley_page
    page.channel.setCurrentText("A")
    page.mode.setCurrentText("current")
    page._controller.call = Mock()
    window._set_device_pages_execution_read_only(True)
    window._run_event("action_finished", {
        "kind": "update_keithley_level",
        "state_snapshot": {
            "output_status": {"keithley.A": "on", "keithley.B": "on"},
            "device_states": {"keithley": {
                "channel_A": {"actual": {"mode": "current", "source_level_si": 0.0014, "compliance_si": 0.02}},
                "channel_B": {"actual": {"mode": "current", "source_level_si": -0.08, "compliance_si": 0.02}},
            }},
        },
    })
    settle()
    assert page.isVisibleTo(window) and page.level.isVisibleTo(window)
    assert page.level.text() == "1.4 mA" and not page.level.isEnabled()
    assert page.channel_cards["A"]["output"].text() == "OUTPUT ON"
    assert window.safety_strip.outputs.text().startswith("2 ")
    assert "off" not in window.safety_strip.outputs.text().lower()
    page._controller.call.assert_not_called()
    assert window.grab().save(str(ARTIFACTS / "keithley-live-projection.png"))
    window._set_device_pages_execution_read_only(False)
    window._set_device_state("keithley", "unknown")
    assert "unknown" in window.safety_strip.outputs.text().lower()
    assert not any(endpoint.startswith("keithley.") for endpoint in window._execution_output_status)


def test_keithley_nplc_follows_confirmed_run_configuration(tmp_path):
    window = MainWindow(".config/settings.yml", simulation=True)
    window.resize(1360, 880)
    window.show()
    window._navigate_to("keithley")
    page = window.keithley_page
    page.channel.setCurrentText("B")
    page.nplc.setText("1")
    window._set_device_pages_execution_read_only(True)
    page.apply_execution_event("action_finished", {"kind": "configure_keithley"}, {
        "channel_B": {"actual": {"mode": "current", "source_level_si": 0.001,
            "compliance_si": 0.02, "nplc": 8.0, "source_range_si": 0.1}},
    }, {"keithley.B": "off"})
    settle()
    try:
        assert float(page.nplc.text()) == 8.0
    finally:
        window._set_device_pages_execution_read_only(False)


def test_visible_baseline_authoring_adds_reviewable_sibling_and_locks_during_run(tmp_path):
    window = MainWindow(".config/settings.yml", simulation=True)
    window.resize(1360, 880)
    window.show()
    window._navigate_to("sweeps")
    page = window.recipe_page
    settings = audit_settings(tmp_path)
    page._settings = settings
    source = """schema_version: 1
name: baseline-authoring
root:
  id: root
  type: sequence
  children:
    - id: selected-b
      type: sequence
      device_module: keithley
      operation: configure_selected_parameters
      channel: B
      source_mode: current
      output_policy: off
      configuration: {channel: B, source_mode: current, source_level: '1 mA', compliance: '20 mV', source_range: '100 mA', nplc: '8', sense_mode: 2wire}
      parameter_actions: [{parameter_id: source.level, mode: set, value: '1.2 mA'}]
      children: [{id: checkpoint, type: checkpoint}]
"""
    page._restore_tree_history_source(source, "Baseline authoring regression")
    page._close_discard_confirmed = True
    page._select_source_node("selected-b")
    settle()
    assert page.add_baseline_button.isVisibleTo(window) and page.add_baseline_button.isEnabled()
    assert page.add_baseline_button.width() > 50 and page.add_baseline_button.height() > 20
    page.add_baseline_button.click()
    settle()
    recipe = parse_recipe_text(page._tree_source)
    assert [node.type for node in recipe.root.children] == ["configure_keithley", "sequence"]
    baseline = recipe.root.children[0]
    assert baseline.data["nplc"] == 8 and baseline.data["sense_mode"] == "2wire"
    assert "review before running" in baseline.data["label"]
    assert RecipeCompiler(settings).compile(recipe).total_points == 1
    scroll_bar = window.navigation_routes["sweeps"].scroll_area.verticalScrollBar()
    scroll_bar.setValue(scroll_bar.maximum())
    settle()
    assert page.measurement_tree.visibleRegion().boundingRect().height() > 180
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(ARTIFACTS / "baseline-authoring-tree-1360-light.png"))
    page._select_source_node("selected-b")
    page.set_execution_controlled(True)
    settle()
    assert not page.add_baseline_button.isEnabled()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(ARTIFACTS / "baseline-authoring-1360-light.png"))
    page.set_execution_controlled(False)
    page._close_discard_confirmed = True


@pytest.mark.parametrize("width,height,theme", [(1360, 880, "light"), (1360, 880, "dark"), (1000, 760, "light")])
def test_hidden_keithley_route_restores_all_confirmed_fields_and_unknown_state(width, height, theme, tmp_path):
    window = MainWindow(".config/settings.yml", simulation=True)
    window.resize(width, height)
    window.show()
    window._set_theme_mode(theme, persist=False)
    window._navigate_to("anritsu")
    page = window.keithley_page
    page._controller.call = Mock()
    window._set_device_pages_execution_read_only(True)
    actual = {"mode": "current", "source_level_si": -.08, "compliance_si": .02,
        "nplc": 8, "settle_time_s": 3, "sense_mode": "4wire", "source_autorange": False,
        "source_range_si": .1, "measure_voltage_autorange": False, "measure_voltage_range_si": .1,
        "measure_current_autorange": True, "measure_current_range_si": .1}
    window._run_event("action_finished", {"kind": "wait", "state_snapshot": {
        "output_status": {"keithley.A": "off", "keithley.B": "unknown"},
        "device_states": {"keithley": {"channel_B": {"actual": actual}}}}})
    window._navigate_to("keithley")
    page.channel.setCurrentText("B")
    settle()
    assert parse_quantity(page.level.text(), DIMENSION_CURRENT).si_value == pytest.approx(-.08)
    assert float(page.nplc.text()) == 8
    assert page.settle.text() == "3 s" and page.source_range.text() == "100 mA"
    assert page.configuration_panel.confirmed_sense.text() == "4wire"
    assert not page.measure_voltage_autorange.isChecked()
    assert page.measure_voltage_range.text() == "100 mV"
    assert page.measure_current_autorange.isChecked() and page.measure_current_range.text() == "AUTO"
    assert page.channel.isEnabled() and not page.level.isEnabled()
    assert page.channel_cards["B"]["output"].text() == "OUTPUT UNKNOWN"
    assert "unknown" in window.safety_strip.outputs.text().lower()
    assert "off" not in window.safety_strip.outputs.text().lower()
    assert page.channel_cards["B"]["output"].visibleRegion().boundingRect().width() > 80
    page._controller.call.assert_not_called()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(ARTIFACTS / f"keithley-full-unknown-{width}-{theme}.png"))
    window._set_device_pages_execution_read_only(False)
