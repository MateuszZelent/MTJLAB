"""Stable tree interactions and truthful compiled-plan presentation."""
import os
import json

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtCore import QPoint, Qt

from app.devices.registry import built_in_device_registry
from app.engine.compiler import RecipeCompiler
from app.engine.estimation import PlanEstimator
from app.recipes import parse_recipe_text
from app.recipes.semantic_tree import normalize_recipe_tree
from app.ui.measurement_tree.model import MeasurementTreeModel
from app.ui.measurement_tree.view import MeasurementTreeView
from app.ui.recipes.page import RecipePage
from app.ui.execution.page import RunMonitorPage
from tests.helpers import simulation_settings


SOURCE = """schema_version: 1
name: tree refresh
root:
  id: root
  type: sequence
  children:
    - {id: connect, type: connect, device: anritsu}
"""


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Segoe UI", 9))
    return app


def snapshot():
    return normalize_recipe_tree(parse_recipe_text(SOURCE), built_in_device_registry().sweep_providers())


def test_collapse_survives_pending_events_reset_and_old_model_reset(app):
    model = MeasurementTreeModel(snapshot())
    view = MeasurementTreeView()
    try:
        view.setModel(model)
        view.resize(1100, 600)
        view.show()
        view.collapse(model.index_for_semantic_id("root"))
        app.processEvents()
        assert not view.isExpanded(model.index_for_semantic_id("root"))
        model.replace_tree(snapshot())
        app.processEvents()
        assert not view.isExpanded(model.index_for_semantic_id("root"))
        replacement = MeasurementTreeModel(snapshot())
        view.setModel(replacement)
        view.collapse(replacement.index_for_semantic_id("root"))
        model.replace_tree(snapshot())
        app.processEvents()
        assert not view.isExpanded(replacement.index_for_semantic_id("root"))
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()


def test_nonzero_parent_column_has_no_child_index(app):
    model = MeasurementTreeModel(snapshot())
    parent = model.index_for_semantic_id("root").siblingAtColumn(1)
    assert model.rowCount(parent) == 0
    assert not model.index(0, 0, parent).isValid()


def test_branch_click_tracks_horizontal_scroll(app):
    node = {"id": "leaf", "type": "wait", "duration": "1 s"}
    for depth in reversed(range(8)):
        node = {"id": f"level-{depth}", "type": "sequence", "children": [node]}
    recipe = parse_recipe_text(json.dumps({"schema_version": 1, "name": "deep", "root": node}))
    model = MeasurementTreeModel(normalize_recipe_tree(recipe, {}))
    view = MeasurementTreeView()
    try:
        view.setModel(model)
        view.resize(500, 600)
        view.show()
        app.processEvents()
        view.setColumnWidth(0, 900)
        view.horizontalScrollBar().setValue(96)
        app.processEvents()
        index = model.index_for_semantic_id("level-5")
        rect = view.visualRect(index)
        position = QPoint(rect.left() - view.indentation() // 2, rect.center().y())
        assert view.viewport().rect().contains(position)
        assert view.isExpanded(index)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=position)
        app.processEvents()
        assert not view.isExpanded(index)
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()


def test_follow_keeps_last_request_and_new_force_cancels_stale_reveal(app):
    model = MeasurementTreeModel(snapshot())
    view = MeasurementTreeView()
    try:
        view.setModel(model)
        view.resize(1000, 600)
        view.show()
        app.processEvents()
        view.follow_semantic_id("root", force=True)
        view.follow_semantic_id("connect")
        QTest.qWait(150)
        assert view.currentIndex() == model.index_for_semantic_id("connect")
        view.follow_semantic_id("root")
        view.follow_semantic_id("__finally__", force=True)
        QTest.qWait(150)
        assert view.currentIndex() == model.index_for_semantic_id("__finally__")
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("fail", [False, True])
def test_shutdown_parent_finishes_after_abort_and_keeps_any_failure(app, fail):
    actions = ("anritsu.rf_off_and_abort", "storage.flush_checkpoint")
    tree = normalize_recipe_tree(parse_recipe_text(SOURCE), built_in_device_registry().sweep_providers(),
                                 safe_shutdown_actions=actions)
    page = RunMonitorPage()
    try:
        page.run_started(actions=1, semantic_tree=tree, safe_shutdown_actions=actions)
        # A fault/stop can end without a run_completed event.
        page.append_event("run_fault", {"error": "acquisition failed"})
        page.append_event("shutdown_action_started", {"action": actions[0]})
        page.append_event("shutdown_error" if fail else "shutdown_action_finished", {"action": actions[0]})
        page.append_event("shutdown_action_started", {"action": actions[1]})
        page.append_event("shutdown_action_finished", {"action": actions[1]})
        index = page.tree_model.index_for_semantic_id("__finally__").siblingAtColumn(3)
        assert page.tree_model.data(index) == ("FAILED" if fail else "APPLIED")
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("change", ["yaml", "mode"])
def test_invalidating_plan_removes_compiled_shutdown_rows(app, change):
    settings = simulation_settings()
    page = RecipePage(settings)
    try:
        page._loading_source = True
        page.editor.setPlainText(SOURCE)
        page._loading_source = False
        page._tree_source = SOURCE
        recipe = parse_recipe_text(SOURCE)
        plan = RecipeCompiler(settings).compile(recipe)
        page._accept_preflight(recipe, plan, PlanEstimator(settings).estimate(plan))
        assert page.tree_model.tree.require("__finally__").data["shutdown_compiled"]
        if change == "yaml":
            page.editor.setPlainText(SOURCE + "# edit\n")
        else:
            page.execution_mode.setCurrentIndex(page.execution_mode.findData("dry_run"))
        assert page._plan is None
        branch = page.tree_model.tree.require("__finally__")
        assert not branch.data["shutdown_compiled"]
        assert not branch.children
    finally:
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        app.processEvents()
