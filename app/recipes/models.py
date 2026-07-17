"""Strict parser for a small, auditable measurement-recipe language."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ruamel.yaml import YAML

from app.domain.dut import (
    AnritsuDutLimits,
    DutRange,
    ExperimentDutLimits,
    KeithleyDutLimits,
    RigolDutLimits,
)
from app.domain.errors import ConfigurationError
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DBM,
    DIMENSION_POWER,
    DIMENSION_RESISTANCE,
    DIMENSION_VOLTAGE,
    parse_quantity,
)


ACTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "sequence",
        "sweep",
        "repeat",
        "if",
        "connect",
        "checkpoint",
        "configure_rigol",
        "configure_keithley",
        "configure_anritsu",
        "configure_anritsu_advanced",
        "configure_anritsu_sg",
        "arm_anritsu_sg_output",
        "set_anritsu_sg_output",
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
    else_children: tuple["RecipeNode", ...] = ()


@dataclass(frozen=True, slots=True)
class Recipe:
    schema_version: int
    name: str
    root: RecipeNode
    finally_nodes: tuple[RecipeNode, ...]
    dut_limits: ExperimentDutLimits
    source_text: str


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
    containers = {"sequence", "sweep", "repeat", "if"}
    if kind in {"sequence", "sweep", "repeat"} and not children:
        raise ConfigurationError(f"{where}: {kind} requires at least one child.")
    if kind == "if" and not children and not else_children:
        raise ConfigurationError(f"{where}: if requires a children or else branch.")
    if kind not in containers and children:
        raise ConfigurationError(f"{where}: action {kind} cannot have children.")
    if kind != "if" and else_children:
        raise ConfigurationError(f"{where}: only an if node can have an else branch.")
    if kind == "sweep":
        for key in ("target", "start", "stop", "points"):
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


def _reject_unknown(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigurationError(f"{where}: unknown fields: {', '.join(sorted(unknown))}.")


def _parse_dut_range(value: object, dimension: str, where: str) -> DutRange:
    raw = _require_mapping(value, where)
    _reject_unknown(raw, {"min", "max"}, where)
    if set(raw) != {"min", "max"}:
        raise ConfigurationError(f"{where} requires both min and max.")
    try:
        return DutRange(
            parse_quantity(raw["min"], dimension).si_value,
            parse_quantity(raw["max"], dimension).si_value,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid {where}: {exc}") from exc


def _parse_dut_limits(value: object) -> ExperimentDutLimits:
    if value is None:
        return ExperimentDutLimits()
    raw = _require_mapping(value, "recipe.dut_limits")
    _reject_unknown(raw, {"keithley", "rigol", "anritsu"}, "recipe.dut_limits")

    keithley: dict[str, KeithleyDutLimits] = {}
    for channel, channel_value in _require_mapping(
        raw.get("keithley", {}), "recipe.dut_limits.keithley"
    ).items():
        if channel not in {"A", "B"}:
            raise ConfigurationError("Keithley DUT limit channel must be A or B.")
        limits = _require_mapping(channel_value, f"recipe.dut_limits.keithley.{channel}")
        _reject_unknown(
            limits,
            {"current", "voltage", "max_abs_power"},
            f"recipe.dut_limits.keithley.{channel}",
        )
        try:
            keithley[channel] = KeithleyDutLimits(
                current=(
                    _parse_dut_range(
                        limits["current"],
                        DIMENSION_CURRENT,
                        f"recipe.dut_limits.keithley.{channel}.current",
                    )
                    if "current" in limits
                    else None
                ),
                voltage=(
                    _parse_dut_range(
                        limits["voltage"],
                        DIMENSION_VOLTAGE,
                        f"recipe.dut_limits.keithley.{channel}.voltage",
                    )
                    if "voltage" in limits
                    else None
                ),
                max_abs_power_w=(
                    parse_quantity(limits["max_abs_power"], DIMENSION_POWER).si_value
                    if "max_abs_power" in limits
                    else None
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"Invalid Keithley DUT limits for channel {channel}: {exc}") from exc

    rigol: dict[int, RigolDutLimits] = {}
    for channel_text, channel_value in _require_mapping(
        raw.get("rigol", {}), "recipe.dut_limits.rigol"
    ).items():
        try:
            channel = int(channel_text)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("Rigol DUT limit channel must be 1 or 2.") from exc
        if channel not in {1, 2}:
            raise ConfigurationError("Rigol DUT limit channel must be 1 or 2.")
        limits = _require_mapping(channel_value, f"recipe.dut_limits.rigol.{channel}")
        _reject_unknown(
            limits,
            {"minimum_impedance", "max_abs_current", "max_abs_power"},
            f"recipe.dut_limits.rigol.{channel}",
        )
        try:
            rigol[channel] = RigolDutLimits(
                minimum_impedance_ohm=(
                    parse_quantity(limits["minimum_impedance"], DIMENSION_RESISTANCE).si_value
                    if "minimum_impedance" in limits
                    else None
                ),
                max_abs_current_a=(
                    parse_quantity(limits["max_abs_current"], DIMENSION_CURRENT).si_value
                    if "max_abs_current" in limits
                    else None
                ),
                max_abs_power_w=(
                    parse_quantity(limits["max_abs_power"], DIMENSION_POWER).si_value
                    if "max_abs_power" in limits
                    else None
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"Invalid Rigol DUT limits for channel {channel}: {exc}") from exc

    anritsu: AnritsuDutLimits | None = None
    if "anritsu" in raw:
        limits = _require_mapping(raw["anritsu"], "recipe.dut_limits.anritsu")
        _reject_unknown(
            limits,
            {"max_expected_input", "max_signal_generator_output"},
            "recipe.dut_limits.anritsu",
        )
        if not limits:
            raise ConfigurationError("Anritsu DUT limits cannot be empty.")
        anritsu = AnritsuDutLimits(
            max_expected_input_dbm=(
                parse_quantity(limits["max_expected_input"], DIMENSION_DBM).si_value
                if "max_expected_input" in limits
                else None
            ),
            max_signal_generator_output_dbm=(
                parse_quantity(
                    limits["max_signal_generator_output"], DIMENSION_DBM
                ).si_value
                if "max_signal_generator_output" in limits
                else None
            ),
        )
    return ExperimentDutLimits(keithley=keithley, rigol=rigol, anritsu=anritsu)


def parse_recipe_text(source: str, *, origin: str = "<memory>") -> Recipe:
    """Parse operator-edited YAML without granting it executable privileges."""

    try:
        raw = YAML(typ="safe").load(source)
    except Exception as exc:
        raise ConfigurationError(f"Cannot read recipe {origin}: {exc}") from exc
    root_raw = _require_mapping(raw, "recipe")
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
    dut_limits = _parse_dut_limits(root_raw.get("dut_limits"))
    _assert_unique_node_ids((root, *finally_nodes))
    return Recipe(1, name, root, finally_nodes, dut_limits, source)


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
