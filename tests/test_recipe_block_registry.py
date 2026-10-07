"""Block identity is enforced independently of UI and survives structural edits."""
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from ruamel.yaml import YAML

from app.domain.errors import ConfigurationError
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text, RecipeRepository
from app.recipes.block_registry import ACTION_TYPES, ACTION_FIELDS, BLOCKS, block_for_node
from app.recipes.editing import canonical_recipe_source, move_recipe_node, wrap_recipe_nodes_in_repeat
from tests.test_moke_smoke_sweep_limits import settings_for_channel_two

LEGACY = """# preserve experiment comment
schema_version: 1
name: identities
root:
  id: main
  type: sequence
  children:
    - {id: delay-one, type: wait, duration: 1 ms}
    - {id: delay-two, type: wait, duration: 2 ms}
finally: [{id: zero, type: stop_moke_voltage}]
"""


@pytest.mark.parametrize("identity", ["recipe.unknown", "device.moke_box", "recipe.acquire_spectrum", "", None, 123])
def test_invalid_or_mismatched_identity_is_rejected(identity):
    source = "schema_version: 1\nname: invalid\nroot: {id: test, type: wait, duration: 1 ms, block_type: "
    import json
    with pytest.raises(ConfigurationError):
        parse_recipe_text(source + json.dumps(identity) + "}\n")


def test_device_identity_cannot_impersonate_another_device():
    source = """
schema_version: 1
name: mismatch
root: {id: device, type: sequence, device_module: keithley, block_type: device.moke_box, children: []}
"""
    with pytest.raises(ConfigurationError, match="does not match"):
        parse_recipe_text(source)


@pytest.mark.parametrize("module", ["", False, "unknown"])
def test_unknown_device_binding_cannot_fall_back_to_sequence(module):
    import json
    source = "schema_version: 1\nname: bad\nroot: {id: test, type: sequence, device_module: " + json.dumps(module) + "}"
    with pytest.raises(ConfigurationError, match="Unknown recipe device"):
        parse_recipe_text(source)


def test_legacy_case_normalization_preserves_operation_identity():
    source = "schema_version: 1\nname: legacy\nroot: {id: test, type: WAIT, duration: 1 ms}"
    assert parse_recipe_text(canonical_recipe_source(source)).root.block_type == "recipe.wait"


def test_registration_is_complete_and_immutable():
    assert ACTION_TYPES == frozenset(ACTION_FIELDS)
    for kind in ACTION_TYPES:
        assert block_for_node(kind, {}).block_type in BLOCKS
    with pytest.raises(TypeError):
        BLOCKS["recipe.fake"] = None
    with pytest.raises(TypeError):
        ACTION_FIELDS["wait"] = frozenset()


def test_legacy_upgrade_preserves_parameters_comments_and_branches():
    upgraded = canonical_recipe_source(LEGACY)
    assert "# preserve experiment comment" in upgraded
    raw = YAML().load(upgraded)
    assert raw["root"]["block_type"] == "recipe.sequence"
    assert all(node["block_type"] == "recipe.wait" for node in raw["root"]["children"])
    assert raw["finally"][0]["block_type"] == "recipe.stop_moke_voltage"
    assert raw["root"]["children"][0]["duration"] == "1 ms"
    assert canonical_recipe_source(upgraded) == upgraded
    old = parse_recipe_text(LEGACY)
    new = parse_recipe_text(upgraded)
    assert old.root == new.root and old.finally_nodes == new.finally_nodes


def test_move_and_repeat_keep_type_identity_separate_from_instance_id():
    source = move_recipe_node(LEGACY, node_id="delay-two", destination_parent_id="main", destination_branch="children", destination_index=0)
    source = wrap_recipe_nodes_in_repeat(source, node_ids=("delay-two", "delay-one"), repeat_id="repeat", count=2)
    root = parse_recipe_text(source).root
    repeat, = root.children
    assert repeat.block_type == "recipe.repeat"
    assert [node.id for node in repeat.children] == ["delay-two", "delay-one"]
    assert {node.block_type for node in repeat.children} == {"recipe.wait"}


def test_repository_saves_explicit_ids_and_rejects_bad_id_before_overwrite(tmp_path):
    repository = RecipeRepository()
    path = tmp_path / "recipe.yml"
    repository.save(path, LEGACY)
    original = path.read_bytes()
    assert b"block_type:" in original
    with pytest.raises(ConfigurationError):
        repository.save(path, original.decode().replace("recipe.wait", "recipe.acquire_spectrum", 1))
    assert path.read_bytes() == original
    repository.autosave(path, "unfinished: [")
    assert repository.load_recovery(path) == "unfinished: ["


def test_compiler_enforces_registry_for_programmatically_built_nodes():
    recipe = parse_recipe_text("schema_version: 1\nname: direct\nroot: {id: pause, type: wait, duration: 1 ms}")
    bad = replace(recipe, root=replace(recipe.root, data={"duration": "1 ms", "hidden_command": "OUTPUT ON"}))
    with pytest.raises(ConfigurationError, match="unknown wait fields"):
        RecipeCompiler(settings_for_channel_two()).compile(bad)


def test_ui_library_and_inspector_share_registry(tmp_path):
    from app.ui.recipes.page import RecipePage
    from PySide6.QtGui import QFontDatabase, QFont
    app = QApplication.instance() or QApplication([])
    if not QFontDatabase.families() and Path("C:/Windows/Fonts/arial.ttf").exists():
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/arial.ttf")
    app.setFont(QFont("Arial", 10))
    page = RecipePage(settings_for_channel_two())
    try:
        page.path.setText(str(tmp_path / "registry.yml"))
        page.resize(1360, 880)
        page.show()
        page._apply_builder_source(LEGACY, "registry test")
        app.processEvents()
        assert page._library_action_buttons
        for button in page._library_action_buttons:
            identity = button.property("recipeBlockType")
            assert identity in BLOCKS
            assert identity in button.toolTip()
        assert "block_type:" in page._builder_source()
        assert page.measurement_tree.isVisible()
        assert page.grab().save(str(tmp_path / "block-registry-ui.png"))
    finally:
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        app.processEvents()
