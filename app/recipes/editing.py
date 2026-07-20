"""Safe structural recipe edits with comment-preserving YAML round trips."""

from __future__ import annotations

from io import StringIO
from copy import deepcopy
from typing import Any

from ruamel.yaml import YAML

from app.domain.errors import ConfigurationError
from app.recipes.models import parse_recipe_text


def add_recipe_node(
    source: str,
    *,
    parent_id: str,
    node: dict[str, Any],
    branch: str = "children",
    index: int | None = None,
) -> str:
    """Insert a new visual-builder node and validate the complete recipe."""

    raw = _load(source)
    if parent_id == "__finally__":
        target = raw.setdefault("finally", [])
    else:
        parent = _find(raw["root"], parent_id)
        if parent is None:
            raise ConfigurationError(f"Recipe parent {parent_id!r} was not found.")
        if branch not in {"children", "else"}:
            raise ConfigurationError("Recipe node branch must be children or else.")
        target = parent.setdefault(branch, [])
    if not isinstance(target, list):
        raise ConfigurationError("Recipe destination is not a list.")
    target.insert(len(target) if index is None else max(0, min(index, len(target))), deepcopy(node))
    return _dump_validated(raw, "tree-builder add")


def delete_recipe_node(source: str, *, node_id: str) -> str:
    """Delete one non-root node from the visual builder."""

    raw = _load(source)
    if raw["root"].get("id") == node_id:
        raise ConfigurationError("The recipe root cannot be deleted.")
    detached = _detach(raw["root"], node_id, section="root")
    finally_nodes = raw.setdefault("finally", [])
    if detached is None and isinstance(finally_nodes, list):
        detached = _detach_list(finally_nodes, node_id, section="finally")
    if detached is None:
        raise ConfigurationError(f"Recipe node {node_id!r} was not found.")
    return _dump_validated(raw, "tree-builder delete")


def replace_recipe_node(source: str, *, node_id: str, node: dict[str, Any]) -> str:
    """Atomically replace fields of one node while retaining its tree position."""

    raw = _load(source)
    target = _find(raw["root"], node_id)
    if target is None:
        for candidate in raw.get("finally", []):
            if isinstance(candidate, dict):
                target = _find(candidate, node_id)
                if target is not None:
                    break
    if target is None:
        raise ConfigurationError(f"Recipe node {node_id!r} was not found.")
    replacement = deepcopy(node)
    if replacement.get("id") != node_id:
        raise ConfigurationError("A visual edit cannot change the recipe node identifier.")
    target.clear()
    target.update(replacement)
    return _dump_validated(raw, "tree-builder edit")


def _load(source: str) -> dict[str, Any]:
    yaml = YAML()
    raw = yaml.load(source)
    if not isinstance(raw, dict) or not isinstance(raw.get("root"), dict):
        raise ConfigurationError("The recipe must contain a mapping root node.")
    return raw


def _dump_validated(raw: dict[str, Any], origin: str) -> str:
    stream = StringIO()
    YAML().dump(raw, stream)
    result = stream.getvalue()
    parse_recipe_text(result, origin=origin)
    return result


def move_recipe_node(
    source: str,
    *,
    node_id: str,
    destination_parent_id: str,
    destination_branch: str,
    destination_index: int,
) -> str:
    """Move one non-root node, then re-parse the entire recipe contract.

    Nodes cannot cross between the normal tree and ``finally``.  This keeps a
    drag operation from silently changing when a cleanup action is executed.
    The strict parser remains the final authority for container and branch
    semantics.
    """

    yaml = YAML()
    raw = _load(source)
    if raw["root"].get("id") == node_id:
        raise ConfigurationError("The recipe root cannot be moved.")

    finally_nodes = raw.setdefault("finally", [])
    if not isinstance(finally_nodes, list):
        raise ConfigurationError("recipe.finally must be a list.")

    source = _locate(raw["root"], node_id, section="root")
    if source is None:
        source = _locate_list(finally_nodes, node_id, section="finally")
    if source is None:
        raise ConfigurationError(f"Recipe node {node_id!r} was not found.")
    moved, source_list, source_index, source_section = source

    if destination_parent_id == "__finally__":
        target = finally_nodes
        destination_section = "finally"
    else:
        if _find(moved, destination_parent_id) is not None:
            raise ConfigurationError(
                "The destination is inside the moved node. "
                "A node cannot be moved into its own descendant."
            )
        parent = _find(raw["root"], destination_parent_id)
        destination_section = "root"
        if parent is None:
            for candidate in finally_nodes:
                if isinstance(candidate, dict):
                    parent = _find(candidate, destination_parent_id)
                    if parent is not None:
                        destination_section = "finally"
                        break
        if parent is None:
            raise ConfigurationError(
                f"Recipe destination {destination_parent_id!r} was not found."
            )
        if destination_branch not in {"children", "else"}:
            raise ConfigurationError("Destination branch must be children or else.")
        target = parent.setdefault(destination_branch, [])
        if not isinstance(target, list):
            raise ConfigurationError(f"Destination {destination_branch} is not a list.")

    if source_section != destination_section:
        raise ConfigurationError("Drag-and-drop cannot move nodes into or out of finally.")

    # The UI reports a gap in the original list.  Once the source is removed,
    # every later gap shifts left by one.  Without this correction, moving a
    # sibling downward can skip a node or appear to move into the wrong block.
    index = int(destination_index)
    if target is source_list and source_index < index:
        index -= 1
    source_list.pop(source_index)
    index = max(0, min(index, len(target)))
    target.insert(index, moved)

    stream = StringIO()
    yaml.dump(raw, stream)
    result = stream.getvalue()
    parse_recipe_text(result, origin="drag-and-drop")
    return result


def _detach(node: dict[str, Any], node_id: str, *, section: str) -> tuple[dict[str, Any], str] | None:
    for branch in ("children", "else"):
        nested = node.get(branch, [])
        if isinstance(nested, list):
            found = _detach_list(nested, node_id, section=section)
            if found is not None:
                return found
    return None


def _detach_list(
    nodes: list[Any],
    node_id: str,
    *,
    section: str,
) -> tuple[dict[str, Any], str] | None:
    for index, candidate in enumerate(tuple(nodes)):
        if not isinstance(candidate, dict):
            continue
        if candidate.get("id") == node_id:
            return nodes.pop(index), section
        found = _detach(candidate, node_id, section=section)
        if found is not None:
            return found
    return None


def _find(node: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    if node.get("id") == node_id:
        return node
    for branch in ("children", "else"):
        nested = node.get(branch, [])
        if not isinstance(nested, list):
            continue
        for candidate in nested:
            if isinstance(candidate, dict):
                found = _find(candidate, node_id)
                if found is not None:
                    return found
    return None


def _locate(
    node: dict[str, Any], node_id: str, *, section: str
) -> tuple[dict[str, Any], list[Any], int, str] | None:
    for branch in ("children", "else"):
        nested = node.get(branch, [])
        if isinstance(nested, list):
            found = _locate_list(nested, node_id, section=section)
            if found is not None:
                return found
    return None


def _locate_list(
    nodes: list[Any], node_id: str, *, section: str
) -> tuple[dict[str, Any], list[Any], int, str] | None:
    for index, candidate in enumerate(nodes):
        if not isinstance(candidate, dict):
            continue
        if candidate.get("id") == node_id:
            return candidate, nodes, index, section
        found = _locate(candidate, node_id, section=section)
        if found is not None:
            return found
    return None
