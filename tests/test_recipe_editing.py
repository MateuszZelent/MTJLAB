from __future__ import annotations

import unittest

from app.domain.errors import ConfigurationError
from app.recipes import add_recipe_node, delete_recipe_node, move_recipe_node, parse_recipe_text


SOURCE = """\
schema_version: 1
name: editing
root:
  id: root
  type: sequence
  children:
    - {id: first, type: wait, duration: 1 ms}
    - id: condition
      type: if
      condition: true
      children:
        - {id: then-node, type: checkpoint, label: then}
      else:
        - {id: else-node, type: checkpoint, label: else}
    - {id: last, type: checkpoint, label: last}
finally:
  - {id: off, type: set_rigol_output, channel: 1, enabled: false}
"""


class RecipeEditingTests(unittest.TestCase):
    def test_builder_can_add_and_delete_leaf_nodes(self) -> None:
        added = add_recipe_node(
            SOURCE,
            parent_id="root",
            node={"id": "new-wait", "type": "wait", "duration": "2 ms"},
        )
        self.assertIn("new-wait", tuple(node.id for node in parse_recipe_text(added).root.children))
        deleted = delete_recipe_node(added, node_id="new-wait")
        self.assertNotIn("new-wait", tuple(node.id for node in parse_recipe_text(deleted).root.children))

    def test_reorders_siblings_and_preserves_valid_recipe(self) -> None:
        moved = move_recipe_node(
            SOURCE,
            node_id="last",
            destination_parent_id="root",
            destination_branch="children",
            destination_index=0,
        )
        recipe = parse_recipe_text(moved)
        self.assertEqual(tuple(node.id for node in recipe.root.children), ("last", "first", "condition"))

    def test_moves_between_if_branches(self) -> None:
        moved = move_recipe_node(
            SOURCE,
            node_id="then-node",
            destination_parent_id="condition",
            destination_branch="else",
            destination_index=1,
        )
        condition = parse_recipe_text(moved).root.children[1]
        self.assertEqual(condition.children, ())
        self.assertEqual(tuple(node.id for node in condition.else_children), ("else-node", "then-node"))

    def test_rejects_crossing_finally_boundary_and_cycles(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "into or out of finally"):
            move_recipe_node(
                SOURCE,
                node_id="first",
                destination_parent_id="__finally__",
                destination_branch="children",
                destination_index=0,
            )
        with self.assertRaisesRegex(ConfigurationError, "own descendant"):
            move_recipe_node(
                SOURCE,
                node_id="condition",
                destination_parent_id="then-node",
                destination_branch="children",
                destination_index=0,
            )

    def test_root_cannot_be_moved(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "root cannot be moved"):
            move_recipe_node(
                SOURCE,
                node_id="root",
                destination_parent_id="condition",
                destination_branch="children",
                destination_index=0,
            )


if __name__ == "__main__":
    unittest.main()
