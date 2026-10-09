"""Repeat edits preserve authored children and remain atomic and undoable."""
import pytest
from PySide6.QtWidgets import QApplication

from app.domain.errors import ConfigurationError
from app.recipes import delete_recipe_nodes, move_recipe_node, move_recipe_nodes, parse_recipe_text, unwrap_recipe_repeat
from app.ui.recipes.page import RecipePage
from tests.helpers import simulation_settings

SOURCE = """schema_version: 1
name: Repeat editing
root:
  id: main
  type: sequence
  children:
    - {id: before, type: wait, duration: 1 ms}
    - id: loop
      type: repeat
      count: 3
      children:
        - {id: a, type: wait, duration: 2 ms} # Keep this comment
        - {id: b, type: wait, duration: 3 ms}
    - {id: after, type: wait, duration: 4 ms}
finally: []
"""


def test_wrap_multiple_selected_siblings_and_undo(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from PySide6.QtCore import QItemSelectionModel
    from PySide6.QtWidgets import QDialog
    app = QApplication.instance() or QApplication([])
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "recipe.yml"))
    try:
        page._apply_builder_source(SOURCE, "test")
        page._select_source_node("a")
        page.measurement_tree.selectionModel().select(page.tree_model.index_for_semantic_id("b"),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        app.processEvents()
        assert set(page._repeat_selected_ids()) == {"a", "b"}
        assert page.wrap_repeat_button.isEnabled()
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.count.value.return_value = 2
        monkeypatch.setattr("app.ui.recipes.page.RepeatCountDialog", Mock(return_value=dialog))
        page.wrap_repeat_button.click()
        loop = parse_recipe_text(page._builder_source()).root.children[1]
        assert len(loop.children) == 1
        assert loop.children[0].type == "repeat"
        assert [node.id for node in loop.children[0].children] == ["a", "b"]
        assert "Keep this comment" in page._builder_source()
        page.undo_tree_edit()
        assert len(parse_recipe_text(page._builder_source()).root.children[1].children) == 2
    finally:
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        app.processEvents()


def ids(source):
    return [node.id for node in parse_recipe_text(source).root.children]


def test_unwrap_preserves_order_ids_values_comments_and_disabled_state():
    result = unwrap_recipe_repeat(SOURCE, node_id="loop")
    assert ids(result) == ["before", "a", "b", "after"]
    assert "Keep this comment" in result
    assert parse_recipe_text(result).root.children[1].data["duration"] == "2 ms"
    disabled = unwrap_recipe_repeat(SOURCE.replace("count: 3", "count: 3\n      disabled: true"), node_id="loop")
    assert all(node.data["disabled"] for node in parse_recipe_text(disabled).root.children[1:3])


def test_root_repeat_becomes_sequence_without_losing_children():
    source = "schema_version: 1\nname: root\nroot: {id: root, type: repeat, count: 2, children: [{id: a, type: wait, duration: 1 ms}]}"
    root = parse_recipe_text(unwrap_recipe_repeat(source, node_id="root")).root
    assert root.id == "root" and root.type == "sequence"
    assert root.children[0].id == "a" and "count" not in root.data


def test_moving_contents_out_removes_only_the_empty_repeat():
    result = move_recipe_node(SOURCE, node_id="a", destination_parent_id="main", destination_branch="children", destination_index=1)
    assert ids(result) == ["before", "a", "loop", "after"]
    result = move_recipe_node(result, node_id="b", destination_parent_id="main", destination_branch="children", destination_index=3)
    assert ids(result) == ["before", "a", "b", "after"]


def test_delete_parent_and_child_once_and_remove_empty_repeat():
    assert ids(delete_recipe_nodes(SOURCE, node_ids=("loop", "a"))) == ["before", "after"]
    assert ids(delete_recipe_nodes(SOURCE, node_ids=("a", "b"))) == ["before", "after"]


@pytest.mark.parametrize("selection", [("a", "a"), ("loop", "a")])
def test_ambiguous_move_is_rejected_atomically(selection):
    with pytest.raises(ConfigurationError):
        move_recipe_nodes(SOURCE, node_ids=selection, destination_parent_id="main", destination_branch="children", destination_index=0)
    assert ids(SOURCE) == ["before", "loop", "after"]


@pytest.mark.parametrize("width", [1360, 820])
def test_repeat_toolbar_moves_out_unwraps_and_undo_restores(tmp_path, width):
    app = QApplication.instance() or QApplication([])
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "recipe.yml"))
    page.resize(width, 880)
    page.show()
    try:
        page._apply_builder_source(SOURCE, "test")
        page._select_source_node("a")
        app.processEvents()
        assert page.move_up_button.isEnabled()
        page.move_up_button.click()
        assert ids(page._builder_source()) == ["before", "a", "loop", "after"]
        page.undo_tree_edit()
        page._select_source_node("b")
        assert page.move_down_button.isEnabled()
        page.move_down_button.click()
        assert ids(page._builder_source()) == ["before", "loop", "b", "after"]
        page.undo_tree_edit()
        page._select_source_node("loop")
        app.processEvents()
        assert page.unwrap_repeat_button.isVisible() and page.unwrap_repeat_button.isEnabled()
        assert page.unwrap_repeat_button.height() > 0
        assert page.grab().save(str(tmp_path / f"repeat-{width}.png"))
        page.unwrap_repeat_button.click()
        assert ids(page._builder_source()) == ["before", "a", "b", "after"]
        page.undo_tree_edit()
        assert ids(page._builder_source()) == ["before", "loop", "after"]
        page.redo_tree_edit()
        assert ids(page._builder_source()) == ["before", "a", "b", "after"]
    finally:
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        app.processEvents()
