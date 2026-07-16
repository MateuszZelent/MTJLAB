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
        "configure_rigol",
        "configure_keithley",
        "configure_anritsu",
        "arm_rigol_output",
        "arm_keithley_output",
        "set_rigol_output",
        "set_keithley_output",
        "ramp_keithley_to_zero",
        "measure_keithley",
        "acquire_spectrum",
        "wait",
        "comment",
    }
)


@dataclass(frozen=True, slots=True)
class RecipeNode:
    """A validated node in an operator-editable recipe tree."""

    id: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    children: tuple["RecipeNode", ...] = ()


@dataclass(frozen=True, slots=True)
class Recipe:
    schema_version: int
    name: str
    root: RecipeNode
    finally_nodes: tuple[RecipeNode, ...]
    source_text: str


def _require_mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{where} musi być mapą YAML.")
    return dict(value)


def _require_string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{where} musi być niepustym tekstem.")
    return value.strip()


def _parse_node(value: object, where: str) -> RecipeNode:
    raw = _require_mapping(value, where)
    allowed = {"id", "type", "children"}
    node_id = _require_string(raw.pop("id", None), f"{where}.id")
    kind = _require_string(raw.pop("type", None), f"{where}.type").lower()
    if kind not in ACTION_TYPES:
        raise ConfigurationError(f"{where}: nieobsługiwany typ węzła {kind!r}.")
    children_raw = raw.pop("children", [])
    if children_raw is None:
        children_raw = []
    if not isinstance(children_raw, list):
        raise ConfigurationError(f"{where}.children musi być listą.")
    children = tuple(_parse_node(item, f"{where}.children[{index}]") for index, item in enumerate(children_raw))
    if kind in {"sequence", "sweep"} and not children:
        raise ConfigurationError(f"{where}: {kind} wymaga co najmniej jednego dziecka.")
    if kind not in {"sequence", "sweep"} and children:
        raise ConfigurationError(f"{where}: akcja {kind} nie może mieć dzieci.")
    if kind == "sweep":
        for key in ("target", "start", "stop", "points"):
            if key not in raw:
                raise ConfigurationError(f"{where}: sweep wymaga pola {key!r}.")
        points = raw["points"]
        if not isinstance(points, int) or points < 2:
            raise ConfigurationError(f"{where}.points musi być liczbą całkowitą >= 2.")
        if raw.get("spacing", "linear") not in {"linear", "log"}:
            raise ConfigurationError(f"{where}.spacing musi wynosić linear albo log.")
    return RecipeNode(id=node_id, type=kind, data=raw, children=children)


def parse_recipe_text(source: str, *, origin: str = "<memory>") -> Recipe:
    """Parse operator-edited YAML without granting it executable privileges."""

    try:
        raw = YAML(typ="safe").load(source)
    except Exception as exc:
        raise ConfigurationError(f"Nie można odczytać receptury {origin}: {exc}") from exc
    root_raw = _require_mapping(raw, "receptura")
    allowed_top = {"schema_version", "name", "root", "finally"}
    unknown = set(root_raw) - allowed_top
    if unknown:
        raise ConfigurationError(f"Nieznane pola receptury: {', '.join(sorted(unknown))}.")
    version = root_raw.get("schema_version")
    if version != 1:
        raise ConfigurationError("Obsługiwana jest wyłącznie schema_version: 1.")
    name = _require_string(root_raw.get("name"), "receptura.name")
    root = _parse_node(root_raw.get("root"), "receptura.root")
    finally_raw = root_raw.get("finally", [])
    if not isinstance(finally_raw, list):
        raise ConfigurationError("receptura.finally musi być listą.")
    finally_nodes = tuple(_parse_node(item, f"receptura.finally[{index}]") for index, item in enumerate(finally_raw))
    _assert_unique_node_ids((root, *finally_nodes))
    return Recipe(1, name, root, finally_nodes, source)


def _assert_unique_node_ids(nodes: tuple[RecipeNode, ...]) -> None:
    """Reject ambiguous node IDs before a recipe is compiled or persisted."""

    seen: set[str] = set()

    def visit(node: RecipeNode) -> None:
        if node.id in seen:
            raise ConfigurationError(f"Identyfikator węzła receptury nie jest unikalny: {node.id!r}.")
        seen.add(node.id)
        for child in node.children:
            visit(child)

    for node in nodes:
        visit(node)


def load_recipe(path: str | Path) -> Recipe:
    """Load a recipe from YAML and keep its exact source for run provenance."""

    recipe_path = Path(path)
    try:
        source = recipe_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise ConfigurationError(f"Nie można odczytać receptury {recipe_path}: {exc}") from exc
    return parse_recipe_text(source, origin=str(recipe_path))
