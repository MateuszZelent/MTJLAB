"""Shutdown rows are evidence from compilation, never a station-wide default."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont
from PySide6.QtCore import QSignalBlocker
from PySide6.QtTest import QTest

from app.devices.registry import built_in_device_registry
from app.engine.compiler import RecipeCompiler
from app.engine.estimation import PlanEstimator
from app.recipes import parse_recipe_text
from app.recipes.semantic_tree import normalize_recipe_tree
from app.ui.recipes.page import RecipePage
from tests.helpers import simulation_settings


SOURCE = """schema_version: 1
name: Anritsu-only shutdown projection
root:
  id: main
  type: sequence
  children:
  - id: spectrum
    type: acquire_spectrum
    trace: TRAC1
    average_count: 1
finally: []
"""


def test_uncompiled_tree_does_not_invent_shutdown_devices():
    tree = normalize_recipe_tree(parse_recipe_text(SOURCE), built_in_device_registry().sweep_providers())
    branch = tree.require("__finally__")
    assert len(branch.children) == 1
    assert tree.require("__finally__.automatic_safeguards").children == ()
    assert not branch.data["shutdown_compiled"]
    assert "Compile" in branch.data["detail"]
    assert RecipePage._automatic_shutdown_projection(None) == ()


@pytest.mark.parametrize("width", [900, 1440])
def test_builder_and_execution_snapshot_show_exact_compiled_shutdown(width, tmp_path):
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("Segoe UI", 9))
    settings = simulation_settings()
    recipe = parse_recipe_text(SOURCE)
    plan = RecipeCompiler(settings).compile(recipe)
    page = RecipePage(settings)
    try:
        page.resize(width, 900)
        page.show()
        with QSignalBlocker(page.editor):
            page.editor.setPlainText(SOURCE)
        page._tree_source = SOURCE
        page._refresh_semantic_tree(SOURCE)
        page._accept_preflight(recipe, plan, PlanEstimator(settings).estimate(plan))
        app.processEvents()
        snapshot = page.semantic_tree_snapshot(plan=plan)
        branch = snapshot.require("__finally__")
        assert tuple(child.data["action"] for child in snapshot.require("__finally__.automatic_safeguards").children) == plan.safe_shutdown_actions
        assert plan.safe_shutdown_actions == ("storage.flush_checkpoint",)
        assert branch.data["shutdown_compiled"]
        assert page.measurement_tree.isVisible()
        page.measurement_tree.expandAll()
        page.measurement_tree.collapse(page.tree_model.index_for_semantic_id(recipe.root.id))
        app.processEvents()
        assert not page.tree_model.index_for_semantic_id("__finally__.anritsu_abort_acquisition").isValid()
        index = page.tree_model.index_for_semantic_id("__finally__.automatic_safeguards")
        assert "Anritsu" not in page.tree_model.data(index, 3)  # Tooltip exposes the exact policy without adding steps.
        page.measurement_tree.scrollTo(index)
        QTest.qWait(400)
        assert page.measurement_tree.visualRect(index).intersects(page.measurement_tree.viewport().rect())
        viewport = page.measurement_tree.viewport()
        bottom = viewport.mapTo(page.workspace_card, viewport.rect().bottomRight())
        assert page.workspace_card.rect().contains(bottom)
        assert page.grab().save(str(tmp_path / f"shutdown-{width}.png"))
        assert page.measurement_tree.grab().save(str(tmp_path / f"shutdown-tree-{width}.png"))
        # Editing invalidates compilation and removes the previous manifest.
        snapshot = page._refresh_semantic_tree(SOURCE)
        assert snapshot.require("__finally__.automatic_safeguards").children == ()
    finally:
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        app.processEvents()

