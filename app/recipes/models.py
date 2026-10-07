"""Strict parser for a small, auditable measurement-recipe language."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from app.domain.errors import ConfigurationError


from app.recipes.block_registry import (
    ACTION_TYPES, ACTION_FIELDS as ACTION_FIELDS, CONTAINER_NODE_TYPES,
    _KEITHLEY_FIELDS, _RIGOL_FIELDS, _ANRITSU_FIELDS, _ADVANCED_FIELDS,
    block_for_node,
)


def validate_action_fields(kind: str, data: dict[str, Any], where: str) -> None:
    definition = block_for_node(kind, data)
    unknown = set(data) - definition.allowed_fields
    if unknown:
        raise ConfigurationError(f"{where}: unknown {kind} fields: {', '.join(sorted(unknown))}.")
    if kind == "sequence":
        for name in ("configuration_required", "roi_required", "acquire_single"):
            if name in data and type(data[name]) is not bool:
                raise ConfigurationError(f"{where}: {name} requires a boolean.")
        if data.get("acquire_single"):
            raise ConfigurationError(f"{where}: acquisition requires an explicit acquire_spectrum child; replace acquire_single.")
        for name in ("text", "managed_acquisition_id"):
            if name in data and not isinstance(data[name], str):
                raise ConfigurationError(f"{where}: {name} requires text.")
        if "acquisition_average_count" in data:
            count = data["acquisition_average_count"]
            if type(count) is not int or not 1 <= count <= 9999:
                raise ConfigurationError(f"{where}: acquisition_average_count requires an integer from 1 to 9999.")
        if "post_configuration_operation" in data and data["post_configuration_operation"] not in {"configure", "acquire_spectrum", "acquire_reference"}:
            raise ConfigurationError(f"{where}: invalid post_configuration_operation.")
        if "acquisition_reference_operation" in data and data["acquisition_reference_operation"] not in {"none", "difference_db", "ratio_linear", "subtract_power_signed", "add_power", "subtract_power", "multiply_linear"}:
            raise ConfigurationError(f"{where}: invalid acquisition_reference_operation.")
    if kind.endswith("rigol") or "rigol_" in kind:
        if "channel" in data and not (type(data["channel"]) is int and data["channel"] in {1, 2}):
            raise ConfigurationError(f"{where}: Rigol channel must be integer 1 or 2.")
    for name in ("nplc", "phase_deg", "square_duty_percent", "ramp_symmetry_percent"):
        if name in data and isinstance(data[name], bool):
            raise ConfigurationError(f"{where}: {name} requires a numeric value, not a boolean.")
    if "checkpoint" in data and type(data["checkpoint"]) is not bool:
        raise ConfigurationError(f"{where}: checkpoint requires a boolean.")
    if kind == "sequence" and data.get("operation") == "configure_selected_parameters" and isinstance(data.get("configuration"), dict):
        fields = {
            "keithley": _KEITHLEY_FIELDS | {"source_mode", "source_level"},
            "rigol": _RIGOL_FIELDS | {"time_mode", "level_mode", "output_polarity", "output_mode", "gate_polarity", "sync_enabled", "sync_polarity", "sync_delay"},
            "anritsu": _ANRITSU_FIELDS | _ADVANCED_FIELDS,
            "anritsu_sg": {"frequency", "power"},
        }.get(data.get("device_module"))
        if fields is not None:
            unknown = set(data["configuration"]) - fields
            if unknown:
                raise ConfigurationError(f"{where}.configuration: unknown fields: {', '.join(sorted(unknown))}.")
            if data.get("device_module") == "rigol":
                channel = data["configuration"].get("channel", data.get("channel"))
                if type(channel) is not int or channel not in {1, 2}:
                    raise ConfigurationError(f"{where}.configuration: Rigol channel must be integer 1 or 2.")
            if data.get("device_module") in {"rigol", "keithley"}:
                authored = data.get("channel")
                stored = data["configuration"].get("channel")
                if authored is not None and stored is not None and authored != stored:
                    raise ConfigurationError(f"{where}: channel differs from the stored configuration channel.")
    if kind == "sequence" and "parameter_actions" in data:
        actions = data["parameter_actions"]
        if not isinstance(actions, list):
            raise ConfigurationError(f"{where}: parameter_actions must be a list.")
        names = set()
        for index, action in enumerate(actions):
            location = f"{where}.parameter_actions[{index}]"
            if not isinstance(action, dict):
                raise ConfigurationError(f"{location} must be a mapping.")
            unknown = set(action) - {"parameter_id", "mode", "value", "segments"}
            if unknown:
                raise ConfigurationError(f"{location}: unknown fields: {', '.join(sorted(unknown))}.")
            parameter = action.get("parameter_id")
            if not isinstance(parameter, str) or not parameter or parameter in names:
                raise ConfigurationError(f"{location}: parameter_id must be nonempty and unique within the node.")
            names.add(parameter)
            mode = action.get("mode")
            if mode not in {"set", "sweep"}:
                raise ConfigurationError(f"{location}: mode must be set or sweep.")
            if mode == "set" and "value" not in action:
                raise ConfigurationError(f"{location}: set requires an explicit value.")


@dataclass(frozen=True, slots=True)
class RecipeNode:
    """A validated node in an operator-editable recipe tree."""

    id: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    children: tuple["RecipeNode", ...] = ()
    else_children: tuple["RecipeNode", ...] = ()

    @property
    def block_type(self) -> str:
        return block_for_node(self.type, self.data).block_type


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
    declared_block = _require_string(raw.pop("block_type"), f"{where}.block_type") if "block_type" in raw else None
    block_for_node(kind, raw, declared_block)
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
    # A native sweep already executes its axis setpoint at every point; an
    # additional child is optional. Repeat has no operation of its own.
    if kind == "repeat" and not children:
        raise ConfigurationError(f"{where}: {kind} requires at least one child.")
    if kind == "if" and not children and not else_children:
        raise ConfigurationError(f"{where}: if requires a children or else branch.")
    if kind not in CONTAINER_NODE_TYPES and children:
        raise ConfigurationError(f"{where}: action {kind} cannot have children.")
    if kind != "if" and else_children:
        raise ConfigurationError(f"{where}: only an if node can have an else branch.")
    validate_action_fields(kind, raw, where)
    if kind == "configure_anritsu" and type(raw.get("points")) is not int:
        raise ConfigurationError(f"{where}.points must be an integer.")
    if kind == "sweep":
        if "target" not in raw:
            raise ConfigurationError(f"{where}: sweep requires field 'target'.")
        raw["target"] = _require_string(raw["target"], f"{where}.target")
        if "binding" in raw:
            binding = raw["binding"]
            if not isinstance(binding, dict):
                raise ConfigurationError(f"{where}.binding must be a YAML mapping.")
            expected_binding_fields = {
                "owner_node_id",
                "device_module",
                "endpoint",
                "parameter_id",
            }
            if set(binding) != expected_binding_fields:
                raise ConfigurationError(
                    f"{where}.binding must contain exactly "
                    "owner_node_id, device_module, endpoint, and parameter_id."
                )
            raw["binding"] = {
                key: _require_string(binding[key], f"{where}.binding.{key}")
                for key in expected_binding_fields
            }
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
                    type(segment["points"]) is not int or segment["points"] < 2
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
            if type(points) is not int or points < 2:
                raise ConfigurationError(f"{where}.points must be an integer >= 2.")
            if raw.get("spacing", "linear") not in {"linear", "log"}:
                raise ConfigurationError(f"{where}.spacing must be linear or log.")
    if kind == "repeat":
        count = raw.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100_000:
            raise ConfigurationError(f"{where}.count must be an integer in 1..100000.")
    if kind == "if":
        has_boolean = isinstance(raw.get("condition"), bool)
        if "condition" in raw and not has_boolean:
            raise ConfigurationError(f"{where}.condition must be true or false, not text or a number.")
        has_comparison = all(key in raw for key in ("left", "operator", "right"))
        if has_boolean == has_comparison:
            raise ConfigurationError(
                f"{where}: if requires either condition: true/false or left/operator/right."
            )
        if has_comparison and raw["operator"] not in {"<", "<=", "==", "!=", ">=", ">"}:
            raise ConfigurationError(f"{where}.operator is unsupported.")
    if kind == "connect" and raw.get("device") not in {"rigol", "keithley", "anritsu"}:
        raise ConfigurationError(f"{where}.device must identify rigol, keithley, or anritsu.")
    if kind in {"upload_to_elab", "upload_elab"}:
        template_id = raw.get("template_id")
        if template_id not in (None, "", 0, "0"):
            try:
                tid = int(template_id)
                if tid <= 0:
                    raise ValueError
                raw["template_id"] = tid
            except (TypeError, ValueError) as exc:
                raise ConfigurationError(
                    f"{where}.template_id must be a positive integer."
                ) from exc
        title_pattern = raw.get("title_pattern")
        if title_pattern is not None:
            if not isinstance(title_pattern, str) or not title_pattern.strip():
                raise ConfigurationError(
                    f"{where}.title_pattern must be non-empty text when specified."
                )
            try:
                title_pattern.format_map(
                    {
                        "run_name": "example",
                        "status": "completed",
                        "created_at": "2026-01-01T00:00:00Z",
                        "sample_id": "example",
                        "sample_name": "example",
                        "sample_coord": "example",
                        "device_label": "example",
                    }
                )
            except (KeyError, ValueError) as exc:
                raise ConfigurationError(
                    f"{where}.title_pattern may use only {{run_name}}, {{status}}, {{created_at}}, "
                    "{{sample_id}}, {{sample_name}}, {{sample_coord}} and {{device_label}}."
                ) from exc
        tags = raw.get("tags")
        if tags is not None:
            if isinstance(tags, str):
                raw["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
            elif isinstance(tags, (list, tuple)):
                raw["tags"] = [str(t).strip() for t in tags if str(t).strip()]
            else:
                raise ConfigurationError(f"{where}.tags must be a list or comma-separated string.")
        if raw.get("attach_hdf5") is False and raw.get("attach_csv") is False:
            raise ConfigurationError(f"{where}: at least one of attach_hdf5 or attach_csv must be enabled.")
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
    if type(version) is not int or version != 1:
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
