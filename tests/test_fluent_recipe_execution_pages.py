from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QStackedWidget, QTabWidget, QToolBar
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CommandBar,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SegmentedWidget,
    TreeWidget,
)

from app.ui.shell import MainWindow
from app.ui.execution import RunMonitorPage
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.ui.recipes import SweepGeneratorDialog
from tests.helpers import simulation_settings


class FluentRecipeAndExecutionPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_sweep_builder_exposes_fluent_workspace_surfaces_at_desktop_size(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1280, 720)
            window.show()
            window._navigate_to("sweeps")
            self.application.processEvents()

            page = window.recipe_page
            self.assertIsInstance(page.hero_card, CardWidget)
            self.assertIsInstance(page.workflow_tabs, SegmentedWidget)
            self.assertIsInstance(page.builder_stack, QStackedWidget)
            self.assertIsInstance(page.recipe_command_bar, CommandBar)
            self.assertIsInstance(page.document_card, CardWidget)
            self.assertIsInstance(page.selection_card, CardWidget)
            self.assertIsInstance(page.status_card, CardWidget)
            self.assertIsInstance(page.path, LineEdit)
            self.assertIsInstance(page.output_directory, LineEdit)
            self.assertIsInstance(page.output_file_stem, LineEdit)
            self.assertIsInstance(page.execution_mode, ComboBox)
            self.assertIsInstance(page.summary, BodyLabel)
            self.assertIsInstance(page.run_button, PrimaryPushButton)
            self.assertIsInstance(page.open_editor_button, PrimaryPushButton)
            self.assertFalse(page.findChildren(QTabWidget))
            self.assertFalse(page.findChildren(QToolBar))
            self.assertTrue(page.hero_card.isVisibleTo(window))
            self.assertGreater(page.hero_card.geometry().width(), 300)
            self.assertTrue(page.workflow_tabs.isVisibleTo(window))
            self.assertGreater(page.builder_container.geometry().height(), 180)
            self.assertGreater(page.workspace_splitter.geometry().width(), 500)
            for button in page._library_action_buttons:
                self.assertIsInstance(button, PushButton)
                # The offscreen Qt platform uses a square fallback font whose
                # metrics substantially overestimate normal Windows text.
                # Guard the actual compact layout contract and require the
                # complete action description to remain available on hover.
                self.assertGreaterEqual(button.width(), 180)
                self.assertTrue(button.toolTip().strip())
                self.assertGreaterEqual(button.height(), 34)
            self.assertFalse(
                page.selection_title.geometry().intersects(
                    page.selection_context.geometry()
                )
            )
            self.assertEqual(page.path.accessibleName(), "Recipe file path")
            self.assertEqual(page.output_directory.accessibleName(), "Sweep result directory")
            self.assertEqual(page.output_file_stem.accessibleName(), "Sweep result file name")
            self.assertIn(".h5", page.output_file_preview.text())
            self.assertEqual(page.execution_mode.currentData(), "measurement")
            page.execution_mode.setCurrentIndex(1)
            self.application.processEvents()
            self.assertEqual(page.execution_mode.currentData(), "dry_run")
            self.assertEqual(page.run_button.text(), "Run dry run")
            self.assertIn("RAW/processed", page.execution_mode_hint.text())
            page.output_file_stem.setText("operator-check")
            self.application.processEvents()
            self.assertIn("operator-check.h5", page.output_file_preview.text())
            page.set_settings(
                simulation_settings()
            )
            self.assertEqual(
                page.recipe_profile_badge.text(),
                "LIMITS + READBACK ACTIVE",
            )
            self.assertEqual(
                page.recipe_profile_badge.property("safetyState"),
                "verified",
            )

            sample = page.hero_card.mapTo(window, QPoint(40, 40))
            window._set_theme_mode("light", persist=False)
            self.application.processEvents()
            light = window.grab().toImage().pixelColor(sample).name()
            window._set_theme_mode("dark", persist=False)
            self.application.processEvents()
            dark = window.grab().toImage().pixelColor(sample).name()
            self.assertNotEqual(light, dark)
        finally:
            window._set_theme_mode("system", persist=False)
            window.close()
            self.application.processEvents()

    def test_execution_monitor_projects_full_recipe_tree_and_tracks_nested_node(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("execution")
            self.application.processEvents()
            monitor = window.run_monitor
            source = """\
schema_version: 1
name: execution-tree
root:
  id: root
  type: sequence
  children:
    - id: frequency-sweep
      type: sweep
      target: rigol.1.frequency
      start: "100 Hz"
      stop: "1 kHz"
      points: 2
      children:
        - id: wait-point
          type: wait
          duration: "10 ms"
finally:
  - id: rigol-off
    type: set_rigol_output
    channel: 1
    enabled: false
"""
            actions = (
                SimpleNamespace(
                    node_id="wait-point", kind="wait", is_finally=False,
                    setpoints_si={"rigol.1.frequency": 100.0},
                ),
                SimpleNamespace(
                    node_id="rigol-off", kind="set_rigol_output", is_finally=True,
                    payload={"channel": 1, "enabled": False}, setpoints_si={},
                ),
            )
            recipe = parse_recipe_text(source, origin="tree-parity-test")
            window.recipe_page._populate_recipe_tree(
                recipe.root, recipe.finally_nodes, SimpleNamespace(actions=actions)
            )
            snapshot = window.recipe_page.execution_tree_snapshot(
                source, SimpleNamespace(actions=actions)
            )

            def tree_text(item):
                return (
                    tuple(item.text(column) for column in range(3)),
                    tuple(tree_text(item.child(index)) for index in range(item.childCount())),
                )

            self.assertEqual(
                tuple(
                    tree_text(window.recipe_page.tree.topLevelItem(index))
                    for index in range(window.recipe_page.tree.topLevelItemCount())
                ),
                tuple(tree_text(item) for item in snapshot),
            )
            monitor.run_started(
                3,
                1.0,
                plan_actions=actions,
                recipe_source=source,
                recipe_tree_items=snapshot,
            )

            root = monitor.steps.topLevelItem(0)
            self.assertTrue(monitor.steps.isVisibleTo(window))
            self.assertGreater(monitor.steps.geometry().height(), 120)
            self.assertEqual(root.text(0), "Measurement sequence")
            sweep = root.child(0)
            self.assertIn("frequency sweep", sweep.text(0))
            def find_node(item, node_id):
                value = item.data(0, Qt.ItemDataRole.UserRole)
                if getattr(value, "id", None) == node_id:
                    return item
                for index in range(item.childCount()):
                    result = find_node(item.child(index), node_id)
                    if result is not None:
                        return result
                return None

            wait = find_node(root, "wait-point")
            self.assertIsNotNone(wait)
            self.assertEqual(
                wait.data(0, Qt.ItemDataRole.UserRole).id, "wait-point"
            )
            finally_item = monitor.steps.topLevelItem(1)
            self.assertIn("Finally", finally_item.text(0))
            self.assertEqual(
                find_node(finally_item, "rigol-off").data(0, Qt.ItemDataRole.UserRole).id,
                "rigol-off",
            )

            monitor.append_event(
                "action_started",
                {
                    "node_id": "wait-point",
                    "kind": "wait",
                    "setpoints_si": {"rigol.1.frequency": 100.0},
                },
            )
            self.assertEqual(wait.text(2), "FLOW")
            self.assertIn("RUNNING", wait.toolTip(2))
            self.assertEqual(monitor.output_states.topLevelItem(0).text(1), "UNKNOWN")
            self.assertEqual(monitor.active_parameters.topLevelItem(0).text(1), "100 Hz")
            monitor.append_event(
                "action_finished",
                {
                    "node_id": "wait-point",
                    "kind": "wait",
                    "timestamp_utc": "2026-07-21T10:00:00+00:00",
                    "state_snapshot": {
                        "output_status": {"rigol.1": "on"},
                        "device_states": {
                            "rigol": {
                                "channel_1": {
                                    "actual": {"frequency_hz": 100.0}
                                }
                            }
                        },
                    },
                },
            )
            self.assertEqual(monitor.output_states.topLevelItem(0).text(1), "ON")
            self.assertEqual(monitor.active_parameters.topLevelItem(0).text(2), "100 Hz")
            self.assertEqual(monitor.active_parameters.topLevelItem(0).text(3), "APPLIED")
        finally:
            window.close()
            self.application.processEvents()

    def test_sweep_builder_keeps_primary_workspace_rendered_at_narrow_desktop_width(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1024, 720)
            window.show()
            window._navigate_to("sweeps")
            self.application.processEvents()

            page = window.recipe_page
            self.assertTrue(page.document_card.isVisibleTo(window))
            self.assertTrue(page.selection_card.isVisibleTo(window))
            self.assertTrue(page.builder_container.isVisibleTo(window))
            self.assertTrue(page.run_button.isVisibleTo(window))
            self.assertGreater(page.builder_container.geometry().width(), 390)
            self.assertGreater(page.builder_container.geometry().height(), 150)
            self.assertFalse(page.inspector_panel.isVisibleTo(window))

            item = page.tree.topLevelItem(0)
            self.assertIsNotNone(item)
            page.tree.setCurrentItem(item)
            self.application.processEvents()
            self.assertNotEqual(
                page.selection_context.text(),
                "Select a block in the measurement tree",
            )

            window._navigate_to("execution")
            self.application.processEvents()
            monitor = window.run_monitor
            self.assertEqual(
                monitor.monitor_splitter.orientation(), Qt.Orientation.Vertical
            )
            self.assertFalse(
                monitor.activity_splitter.geometry().intersects(
                    monitor.spectrum_preview.geometry()
                )
            )
            self.assertLessEqual(
                monitor.spectrum_preview.geometry().right(),
                monitor.monitor_splitter.width(),
            )
        finally:
            window.close()
            self.application.processEvents()

    def test_execution_cockpit_prioritizes_current_operation_and_workspace(self) -> None:
        page = RunMonitorPage()
        try:
            page.resize(1360, 820)
            page.show()
            self.application.processEvents()
            page.run_started(1, 1.0)
            page.append_event(
                "action_started",
                {
                    "node_id": "keithley-b-current",
                    "kind": "update_keithley_level",
                    "channel": "B",
                    "setpoints_si": {"keithley.B.current": 0.01},
                },
            )
            self.application.processEvents()

            self.assertTrue(page.current_operation_card.isVisibleTo(page))
            self.assertIn("Keithley", page.current_operation_device.text())
            self.assertIn("source current", page.current_operation_parameter.text().lower())
            self.assertIn("10 mA", page.current_operation_value.text())
            self.assertIn("0.01 A", page.current_operation_si.text())
            self.assertGreaterEqual(page.steps.geometry().height(), 220)
            self.assertGreaterEqual(page.spectrum_preview.geometry().height(), 220)
            self.assertTrue(page._activity_pulse_timer.isActive())
            self.assertFalse(page.warnings.isVisible())
        finally:
            page.close()
            self.application.processEvents()

    def test_execution_tree_fallback_renders_plan_without_recipe_source(self) -> None:
        page = RunMonitorPage()
        try:
            action = SimpleNamespace(
                node_id="keithley-b-current",
                kind="update_keithley_level",
                is_finally=False,
                setpoints_si={"keithley.B.current": 0.01},
            )
            page.run_started(2, 1.0, plan_actions=(action, action))
            self.assertEqual(page.steps.topLevelItemCount(), 1)
            item = page.steps.topLevelItem(0)
            self.assertIn("keithley b current", item.text(0).lower())
            self.assertIn("update keithley level", item.text(1).lower())
            self.assertIn("0/2", item.text(2))
        finally:
            page.close()
            self.application.processEvents()

    def test_execution_tree_fallback_keeps_sweep_children_inside_roi_loop(self) -> None:
        """A source-only fallback must preserve the executable loop boundary."""

        page = RunMonitorPage()
        try:
            source = """\
schema_version: 1
name: fallback-loop
root:
  id: root
  type: sequence
  children:
    - id: current-sweep
      type: sweep
      target: keithley.B.current
      start: "0 A"
      stop: "1 mA"
      points: 2
      children:
        - id: current-point
          type: update_keithley_level
          channel: B
          mode: current
          level: "${keithley.B.current}"
"""
            configure = SimpleNamespace(
                node_id="current-sweep.configure",
                kind="configure_keithley",
                is_finally=False,
                setpoints_si={"keithley.B.current": 0.0},
            )
            update = SimpleNamespace(
                node_id="current-sweep.update-level",
                kind="update_keithley_level",
                is_finally=False,
                setpoints_si={"keithley.B.current": 0.0},
            )
            page.run_started(
                2,
                1.0,
                plan_actions=(configure, update),
                recipe_source=source,
            )

            root = page.steps.topLevelItem(0)
            sweep = root.child(0)
            self.assertIn("sweep", sweep.text(0).lower())
            loop = next(
                sweep.child(index)
                for index in range(sweep.childCount())
                if sweep.child(index).text(0) == "For each ROI point"
            )
            self.assertEqual(loop.childCount(), 2)
            self.assertEqual(loop.child(0).data(0, Qt.ItemDataRole.UserRole), "current-point")
            generated = page._step_items["current-sweep.update-level"]
            self.assertIs(generated.parent(), loop)
        finally:
            page.close()
            self.application.processEvents()

    def test_execution_tree_projects_generated_device_actions_into_their_loop(self) -> None:
        """Compiler-generated per-point IDs must never become flat top-level rows."""

        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            source = (Path(__file__).parents[1] / "recipes" / "untitled_sweep.yml").read_text(
                encoding="utf-8"
            )
            plan = RecipeCompiler(window._settings).compile(parse_recipe_text(source))
            snapshot = window.recipe_page.execution_tree_snapshot(source, plan)
            monitor = window.run_monitor
            monitor.run_started(
                len(plan.actions),
                1.0,
                plan_actions=plan.actions,
                recipe_source=source,
                recipe_tree_items=snapshot,
            )

            def find_item(item, value):
                raw = item.data(0, Qt.ItemDataRole.UserRole)
                if raw == value or getattr(raw, "id", None) == value:
                    return item
                for index in range(item.childCount()):
                    found = find_item(item.child(index), value)
                    if found is not None:
                        return found
                return None

            update = find_item(monitor.steps.topLevelItem(0), "keithley-81c50119.update-level")
            settle = find_item(monitor.steps.topLevelItem(0), "keithley-81c50119.settle")
            configure = find_item(monitor.steps.topLevelItem(0), "keithley-81c50119.configure")
            output_off = find_item(monitor.steps.topLevelItem(0), "keithley-81c50119.output-off")
            self.assertIsNotNone(update)
            self.assertIsNotNone(settle)
            self.assertIsNotNone(configure)
            self.assertIsNotNone(output_off)
            self.assertEqual(update.parent().text(0), "For each ROI point")
            self.assertIs(update.parent(), settle.parent())
            self.assertIn("Keithley B", configure.parent().text(0))
            self.assertNotEqual(configure.parent().text(0), "For each ROI point")
            self.assertIs(output_off.parent(), configure.parent())
            self.assertEqual(monitor.steps.topLevelItemCount(), 2)

            monitor.append_event(
                "shutdown_action_started",
                {"action": "keithley.outputs_off"},
            )
            shutdown = monitor._step_items["shutdown:keithley.outputs_off"]
            self.assertEqual(shutdown.parent().text(0), "Finally — safe shutdown")
            self.assertEqual(monitor.steps.topLevelItemCount(), 2)

            monitor.append_event(
                "action_started",
                {
                    "node_id": "keithley-81c50119.update-level",
                    "kind": "update_keithley_level",
                    "setpoints_si": {"keithley.B.current": 0.001},
                },
            )
            self.assertEqual(update.text(2), "RUNNING")
            monitor.append_event(
                "action_finished",
                {
                    "node_id": "keithley-81c50119.update-level",
                    "kind": "update_keithley_level",
                },
            )
            monitor._step_visual_timer.stop()
            monitor._flush_step_visual()
            self.assertIn("1/9", update.text(2))
        finally:
            window.close()
            self.application.processEvents()

    def test_execution_event_log_throttles_repeated_action_telemetry(self) -> None:
        """Thousands of loop actions must not enqueue thousands of repaints."""

        page = RunMonitorPage()
        try:
            page.run_started(200, 1.0)
            payload = {
                "node_id": "current-point",
                "kind": "update_keithley_level",
                "setpoints_si": {"keithley.B.current": 0.001},
            }
            for _ in range(100):
                page.append_event("action_started", payload)
                page.append_event("action_finished", payload)
            self.assertLessEqual(page.events.document().blockCount(), 12)
        finally:
            page.close()
            self.application.processEvents()

    def test_main_window_coalesces_read_only_device_projection_during_run(self) -> None:
        """Worker action bursts keep only the newest device-page snapshot."""

        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window._run_controller._thread = SimpleNamespace(isRunning=lambda: True)
            projected = Mock()
            window._apply_runner_device_readback = projected  # type: ignore[method-assign]
            payload = {
                "node_id": "current-point",
                "kind": "update_keithley_level",
                "setpoints_si": {"keithley.B.current": 0.001},
            }
            window._run_event("action_started", payload)
            window._run_event("action_finished", payload)
            self.assertEqual(projected.call_count, 0)

            window._execution_readback_timer.stop()
            window._flush_execution_readback()
            self.assertEqual(projected.call_count, 1)
            self.assertEqual(projected.call_args.args[0], "action_finished")
        finally:
            window._run_controller._thread = None
            window.close()
            self.application.processEvents()

    def test_sweep_workspace_has_framed_scrollable_surfaces_and_read_only_run_state(self) -> None:
        """The builder remains inspectable when a run owns the recipe."""

        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(820, 560)
            window.show()
            window._navigate_to("sweeps")
            self.application.processEvents()

            page = window.recipe_page
            host = window.navigation_routes["sweeps"]
            self.assertIsInstance(page.workspace_card, CardWidget)
            self.assertGreaterEqual(page.workspace_splitter.minimumHeight(), 420)
            self.assertTrue(page.execution_lock_banner.isHidden())
            self.assertGreater(host.scroll_area.verticalScrollBar().maximum(), 0)
            self.assertEqual(host.scroll_area.horizontalScrollBar().maximum(), 0)
            library_button_state = page._library_action_buttons[0].isEnabled()
            editor_read_only = page.editor.isReadOnly()

            window._set_run_ui_locked(True)
            self.application.processEvents()
            item = window.navigationInterface.widget(
                host.objectName()
            )
            self.assertTrue(host.isEnabled())
            self.assertIsNotNone(item)
            self.assertTrue(item.isEnabled())
            self.assertTrue(page.execution_lock_banner.isVisibleTo(window))
            self.assertFalse(page.run_button.isEnabled())
            self.assertFalse(page.path.isEnabled())
            self.assertTrue(page.editor.isReadOnly())
            self.assertTrue(page.library_panel.isEnabled())
            self.assertTrue(page.tree.isVisibleTo(window))

            window._set_run_ui_locked(False)
            self.application.processEvents()
            self.assertTrue(page.execution_lock_banner.isHidden())
            self.assertEqual(page._library_action_buttons[0].isEnabled(), library_button_state)
            self.assertEqual(page.editor.isReadOnly(), editor_read_only)
        finally:
            window._set_run_ui_locked(False)
            window.close()
            self.application.processEvents()

    def test_spectrum_preview_updates_are_coalesced_to_latest_frame(self) -> None:
        """A burst of worker previews must not schedule one repaint per point."""

        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            monitor = window.run_monitor
            monitor._preview_timer.setInterval(0)
            rendered: list[dict[str, object]] = []
            monitor.update_spectrum_preview = rendered.append  # type: ignore[method-assign]
            for point_index in range(25):
                monitor.queue_spectrum_preview(
                    {
                        "point_index": point_index,
                        "frequency_hz": [1.0, 2.0],
                        "power_dbm": [-70.0, -71.0],
                    }
                )
            self.application.processEvents()
            self.assertEqual(len(rendered), 1)
            self.assertEqual(rendered[0]["point_index"], 24)
        finally:
            window.close()
            self.application.processEvents()

    def test_execution_event_log_summarizes_large_payloads(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            monitor = window.run_monitor
            monitor.append_event(
                "point_stored",
                {
                    "point_index": 4,
                    "state_snapshot": {"device_states": {"anritsu": {"raw": "..."}}},
                    "frequency_hz": list(range(5_000)),
                    "power_dbm": list(range(5_000)),
                },
            )
            rendered = monitor.events.toPlainText()
            self.assertIn("state_snapshot=<confirmed>", rendered)
            self.assertIn("frequency_hz=<5000 values>", rendered)
            self.assertLess(len(rendered), 1_200)
        finally:
            window.close()
            self.application.processEvents()

    def test_sweep_workspace_and_roi_dialog_retheme_together(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        dialog = None
        try:
            window.resize(1440, 900)
            window.show()
            window._navigate_to("sweeps")
            page = window.recipe_page
            dialog = SweepGeneratorDialog(
                {
                    "device": "Keithley",
                    "label": "Channel B · source current",
                    "target": "keithley.B.current",
                    "dimension": "current",
                },
                page,
            )
            dialog.resize(980, 700)
            dialog.show()

            window._set_theme_mode("light", persist=False)
            self.application.processEvents()
            light_library = page.library_panel.viewport().grab().toImage().pixelColor(8, 8)
            light_tree = page.tree.viewport().grab().toImage().pixelColor(8, 8)
            light_dialog = dialog.grab().toImage().pixelColor(8, 8)

            window._set_theme_mode("dark", persist=False)
            self.application.processEvents()
            dark_library = page.library_panel.viewport().grab().toImage().pixelColor(8, 8)
            dark_tree = page.tree.viewport().grab().toImage().pixelColor(8, 8)
            dark_dialog = dialog.grab().toImage().pixelColor(8, 8)

            self.assertNotEqual(light_library.name(), dark_library.name())
            self.assertNotEqual(light_tree.name(), dark_tree.name())
            self.assertNotEqual(light_dialog.name(), dark_dialog.name())
            self.assertGreater(light_library.lightness(), dark_library.lightness())
            self.assertGreater(light_dialog.lightness(), dark_dialog.lightness())
        finally:
            if dialog is not None:
                dialog.close()
            window._set_theme_mode("system", persist=False)
            window.close()
            self.application.processEvents()

    def test_execution_monitor_rethemes_fluent_cards_without_losing_visible_actions(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("execution")
            self.application.processEvents()

            page = window.run_monitor
            self.assertEqual(
                page.monitor_splitter.orientation(), Qt.Orientation.Horizontal
            )
            self.assertIsInstance(page.hero_card, CardWidget)
            self.assertIsInstance(page.monitor_card, CardWidget)
            self.assertIsInstance(page.pause_button, PushButton)
            self.assertIsInstance(page.stop_button, PrimaryPushButton)
            self.assertIsInstance(page.steps, TreeWidget)
            self.assertTrue(page.stop_button.isVisibleTo(window))
            self.assertTrue(page.steps.isVisibleTo(window))
            self.assertGreater(page.steps.geometry().height(), 120)
            page.run_started(
                1,
                1.0,
                execution_mode="dry_run",
            )
            self.assertEqual(page.state.text(), "DRY RUN — OUTPUTS OFF")
            self.assertIn("forced OFF", page.state.toolTip())

            sample = page.hero_card.mapTo(window, QPoint(40, 40))
            window._set_theme_mode("light", persist=False)
            self.application.processEvents()
            light = window.grab().toImage().pixelColor(sample).name()
            window._set_theme_mode("dark", persist=False)
            self.application.processEvents()
            dark = window.grab().toImage().pixelColor(sample).name()
            self.assertNotEqual(light, dark)
        finally:
            window._set_theme_mode("system", persist=False)
            window.close()
            self.application.processEvents()

    def test_manual_execution_shows_floating_stage_gate_and_requires_next(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            window._navigate_to("execution")
            monitor = window.run_monitor
            requested: list[bool] = []
            monitor.manual_next_requested.connect(lambda: requested.append(True))
            monitor.run_started(1, 1.0, execution_mode="manual_step")
            self.assertEqual(monitor.state.text(), "MANUAL — PREPARING")
            self.application.processEvents()
            self.assertTrue(monitor._manual_dialog.isVisible())
            monitor.append_event(
                "manual_stage_waiting",
                {
                    "node_id": "configure-rigol",
                    "kind": "configure_rigol",
                    "action_index": 0,
                    "total_actions": 1,
                    "setpoints_si": {"rigol.1.frequency": 1_000.0},
                },
            )
            self.assertIn("frequency: 1 kHz", monitor._manual_dialog.details.text())
            self.assertNotIn("1000", monitor._manual_dialog.details.text())
            self.assertTrue(monitor._manual_dialog.next_button.isEnabled())
            monitor._manual_dialog.next_button.click()
            self.assertEqual(requested, [True])
            self.assertFalse(monitor._manual_dialog.next_button.isEnabled())
            monitor.complete({"result": SimpleNamespace(state=SimpleNamespace(value="safe"), error=None, stored_points=0), "path": "run.h5"})
            self.assertFalse(monitor._manual_dialog.isVisible())
            self.assertTrue(monitor.completion_card.isVisibleTo(window))
            self.assertIn("completed", monitor.completion_title.text().lower())
            self.assertEqual(monitor.completion_path.text(), "run.h5")
        finally:
            window.close()
            self.application.processEvents()

    def test_runner_telemetry_projects_confirmed_state_to_read_only_device_pages(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            rigol = window.rigol_page
            keithley = window.keithley_page
            rigol._controller.call = Mock()
            keithley._controller.call = Mock()
            keithley.channel.setCurrentText("A")
            keithley.mode.setCurrentText("current")
            window._set_device_pages_execution_read_only(True)
            self.assertFalse(rigol.frequency.isEnabled())
            self.assertFalse(keithley.level.isEnabled())

            window._run_event(
                "action_finished",
                {
                    "state_snapshot": {
                        "output_status": {"rigol.1": "on", "keithley.A": "off"},
                        "device_states": {
                            "rigol": {
                                "channel_1": {
                                    "actual": {
                                        "frequency_hz": 2_000.0,
                                        "high_level_v": 0.003,
                                        "low_level_v": -0.001,
                                    }
                                }
                            },
                            "keithley": {
                                "channel_A": {
                                    "actual": {
                                        "mode": "current",
                                        "source_level_si": 0.001,
                                        "compliance_si": 0.67,
                                    }
                                }
                            },
                        },
                    }
                },
            )
            self.application.processEvents()

            self.assertEqual(rigol.frequency.text(), "2 kHz")
            self.assertEqual(rigol.high_level.text(), "3 mV")
            self.assertEqual(rigol.low_level.text(), "-1 mV")
            self.assertEqual(rigol.output_channel_state.text(), "CH1 OUTPUT ON")
            self.assertEqual(keithley.level.text(), "1 mA")
            self.assertEqual(keithley.compliance.text(), "670 mV")
            self.assertEqual(
                keithley.channel_cards["A"]["output"].text(), "OUTPUT OFF"
            )
            self.assertFalse(rigol.frequency.isEnabled())
            self.assertFalse(keithley.level.isEnabled())
            rigol._controller.call.assert_not_called()
            keithley._controller.call.assert_not_called()
        finally:
            window._set_device_pages_execution_read_only(False)
            window.close()
            self.application.processEvents()

    def test_runner_projects_anritsu_moke_and_lakeshore_into_their_live_pages(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1360, 880)
            window.show()
            pages = (
                window.rigol_page,
                window.keithley_page,
                window.anritsu_page,
                window.moke_box_page,
                window.lakeshore_gaussmeter_page,
            )
            for page in pages:
                page._controller.call = Mock()
            window._set_device_pages_execution_read_only(True)
            self.assertTrue(all(not page.execution_badge.isHidden() for page in pages))

            window._run_event(
                "action_finished",
                {
                    "kind": "configure_anritsu",
                    "device": "anritsu",
                    "state_snapshot": {
                        "output_status": {"anritsu.sg": "off"},
                        "device_states": {
                            "anritsu": {
                                "spectrum": {
                                    "actual": {
                                        "start_hz": 1_000_000.0,
                                        "stop_hz": 2_000_000.0,
                                        "reference_level_dbm": -20.0,
                                        "points": 1001,
                                        "instrument_mode": "SPECT",
                                    }
                                },
                                "signal_generator": {
                                    "actual": {
                                        "frequency_hz": 10_000_000.0,
                                        "power_dbm": -30.0,
                                        "output_enabled": False,
                                    }
                                },
                            }
                        },
                    },
                },
            )
            anritsu = window.anritsu_page
            self.assertEqual(anritsu.start.text(), "1 MHz")
            self.assertEqual(anritsu.stop.text(), "2 MHz")
            self.assertEqual(anritsu.reference.text(), "-20 dBm")
            self.assertEqual(anritsu.points.currentData(), 1001)
            self.assertEqual(anritsu.sg_frequency.text(), "10 MHz")
            self.assertEqual(anritsu.sg_power.text(), "-30 dBm")
            self.assertIn("RF OUTPUT OFF", anritsu.sg_status.text())

            window._run_event(
                "action_started",
                {"kind": "acquire_spectrum", "device": "anritsu"},
            )
            self.assertIn("SWEEP ACQUIRING", anritsu.live_indicator.text())
            window._run_event(
                "spectrum_preview",
                {
                    "preview_kind": "measurement",
                    "trace_name": "TRAC1",
                    "point_index": 3,
                    "source_points": 3,
                    "frequency_hz": [1e6, 1.5e6, 2e6],
                    "power_dbm": [-50.0, -40.0, -45.0],
                    "timestamp_utc": "2026-07-22T10:00:00+00:00",
                },
            )
            self.assertIsNotNone(anritsu._latest_trace)
            self.assertIn("SPECTRUM STORED", anritsu.live_indicator.text())

            window._run_event(
                "action_finished",
                {
                    "kind": "measure_moke_hall",
                    "device": "moke_box",
                    "state_snapshot": {
                        "device_states": {
                            "moke_box": {
                                "hall_readback": {
                                    "actual": {
                                        "voltage_v": 0.125,
                                        "field_t": 0.001,
                                        "stddev_v": 0.0001,
                                        "samples": 1,
                                        "raw_ad7734": 123456,
                                        "timestamp_utc": "2026-07-22T10:00:01+00:00",
                                    }
                                }
                            }
                        }
                    },
                },
            )
            self.assertEqual(
                window.moke_box_page.field_values["hall1_voltage"].text(),
                "+0.125000 V",
            )
            self.assertEqual(len(window.moke_box_page._history), 1)

            window._run_event(
                "action_finished",
                {
                    "kind": "measure_lakeshore_field",
                    "device": "lakeshore",
                    "state_snapshot": {
                        "device_states": {
                            "lakeshore": {
                                "measurement": {
                                    "actual": {
                                        "mode": "dc",
                                        "unit": "tesla",
                                        "mode_code": "1",
                                        "unit_code": "2",
                                        "range_code": "3",
                                        "autorange_enabled": True,
                                        "probe_type_code": "1",
                                        "field_t": 0.0025,
                                        "frequency_hz": None,
                                        "negative_peak_t": None,
                                        "positive_peak_t": None,
                                        "timestamp_utc": "2026-07-22T10:00:02+00:00",
                                    }
                                }
                            }
                        }
                    },
                },
            )
            lakeshore = window.lakeshore_gaussmeter_page
            self.assertEqual(lakeshore.field.text(), "+0.0025 T")
            self.assertEqual(lakeshore.mode.text(), "DC")
            self.assertEqual(len(lakeshore._history), 1)
            self.assertFalse(anritsu.start.isEnabled())
            self.assertFalse(window.moke_box_page.read_fields_button.isEnabled())
            self.assertFalse(lakeshore.read_now.isEnabled())
            for page in pages:
                page._controller.call.assert_not_called()
        finally:
            window._set_device_pages_execution_read_only(False)
            window.close()
            self.application.processEvents()
