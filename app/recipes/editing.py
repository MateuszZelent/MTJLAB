"""Safe structural recipe edits with comment-preserving YAML round trips."""

from __future__ import annotations

from copy import deepcopy
from io import StringIO
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


def delete_recipe_nodes(source: str, *, node_ids: list[str] | tuple[str, ...]) -> str:
    """Delete one or more non-root nodes from the visual builder."""
    if not node_ids:
        return source

    raw = _load(source)
    root_id = raw["root"].get("id")
    for node_id in node_ids:
        if root_id == node_id:
            raise ConfigurationError("The recipe root cannot be deleted.")

    finally_nodes = raw.setdefault("finally", [])
    if not isinstance(finally_nodes, list):
        raise ConfigurationError("recipe.finally must be a list.")

    for node_id in node_ids:
        detached = _detach(raw["root"], node_id, section="root")
        if detached is None and isinstance(finally_nodes, list):
            detached = _detach_list(finally_nodes, node_id, section="finally")
        if detached is None:
            raise ConfigurationError(f"Recipe node {node_id!r} was not found.")
    return _dump_validated(raw, "tree-builder delete")


def delete_recipe_node(source: str, *, node_id: str) -> str:
    """Delete one non-root node from the visual builder."""
    return delete_recipe_nodes(source, node_ids=(node_id,))


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


def wrap_recipe_nodes_in_repeat(
    source: str,
    *,
    node_ids: tuple[str, ...],
    repeat_id: str,
    count: int,
) -> str:
    """Wrap contiguous sibling nodes in one validated Repeat transaction.

    A Repeat is never materialized as an empty draft node.  The selected nodes
    are detached and inserted into the new container in one in-memory YAML
    transaction, and the strict recipe parser approves the complete result
    before it is returned to the UI.
    """

    if not node_ids:
        raise ConfigurationError("Select at least one recipe node to repeat.")
    if len(set(node_ids)) != len(node_ids):
        raise ConfigurationError("The Repeat selection contains a duplicate node.")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not 1 <= count <= 100_000
    ):
        raise ConfigurationError("Repeat count must be an integer from 1 to 100000.")
    if not isinstance(repeat_id, str) or not repeat_id.strip():
        raise ConfigurationError("Repeat node identifier cannot be empty.")

    raw = _load(source)
    root = raw["root"]
    if root.get("id") in node_ids:
        raise ConfigurationError(
            "The recipe root cannot be wrapped. Select its contents instead."
        )
    if _find(root, repeat_id) is not None:
        raise ConfigurationError(f"Recipe node {repeat_id!r} already exists.")
    finally_nodes = raw.setdefault("finally", [])
    if not isinstance(finally_nodes, list):
        raise ConfigurationError("recipe.finally must be a list.")
    for candidate in finally_nodes:
        if isinstance(candidate, dict) and _find(candidate, repeat_id) is not None:
            raise ConfigurationError(f"Recipe node {repeat_id!r} already exists.")

    locations: list[tuple[dict[str, Any], list[Any], int, str]] = []
    for node_id in node_ids:
        location = _locate(root, node_id, section="root")
        if location is None:
            location = _locate_list(finally_nodes, node_id, section="finally")
        if location is None:
            raise ConfigurationError(f"Recipe node {node_id!r} was not found.")
        locations.append(location)

    first_list = locations[0][1]
    if any(
        source_list is not first_list
        for _node, source_list, _index, _section in locations
    ):
        raise ConfigurationError(
            "Repeat can wrap only sibling nodes from the same recipe branch."
        )
    if any(
        section != "root"
        for _node, _source_list, _index, section in locations
    ):
        raise ConfigurationError(
            "Finally safety actions cannot be wrapped in Repeat."
        )
    ordered = sorted(locations, key=lambda location: location[2])
    indices = [index for _node, _source_list, index, _section in ordered]
    expected = list(range(indices[0], indices[0] + len(indices)))
    if indices != expected:
        raise ConfigurationError(
            "Repeat can wrap only a contiguous range of sibling nodes."
        )

    children = [
        deepcopy(node) for node, _source_list, _index, _section in ordered
    ]
    del first_list[indices[0] : indices[-1] + 1]
    first_list.insert(
        indices[0],
        {
            "id": repeat_id,
            "type": "repeat",
            "count": count,
            "children": children,
        },
    )
    return _dump_validated(raw, "tree-builder wrap repeat")


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


def move_recipe_nodes(
    source: str,
    *,
    node_ids: list[str] | tuple[str, ...],
    destination_parent_id: str,
    destination_branch: str,
    destination_index: int,
) -> str:
    """Move one or more non-root nodes, then re-parse the entire recipe contract.

    Nodes cannot cross between the normal tree and ``finally``.  This keeps a
    drag operation from silently changing when a cleanup action is executed.
    Relative order of the moved nodes is preserved.
    """
    if not node_ids:
        return source

    yaml = YAML()
    raw = _load(source)
    root_id = raw["root"].get("id")
    for nid in node_ids:
        if root_id == nid:
            raise ConfigurationError("The recipe root cannot be moved.")

    finally_nodes = raw.setdefault("finally", [])
    if not isinstance(finally_nodes, list):
        raise ConfigurationError("recipe.finally must be a list.")

    located_nodes: list[tuple[dict[str, Any], list[Any], int, str]] = []
    for nid in node_ids:
        loc = _locate(raw["root"], nid, section="root")
        if loc is None:
            loc = _locate_list(finally_nodes, nid, section="finally")
        if loc is None:
            raise ConfigurationError(f"Recipe node {nid!r} was not found.")
        located_nodes.append(loc)

    if destination_parent_id == "__finally__":
        target = finally_nodes
        destination_section = "finally"
    else:
        for moved_item, _, _, _ in located_nodes:
            if _find(moved_item, destination_parent_id) is not None:
                raise ConfigurationError(
                    "The destination is inside a moved node. "
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

    for _, _, _, source_section in located_nodes:
        if source_section != destination_section:
            raise ConfigurationError("Drag-and-drop cannot move nodes into or out of finally.")

    moved_dicts = [loc[0] for loc in located_nodes]
    index = int(destination_index)

    # Detach from source lists
    items_to_remove: dict[int, list[tuple[int, list[Any], dict[str, Any]]]] = {}
    for item, s_list, s_idx, _ in located_nodes:
        key = id(s_list)
        if key not in items_to_remove:
            items_to_remove[key] = []
        items_to_remove[key].append((s_idx, s_list, item))

    target_removals_before = 0
    if id(target) in items_to_remove:
        for s_idx, _, _ in items_to_remove[id(target)]:
            if s_idx < index:
                target_removals_before += 1

    for removals in items_to_remove.values():
        removals.sort(key=lambda x: x[0], reverse=True)
        for s_idx, s_list, item in removals:
            if s_idx < len(s_list) and s_list[s_idx] is item:
                s_list.pop(s_idx)
            else:
                s_list.remove(item)

    index = max(0, min(index - target_removals_before, len(target)))
    for offset, moved_dict in enumerate(moved_dicts):
        target.insert(index + offset, moved_dict)

    stream = StringIO()
    yaml.dump(raw, stream)
    result = stream.getvalue()
    parse_recipe_text(result, origin="drag-and-drop")
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
    return move_recipe_nodes(
        source,
        node_ids=(node_id,),
        destination_parent_id=destination_parent_id,
        destination_branch=destination_branch,
        destination_index=destination_index,
    )


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
