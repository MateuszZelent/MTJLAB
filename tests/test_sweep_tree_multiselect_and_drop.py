from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, QPoint, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QAbstractItemView

from app.recipes import parse_recipe_text
from app.recipes.semantic_tree import SemanticNodeKind
from app.ui.measurement_tree import (
    MeasurementTreeLibraryDropRequest,
    MeasurementTreeMoveRequest,
    TreeDropPlacement,
)
from app.ui.recipes.page import RecipePage
from tests.helpers import simulation_settings


SWEEP_RECIPE_FIXTURE = """\
schema_version: 1
name: sweep-loop-test
root:
  id: sequence-main
  type: sequence
  children:
    - id: rigol-sweep
      type: sequence
      device_module: rigol
      operation: configure_selected_parameters
      configuration:
        channel: 1
        waveform: SIN
        frequency: 1 MHz
        high_level: 1 V
        low_level: 0 V
      parameter_actions:
        - parameter_id: carrier.frequency
          mode: sweep
          segments:
            - {start: 1 MHz, stop: 5 MHz, points: 5}
      children: []
    - id: wait-outside-1
      type: wait
      duration: 10 ms
    - id: wait-outside-2
      type: wait
      duration: 20 ms
"""


class SweepTreeMultiSelectAndDropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = RecipePage(simulation_settings())
        self.page._apply_builder_source(
            SWEEP_RECIPE_FIXTURE, "Loaded sweep fixture"
        )
        self.application.processEvents()

    def tearDown(self) -> None:
        self.page._close_discard_confirmed = True
        self.page.close()

    def _find_semantic_nodes(self) -> tuple[str, str, str]:
        """Return semantic IDs for (axis_id, loop_body_id, set_roi_id)."""
        tree = self.page.tree_model.tree
        loop_body_id = None
        set_roi_id = None
        axis_id = None
        for node in tree.by_id.values():
            if node.kind is SemanticNodeKind.SWEEP_AXIS:
                axis_id = node.semantic_id
            elif node.kind is SemanticNodeKind.LOOP_BODY:
                loop_body_id = node.semantic_id
            elif node.kind is SemanticNodeKind.SET_ROI_VALUE:
                set_roi_id = node.semantic_id
        self.assertIsNotNone(loop_body_id, "LOOP_BODY semantic node must exist")
        self.assertIsNotNone(set_roi_id, "SET_ROI_VALUE semantic node must exist")
        return axis_id, loop_body_id, set_roi_id

    def test_tree_selection_mode_is_extended_selection(self) -> None:
        self.assertEqual(
            self.page.measurement_tree.selectionMode(),
            QAbstractItemView.SelectionMode.ExtendedSelection,
        )

    def test_selected_semantic_ids_returns_all_selected_rows_in_order(self) -> None:
        tree_view = self.page.measurement_tree
        model = self.page.tree_model
        sel_model = tree_view.selectionModel()

        idx1 = model.index_for_semantic_id("wait-outside-1")
        idx2 = model.index_for_semantic_id("wait-outside-2")
        self.assertTrue(idx1.isValid())
        self.assertTrue(idx2.isValid())

        sel_model.clearSelection()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        sel_model.select(idx1, flags)
        sel_model.select(idx2, flags)

        selected_ids = tree_view.selected_semantic_ids()
        self.assertEqual(selected_ids, ["wait-outside-1", "wait-outside-2"])

    def test_start_drag_serializes_multiple_selected_nodes_to_mime_json(self) -> None:
        tree_view = self.page.measurement_tree
        model = self.page.tree_model
        sel_model = tree_view.selectionModel()

        idx1 = model.index_for_semantic_id("wait-outside-1")
        idx2 = model.index_for_semantic_id("wait-outside-2")
        sel_model.clearSelection()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        sel_model.select(idx1, flags)
        sel_model.select(idx2, flags)

        captured_mime = []

        def mock_exec(drag_self, actions):
            captured_mime.append(drag_self.mimeData())
            return Qt.DropAction.IgnoreAction

        with patch("PySide6.QtGui.QDrag.exec", mock_exec):
            tree_view.startDrag(Qt.DropAction.MoveAction)

        self.assertEqual(len(captured_mime), 1)
        mime = captured_mime[0]
        self.assertTrue(mime.hasFormat(tree_view._semantic_mime_type))
        payload = bytes(mime.data(tree_view._semantic_mime_type)).decode("utf-8")
        parsed = json.loads(payload)
        self.assertEqual(parsed, ["wait-outside-1", "wait-outside-2"])

    def test_measurement_tree_model_supported_drag_and_drop_actions(self) -> None:
        model = self.page.tree_model
        self.assertTrue(bool(model.supportedDragActions() & Qt.DropAction.MoveAction))
        self.assertTrue(bool(model.supportedDropActions() & Qt.DropAction.MoveAction))

    def test_start_drag_defaults_to_move_action_when_copy_action_passed(self) -> None:
        tree_view = self.page.measurement_tree
        model = self.page.tree_model
        idx = model.index_for_semantic_id("wait-outside-1")
        tree_view.setCurrentIndex(idx)

        captured_actions = []

        def mock_exec(drag_self, actions):
            captured_actions.append(actions)
            return Qt.DropAction.IgnoreAction

        with patch("PySide6.QtGui.QDrag.exec", mock_exec):
            # Qt's default QAbstractItemView passes supportedDragActions() which is often CopyAction
            tree_view.startDrag(Qt.DropAction.CopyAction)

        self.assertEqual(captured_actions, [Qt.DropAction.MoveAction])

    def test_mouse_drag_gesture_triggers_qdrag_exec_with_multiple_selected_items(self) -> None:
        tree_view = self.page.measurement_tree
        model = self.page.tree_model
        sel_model = tree_view.selectionModel()

        self.page.resize(1200, 900)
        self.page.workspace_splitter.setSizes([200, 600, 200])
        self.page.show()
        self.application.processEvents()

        idx1 = model.index_for_semantic_id("wait-outside-1")
        idx2 = model.index_for_semantic_id("wait-outside-2")
        tree_view.scrollTo(idx1)
        self.application.processEvents()

        sel_model.clearSelection()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        sel_model.select(idx1, flags)
        sel_model.select(idx2, flags)

        rect1 = tree_view.visualRect(idx1)
        self.assertTrue(rect1.isValid())
        center_pos = rect1.center()
        self.assertTrue(tree_view.viewport().rect().contains(center_pos))

        exec_calls = []

        def mock_exec(drag_self, actions):
            payload = bytes(drag_self.mimeData().data(tree_view._semantic_mime_type)).decode("utf-8")
            exec_calls.append((actions, json.loads(payload)))
            return Qt.DropAction.IgnoreAction

        start_drag_invocations = []
        orig_start = tree_view.startDrag

        def spy_start(actions):
            start_drag_invocations.append(actions)
            return orig_start(actions)

        tree_view.startDrag = spy_start

        with patch("PySide6.QtGui.QDrag.exec", mock_exec):
            press_ev = QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                center_pos,
                center_pos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            move_ev = QMouseEvent(
                QMouseEvent.Type.MouseMove,
                center_pos + QPoint(40, 40),
                center_pos + QPoint(40, 40),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            tree_view.mousePressEvent(press_ev)
            tree_view.mouseMoveEvent(move_ev)

        self.assertEqual(len(exec_calls), 1)
        actions, selected_in_drag = exec_calls[0]
        self.assertEqual(actions, Qt.DropAction.MoveAction)
        self.assertEqual(selected_in_drag, ["wait-outside-1", "wait-outside-2"])

    def test_library_default_destination_when_loop_body_selected(self) -> None:
        _, loop_body_id, _ = self._find_semantic_nodes()
        index = self.page.tree_model.index_for_semantic_id(loop_body_id)
        self.page.measurement_tree.setCurrentIndex(index)
        self.application.processEvents()

        dest_parent, dest_branch, dest_index = self.page._library_default_destination()
        self.assertEqual(dest_parent, "rigol-sweep")
        self.assertEqual(dest_branch, "children")
        self.assertEqual(dest_index, 0)  # rigol-sweep currently has 0 children

    def test_library_action_adds_inside_sweep_loop_body(self) -> None:
        _, loop_body_id, _ = self._find_semantic_nodes()
        index = self.page.tree_model.index_for_semantic_id(loop_body_id)
        self.page.measurement_tree.setCurrentIndex(index)
        self.application.processEvents()

        # Add a wait block using the library default destination
        self.page._add_basic_node(
            "wait",
            parent_id="rigol-sweep",
            branch="children",
            insert_index=0,
        )

        recipe = parse_recipe_text(self.page.editor.toPlainText())
        sweep_node = recipe.root.children[0]
        self.assertEqual(sweep_node.id, "rigol-sweep")
        self.assertEqual(len(sweep_node.children), 1)
        self.assertEqual(sweep_node.children[0].type, "wait")
        # Root children should still have sweep and the 2 outside waits
        self.assertEqual(len(recipe.root.children), 3)

    def test_library_default_destination_when_set_roi_value_selected(self) -> None:
        _, _, set_roi_id = self._find_semantic_nodes()
        index = self.page.tree_model.index_for_semantic_id(set_roi_id)
        self.page.measurement_tree.setCurrentIndex(index)
        self.application.processEvents()

        dest_parent, dest_branch, dest_index = self.page._library_default_destination()
        self.assertEqual(dest_parent, "rigol-sweep")
        self.assertEqual(dest_branch, "children")
        self.assertEqual(dest_index, 0)

    def test_drag_and_drop_single_node_into_sweep_loop(self) -> None:
        _, loop_body_id, _ = self._find_semantic_nodes()
        request = MeasurementTreeMoveRequest(
            source_semantic_id="wait-outside-1",
            destination_semantic_id=loop_body_id,
            placement=TreeDropPlacement.INSIDE,
        )
        self.page._handle_semantic_tree_move_request(request)

        self.assertTrue(request.accepted)
        recipe = parse_recipe_text(self.page.editor.toPlainText())
        sweep_node = recipe.root.children[0]
        self.assertEqual(sweep_node.id, "rigol-sweep")
        self.assertEqual([node.id for node in sweep_node.children], ["wait-outside-1"])
        self.assertEqual([node.id for node in recipe.root.children], ["rigol-sweep", "wait-outside-2"])

    def test_drag_and_drop_multiple_nodes_into_sweep_loop(self) -> None:
        _, loop_body_id, _ = self._find_semantic_nodes()
        request = MeasurementTreeMoveRequest(
            source_semantic_id="wait-outside-1",
            destination_semantic_id=loop_body_id,
            placement=TreeDropPlacement.INSIDE,
            source_semantic_ids=("wait-outside-1", "wait-outside-2"),
        )
        self.page._handle_semantic_tree_move_request(request)

        self.assertTrue(request.accepted)
        recipe = parse_recipe_text(self.page.editor.toPlainText())
        sweep_node = recipe.root.children[0]
        self.assertEqual(sweep_node.id, "rigol-sweep")
        self.assertEqual(
            [node.id for node in sweep_node.children],
            ["wait-outside-1", "wait-outside-2"],
        )
        self.assertEqual([node.id for node in recipe.root.children], ["rigol-sweep"])

    def test_drag_and_drop_onto_set_roi_value_inserts_at_beginning_of_loop(self) -> None:
        # First put a child inside rigol-sweep
        self.page._add_basic_node(
            "wait",
            parent_id="rigol-sweep",
            branch="children",
            insert_index=0,
        )
        recipe = parse_recipe_text(self.page.editor.toPlainText())
        first_child_id = recipe.root.children[0].children[0].id

        _, _, set_roi_id = self._find_semantic_nodes()
        request = MeasurementTreeMoveRequest(
            source_semantic_id="wait-outside-1",
            destination_semantic_id=set_roi_id,
            placement=TreeDropPlacement.AFTER,
        )
        self.page._handle_semantic_tree_move_request(request)

        self.assertTrue(request.accepted)
        recipe = parse_recipe_text(self.page.editor.toPlainText())
        sweep_node = recipe.root.children[0]
        # wait-outside-1 should be at index 0 before first_child_id
        self.assertEqual(
            [node.id for node in sweep_node.children],
            ["wait-outside-1", first_child_id],
        )

    def test_library_drop_into_sweep_loop_body(self) -> None:
        _, loop_body_id, _ = self._find_semantic_nodes()
        request = MeasurementTreeLibraryDropRequest(
            drag_kind="flow:wait",
            destination_semantic_id=loop_body_id,
            placement=TreeDropPlacement.INSIDE,
        )
        self.page._handle_semantic_library_drop_request(request)

        self.assertTrue(request.accepted)
        recipe = parse_recipe_text(self.page.editor.toPlainText())
        sweep_node = recipe.root.children[0]
        self.assertEqual(len(sweep_node.children), 1)
        self.assertEqual(sweep_node.children[0].type, "wait")

    def test_library_drop_onto_set_roi_value(self) -> None:
        _, _, set_roi_id = self._find_semantic_nodes()
        request = MeasurementTreeLibraryDropRequest(
            drag_kind="flow:wait",
            destination_semantic_id=set_roi_id,
            placement=TreeDropPlacement.AFTER,
        )
        self.page._handle_semantic_library_drop_request(request)

        self.assertTrue(request.accepted)
        recipe = parse_recipe_text(self.page.editor.toPlainText())
        sweep_node = recipe.root.children[0]
        self.assertEqual(len(sweep_node.children), 1)
        self.assertEqual(sweep_node.children[0].type, "wait")

    def test_delete_selected_nodes_deletes_multiple_nodes(self) -> None:
        tree_view = self.page.measurement_tree
        model = self.page.tree_model
        sel_model = tree_view.selectionModel()

        idx1 = model.index_for_semantic_id("wait-outside-1")
        idx2 = model.index_for_semantic_id("wait-outside-2")
        sel_model.clearSelection()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        sel_model.select(idx1, flags)
        sel_model.select(idx2, flags)

        self.page._delete_selected_node()

        recipe = parse_recipe_text(self.page.editor.toPlainText())
        self.assertEqual([node.id for node in recipe.root.children], ["rigol-sweep"])

    def test_multi_selection_updates_delete_button_enablement(self) -> None:
        tree_view = self.page.measurement_tree
        model = self.page.tree_model
        sel_model = tree_view.selectionModel()

        idx1 = model.index_for_semantic_id("wait-outside-1")
        idx2 = model.index_for_semantic_id("wait-outside-2")
        sel_model.clearSelection()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        sel_model.select(idx1, flags)
        sel_model.select(idx2, flags)

        # Trigger selection change handler
        self.page._node_selected("wait-outside-2")
        self.application.processEvents()

        self.assertTrue(self.page.delete_node_button.isEnabled())
        # Buttons that require a single node should be disabled for multi-selection
        self.assertFalse(self.page.duplicate_node_button.isEnabled())
        self.assertFalse(self.page.move_up_button.isEnabled())
        self.assertFalse(self.page.move_down_button.isEnabled())
