"""Shown Fluent shell and live instrument projection for the sweep audit."""

import os
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QTabWidget

from app.devices.registry import built_in_device_registry
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.recipes.semantic_tree import normalize_recipe_tree
from app.ui.shell import MainWindow
from tests.shell_test_isolation import (  # noqa: F401
    isolated_shell_persistence,
    shell_qt_application,
)
from tests.test_sweep_audit_contracts import AXES, SETUP, audit_settings

ARTIFACTS = Path("docs/audits/2026-10-04-sweeps")


def settle():
    for _ in range(8):
        QApplication.processEvents()


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
    page = window.recipe_page
    assert page.isVisibleTo(window)
    assert page.workspace_splitter.width() > 400 and page.workspace_splitter.height() > 180
    assert not page.findChildren(QTabWidget)
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
    page._controller.call.assert_not_called()
    assert window.grab().save(str(ARTIFACTS / "keithley-live-projection.png"))
    window._set_device_pages_execution_read_only(False)


@pytest.mark.xfail(strict=True, reason="SW-17: full confirmed settings are not projected to the device form")
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
