"""Strict parser for a small, auditable measurement-recipe language."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML

from app.domain.errors import ConfigurationError


ACTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "sequence",
        "sweep",
        "repeat",
        "if",
        "connect",
        "checkpoint",
        "configure_rigol",
        "configure_rigol_output",
        "configure_keithley",
        "configure_anritsu",
        "configure_anritsu_advanced",
        "configure_anritsu_sg",
        "configure_moke_box",
        "set_anritsu_sg_output",
        "enable_anritsu_sg_output",
        "set_rigol_output",
        "enable_rigol_output",
        "set_keithley_output",
        "ramp_keithley_to_zero",
        "measure_keithley",
        "measure_moke_hall",
        "measure_lakeshore_field",
        "update_keithley_level",
        "update_keithley_compliance",
        "update_rigol_frequency",
        "update_rigol_levels",
        "update_anritsu_sg",
        "acquire_reference",
        "acquire_spectrum",
        "wait",
        "comment",
    }
)

# Authoritative structural contract shared by the parser and visual builder.
# Every other node type is an atomic action and cannot own executable children.
CONTAINER_NODE_TYPES: Final[frozenset[str]] = frozenset(
    {"sequence", "sweep", "repeat", "if"}
)


@dataclass(frozen=True, slots=True)
class RecipeNode:
    """A validated node in an operator-editable recipe tree."""

    id: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    children: tuple["RecipeNode", ...] = ()
    else_children: tuple["RecipeNode", ...] = ()


@dataclass(frozen=True, slots=True)
class Recipe:
    schema_version: int
    name: str
    root: RecipeNode
    finally_nodes: tuple[RecipeNode, ...]
    source_text: str
    dut_limits: dict[str, Any] = field(default_factory=dict)


def legacy_dut_limits_policy() -> dict[str, object]:
    """Describe the non-enforcing compatibility status of recipe DUT metadata."""

    return {
        "schema_version": 1,
        "enforced": False,
        "mode": "legacy_metadata_only",
        "safety_authority": "station_profile_and_device_hardware",
    }


def _require_mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{where} must be a YAML mapping.")
    return dict(value)


def _require_string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{where} must be non-empty text.")
    return value.strip()


def _parse_node(value: object, where: str) -> RecipeNode:
    raw = _require_mapping(value, where)
    node_id = _require_string(raw.pop("id", None), f"{where}.id")
    kind = _require_string(raw.pop("type", None), f"{where}.type").lower()
    if kind not in ACTION_TYPES:
        raise ConfigurationError(f"{where}: unsupported node type {kind!r}.")
    children_raw = raw.pop("children", [])
    else_raw = raw.pop("else", [])
    if children_raw is None:
        children_raw = []
    if not isinstance(children_raw, list):
        raise ConfigurationError(f"{where}.children must be a list.")
    if not isinstance(else_raw, list):
        raise ConfigurationError(f"{where}.else must be a list.")
    children = tuple(_parse_node(item, f"{where}.children[{index}]") for index, item in enumerate(children_raw))
    else_children = tuple(
        _parse_node(item, f"{where}.else[{index}]")
        for index, item in enumerate(else_raw)
    )
    if kind in {"sweep", "repeat"} and not children:
        raise ConfigurationError(f"{where}: {kind} requires at least one child.")
    if kind == "if" and not children and not else_children:
        raise ConfigurationError(f"{where}: if requires a children or else branch.")
    if kind not in CONTAINER_NODE_TYPES and children:
        raise ConfigurationError(f"{where}: action {kind} cannot have children.")
    if kind != "if" and else_children:
        raise ConfigurationError(f"{where}: only an if node can have an else branch.")
    if kind == "sweep":
        if "target" not in raw:
            raise ConfigurationError(f"{where}: sweep requires field 'target'.")
        segments = raw.get("segments")
        if segments is not None:
            if not isinstance(segments, list) or not segments:
                raise ConfigurationError(f"{where}.segments must be a non-empty list.")
            for index, segment in enumerate(segments):
                if not isinstance(segment, dict):
                    raise ConfigurationError(
                        f"{where}.segments[{index}] must be a mapping."
                    )
                if "value" in segment:
                    if set(segment) != {"value"}:
                        raise ConfigurationError(
                            f"{where}.segments[{index}] single value cannot define interval fields."
                        )
                    continue
                if not {"start", "stop"}.issubset(segment):
                    raise ConfigurationError(
                        f"{where}.segments[{index}] requires start and stop."
                    )
                has_points = "points" in segment
                has_step = "step" in segment
                if has_points == has_step:
                    raise ConfigurationError(
                        f"{where}.segments[{index}] requires exactly one of points or step."
                    )
                if has_points and (
                    not isinstance(segment["points"], int) or segment["points"] < 2
                ):
                    raise ConfigurationError(
                        f"{where}.segments[{index}].points must be an integer >= 2."
                    )
                if segment.get("spacing", "linear") not in {"linear", "log"}:
                    raise ConfigurationError(
                        f"{where}.segments[{index}].spacing must be linear or log."
                    )
        else:
            for key in ("start", "stop", "points"):
                if key not in raw:
                    raise ConfigurationError(f"{where}: sweep requires field {key!r}.")
            points = raw["points"]
            if not isinstance(points, int) or points < 2:
                raise ConfigurationError(f"{where}.points must be an integer >= 2.")
            if raw.get("spacing", "linear") not in {"linear", "log"}:
                raise ConfigurationError(f"{where}.spacing must be linear or log.")
    if kind == "repeat":
        count = raw.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100_000:
            raise ConfigurationError(f"{where}.count must be an integer in 1..100000.")
    if kind == "if":
        has_boolean = isinstance(raw.get("condition"), bool)
        has_comparison = all(key in raw for key in ("left", "operator", "right"))
        if has_boolean == has_comparison:
            raise ConfigurationError(
                f"{where}: if requires either condition: true/false or left/operator/right."
            )
        if has_comparison and raw["operator"] not in {"<", "<=", "==", "!=", ">=", ">"}:
            raise ConfigurationError(f"{where}.operator is unsupported.")
    if kind == "connect" and raw.get("device") not in {"rigol", "keithley", "anritsu"}:
        raise ConfigurationError(f"{where}.device must identify rigol, keithley, or anritsu.")
    return RecipeNode(
        id=node_id,
        type=kind,
        data=raw,
        children=children,
        else_children=else_children,
    )


def parse_recipe_text(source: str, *, origin: str = "<memory>") -> Recipe:
    """Parse operator-edited YAML without granting it executable privileges."""

    try:
        raw = YAML(typ="safe").load(source)
    except Exception as exc:
        raise ConfigurationError(f"Cannot read recipe {origin}: {exc}") from exc
    root_raw = _require_mapping(raw, "recipe")
    # Accept the removed safety field so saved recipes from older releases still
    # open. Its raw value is retained for provenance but never limits execution.
    allowed_top = {"schema_version", "name", "root", "finally", "dut_limits"}
    unknown = set(root_raw) - allowed_top
    if unknown:
        raise ConfigurationError(f"Unknown recipe fields: {', '.join(sorted(unknown))}.")
    version = root_raw.get("schema_version")
    if version != 1:
        raise ConfigurationError("Only schema_version: 1 is supported.")
    name = _require_string(root_raw.get("name"), "recipe.name")
    root = _parse_node(root_raw.get("root"), "recipe.root")
    finally_raw = root_raw.get("finally", [])
    if not isinstance(finally_raw, list):
        raise ConfigurationError("recipe.finally must be a list.")
    finally_nodes = tuple(_parse_node(item, f"recipe.finally[{index}]") for index, item in enumerate(finally_raw))
    _assert_unique_node_ids((root, *finally_nodes))
    dut_limits = root_raw.get("dut_limits", {})
    if dut_limits is None:
        dut_limits = {}
    if not isinstance(dut_limits, dict):
        raise ConfigurationError("recipe.dut_limits must be a YAML mapping.")
    return Recipe(1, name, root, finally_nodes, source, dict(dut_limits))


def _assert_unique_node_ids(nodes: tuple[RecipeNode, ...]) -> None:
    """Reject ambiguous node IDs before a recipe is compiled or persisted."""

    seen: set[str] = set()

    def visit(node: RecipeNode) -> None:
        if node.id in seen:
            raise ConfigurationError(f"Recipe node identifier is not unique: {node.id!r}.")
        seen.add(node.id)
        for child in node.children:
            visit(child)
        for child in node.else_children:
            visit(child)

    for node in nodes:
        visit(node)


def load_recipe(path: str | Path) -> Recipe:
    """Load a recipe from YAML and keep its exact source for run provenance."""

    recipe_path = Path(path)
    try:
        source = recipe_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise ConfigurationError(f"Cannot read recipe {recipe_path}: {exc}") from exc
    return parse_recipe_text(source, origin=str(recipe_path))
