from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHeaderView,
    QMessageBox,
    QTreeWidgetItem,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from qfluentwidgets import (
    ComboBox, LineEdit, PlainTextEdit, PrimaryPushButton, PushButton,
    SpinBox, TableWidget,
)

from app.recipes import RecipeNode, parse_recipe_text
from app.devices.anritsu_ms2830a.ui import (
    AnritsuAdvancedSpectrumPanel,
    AnritsuNodeEditorDialog,
    AnritsuSignalGeneratorNodeEditorDialog,
    AnritsuSpectrumConfigurationPanel,
)
from app.devices.keithley_2600.ui import (
    KeithleyConfigurationPanel,
    KeithleyNodeEditorDialog,
    KeithleyPage,
)
from app.devices.rigol_dg1000z.ui import RigolNodeEditorDialog
from app.ui.recipes import DeviceParameterDialog, RecipeTreeWidget, SweepGeneratorDialog
from app.ui.recipes.page import (
    AnritsuAcquisitionEditorDialog,
    CommentEditorDialog,
    FixedValueDialog,
    KeithleySweepBuilderDialog,
    RecipePage,
)
from app.ui.workers import DeviceController
from app.ui.design_system import apply_application_theme, tokens_for
from app.devices.keithley_2600 import KeithleyAdapter
from app.devices.anritsu_ms2830a import SignalGeneratorSnapshot
from app.devices.simulators import SimulatedVisaFactory
from app.storage import Hdf5RunWriter
from tests.helpers import simulation_settings


class RecipeBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_tree_builder_exposes_node_actions_and_yaml_as_secondary_tab(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            self.assertEqual(page.workflow_tabs.currentRouteKey(), "tree")
            self.assertIs(page.builder_stack.widget(0), page.tree)
            self.assertIs(page.builder_stack.widget(1), page.editor)
            self.assertFalse(hasattr(page, "add_node_button"))
            self.assertFalse(hasattr(page, "add_controls_button"))
            self.assertFalse(hasattr(page, "node_kind"))
            self.assertTrue(page.library_panel.isEnabled())
            self.assertEqual(page.edit_device_button.text(), "Device settings")
            self.assertEqual(page.edit_generator_button.text(), "Edit ROI")
            self.assertEqual(page.delete_node_button.text(), "Delete")
            self.assertEqual(page.duplicate_node_button.text(), "Duplicate")
            self.assertEqual(page.move_up_button.text(), "Up")
            self.assertEqual(page.move_down_button.text(), "Down")
            self.assertEqual(page.load_recipe_action.text(), "Load recipe")
            self.assertEqual(page.open_hdf5_action.text(), "Open HDF5 result")
            self.assertEqual(page.tree.topLevelItem(0).text(0), "Measurement sequence")
        finally:
            page.close()

    def test_new_action_starts_a_valid_empty_sweep(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page.new_recipe(confirm=False)
            recipe = parse_recipe_text(page.editor.toPlainText())
            self.assertEqual(recipe.name, "Untitled sweep")
            self.assertEqual(recipe.root.children, ())
            self.assertTrue(page.path.text().endswith("untitled_sweep.yml"))
            self.assertEqual(page.tree.topLevelItem(0).childCount(), 0)
        finally:
            page.close()

    def test_load_editor_routes_h5_result_to_reconstructed_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "historical-result.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="",
                settings_source="schema_version: 1\n",
                plan_hash="h5-import",
                device_idn={"rigol": "RIGOL,DG1032Z,SIM,1.0"},
            )
            writer.close("completed")
            page = RecipePage(simulation_settings())
            try:
                page.path.setText(str(path))
                page.load_editor(show_error=False)
                self.assertTrue(page.historical_sweep_active)
                self.assertFalse(page.library_panel.isEnabled())
                self.assertIn("Historical THATEC Sweep", page.tree.topLevelItem(0).text(0))
                self.assertIn("Historical THATEC Sweep loaded", page.summary.text())
            finally:
                page.close()

    def test_tree_builder_can_add_read_only_moke_hall_checkpoint(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page.new_recipe(confirm=False)
            self.assertTrue(
                any(
                    button.drag_kind == "flow:measure_moke_hall"
                    for button in page._library_action_buttons
                )
            )
            page._library_add_basic("measure_moke_hall")

            recipe = parse_recipe_text(page.editor.toPlainText())
            node = recipe.root.children[0]
            self.assertEqual(node.type, "measure_moke_hall")
            item = page._find_tree_item(node.id)
            self.assertIsNotNone(item)
            self.assertIn("MOKE Hall 1", item.text(0))
        finally:
            page.close()

    def test_tree_rebuild_preserves_selected_node_and_expansion_state(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            recipe = parse_recipe_text(page.editor.toPlainText())
            selected_node = recipe.root.children[0]
            selected_item = page._find_tree_item(selected_node.id)
            root_item = page._find_tree_item(recipe.root.id)
            self.assertIsNotNone(selected_item)
            self.assertIsNotNone(root_item)
            page.tree.setCurrentItem(selected_item)
            root_item.setExpanded(True)

            page._populate_recipe_tree(recipe.root, recipe.finally_nodes, None)

            current = page.tree.currentItem()
            self.assertEqual(
                current.data(0, Qt.ItemDataRole.UserRole).id,
                selected_node.id,
            )
            self.assertTrue(page._find_tree_item(recipe.root.id).isExpanded())
        finally:
            page.close()

    def test_selected_subtree_can_be_disabled_and_enabled_without_deletion(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            recipe = parse_recipe_text(page.editor.toPlainText())
            target = recipe.root.children[0]
            page.tree.setCurrentItem(page._find_tree_item(target.id))

            page._toggle_selected_node_disabled()
            disabled = parse_recipe_text(page.editor.toPlainText())
            disabled_node = next(
                node for node in disabled.root.children if node.id == target.id
            )
            self.assertIs(disabled_node.data["disabled"], True)
            self.assertEqual(page._find_tree_item(target.id).text(2), "DISABLED")

            page._toggle_selected_node_disabled()
            enabled = parse_recipe_text(page.editor.toPlainText())
            enabled_node = next(
                node for node in enabled.root.children if node.id == target.id
            )
            self.assertNotIn("disabled", enabled_node.data)
        finally:
            page.close()

    def test_complete_subtree_can_be_duplicated_with_fresh_node_ids(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            before = parse_recipe_text(page.editor.toPlainText())
            target = next(node for node in before.root.children if node.children)
            page.tree.setCurrentItem(page._find_tree_item(target.id))

            page._duplicate_selected_node()

            after = parse_recipe_text(page.editor.toPlainText())
            self.assertEqual(len(after.root.children), len(before.root.children) + 1)
            original_index = next(
                index
                for index, node in enumerate(after.root.children)
                if node.id == target.id
            )
            original = after.root.children[original_index]
            duplicate = after.root.children[original_index + 1]
            self.assertEqual(original.type, duplicate.type)
            self.assertEqual(len(original.children), len(duplicate.children))

            def ids(node: object) -> set[str]:
                return {
                    node.id,
                    *(
                        child_id
                        for child in (*node.children, *node.else_children)
                        for child_id in ids(child)
                    ),
                }

            self.assertTrue(ids(original).isdisjoint(ids(duplicate)))
        finally:
            page.close()

    def test_command_bar_load_and_save_actions_execute_without_checked_argument(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            recipe_path = Path(
                "recipes/keithley_b_rigol_frequency_anritsu_reference_10x100.yml"
            )
            page.path.setText(str(recipe_path))
            page.editor.setPlainText("not the requested recipe")

            with patch.object(
                QFileDialog,
                "getOpenFileName",
                return_value=(str(recipe_path.resolve()), "YAML recipes (*.yml *.yaml)"),
            ):
                page.load_recipe_action.trigger()

            self.assertIn(
                "Keithley B current × Rigol sine frequency",
                page.editor.toPlainText(),
            )
            self.assertEqual(page.summary.text(), "Recipe loaded. Compile it before running.")
            self.assertEqual(page.path.text(), str(recipe_path.resolve()))

            saved = SimpleNamespace(path=recipe_path, backup_path=None)
            page._repository.save = Mock(return_value=saved)
            page.save_recipe_action.trigger()

            page._repository.save.assert_called_once_with(
                str(recipe_path.resolve()), page.editor.toPlainText()
            )

            page.editor.setPlainText(
                """
schema_version: 1
name: Toolbar validation
root:
  id: root
  type: sequence
  children:
    - id: settle
      type: wait
      duration: 1 ms
finally: []
""".strip()
            )
            page.compile_recipe_action.trigger()
            for _attempt in range(200):
                if page.run_button.isEnabled():
                    break
                QTest.qWait(10)

            self.assertTrue(page.run_button.isEnabled())
            self.assertIn("Plan:", page.summary.text())
        finally:
            page.close()

    def test_canceling_recipe_explorer_keeps_current_path_and_tree(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            current_path = page.path.text()
            current_source = page.editor.toPlainText()

            with patch.object(
                QFileDialog, "getOpenFileName", return_value=("", "")
            ):
                page.load_recipe_action.trigger()

            self.assertEqual(page.path.text(), current_path)
            self.assertEqual(page.editor.toPlainText(), current_source)
        finally:
            page.close()

    def test_background_preflight_discards_result_after_source_change(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page._preflight_source = "old source"
            page.editor.setPlainText("new source")
            page._preflight_succeeded(Mock(), Mock(), Mock())

            self.assertFalse(page.run_button.isEnabled())
            self.assertIn("stale result was discarded", page.summary.text())
        finally:
            page.close()

    def test_native_sweeps_render_their_legacy_ranges_as_clickable_roi_rows(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page.path.setText(
                "recipes/keithley_b_rigol_frequency_anritsu_reference_10x100.yml"
            )
            page.load_editor()

            keithley = page._find_tree_item("keithley-b-current-sweep")
            rigol = page._find_tree_item("rigol-ch1-frequency-sweep")
            self.assertIsNotNone(keithley)
            self.assertIsNotNone(rigol)

            keithley_roi = keithley.child(0)
            rigol_roi = rigol.child(0)
            self.assertEqual(keithley_roi.text(0), "ROI 1")
            self.assertIn("0 A", keithley_roi.text(1))
            self.assertIn("150 mA", keithley_roi.text(1))
            self.assertIn("10 pts", keithley_roi.text(1))
            self.assertEqual(
                keithley_roi.data(0, page.operator_row_role)["kind"],
                "native_sweep_roi",
            )
            self.assertEqual(rigol_roi.text(0), "ROI 1")
            self.assertIn("100 kHz", rigol_roi.text(1))
            self.assertIn("30 MHz", rigol_roi.text(1))
            self.assertIn("100 pts", rigol_roi.text(1))

            with patch.object(page, "_edit_selected_generator") as editor:
                page._operator_row_clicked(keithley_roi, 0)

            editor.assert_called_once()
            self.assertEqual(
                editor.call_args.kwargs["node"].id,
                "keithley-b-current-sweep",
            )
            self.assertEqual(editor.call_args.kwargs["stage_index"], 0)
        finally:
            page.close()

    def test_editing_native_sweep_converts_roi_without_losing_nested_sequence(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page.path.setText(
                "recipes/keithley_b_rigol_frequency_anritsu_reference_10x100.yml"
            )
            page.load_editor()
            before = parse_recipe_text(page.editor.toPlainText())
            before_sweep = next(
                node
                for node in before.root.children
                if node.id == "keithley-b-current-sweep"
            )
            page.tree.setCurrentItem(
                page._find_tree_item("keithley-b-current-sweep")
            )

            with (
                patch.object(
                    KeithleySweepBuilderDialog,
                    "exec",
                    return_value=QDialog.DialogCode.Accepted,
                ),
                patch.object(
                    KeithleySweepBuilderDialog,
                    "segment_data",
                    return_value=[
                        {
                            "start": "0 A",
                            "stop": "150 mA",
                            "points": 10,
                            "spacing": "linear",
                        }
                    ],
                ),
                patch.object(QMessageBox, "information") as information,
            ):
                page._edit_selected_generator()

            information.assert_not_called()
            after = parse_recipe_text(page.editor.toPlainText())
            after_sweep = next(
                node
                for node in after.root.children
                if node.id == "keithley-b-current-sweep"
            )
            self.assertEqual(
                tuple(child.id for child in after_sweep.children),
                tuple(child.id for child in before_sweep.children),
            )
            self.assertEqual(
                after_sweep.data["segments"],
                [
                    {
                        "start": "0 A",
                        "stop": "150 mA",
                        "points": 10,
                        "spacing": "linear",
                    }
                ],
            )
            nested_rigol = after_sweep.children[1]
            self.assertEqual(nested_rigol.id, "rigol-ch1-frequency-sweep")
            self.assertEqual(
                tuple(child.id for child in nested_rigol.children),
                (
                    "rigol-ch1-frequency-point",
                    "acquire-raw-and-reference-difference",
                ),
            )
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
                    "Anritsu configuration",
                    "Anritsu signal generator",
                    "MOKE Hall (V + field)",
                    "Measure Lake Shore field",
                    "Acquire reference",
                    "Acquire spectrum once",
                    "Keithley A â†’ 0 + OFF",
                    "Keithley B â†’ 0 + OFF",
                    "Rigol CH1 OFF",
                    "Rigol CH2 OFF",
                    "Anritsu SG OFF",
                    "Wait",
                    "Sequence / group",
                    "Repeat",
                    "Comment",
                    "Anritsu SG ARM",
                    "Anritsu SG RF ON",
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
            self.assertFalse(page.inspector_panel.isVisible())
            self.assertFalse(page.inspector_visibility_action.isChecked())
            self.assertEqual(narrow[2], 0)

            page.inspector_visibility_action.setChecked(True)
            self.application.processEvents()
            self.assertTrue(page.inspector_panel.isVisible())
            self.assertTrue(page.inspector_visibility_action.isChecked())
        finally:
            page.close()

    def test_node_library_is_the_only_add_surface_above_measurement_tree(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page.resize(1900, 850)
            page.show()
            self.application.processEvents()

            self.assertFalse(hasattr(page, "node_kind"))
            self.assertFalse(hasattr(page, "_node_kind_model_host"))
            self.assertIn(
                "Sequence / group",
                [button.text() for button in page._library_action_buttons],
            )
            self.assertTrue(page.selection_title.isVisibleTo(page))
            self.assertTrue(page.selection_context.isVisibleTo(page))
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
            page._add_basic_node("comment")
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

    def test_reference_and_point_update_nodes_are_operator_readable(self) -> None:
        recipe = parse_recipe_text(
            """\
schema_version: 1
name: readable-actions
root:
  id: root
  type: sequence
  children:
    - {id: reference, type: acquire_reference, trace: TRAC1}
    - {id: current, type: update_keithley_level, channel: B, mode: current, level: 1 mA}
    - {id: frequency, type: update_rigol_frequency, channel: 1, frequency: 1 MHz}
    - id: spectrum
      type: acquire_spectrum
      trace: TRAC1
      reference_operation: difference_db
      store_raw: true
      store_processed: true
"""
        )
        page = RecipePage(simulation_settings())
        try:
            labels = [
                page._tree_presentation(node, 0, False)[:2]
                for node in recipe.root.children
            ]
            self.assertIn("reference", labels[0][0].lower())
            self.assertIn("OUTPUT unchanged", labels[1][1])
            self.assertIn("OUTPUT unchanged", labels[2][1])
            self.assertIn("raw-reference", labels[3][0])
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

    def test_drop_anritsu_configuration_under_keithley_preserves_tree(self) -> None:
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
            anritsu = updated_keithley.children[-1]
            self.assertEqual(anritsu.type, "sequence")
            self.assertEqual(anritsu.data["device_module"], "anritsu")
            self.assertEqual(anritsu.children, ())
            self.assertEqual(page.tree.topLevelItemCount(), top_level_before)
            anritsu_item = page._find_tree_item(anritsu.id)
            self.assertIsNotNone(anritsu_item)
            self.assertIn("Anritsu Spectrum", anritsu_item.text(0))
        finally:
            dialog.close()
            page.close()

    def test_failed_tree_render_keeps_previous_tree_and_yaml_unchanged(self) -> None:
        page = RecipePage(simulation_settings())
        events: list[str] = []
        page.status.connect(events.append)
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
            self.assertTrue(
                any(
                    "TREE_RENDER_REJECTED" in event
                    and "synthetic render failure" in event
                    for event in events
                )
            )
            page._add_operator_control_rows = original_renderer  # type: ignore[method-assign]
        finally:
            page.close()

    def test_invalid_tree_move_is_logged_and_keeps_existing_tree(self) -> None:
        page = RecipePage(simulation_settings())
        events: list[str] = []
        page.status.connect(events.append)
        try:
            page._library_add_device("keithley")
            recipe = parse_recipe_text(page.editor.toPlainText())
            keithley = recipe.root.children[-1]
            page._library_add_device(
                "anritsu",
                parent_id=keithley.id,
                branch="children",
                index=len(keithley.children),
            )
            recipe = parse_recipe_text(page.editor.toPlainText())
            keithley = next(
                node for node in recipe.root.children if node.id == keithley.id
            )
            anritsu = keithley.children[-1]
            source_before = page.editor.toPlainText()
            labels_before = [
                page.tree.topLevelItem(index).text(0)
                for index in range(page.tree.topLevelItemCount())
            ]
            with patch.object(QMessageBox, "warning"):
                page._move_recipe_node(
                    keithley.id, anritsu.id, "children", 0
                )
            self.assertEqual(page.editor.toPlainText(), source_before)
            self.assertEqual(
                [
                    page.tree.topLevelItem(index).text(0)
                    for index in range(page.tree.topLevelItemCount())
                ],
                labels_before,
            )
            self.assertTrue(
                any(
                    "TREE_MOVE_REJECTED" in event
                    and keithley.id in event
                    and anritsu.id in event
                    for event in events
                )
            )
        finally:
            page.close()

    def test_move_into_collapsed_device_keeps_node_selected_and_visible(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page.resize(1500, 850)
            page.show()
            self.application.processEvents()
            page.new_recipe(confirm=False)
            page._library_add_device("keithley")
            source = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            page.tree.setCurrentItem(page.tree.topLevelItem(0))
            page._library_add_device("rigol")
            target = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            target_item = page._find_tree_item(target.id)
            source_item = page._find_tree_item(source.id)
            self.assertIsNotNone(target_item)
            self.assertIsNotNone(source_item)
            target_item.setExpanded(False)
            page.tree.setCurrentItem(source_item)

            with patch.object(QMessageBox, "warning") as warning:
                page._move_recipe_node(
                    source.id,
                    target.id,
                    "children",
                    len(target.children),
                )
            warning.assert_not_called()

            updated = parse_recipe_text(page.editor.toPlainText())
            updated_target = updated.root.children[-1]
            self.assertIn(source.id, tuple(node.id for node in updated_target.children))
            selected = page.tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
            self.assertIsInstance(selected, RecipeNode)
            self.assertEqual(selected.id, source.id)
            self.assertTrue(page._find_tree_item(target.id).isExpanded())
            self.assertFalse(page.tree.visualItemRect(page.tree.currentItem()).isEmpty())

            page.undo_tree_edit()
            undone = parse_recipe_text(page.editor.toPlainText())
            self.assertEqual(
                tuple(node.id for node in undone.root.children),
                (source.id, target.id),
            )
            page.redo_tree_edit()
            redone_target = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            self.assertIn(source.id, tuple(node.id for node in redone_target.children))
        finally:
            page.close()

    def test_drop_uses_node_pinned_at_drag_start_not_hover_selection(self) -> None:
        tree = RecipeTreeWidget()
        source_item = QTreeWidgetItem(["Source"])
        target_item = QTreeWidgetItem(["Target"])
        source_item.setData(
            0,
            Qt.ItemDataRole.UserRole,
            RecipeNode("source", "comment"),
        )
        target_item.setData(
            0,
            Qt.ItemDataRole.UserRole,
            RecipeNode("target", "sequence"),
        )
        tree.addTopLevelItems([source_item, target_item])
        tree._dragged_node_id = "source"
        tree.setCurrentItem(target_item)
        moves: list[tuple[str, str, str, int]] = []
        tree.move_requested.connect(lambda *move: moves.append(move))
        event = Mock()
        event.mimeData().hasFormat.return_value = False

        with (
            patch.object(tree, "itemAt", return_value=target_item),
            patch.object(
                tree,
                "_drop_destination",
                return_value=("target", "children", 0),
            ),
        ):
            tree.dropEvent(event)

        self.assertEqual(moves, [("source", "target", "children", 0)])
        event.accept.assert_called_once_with()

    def test_logical_drop_index_ignores_projected_parameter_rows(self) -> None:
        parent = QTreeWidgetItem(["Device"])
        projected = QTreeWidgetItem(["ROI 1"])
        first = QTreeWidgetItem(["First"])
        second = QTreeWidgetItem(["Second"])
        first.setData(
            0,
            Qt.ItemDataRole.UserRole,
            RecipeNode("first", "comment"),
        )
        second.setData(
            0,
            Qt.ItemDataRole.UserRole,
            RecipeNode("second", "comment"),
        )
        parent.addChildren([projected, first, second])

        self.assertEqual(RecipeTreeWidget._logical_child_count(parent), 2)
        self.assertEqual(
            RecipeTreeWidget._logical_index(parent, second, below=False),
            1,
        )
        self.assertEqual(
            RecipeTreeWidget._logical_index(parent, second, below=True),
            2,
        )

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

    def test_anritsu_plan_dialog_uses_shared_spectrum_panel_and_selective_actions(self) -> None:
        dialog = AnritsuNodeEditorDialog(simulation_settings())
        try:
            self.assertIsInstance(
                dialog.configuration_panel, AnritsuSpectrumConfigurationPanel
            )
            self.assertIsInstance(
                dialog.advanced_panel, AnritsuAdvancedSpectrumPanel
            )
            self.assertTrue(dialog.plan_mode)
            self.assertFalse(dialog.hardware_actions_enabled)
            self.assertEqual(
                set(dialog.parameter_selectors),
                {
                    "spectrum.start_frequency",
                    "spectrum.stop_frequency",
                    "spectrum.reference_level",
                    "spectrum.points",
                    "advanced.rbw_mode",
                    "advanced.rbw",
                    "advanced.vbw_mode",
                    "advanced.vbw",
                    "advanced.detector",
                    "advanced.attenuation_mode",
                    "advanced.attenuation",
                    "advanced.preamplifier_enabled",
                    "advanced.sweep_time_mode",
                    "advanced.sweep_time",
                },
            )
            self.assertFalse(dialog.acquire_single.isChecked())
            self.assertFalse(dialog.acquire_single.isVisible())
            selector = dialog.parameter_selectors["spectrum.start_frequency"]
            selector.setCurrentIndex(selector.findData("sweep"))
            actions = dialog.planned_parameter_actions()
            self.assertEqual(actions[0]["parameter_id"], "spectrum.start_frequency")
            self.assertEqual(actions[0]["mode"], "sweep")
            self.assertEqual(actions[0]["value"], "1 MHz")
            self.assertTrue(dialog.open_roi_button.isEnabled())
        finally:
            dialog.close()

    def test_anritsu_acquisition_dialog_exposes_reference_and_storage_policy(self) -> None:
        node = parse_recipe_text(
            """\
schema_version: 1
name: acquisition-editor
root:
  id: spectrum
  type: acquire_spectrum
  trace: TRAC1
  reference_operation: difference_db
  store_raw: true
  store_processed: true
"""
        ).root
        dialog = AnritsuAcquisitionEditorDialog(node)
        try:
            self.assertEqual(
                dialog.reference_operation.currentData(), "difference_db"
            )
            self.assertTrue(dialog.store_raw.isChecked())
            self.assertTrue(dialog.store_processed.isChecked())
            dialog.average_count.setValue(7)
            dialog.reference_operation.setCurrentIndex(
                dialog.reference_operation.findData("ratio_linear")
            )
            dialog.store_raw.setChecked(False)
            self.assertEqual(
                dialog.node_fields(),
                {
                    "trace": "TRAC1",
                    "average_count": 7,
                    "reference_operation": "ratio_linear",
                    "store_raw": False,
                    "store_processed": True,
                },
            )
        finally:
            dialog.close()

    def test_anritsu_plan_dialog_stores_selected_advanced_spectrum_controls(self) -> None:
        dialog = AnritsuNodeEditorDialog(simulation_settings())
        try:
            dialog.advanced_panel.rbw_mode.setCurrentIndex(
                dialog.advanced_panel.rbw_mode.findData("manual")
            )
            dialog.advanced_panel.rbw.setText("3 kHz")
            dialog.advanced_panel.detector.setCurrentIndex(
                dialog.advanced_panel.detector.findData("RMS")
            )
            for parameter_id in ("advanced.rbw_mode", "advanced.rbw", "advanced.detector"):
                selector = dialog.parameter_selectors[parameter_id]
                selector.setCurrentIndex(selector.findData("set"))
            actions = {
                action["parameter_id"]: action
                for action in dialog.planned_parameter_actions()
            }
            self.assertEqual(actions["advanced.rbw_mode"]["value"], "manual")
            self.assertEqual(actions["advanced.rbw"]["value"], "3 kHz")
            self.assertEqual(actions["advanced.detector"]["value"], "RMS")
        finally:
            dialog.close()

    def test_anritsu_plan_dialog_blocks_unpaired_manual_advanced_value(self) -> None:
        dialog = AnritsuNodeEditorDialog(simulation_settings())
        try:
            dialog.advanced_panel.rbw_mode.setCurrentIndex(
                dialog.advanced_panel.rbw_mode.findData("manual")
            )
            selector = dialog.parameter_selectors["advanced.rbw_mode"]
            selector.setCurrentIndex(selector.findData("set"))
            with patch.object(QMessageBox, "warning") as warning:
                dialog.accept()
            warning.assert_called_once()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        finally:
            dialog.close()

    def test_anritsu_plan_dialog_stacks_panels_on_a_small_screen(self) -> None:
        dialog = AnritsuNodeEditorDialog(simulation_settings())
        try:
            self.assertLessEqual(dialog.minimumWidth(), 680)
            self.assertLessEqual(dialog.minimumHeight(), 480)
            dialog.resize(720, 560)
            dialog.show()
            self.application.processEvents()
            self.assertEqual(
                dialog.content_splitter.orientation(), Qt.Orientation.Vertical
            )
            dialog.resize(1180, 760)
            self.application.processEvents()
            self.assertEqual(
                dialog.content_splitter.orientation(), Qt.Orientation.Horizontal
            )
        finally:
            dialog.close()

    def test_roi_table_and_editor_follow_light_and_dark_fluent_theme(self) -> None:
        apply_application_theme(self.application, "light")
        dialog = SweepGeneratorDialog(
            {
                "device": "Keithley",
                "label": "Channel B · source current",
                "target": "keithley.B.current",
                "dimension": "current",
            }
        )
        try:
            self.assertIsInstance(dialog.segments, TableWidget)
            dialog.resize(900, 700)
            dialog.show()
            self.application.processEvents()
            light = dialog.plot.backgroundBrush().color().name()
            self.assertEqual(dialog.plot_theme, "light")
            apply_application_theme(self.application, "dark")
            self.application.processEvents()
            self.application.processEvents()
            dark = dialog.plot.backgroundBrush().color().name()
            self.assertEqual(dialog.plot_theme, "dark")
            self.assertNotEqual(light, dark)
            self.assertEqual(
                dialog.segments.palette().color(QPalette.ColorRole.Base).name(),
                tokens_for("dark").surface,
            )
            editor = dialog._roi_cell_delegate.createEditor(
                dialog.segments.viewport(), None, None
            )
            self.assertIsInstance(editor, LineEdit)
            self.assertIn("background: transparent", editor.styleSheet())
            self.assertIsInstance(dialog.create_button, PrimaryPushButton)
            self.assertIsInstance(dialog.cancel_button, PushButton)
        finally:
            dialog.close()
            apply_application_theme(self.application, "light")

    def test_rigol_frequency_roi_shows_and_enforces_effective_hardware_limit(self) -> None:
        page = RecipePage(simulation_settings())
        dialog = SweepGeneratorDialog(
            {
                "device": "Rigol",
                "label": "Channel 1 · frequency",
                "target": "rigol.1.frequency",
                "dimension": "frequency",
            },
            page,
            initial_segments=[
                {"start": "1 MHz", "stop": "30 MHz", "points": 10}
            ],
        )
        try:
            dialog.resize(1180, 700)
            dialog.show()
            self.application.processEvents()
            self.assertTrue(dialog.safety_limits.isVisibleTo(dialog))
            self.assertIn("MIN 1 Hz", dialog.safety_limits.text())
            self.assertIn("MAX 30 MHz", dialog.safety_limits.text())
            self.assertTrue(dialog.create_button.isEnabled())

            for forbidden in ("100 MHz", "1 GHz"):
                dialog.segments.item(0, 1).setText(forbidden)
                self.application.processEvents()
                self.assertFalse(dialog.create_button.isEnabled())
                self.assertIn("outside the allowed range", dialog.preview.text())
                with patch.object(QMessageBox, "warning") as warning:
                    dialog.accept()
                warning.assert_called_once()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        finally:
            dialog.close()
            page.close()

    def test_roi_plot_uses_active_application_theme_not_windows_theme(self) -> None:
        previous = self.application.property("activeTheme")
        self.application.setProperty("activeTheme", "light")
        dialog = SweepGeneratorDialog(
            {
                "device": "Keithley",
                "label": "Channel B · source current",
                "target": "keithley.B.current",
                "dimension": "current",
            }
        )
        try:
            self.assertEqual(dialog.plot_theme, "light")
            self.assertEqual(dialog.plot.backgroundBrush().color().name(), "#ffffff")
            dialog._set_plot_theme("dark")
            self.assertEqual(dialog.plot_theme, "dark")
            self.assertEqual(
                dialog.plot.backgroundBrush().color().name(),
                tokens_for("dark").plot_background,
            )
        finally:
            dialog.close()
            self.application.setProperty("activeTheme", previous)

    def test_anritsu_library_device_is_configuration_without_implicit_acquisition(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page._library_add_device("anritsu")
            node = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            self.assertEqual(node.type, "sequence")
            self.assertEqual(node.data["device_module"], "anritsu")
            self.assertFalse(node.data["acquire_single"])
            self.assertEqual(node.children, ())
            item = page._find_tree_item(node.id)
            page._node_selected(item, None)
            self.assertTrue(page.open_editor_button.isEnabled())
        finally:
            page.close()

    def test_acquire_once_is_a_separate_draggable_library_block(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            acquire = next(
                button
                for button in page._library_action_buttons
                if button.text() == "Acquire spectrum once"
            )
            self.assertEqual(acquire.drag_kind, "flow:acquire_spectrum")
            page._library_add_device("keithley")
            keithley = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            page._drop_library_block(
                "flow:acquire_spectrum",
                keithley.id,
                "children",
                len(keithley.children),
            )
            updated = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            self.assertEqual(updated.children[-1].type, "acquire_spectrum")
            self.assertEqual(updated.children[-1].data["trace"], "TRAC1")
        finally:
            page.close()

    def test_flow_drop_preserves_requested_branch_and_insertion_index(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page._library_add_basic("if")
            conditional = parse_recipe_text(page.editor.toPlainText()).root.children[-1]

            page._drop_library_block("flow:wait", conditional.id, "else", 0)

            updated = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            self.assertEqual(updated.else_children[0].type, "wait")
            self.assertEqual(updated.else_children[1].type, "comment")

            page._drop_library_block("flow:wait", updated.id, "children", 0)
            updated = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            self.assertEqual(updated.children[0].type, "wait")
            self.assertEqual(updated.children[1].type, "comment")
        finally:
            page.close()

    def test_editing_anritsu_configuration_preserves_separate_acquisition_child(self) -> None:
        recipe = parse_recipe_text(
            """\
schema_version: 1
name: preserve-acquisition
root:
  id: anritsu-module
  type: sequence
  device_module: anritsu
  operation: configure_selected_parameters
  children:
    - id: spectrum
      type: acquire_spectrum
      trace: TRAC2
"""
        )
        replacement = RecipePage._configured_anritsu_node(
            recipe.root,
            parameter_actions=[],
            acquire_single=False,
            trace="TRAC1",
        )

        self.assertEqual(len(replacement["children"]), 1)
        self.assertEqual(replacement["children"][0]["id"], "spectrum")
        self.assertEqual(replacement["children"][0]["trace"], "TRAC2")

    def test_rigol_module_exposes_direct_channel_output_policy(self) -> None:
        page = RecipePage(simulation_settings())
        dialog = RigolNodeEditorDialog()
        try:
            page._library_add_device("rigol")
            original = parse_recipe_text(page.editor.toPlainText()).root.children[-1]
            dialog.channel.setCurrentIndex(dialog.channel.findData(2))
            dialog.output_polarity.setCurrentText("INV")
            dialog.output_mode.setCurrentText("GAT")
            dialog.sync_enabled.setChecked(True)
            dialog.sync_delay.setText("25 ms")
            dialog.output_policy.setCurrentIndex(
                dialog.output_policy.findData("on")
            )
            replacement = page._configured_rigol_node(
                original,
                snapshot=dialog.configuration_snapshot(),
                output_policy=dialog.selected_output_policy(),
            )
            self.assertEqual(replacement["channel"], 2)
            self.assertEqual(replacement["output_policy"], "on")
            self.assertFalse(replacement["configuration_required"])
            self.assertIn("configuration", replacement)
            self.assertEqual(
                replacement["configuration"]["output_polarity"], "INV"
            )
            self.assertEqual(replacement["configuration"]["output_mode"], "GAT")
            self.assertTrue(replacement["configuration"]["sync_enabled"])
            self.assertEqual(replacement["configuration"]["sync_delay"], "25 ms")
            self.assertEqual(
                {
                    action["parameter_id"]
                    for action in replacement["parameter_actions"]
                },
                {
                    "carrier.frequency",
                    "carrier.high_level",
                    "carrier.low_level",
                },
            )
            self.assertEqual(replacement["children"], [])
        finally:
            dialog.close()
            page.close()

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
            self.assertEqual(
                replacement["configuration"]["source_level"],
                "2 mA",
            )
            self.assertEqual(
                replacement["configuration"]["compliance"],
                "50 mV",
            )
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

    def test_clicking_roi_stage_routes_directly_to_roi_editor_metadata(self) -> None:
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
                            {"start": "1 mA", "stop": "0 A", "points": 3, "spacing": "linear"},
                        ],
                    }
                ],
            )
            from app.recipes import replace_recipe_node

            page._apply_builder_source(
                replace_recipe_node(
                    page.editor.toPlainText(), node_id=original.id, node=replacement
                ),
                "Configured clickable ROI",
            )
            owner = page._find_tree_item(original.id)
            sweep_row = next(
                owner.child(index)
                for index in range(owner.childCount())
                if owner.child(index).text(0) == "Source current"
            )
            roi_two = sweep_row.child(1)
            metadata = roi_two.data(0, page.operator_row_role)
            self.assertEqual(metadata["kind"], "roi_stage")
            self.assertEqual(metadata["owner_node_id"], original.id)
            self.assertEqual(metadata["parameter_id"], "source.level")
            self.assertEqual(metadata["stage_index"], 1)
            self.assertTrue(roi_two.flags() & Qt.ItemFlag.ItemIsSelectable)
            page._node_selected(roi_two, None)
            self.assertTrue(page.open_editor_button.isEnabled())
            self.assertIn("ROI 2", page.inspector_summary.text())

            captured: list[dict[str, object]] = []
            page._edit_keithley_roi_from_tree = captured.append  # type: ignore[method-assign]
            page._operator_row_clicked(roi_two, 0)
            self.assertEqual(captured, [metadata])
        finally:
            dialog.close()
            page.close()

    def test_direct_roi_save_changes_only_selected_segments(self) -> None:
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
                            {"start": "0 A", "stop": "1 mA", "points": 3, "spacing": "linear"}
                        ],
                    },
                    {
                        "parameter_id": "source.compliance",
                        "mode": "set",
                        "value": "67 mV",
                    },
                ],
                output_policy="on",
            )
            from app.recipes import replace_recipe_node

            page._apply_builder_source(
                replace_recipe_node(
                    page.editor.toPlainText(), node_id=original.id, node=replacement
                ),
                "Configured direct ROI save",
            )
            node = page._find_tree_item(original.id).data(
                0, Qt.ItemDataRole.UserRole
            )
            page._apply_keithley_roi_segments(
                node,
                "source.level",
                [{"value": "250 mA"}],
            )
            updated = next(
                item
                for item in parse_recipe_text(page.editor.toPlainText()).root.children
                if item.id == original.id
            )
            actions = updated.data["parameter_actions"]
            self.assertEqual(actions[0]["segments"], [{"value": "250 mA"}])
            self.assertEqual(actions[1]["value"], "67 mV")
            self.assertEqual(updated.data["output_policy"], "on")
            self.assertEqual(
                tuple(child.id for child in updated.children),
                tuple(child.id for child in original.children),
            )
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
            self.assertEqual(len(shortcuts), 7)
            self.assertTrue(any(value in shortcuts for value in {"Delete", "Del"}))
            self.assertTrue(any("Ctrl+D" in value for value in shortcuts))
            self.assertTrue(any("Alt+Up" in value for value in shortcuts))
            self.assertTrue(any("Alt+Down" in value for value in shortcuts))
            self.assertIn("Return", shortcuts)
            self.assertIn("Enter", shortcuts)
        finally:
            page.close()

    def test_node_library_filters_actions_and_adds_a_tree_node(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            self.assertEqual(len(page._library_action_buttons), 19)
            page.library_search.setText("spectrum analyzer")
            visible = [button.text() for button in page._library_action_buttons if not button.isHidden()]
            self.assertEqual(
                visible,
                ["Anritsu configuration", "Anritsu SG OFF"],
            )
            page.library_search.clear()
            page._library_add_basic("wait")
            recipe = parse_recipe_text(page.editor.toPlainText())
            self.assertEqual(recipe.root.children[-1].type, "wait")
        finally:
            page.close()

    def test_sweeps_reads_anritsu_sg_values_from_manual_module(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page.set_anritsu_sg_snapshot_provider(
                lambda: SignalGeneratorSnapshot(
                    frequency_hz=2.45e9,
                    power_dbm=-17.5,
                    output_enabled=True,
                    instrument_mode="SG",
                )
            )
            snapshot = page._current_anritsu_sg_snapshot()
            self.assertEqual(snapshot.frequency_hz, 2.45e9)
            self.assertEqual(snapshot.power_dbm, -17.5)
            self.assertTrue(snapshot.output_enabled)
        finally:
            page.close()

    def test_anritsu_sg_library_node_has_separate_editor_and_rf_off_contract(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            page._library_add_device("anritsu_sg")
            recipe = parse_recipe_text(page.editor.toPlainText())
            node = recipe.root.children[-1]
            self.assertEqual(node.data["device_module"], "anritsu_sg")
            self.assertTrue(node.data["configuration_required"])

            dialog = AnritsuSignalGeneratorNodeEditorDialog(
                frequency="1 GHz",
                power="-30 dBm",
                parameter_actions=[
                    {
                        "parameter_id": "sg.power",
                        "mode": "set",
                        "value": "-40 dBm",
                    }
                ],
            )
            try:
                self.assertEqual(dialog.power.text(), "-40 dBm")
                replacement = page._configured_anritsu_sg_node(
                    node,
                    frequency=dialog.frequency.text(),
                    power=dialog.power.text(),
                    parameter_actions=dialog.planned_parameter_actions(),
                )
                self.assertEqual(replacement["device_module"], "anritsu_sg")
                self.assertFalse(replacement["configuration_required"])
                self.assertIn("RF OFF", replacement["text"])
                self.assertEqual(
                    replacement["configuration"]["power"], "-40 dBm"
                )
            finally:
                dialog.close()
        finally:
            page.close()

    def test_tree_builder_undo_and_redo_restore_recipe_structure(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            initial = page.editor.toPlainText()
            page._library_add_basic("wait")
            changed = page.editor.toPlainText()
            self.assertNotEqual(changed, initial)
            self.assertTrue(page.undo_tree_action.isEnabled())
            page.undo_tree_edit()
            self.assertEqual(page.editor.toPlainText(), initial)
            self.assertTrue(page.redo_tree_action.isEnabled())
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

    def test_roi_dialog_supports_single_value_stage(self) -> None:
        dialog = SweepGeneratorDialog(
            {
                "device": "Keithley",
                "label": "Channel B · source current",
                "target": "keithley.B.current",
                "dimension": "current",
            }
        )
        try:
            method = dialog.segments.cellWidget(0, 2)
            method.setCurrentText("Single value")
            dialog.segments.item(0, 0).setText("250 mA")
            self.assertEqual(dialog.segment_data(), [{"value": "250 mA"}])
            self.assertFalse(dialog.segments.item(0, 1).flags() & Qt.ItemFlag.ItemIsEditable)
            self.assertFalse(dialog.segments.cellWidget(0, 4).isEnabled())
            self.assertIn("Generated 1", dialog.preview.text())
        finally:
            dialog.close()

    def test_roi_plot_connects_descending_stage_to_shared_boundary(self) -> None:
        dialog = SweepGeneratorDialog(
            {
                "device": "Keithley",
                "label": "Channel B · source current",
                "target": "keithley.B.current",
                "dimension": "current",
            },
            initial_segments=[
                {"start": "0 A", "stop": "1 A", "points": 3, "spacing": "linear"},
                {"start": "1 A", "stop": "0 A", "points": 3, "spacing": "linear"},
            ],
        )
        try:
            curves = dialog.plot.listDataItems()
            self.assertEqual(len(curves), 2)
            self.assertEqual(tuple(curves[0].xData), (0, 1, 2))
            self.assertEqual(tuple(curves[0].yData), (0.0, 0.5, 1.0))
            self.assertEqual(tuple(curves[1].xData), (2, 3, 4))
            self.assertEqual(tuple(curves[1].yData), (1.0, 0.5, 0.0))
            dialog.select_interval(1)
            self.assertEqual(dialog.segments.currentRow(), 1)
        finally:
            dialog.close()

    def test_roi_text_editor_is_borderless_and_fills_the_cell(self) -> None:
        dialog = SweepGeneratorDialog(
            {
                "device": "Keithley",
                "label": "Channel B · source current",
                "target": "keithley.B.current",
                "dimension": "current",
            }
        )
        try:
            delegate = dialog.segments.itemDelegateForColumn(0)
            self.assertEqual(delegate.objectName(), "seamlessRoiCellDelegate")
            editor = delegate.createEditor(dialog.segments, None, None)
            self.assertFalse(editor.hasFrame())
            self.assertIn("border: none", editor.styleSheet())
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
            expected = tokens_for(dialog.plot_theme).plot_background
            self.assertEqual(background, expected)
        finally:
            dialog.close()

    def test_sweep_dialogs_use_fluent_interactive_controls(self) -> None:
        sweep = SweepGeneratorDialog(
            {
                "device": "Keithley",
                "label": "Channel B · source current",
                "target": "keithley.B.current",
                "dimension": "current",
            }
        )
        fixed = FixedValueDialog(
            {
                "device": "Keithley",
                "label": "Channel B · source current",
                "target": "keithley.B.current",
                "dimension": "current",
            }
        )
        picker = DeviceParameterDialog()
        comment = CommentEditorDialog("Operator note")
        keithley = KeithleyNodeEditorDialog(simulation_settings())
        rigol = RigolNodeEditorDialog(settings=simulation_settings())
        anritsu = AnritsuNodeEditorDialog(simulation_settings())
        anritsu_sg = AnritsuSignalGeneratorNodeEditorDialog()
        dialogs = (sweep, fixed, picker, comment, keithley, rigol, anritsu, anritsu_sg)
        try:
            self.assertIsInstance(sweep.segments, TableWidget)
            self.assertIsInstance(sweep.create_button, PrimaryPushButton)
            self.assertIsInstance(fixed.value, LineEdit)
            self.assertIsInstance(fixed.create_button, PrimaryPushButton)
            self.assertIsInstance(picker.device, ComboBox)
            self.assertIsInstance(picker.open_button, PrimaryPushButton)
            self.assertIsInstance(comment.editor, PlainTextEdit)
            self.assertIsInstance(comment.save_button, PrimaryPushButton)
            self.assertIsInstance(keithley.open_roi_button, PrimaryPushButton)
            self.assertIsInstance(keithley.configuration_panel.channel, ComboBox)
            self.assertIsInstance(keithley.configuration_panel.level, LineEdit)
            self.assertIsInstance(rigol.open_roi_button, PrimaryPushButton)
            self.assertIsInstance(anritsu.open_roi_button, PrimaryPushButton)
            self.assertIsInstance(anritsu.configuration_panel.frequency_representation, ComboBox)
            self.assertIsInstance(anritsu.advanced_panel.attenuation, SpinBox)
            self.assertIsInstance(anritsu_sg.open_roi_button, PrimaryPushButton)
            for dialog in dialogs:
                self.assertEqual(dialog.property("stationSurface"), "page")
                self.assertFalse(dialog.findChildren(QDialogButtonBox))
        finally:
            for dialog in dialogs:
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
            page._add_basic_node("sequence")
            parsed = parse_recipe_text(page.editor.toPlainText())
            self.assertIn("sequence", tuple(node.type for node in parsed.root.children))

            finally_item = next(
                page.tree.topLevelItem(index)
                for index in range(page.tree.topLevelItemCount())
                if page.tree.topLevelItem(index).text(0).startswith("Finally")
            )
            page.tree.setCurrentItem(finally_item)
            page._add_basic_node("set_rigol_output")
            parsed = parse_recipe_text(page.editor.toPlainText())
            self.assertEqual(parsed.finally_nodes[-1].type, "set_rigol_output")
            self.assertFalse(parsed.finally_nodes[-1].data["enabled"])
        finally:
            page.close()

    def test_empty_finally_is_visible_and_library_adds_explicit_safe_outputs(self) -> None:
        page = RecipePage(simulation_settings())
        try:
            recipe = parse_recipe_text(
                """\
schema_version: 1
name: empty-finally
root: {id: root, type: sequence, children: []}
finally: []
"""
            )
            page.editor.setPlainText(recipe.source_text)
            page._populate_recipe_tree(recipe.root, recipe.finally_nodes, None)
            finally_item = page.tree.topLevelItem(page.tree.topLevelItemCount() - 1)
            self.assertTrue(finally_item.text(0).startswith("Finally"))
            self.assertIn("Empty", finally_item.text(1))

            page._library_add_keithley_shutdown("A")
            page._library_add_output_off("set_rigol_output", channel=2)
            page._library_add_output_off("set_anritsu_sg_output")
            parsed = parse_recipe_text(page.editor.toPlainText())
            self.assertEqual(
                [
                    (node.type, node.data.get("channel"), node.data.get("enabled"))
                    for node in parsed.finally_nodes
                ],
                [
                    ("ramp_keithley_to_zero", "A", None),
                    ("set_keithley_output", "A", False),
                    ("set_rigol_output", 2, False),
                    ("set_anritsu_sg_output", None, False),
                ],
            )
            labels = {button.text() for button in page._library_action_buttons}
            self.assertIn("Keithley A ramp to zero + OFF", labels)
            self.assertIn("Rigol CH2 OUTPUT OFF", labels)
            self.assertIn("Anritsu SG RF OFF", labels)
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
