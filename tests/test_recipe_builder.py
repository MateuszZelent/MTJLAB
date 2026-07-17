from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QHeaderView
from PySide6.QtCore import Qt

from app.recipes import parse_recipe_text
from app.ui.main_window import (
    CommentEditorDialog,
    DeviceParameterDialog,
    FixedValueDialog,
    KeithleyConfigurationPanel,
    KeithleyNodeEditorDialog,
    KeithleyPage,
    KeithleySweepBuilderDialog,
    RecipePage,
    SweepGeneratorDialog,
)
from app.ui.workers import DeviceController
from app.devices.keithley import KeithleyAdapter
from app.devices.simulators import SimulatedVisaFactory
from tests.helpers import simulation_settings


class RecipeBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_tree_builder_exposes_node_actions_and_yaml_as_secondary_tab(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            self.assertEqual(page.builder_tabs.tabText(0), "Measurement tree")
            self.assertEqual(page.builder_tabs.tabText(1), "YAML source")
            self.assertTrue(page.add_node_button.text().startswith("+ Add"))
            self.assertIn("point generator", page.add_controls_button.text())
            self.assertEqual(page.edit_generator_button.text(), "Edit")
            self.assertEqual(page.delete_node_button.text(), "Delete")
            self.assertEqual(page.duplicate_node_button.text(), "Duplicate")
            self.assertEqual(page.move_up_button.text(), "Up")
            self.assertEqual(page.move_down_button.text(), "Down")
            self.assertEqual(page.tree.topLevelItem(0).text(0), "Measurement sequence")
        finally:
            page.close()

    def test_sweeps_library_contains_devices_and_flow_not_device_parameters(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            labels = [button.text() for button in page._library_action_buttons]
            self.assertEqual(
                labels,
                [
                    "Keithley 2600",
                    "Rigol DG1032Z",
                    "Anritsu MS2830A",
                    "Wait",
                    "Sequence / group",
                    "Repeat",
                    "Comment",
                ],
            )
            self.assertNotIn("Sweep current", labels)
            self.assertNotIn("Set fixed voltage", labels)
            self.assertEqual(page.tree.headerItem().text(0), "Measurement sequence")
        finally:
            page.close()

    def test_sweep_workspace_distributes_wide_and_narrow_windows_responsively(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page.resize(1900, 850)
            page.show()
            self.application.processEvents()
            page._update_workspace_layout(force=True)
            wide = page.workspace_splitter.sizes()
            self.assertGreater(wide[1], wide[0] * 2)
            self.assertGreaterEqual(wide[2], 420)

            page.resize(1050, 720)
            self.application.processEvents()
            page._update_workspace_layout(force=True)
            narrow = page.workspace_splitter.sizes()
            self.assertGreaterEqual(narrow[0], 210)
            self.assertGreaterEqual(narrow[1], 390)
            self.assertGreaterEqual(narrow[2], 280)
        finally:
            page.close()

    def test_measurement_tree_stretches_node_name_and_keeps_metadata_readable(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            header = page.tree.header()
            self.assertEqual(
                header.sectionResizeMode(0), QHeaderView.ResizeMode.Stretch
            )
            self.assertEqual(
                header.sectionResizeMode(1), QHeaderView.ResizeMode.Interactive
            )
            self.assertEqual(
                header.sectionResizeMode(2), QHeaderView.ResizeMode.Fixed
            )
            self.assertGreaterEqual(page.tree.columnWidth(1), 160)
            self.assertGreaterEqual(page.tree.columnWidth(2), 80)
        finally:
            page.close()

    def test_comment_editor_round_trips_multiline_text_and_updates_counter(self) -> None:
        dialog = CommentEditorDialog("Prepare sample\nCheck contacts")
        try:
            self.assertEqual(dialog.comment_text(), "Prepare sample\nCheck contacts")
            dialog.editor.setPlainText("Measure after thermal stabilization")
            self.assertEqual(
                dialog.comment_text(), "Measure after thermal stabilization"
            )
            self.assertIn("35", dialog.counter.text())
        finally:
            dialog.close()

    def test_comment_node_is_presented_by_content_and_is_editable(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page.node_kind.setCurrentIndex(page.node_kind.findData("comment"))
            page._add_basic_node()
            root = page.tree.topLevelItem(0)
            item = next(
                root.child(index)
                for index in range(root.childCount())
                if getattr(
                    root.child(index).data(0, Qt.ItemDataRole.UserRole),
                    "type",
                    None,
                )
                == "comment"
            )
            node = item.data(0, Qt.ItemDataRole.UserRole)
            self.assertEqual(node.type, "comment")
            label, detail, _icon = page._tree_presentation(node, 0, False)
            self.assertEqual(label, "Describe this step")
            self.assertEqual(detail, "Comment")
            page._node_selected(item, None)
            self.assertTrue(page.open_editor_button.isEnabled())
        finally:
            page.close()

    def test_device_library_block_adds_safe_non_executable_placeholder(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page._library_add_device("keithley")
            recipe = parse_recipe_text(page.editor.toPlainText())
            node = recipe.root.children[-1]
            self.assertEqual(node.type, "sequence")
            self.assertEqual(node.data["device_module"], "keithley")
            self.assertEqual(node.data["label"], "Keithley 2600")
            self.assertTrue(node.data["configuration_required"])
            self.assertIn("configuration required", page.summary.text().lower())
        finally:
            page.close()

    def test_drop_anritsu_under_keithley_preserves_tree_and_adds_single_spectrum(self) -> None:
        page = RecipePage(simulation_settings())
        dialog = KeithleyNodeEditorDialog(simulation_settings())
        try:
            page._library_add_device("keithley")
            recipe = parse_recipe_text(page.editor.toPlainText())
            keithley = recipe.root.children[-1]
            configured = page._configured_keithley_node(
                keithley,
                dialog.configuration_snapshot(),
                parameter_actions=[
                    {
                        "parameter_id": "source.level",
                        "mode": "sweep",
                        "value": "1 mA",
                        "segments": [
                            {
                                "start": "0 A",
                                "stop": "1 mA",
                                "points": 3,
                                "spacing": "linear",
                            }
                        ],
                    }
                ],
            )
            from app.recipes import replace_recipe_node

            page._apply_builder_source(
                replace_recipe_node(
                    page.editor.toPlainText(), node_id=keithley.id, node=configured
                ),
                "Configured nested sweep",
            )
            keithley = next(
                node
                for node in parse_recipe_text(page.editor.toPlainText()).root.children
                if node.id == keithley.id
            )
            keithley_item = page._find_tree_item(keithley.id)
            top_level_before = page.tree.topLevelItemCount()

            page._drop_library_block(
                "device:anritsu",
                keithley.id,
                "children",
                keithley_item.childCount(),
            )

            updated = parse_recipe_text(page.editor.toPlainText())
            updated_keithley = next(
                node for node in updated.root.children if node.id == keithley.id
            )
            acquisition = updated_keithley.children[-1]
            self.assertEqual(acquisition.type, "acquire_spectrum")
            self.assertEqual(acquisition.data["trace"], "TRAC1")
            self.assertEqual(page.tree.topLevelItemCount(), top_level_before)
            acquisition_item = page._find_tree_item(acquisition.id)
            self.assertIsNotNone(acquisition_item)
            self.assertIn("Acquire single spectrum", acquisition_item.text(0))
        finally:
            dialog.close()
            page.close()

    def test_failed_tree_render_keeps_previous_tree_and_yaml_unchanged(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            source_before = page.editor.toPlainText()
            labels_before = [
                page.tree.topLevelItem(index).text(0)
                for index in range(page.tree.topLevelItemCount())
            ]
            original_renderer = page._add_operator_control_rows

            def fail_render(_node: object, _parent: object) -> None:
                raise RuntimeError("synthetic render failure")

            page._add_operator_control_rows = fail_render  # type: ignore[method-assign]
            with self.assertRaisesRegex(RuntimeError, "synthetic render failure"):
                page._apply_builder_source(source_before, "Must not commit")

            self.assertEqual(page.editor.toPlainText(), source_before)
            self.assertEqual(
                [
                    page.tree.topLevelItem(index).text(0)
                    for index in range(page.tree.topLevelItemCount())
                ],
                labels_before,
            )
            page._add_operator_control_rows = original_renderer  # type: ignore[method-assign]
        finally:
            page.close()

    def test_manual_page_and_sweep_editor_share_keithley_configuration_panel(self) -> None:
        settings = simulation_settings()
        adapter = KeithleyAdapter(
            settings, session_factory=SimulatedVisaFactory("keithley")
        )
        controller = DeviceController(adapter)
        manual = KeithleyPage(controller, settings)
        dialog = KeithleyNodeEditorDialog(settings)
        try:
            self.assertIsInstance(manual.configuration_panel, KeithleyConfigurationPanel)
            self.assertIsInstance(dialog.configuration_panel, KeithleyConfigurationPanel)
            self.assertIs(manual.channel, manual.configuration_panel.channel)
            self.assertIs(dialog.channel, dialog.configuration_panel.channel)
            self.assertEqual(
                manual.configuration_panel.limit_values("level"),
                dialog.configuration_panel.limit_values("level"),
            )
            self.assertFalse(dialog.findChild(type(manual.output_toggle), "outputOnButton"))
        finally:
            dialog.close()
            manual.close()
            controller.close()

    def test_keithley_plan_dialog_round_trips_offline_configuration(self) -> None:
        dialog = KeithleyNodeEditorDialog(simulation_settings())
        try:
            dialog.channel.setCurrentText("A")
            dialog.mode.setCurrentText("voltage")
            dialog.level.setText("10 mV")
            dialog.compliance.setText("1 mA")
            snapshot = dialog.configuration_snapshot()
            self.assertEqual(snapshot.channel, "A")
            self.assertEqual(snapshot.source_mode, "voltage")
            self.assertEqual(snapshot.source_level, "10 mV")
            self.assertEqual(snapshot.compliance, "1 mA")
            self.assertFalse(dialog.hardware_actions_enabled)
        finally:
            dialog.close()

    def test_keithley_plan_dialog_exposes_explicit_action_for_each_parameter_and_output(self) -> None:
        page = RecipePage(simulation_settings())
        dialog = KeithleyNodeEditorDialog(simulation_settings())
        try:
            page._library_add_device("keithley")
            original = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            dialog.level.setText("2 mA")
            dialog.compliance.setText("50 mV")
            dialog.parameter_selectors["source.level"].setCurrentIndex(
                dialog.parameter_selectors["source.level"].findData("set")
            )
            dialog.parameter_selectors["source.compliance"].setCurrentIndex(
                dialog.parameter_selectors["source.compliance"].findData("sweep")
            )
            dialog.output_policy.setCurrentIndex(
                dialog.output_policy.findData("on")
            )
            actions = dialog.planned_parameter_actions()
            self.assertEqual(
                actions,
                [
                    {"parameter_id": "source.level", "mode": "set", "value": "2 mA"},
                    {
                        "parameter_id": "source.compliance",
                        "mode": "sweep",
                        "value": "50 mV",
                    },
                ],
            )
            replacement = page._configured_keithley_node(
                original,
                dialog.configuration_snapshot(),
                parameter_actions=actions,
                output_policy=dialog.selected_output_policy(),
            )
            self.assertEqual(
                replacement["parameter_actions"],
                actions,
            )
            self.assertNotIn("configuration", replacement)
            self.assertEqual(replacement["output_policy"], "on")
            self.assertTrue(replacement["roi_required"])
        finally:
            dialog.close()
            page.close()

    def test_sweeps_reads_unselected_keithley_values_from_manual_module(self) -> None:
        page = RecipePage(simulation_settings())
        manual_state = KeithleyConfigurationPanel(simulation_settings())
        try:
            manual_state.channel.setCurrentText("A")
            manual_state.mode.setCurrentText("voltage")
            manual_state.level.setText("12 mV")
            manual_state.compliance.setText("2 mA")
            page.set_keithley_snapshot_provider(manual_state.snapshot)
            snapshot = page._current_keithley_snapshot()
            self.assertEqual(snapshot.channel, "A")
            self.assertEqual(snapshot.source_mode, "voltage")
            self.assertEqual(snapshot.source_level, "12 mV")
            self.assertEqual(snapshot.compliance, "2 mA")
        finally:
            manual_state.close()
            page.close()

    def test_keithley_output_policy_does_not_implicitly_change_source_value(self) -> None:
        page = RecipePage(simulation_settings())
        dialog = KeithleyNodeEditorDialog(simulation_settings())
        try:
            page._library_add_device("keithley")
            original = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            replacement = page._configured_keithley_node(
                original,
                dialog.configuration_snapshot(),
                parameter_actions=[],
                output_policy="on",
            )
            self.assertEqual(replacement["parameter_actions"], [])
            self.assertEqual(replacement["output_policy"], "on")
            self.assertFalse(replacement["roi_required"])
        finally:
            dialog.close()
            page.close()

    def test_keithley_roi_definition_uses_selected_source_dimension(self) -> None:
        page = RecipePage(simulation_settings())
        dialog = KeithleyNodeEditorDialog(simulation_settings())
        snapshot = dialog.configuration_snapshot()
        try:
            definition = page._keithley_roi_definition(snapshot, "source.level")
            self.assertEqual(definition["target"], "keithley.B.current")
            self.assertEqual(definition["dimension"], "current")
            compliance = page._keithley_roi_definition(
                snapshot, "source.compliance"
            )
            self.assertEqual(compliance["dimension"], "voltage")
        finally:
            dialog.close()
            page.close()

    def test_keithley_configuration_has_contextual_go_to_roi_button(self) -> None:
        dialog = KeithleyNodeEditorDialog(simulation_settings())
        try:
            self.assertEqual(dialog.open_roi_button.text(), "Przejdź do ROI…")
            self.assertFalse(dialog.open_roi_button.isEnabled())
            source = dialog.parameter_selectors["source.level"]
            source.setCurrentIndex(source.findData("sweep"))
            self.assertTrue(dialog.open_roi_button.isEnabled())
            compliance = dialog.parameter_selectors["source.compliance"]
            compliance.setCurrentIndex(compliance.findData("sweep"))
            self.assertFalse(dialog.open_roi_button.isEnabled())
            compliance.setCurrentIndex(compliance.findData("unchanged"))
            dialog._store_roi_segments(
                "source.level",
                [{"start": "0 A", "stop": "1 mA", "points": 3, "spacing": "linear"}],
            )
            actions = dialog.planned_parameter_actions()
            self.assertEqual(actions[0]["segments"][0]["points"], 3)
        finally:
            dialog.close()

    def test_complete_keithley_roi_is_stored_and_summarized_as_sweep(self) -> None:
        page = RecipePage(simulation_settings())
        dialog = KeithleyNodeEditorDialog(simulation_settings())
        try:
            page._library_add_device("keithley")
            original = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            actions = [
                {
                    "parameter_id": "source.level",
                    "mode": "sweep",
                    "value": "1 mA",
                    "segments": [
                        {
                            "start": "0 A",
                            "stop": "1 mA",
                            "points": 3,
                            "spacing": "linear",
                        },
                        {
                            "start": "1 mA",
                            "stop": "2 mA",
                            "points": 3,
                            "spacing": "linear",
                        },
                    ],
                }
            ]
            replacement = page._configured_keithley_node(
                original,
                dialog.configuration_snapshot(),
                parameter_actions=actions,
                output_policy="on",
            )
            self.assertFalse(replacement["roi_required"])
            self.assertFalse(replacement["configuration_required"])
            from app.recipes import replace_recipe_node

            page._apply_builder_source(
                replace_recipe_node(
                    page.editor.toPlainText(),
                    node_id=original.id,
                    node=replacement,
                ),
                "Configured Keithley ROI",
            )
            item = page._find_tree_item(original.id)
            self.assertIsNotNone(item)
            self.assertIn("5 pts", item.text(0))
            self.assertEqual(item.text(1), "Sweep axis")
            self.assertEqual(item.text(2), "SWEEP")
        finally:
            dialog.close()
            page.close()

    def test_keithley_tree_exposes_each_changed_parameter_and_every_roi_stage(self) -> None:
        page = RecipePage(simulation_settings())
        dialog = KeithleyNodeEditorDialog(simulation_settings())
        try:
            page._library_add_device("keithley")
            original = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            replacement = page._configured_keithley_node(
                original,
                dialog.configuration_snapshot(),
                parameter_actions=[
                    {
                        "parameter_id": "source.level",
                        "mode": "sweep",
                        "value": "1 mA",
                        "segments": [
                            {"start": "0 A", "stop": "1 mA", "points": 3, "spacing": "linear"},
                            {"start": "1 mA", "stop": "2 mA", "points": 3, "spacing": "linear"},
                        ],
                    },
                    {"parameter_id": "source.compliance", "mode": "set", "value": "67 mV"},
                    {"parameter_id": "measurement.nplc", "mode": "set", "value": "1"},
                ],
                output_policy="on",
            )
            from app.recipes import replace_recipe_node

            page._apply_builder_source(
                replace_recipe_node(
                    page.editor.toPlainText(), node_id=original.id, node=replacement
                ),
                "Configured operator summary",
            )
            item = page._find_tree_item(original.id)
            rows = {
                item.child(index).text(0): item.child(index)
                for index in range(item.childCount())
            }
            self.assertIn("Source current", rows)
            self.assertEqual(rows["Source current"].text(2), "SWEEP")
            self.assertIn("0 A → 2 mA", rows["Source current"].text(1))
            self.assertIn("5 pts", rows["Source current"].text(1))
            self.assertEqual(rows["Source current"].childCount(), 2)
            self.assertEqual(rows["Source current"].child(0).text(0), "ROI 1")
            self.assertIn("0 A → 1 mA", rows["Source current"].child(0).text(1))
            self.assertIn("Voltage compliance", rows)
            self.assertEqual(rows["Voltage compliance"].text(1), "Set to 67 mV")
            self.assertEqual(rows["NPLC"].text(2), "SET")
            self.assertIn("Output", rows)
            self.assertEqual(rows["Output"].text(2), "ON")
        finally:
            dialog.close()
            page.close()

    def test_keithley_node_rejects_more_than_one_sweep_axis(self) -> None:
        page = RecipePage(simulation_settings())
        dialog = KeithleyNodeEditorDialog(simulation_settings())
        try:
            page._library_add_device("keithley")
            original = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            with self.assertRaisesRegex(Exception, "one sweep axis"):
                page._configured_keithley_node(
                    original,
                    dialog.configuration_snapshot(),
                    parameter_actions=[
                        {"parameter_id": "source.level", "mode": "sweep", "value": "1 mA"},
                        {
                            "parameter_id": "source.compliance",
                            "mode": "sweep",
                            "value": "67 mV",
                        },
                    ],
                )
        finally:
            dialog.close()
            page.close()

    def test_configured_keithley_placeholder_gets_modern_tree_summary(self) -> None:
        page = RecipePage(simulation_settings())
        dialog = KeithleyNodeEditorDialog(simulation_settings())
        try:
            page._library_add_device("keithley")
            recipe = parse_recipe_text(page.editor.toPlainText())
            original = recipe.root.children[-1]
            dialog.channel.setCurrentText("B")
            dialog.mode.setCurrentText("current")
            dialog.level.setText("1 mA")
            replacement = page._configured_keithley_node(
                original,
                dialog.configuration_snapshot(),
                parameter_actions=[
                    {
                        "parameter_id": "source.level",
                        "mode": "set",
                        "value": "1 mA",
                    }
                ],
                output_policy="unchanged",
            )
            from app.recipes import replace_recipe_node

            page._apply_builder_source(
                replace_recipe_node(page.editor.toPlainText(), node_id=original.id, node=replacement),
                "Configured Keithley",
            )
            item = page._find_tree_item(original.id)
            self.assertIsNotNone(item)
            self.assertEqual(item.text(0), "Keithley B · Source current = 1 mA")
            self.assertEqual(item.text(1), "Fixed configuration")
            self.assertEqual(item.text(2), "FIXED")
        finally:
            dialog.close()
            page.close()

    def test_library_blocks_expose_drag_payload_and_tree_accepts_external_blocks(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            keithley = page._library_action_buttons[0]
            self.assertEqual(keithley.property("dragKind"), "device:keithley")
            self.assertEqual(
                keithley.drag_mime_data().data("application/x-lab-control-sweep-block").data(),
                b"device:keithley",
            )
            self.assertTrue(page.tree.acceptDrops())
            self.assertEqual(page.tree.library_mime_type, "application/x-lab-control-sweep-block")
        finally:
            page.close()

    def test_tree_builder_exposes_context_menu_and_keyboard_operations(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            self.assertEqual(page.tree.contextMenuPolicy(), Qt.ContextMenuPolicy.CustomContextMenu)
            shortcuts = {shortcut.key().toString() for shortcut in page._tree_shortcuts}
            self.assertEqual(len(shortcuts), 5)
            self.assertTrue(any(value in shortcuts for value in {"Delete", "Del"}))
            self.assertTrue(any("Ctrl+D" in value for value in shortcuts))
            self.assertTrue(any("Alt+Up" in value for value in shortcuts))
            self.assertTrue(any("Alt+Down" in value for value in shortcuts))
        finally:
            page.close()

    def test_node_library_filters_actions_and_adds_a_tree_node(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            self.assertEqual(len(page._library_action_buttons), 7)
            page.library_search.setText("spectrum analyzer")
            visible = [button.text() for button in page._library_action_buttons if not button.isHidden()]
            self.assertEqual(visible, ["Anritsu MS2830A"])
            page.library_search.clear()
            page._library_add_basic("wait")
            recipe = parse_recipe_text(page.editor.toPlainText())
            self.assertEqual(recipe.root.children[-1].type, "wait")
        finally:
            page.close()

    def test_tree_builder_undo_and_redo_restore_recipe_structure(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            initial = page.editor.toPlainText()
            page._library_add_basic("wait")
            changed = page.editor.toPlainText()
            self.assertNotEqual(changed, initial)
            self.assertTrue(page.undo_tree_button.isEnabled())
            page.undo_tree_edit()
            self.assertEqual(page.editor.toPlainText(), initial)
            self.assertTrue(page.redo_tree_button.isEnabled())
            page.redo_tree_edit()
            self.assertEqual(page.editor.toPlainText(), changed)
        finally:
            page.close()

    def test_generator_creates_a_segmented_sweep_with_device_configuration(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            definition = {
                "device": "Keithley",
                "label": "Channel B · source current",
                "target": "keithley.B.current",
                "dimension": "current",
            }
            node = page._sweep_node_from_generator(
                definition,
                [
                    {"start": "0 A", "stop": "1 mA", "step": "500 uA", "spacing": "linear"},
                    {"start": "1 mA", "stop": "2 mA", "points": 3, "spacing": "linear"},
                ],
            )
            self.assertEqual(node["target"], "keithley.B.current")
            self.assertEqual(node["children"][0]["level"], "${keithley.B.current}")
            source = page.editor.toPlainText()
            from app.recipes import add_recipe_node

            parsed = parse_recipe_text(
                add_recipe_node(source, parent_id="sequence-main", node=node)
            )
            self.assertEqual(parsed.root.children[-1].data["segments"][1]["points"], 3)
        finally:
            page.close()

    def test_generator_dialog_previews_multiple_intervals(self) -> None:
        dialog = SweepGeneratorDialog(
            {
                "device": "Rigol",
                "label": "CH1 · frequency",
                "target": "rigol.1.frequency",
                "dimension": "frequency",
            }
        )
        try:
            dialog.add_interval()
            self.assertEqual(dialog.segments.rowCount(), 2)
            self.assertIn("Generated", dialog.preview.text())
            self.assertEqual(len(dialog.plot.listDataItems()), 2)
            self.assertGreater(dialog.plot.listDataItems()[0].xData.size, 1)
        finally:
            dialog.close()

    def test_roi_dialog_reflows_for_narrow_and_wide_windows(self) -> None:
        dialog = SweepGeneratorDialog(
            {
                "device": "Keithley",
                "label": "Channel B · source current",
                "target": "keithley.B.current",
                "dimension": "current",
            }
        )
        try:
            dialog.resize(760, 700)
            dialog.show()
            self.application.processEvents()
            dialog._update_responsive_layout()
            self.application.processEvents()
            self.assertEqual(dialog.splitter.orientation(), Qt.Orientation.Vertical)
            self.assertEqual(dialog.segments.horizontalScrollBar().maximum(), 0)
            dialog.resize(1180, 700)
            self.application.processEvents()
            dialog._update_responsive_layout()
            self.application.processEvents()
            self.assertEqual(dialog.splitter.orientation(), Qt.Orientation.Horizontal)
            self.assertGreaterEqual(dialog.splitter.sizes()[0], 540)
            self.assertEqual(
                dialog.segments.horizontalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )
            self.assertEqual(dialog.segments.horizontalScrollBar().maximum(), 0)
            self.assertGreaterEqual(dialog.segments.columnWidth(0), 88)
            self.assertGreaterEqual(dialog.segments.columnWidth(1), 88)
            self.assertGreaterEqual(dialog.segments.columnWidth(2), 100)
            self.assertGreaterEqual(dialog.segments.columnWidth(3), 120)
            self.assertGreaterEqual(dialog.segments.columnWidth(4), 120)
        finally:
            dialog.close()

    def test_roi_plot_uses_explicit_application_theme_colors(self) -> None:
        dialog = SweepGeneratorDialog(
            {
                "device": "Keithley",
                "label": "Channel B · source current",
                "target": "keithley.B.current",
                "dimension": "current",
            }
        )
        try:
            self.assertIn(dialog.plot_theme, {"light", "dark"})
            background = dialog.plot.backgroundBrush().color().name().lower()
            expected = "#ffffff" if dialog.plot_theme == "light" else "#101419"
            self.assertEqual(background, expected)
        finally:
            dialog.close()

    def test_keithley_library_dialog_combines_source_parameters_and_point_preview(self) -> None:
        dialog = KeithleySweepBuilderDialog(simulation_settings())
        try:
            self.assertEqual(dialog.channel.currentText(), "B")
            self.assertEqual(dialog.mode.currentText(), "current")
            self.assertEqual(dialog.definition["target"], "keithley.B.current")
            self.assertTrue(dialog.compliance.text())
            self.assertIn("Generated", dialog.preview.text())
            dialog.channel.setCurrentText("A")
            dialog.mode.setCurrentText("voltage")
            self.assertEqual(dialog.definition["target"], "keithley.A.voltage")
        finally:
            dialog.close()

    def test_fixed_value_dialog_creates_a_keithley_configuration_not_an_axis(self) -> None:
        page = RecipePage(simulation_settings())
        dialog = FixedValueDialog(
            {
                "device": "Keithley",
                "label": "Channel B · source current",
                "target": "keithley.B.current",
                "dimension": "current",
            },
            page,
        )
        try:
            dialog.value.setText("1 mA")
            node = page._fixed_node_from_dialog(dialog.definition, dialog)
            self.assertEqual(node["type"], "configure_keithley")
            self.assertEqual(node["channel"], "B")
            self.assertEqual(node["level"], "1 mA")
            self.assertNotIn("segments", node)
        finally:
            dialog.close()
            page.close()

    def test_device_picker_lists_every_supported_sweepable_field_for_selected_device(self) -> None:
        dialog = DeviceParameterDialog()
        try:
            self.assertEqual(
                [dialog.fields.item(index).text() for index in range(dialog.fields.count())],
                [
                    "Channel A · source current",
                    "Channel A · source voltage",
                    "Channel B · source current",
                    "Channel B · source voltage",
                ],
            )
            dialog.device.setCurrentText("Rigol")
            self.assertEqual(dialog.fields.count(), 6)
            dialog.device.setCurrentText("Anritsu SG")
            self.assertEqual(dialog.fields.count(), 2)
            dialog.device.setCurrentText("Anritsu Spectrum")
            self.assertEqual(dialog.fields.count(), 3)
        finally:
            dialog.close()

    def test_tree_builder_adds_containers_and_safe_nodes_to_finally(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page.node_kind.setCurrentIndex(page.node_kind.findData("sequence"))
            page._add_basic_node()
            parsed = parse_recipe_text(page.editor.toPlainText())
            self.assertIn("sequence", tuple(node.type for node in parsed.root.children))

            finally_item = next(
                page.tree.topLevelItem(index)
                for index in range(page.tree.topLevelItemCount())
                if page.tree.topLevelItem(index).text(0).startswith("Finally")
            )
            page.tree.setCurrentItem(finally_item)
            page.node_kind.setCurrentIndex(page.node_kind.findData("set_rigol_output"))
            page._add_basic_node()
            parsed = parse_recipe_text(page.editor.toPlainText())
            self.assertEqual(parsed.finally_nodes[-1].type, "set_rigol_output")
            self.assertFalse(parsed.finally_nodes[-1].data["enabled"])
        finally:
            page.close()

    def test_spectrum_parameter_generator_builds_anritsu_configuration_child(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            definition = {
                "device": "Anritsu Spectrum",
                "label": "Spectrum · reference level",
                "target": "anritsu.spectrum.reference_level",
                "dimension": "rf_power",
            }
            node = page._sweep_node_from_generator(
                definition,
                [{"start": "-20 dBm", "stop": "0 dBm", "points": 3, "spacing": "linear"}],
            )
            self.assertEqual(node["children"][0]["type"], "configure_anritsu")
            self.assertEqual(
                node["children"][0]["reference_level"], "${anritsu.spectrum.reference_level}"
            )
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
