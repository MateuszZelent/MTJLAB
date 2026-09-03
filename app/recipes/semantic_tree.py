"""Immutable semantic representation of recipe execution and sweep axes.

The parser deliberately keeps the schema-version-1 recipe model small and
backwards compatible.  This module is the typed normalization boundary used
by presentation and (eventually) compilation: legacy device-local sweep
actions and explicit sweep nodes become the same axis/loop/setpoint shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from itertools import product
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from app.domain.errors import ConfigurationError
from app.domain.quantities import Quantity, QuantityError, parse_quantity
from app.recipes.models import Recipe, RecipeNode
from app.recipes.parameter_registry import parameter_descriptor
from app.recipes.sweep_points import generate_sweep_stage_points


class SweepBindingDraft:
    """Provider result before stages are parsed into typed quantities."""

    __slots__ = (
        "owner_node_id", "device_module", "endpoint", "parameter_id",
        "target", "dimension", "stages",
    )

    def __init__(
        self,
        *,
        owner_node_id: str,
        device_module: str,
        endpoint: str,
        parameter_id: str,
        target: str,
        dimension: str,
        stages: Sequence[Mapping[str, object]],
    ) -> None:
        self.owner_node_id = owner_node_id
        self.device_module = device_module
        self.endpoint = endpoint
        self.parameter_id = parameter_id
        self.target = target
        self.dimension = dimension
        self.stages = tuple(stages)


@dataclass(frozen=True, slots=True)
class SweepStageSpec:
    stage_index: int
    start: Quantity | None
    stop: Quantity | None
    value: Quantity | None
    spacing: str
    points: tuple[Quantity, ...]


@dataclass(frozen=True, slots=True)
class SweepAxisBinding:
    axis_id: str
    source_node_id: str
    owner_node_id: str
    device_module: str
    endpoint: str
    parameter_id: str
    target: str
    dimension: str
    stages: tuple[SweepStageSpec, ...]
    points: tuple[Quantity, ...]


@dataclass(frozen=True, slots=True)
class AxisPointContext:
    axis_id: str
    point_index: int
    point_count: int
    stage_index: int
    value_si: float
    active_setpoints_si: Mapping[str, float]
    loop_path: tuple[str, ...]


class SemanticNodeKind(StrEnum):
    SEQUENCE = "sequence"
    DEVICE = "device"
    SWEEP_AXIS = "sweep_axis"
    LOOP_BODY = "loop_body"
    SET_ROI_VALUE = "set_roi_value"
    ACTION = "action"
    FINALLY = "finally"
    GENERATED_SAFETY = "generated_safety"


@dataclass(frozen=True, slots=True)
class SemanticTreeNode:
    semantic_id: str
    kind: SemanticNodeKind
    source_node_id: str | None = None
    label: str = ""
    data: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    axis: SweepAxisBinding | None = None
    children: tuple["SemanticTreeNode", ...] = ()
    editable: bool = True
    draggable: bool = True


@dataclass(frozen=True, slots=True)
class SemanticMeasurementTree:
    roots: tuple[SemanticTreeNode, ...]
    by_id: Mapping[str, SemanticTreeNode]
    parent_by_id: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    children_by_id: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    point_contexts: Mapping[str, tuple[AxisPointContext, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    source_text: str = ""

    def require(self, semantic_id: str) -> SemanticTreeNode:
        try:
            return self.by_id[semantic_id]
        except KeyError as exc:
            raise ConfigurationError(f"Unknown semantic node {semantic_id!r}.") from exc


class AxisBindingResolver(Protocol):
    module_key: str

    def bind_legacy_action(
        self, node: RecipeNode, action: Mapping[str, object]
    ) -> SweepBindingDraft:
        ...


def _as_mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{where} must be a mapping.")
    return value


def _as_nonempty(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{where} must be non-empty text.")
    return value.strip()


def _all_recipe_nodes(recipe: Recipe) -> dict[str, RecipeNode]:
    result: dict[str, RecipeNode] = {}

    def visit(node: RecipeNode) -> None:
        result[node.id] = node
        for child in (*node.children, *node.else_children):
            visit(child)

    visit(recipe.root)
    for node in recipe.finally_nodes:
        visit(node)
    return result


def _binding_for_explicit(
    node: RecipeNode,
    recipe_nodes: Mapping[str, RecipeNode],
    resolvers: Mapping[str, AxisBindingResolver],
) -> SweepBindingDraft:
    raw = _as_mapping(node.data.get("binding"), f"sweep {node.id}.binding")
    expected = {"owner_node_id", "device_module", "endpoint", "parameter_id"}
    if set(raw) != expected:
        raise ConfigurationError(
            f"sweep {node.id}.binding must contain exactly "
            "owner_node_id, device_module, endpoint, and parameter_id."
        )
    owner = _as_nonempty(raw["owner_node_id"], f"sweep {node.id}.binding.owner_node_id")
    module = _as_nonempty(raw["device_module"], f"sweep {node.id}.binding.device_module")
    provider_module = "anritsu" if module == "anritsu_sg" else module
    endpoint = _as_nonempty(raw["endpoint"], f"sweep {node.id}.binding.endpoint")
    parameter_id = _as_nonempty(raw["parameter_id"], f"sweep {node.id}.binding.parameter_id")
    if owner not in recipe_nodes:
        raise ConfigurationError(f"sweep {node.id}.binding.owner_node_id {owner!r} is unknown.")
    if provider_module not in resolvers:
        raise ConfigurationError(f"sweep {node.id}.binding.device_module {module!r} has no resolver.")
    target = _as_nonempty(node.data.get("target"), f"sweep {node.id}.target")
    try:
        descriptor = parameter_descriptor(target)
    except KeyError as exc:
        raise ConfigurationError(f"Unknown sweep target {target!r}.") from exc
    if descriptor.device_module != provider_module:
        raise ConfigurationError(
            f"sweep {node.id}.binding.device_module {module!r} does not own target {target!r}."
        )
    return SweepBindingDraft(
        owner_node_id=owner,
        device_module=provider_module,
        endpoint=endpoint,
        parameter_id=parameter_id,
        target=target,
        dimension=descriptor.dimension,
        stages=_raw_stages(node.data),
    )


def _raw_stages(data: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    segments = data.get("segments")
    if segments is not None:
        if not isinstance(segments, (list, tuple)) or not segments:
            raise ConfigurationError("A sweep axis requires at least one non-empty stage.")
        return tuple(_as_mapping(segment, "sweep stage") for segment in segments)
    required = ("start", "stop", "points")
    if any(key not in data for key in required):
        raise ConfigurationError("A sweep axis requires start, stop, and points.")
    return ({key: data[key] for key in (*required, "spacing") if key in data},)


def _binding_for_legacy(
    node: RecipeNode,
    action: Mapping[str, object],
    resolvers: Mapping[str, AxisBindingResolver],
) -> SweepBindingDraft:
    module = _as_nonempty(node.data.get("device_module"), f"node {node.id}.device_module")
    resolver = resolvers.get(module)
    # The Anritsu signal-generator block is a recipe-facing variant of the
    # single Anritsu instrument module.  Keep ``anritsu_sg`` in authored YAML
    # (and in the dedicated editor) while resolving its sweep through the
    # registered, safety-reviewed Anritsu provider.
    if resolver is None and module == "anritsu_sg":
        resolver = resolvers.get("anritsu")
    if resolver is None:
        raise ConfigurationError(f"No sweep resolver registered for device module {module!r}.")
    try:
        draft = resolver.bind_legacy_action(node, action)
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(f"Invalid legacy sweep binding on {node.id}: {exc}") from exc
    if not isinstance(draft, SweepBindingDraft):
        raise ConfigurationError(f"Resolver for {module!r} returned an invalid sweep binding.")
    return draft


def _typed_binding(
    draft: SweepBindingDraft, axis_id: str, source_node_id: str
) -> SweepAxisBinding:
    for field_name in ("owner_node_id", "device_module", "endpoint", "parameter_id", "target", "dimension"):
        _as_nonempty(getattr(draft, field_name), f"sweep binding {field_name}")
    try:
        descriptor = parameter_descriptor(draft.target)
    except KeyError as exc:
        raise ConfigurationError(f"Unknown sweep target {draft.target!r}.") from exc
    if descriptor.dimension != draft.dimension:
        raise ConfigurationError(
            f"Sweep target {draft.target!r} dimension {descriptor.dimension!r} "
            f"does not match binding dimension {draft.dimension!r}."
        )
    if descriptor.device_module != draft.device_module:
        raise ConfigurationError(
            f"Sweep target {draft.target!r} belongs to {descriptor.device_module!r}, "
            f"not {draft.device_module!r}."
        )
    stages_raw = tuple(draft.stages)
    if not stages_raw:
        raise ConfigurationError(f"Sweep axis {axis_id!r} must contain at least one stage.")
    try:
        generated_stages = generate_sweep_stage_points(stages_raw, draft.dimension)
    except (ConfigurationError, QuantityError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"Invalid stages for sweep axis {axis_id!r}: {exc}") from exc
    stages: list[SweepStageSpec] = []
    for index, (raw, points) in enumerate(zip(stages_raw, generated_stages, strict=True)):
        if not points:
            raise ConfigurationError(f"Sweep axis {axis_id!r} contains an empty stage.")
        if "value" in raw:
            value = parse_quantity(raw["value"], draft.dimension)
            stages.append(SweepStageSpec(index, None, None, value, "linear", points))
        else:
            try:
                start = parse_quantity(raw["start"], draft.dimension)
                stop = parse_quantity(raw["stop"], draft.dimension)
            except (KeyError, QuantityError, TypeError, ValueError) as exc:
                raise ConfigurationError(
                    f"Invalid dimension in sweep axis {axis_id!r}: {exc}"
                ) from exc
            stages.append(
                SweepStageSpec(index, start, stop, None, str(raw.get("spacing", "linear")), points)
            )
    points = tuple(point for stage in stages for point in stage.points)
    if not points:
        raise ConfigurationError(f"Sweep axis {axis_id!r} must generate points.")
    return SweepAxisBinding(
        axis_id=axis_id,
        source_node_id=source_node_id,
        owner_node_id=draft.owner_node_id,
        device_module=draft.device_module,
        endpoint=draft.endpoint,
        parameter_id=draft.parameter_id,
        target=draft.target,
        dimension=draft.dimension,
        stages=tuple(stages),
        points=points,
    )


def _axis_id(node: RecipeNode, draft: SweepBindingDraft | None = None) -> str:
    if draft is None:
        return node.id
    parameter = draft.parameter_id.replace(".", "-").replace("/", "-")
    return f"{node.id}.axis.{parameter}"


def _device_prefix(node: RecipeNode) -> str | None:
    """Return the concise operator-facing device name for a recipe node."""

    data = node.data
    configuration = data.get("configuration")
    configuration = configuration if isinstance(configuration, Mapping) else data
    module = str(data.get("device_module", ""))
    node_type = node.type
    if module == "keithley" or node_type.startswith("configure_keithley"):
        channel = str(configuration.get("channel", data.get("channel", ""))).upper()
        return f"Keithley {channel}" if channel in {"A", "B"} else "Keithley"
    if module == "rigol" or node_type.startswith("configure_rigol"):
        channel = str(configuration.get("channel", data.get("channel", "")))
        return f"Rigol CH{channel}" if channel in {"1", "2"} else "Rigol"
    if module == "anritsu" or node_type.startswith("configure_anritsu"):
        return "Anritsu"
    return None


def _parameter_tail(target: str) -> str:
    descriptor = parameter_descriptor(target)
    text = descriptor.ui_label.split("·", 1)[-1].strip()
    return text[:1].upper() + text[1:] if text else target.rsplit(".", 1)[-1]


def _axis_slug(target: str) -> str:
    parts = target.split(".")
    if len(parts) >= 3 and parts[0] == "keithley":
        name = parts[-1]
        if name in {"current", "voltage"}:
            return f"source-{name}"
        if name.startswith("compliance_"):
            return name.replace("_", "-")
        return name.replace("_", "-")
    # Channel/endpoint identifiers are already shown by the axis label.  The
    # loop caption should read “For each frequency point”, not “For each
    # 1-frequency point”.
    return parts[-1].replace("_", "-") if parts else target


def _binding_device_prefix(binding: SweepAxisBinding, recipe_nodes: Mapping[str, RecipeNode]) -> str:
    owner = recipe_nodes.get(binding.owner_node_id)
    if owner is not None:
        prefix = _device_prefix(owner)
        if prefix:
            return prefix
    if binding.device_module == "keithley" and binding.endpoint.upper() in {"A", "B"}:
        return f"Keithley {binding.endpoint.upper()}"
    if binding.device_module == "rigol" and str(binding.endpoint) in {"1", "2"}:
        return f"Rigol CH{binding.endpoint}"
    if binding.device_module == "anritsu":
        return "Anritsu"
    return binding.endpoint


def _canonical_parameter_id(target: str) -> str:
    """Map a canonical target to the provider's stable control identity."""

    parts = target.split(".")
    if len(parts) >= 3 and parts[0] == "keithley":
        return {
            "current": "source.level",
            "voltage": "source.level",
            "compliance_voltage": "source.compliance",
            "compliance_current": "source.compliance",
            "settling_time": "measurement.settling_time",
        }.get(parts[-1], parts[-1])
    if len(parts) >= 3 and parts[0] == "rigol":
        return {
            "frequency": "carrier.frequency",
            "high_level": "carrier.high_level",
            "low_level": "carrier.low_level",
        }.get(parts[-1], parts[-1])
    if parts[:2] == ["anritsu", "sg"]:
        return f"signal_generator.{parts[-1]}"
    if parts[:2] == ["anritsu", "spectrum"]:
        return f"spectrum.{parts[-1]}"
    return target.rsplit(".", 1)[-1]


def _endpoint_for_target(target: str) -> str:
    parts = target.split(".")
    if parts[:2] == ["anritsu", "sg"]:
        return "SG"
    if parts[:2] == ["anritsu", "spectrum"]:
        return "SPECTRUM"
    return parts[1] if len(parts) > 1 else target


def _label(node: RecipeNode) -> str:
    operation = node.data.get("operation")
    if node.type == "sequence" and not node.data.get("device_module"):
        return "Measurement sequence"
    if node.type.startswith("configure_") or node.data.get("device_module"):
        device = _device_prefix(node)
        if device and (
            node.type.startswith("configure")
            or str(operation or "").startswith("configure")
            or node.data.get("device_module")
        ):
            return f"{device} · configuration"
    if node.type == "acquire_spectrum":
        return "Acquire spectrum · Anritsu"
    if node.type == "acquire_reference":
        return "Acquire reference · Anritsu"
    if node.type == "wait":
        return f"Wait · {node.data.get('duration', 'timing')}"
    if node.type == "set_keithley_output":
        channel = str(node.data.get("channel", "")).upper()
        enabled = bool(node.data.get("enabled", False))
        return f"Keithley {channel or 'output'} · OUTPUT {'ON' if enabled else 'OFF'}"
    if node.type == "set_rigol_output":
        channel = str(node.data.get("channel", ""))
        enabled = bool(node.data.get("enabled", False))
        return f"Rigol CH{channel or '?'} · OUTPUT {'ON' if enabled else 'OFF'}"
    if node.type == "set_anritsu_sg_output":
        enabled = bool(node.data.get("enabled", False))
        return f"Anritsu SG · OUTPUT {'ON' if enabled else 'OFF'}"
    if node.type == "update_keithley_level":
        channel = str(node.data.get("channel", "")).upper()
        return f"Set Keithley {channel} · source level"
    if node.type == "update_keithley_compliance":
        channel = str(node.data.get("channel", "")).upper()
        return f"Set Keithley {channel} · compliance"
    if node.type == "update_rigol_frequency":
        channel = str(node.data.get("channel", ""))
        return f"Set Rigol CH{channel or '?'} · frequency"
    if node.type == "update_rigol_levels":
        channel = str(node.data.get("channel", ""))
        return f"Set Rigol CH{channel or '?'} · levels"
    if node.type == "update_anritsu_sg":
        return "Set Anritsu SG · carrier"
    if node.type == "checkpoint":
        return f"Checkpoint · {node.data.get('label', node.id)}"
    if node.type == "connect":
        return f"Connect · {node.data.get('device', node.id)}"
    if node.type == "comment":
        return f"Comment · {node.data.get('text', node.data.get('comment', ''))}".rstrip(" ·")
    if operation:
        return str(operation)
    return str(node.data.get("label") or node.type or node.id)


def _mapping(data: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(data))


def normalize_recipe_tree(
    recipe: Recipe,
    resolvers: Mapping[str, AxisBindingResolver],
) -> SemanticMeasurementTree:
    """Return one immutable semantic snapshot for a schema-version-1 recipe."""

    recipe_nodes = _all_recipe_nodes(recipe)
    built_ids: set[str] = set()

    def add_id(semantic_id: str) -> None:
        if semantic_id in built_ids:
            raise ConfigurationError(f"Duplicate semantic node identifier: {semantic_id!r}.")
        built_ids.add(semantic_id)

    def make_axis(node: RecipeNode, draft: SweepBindingDraft) -> SemanticTreeNode:
        axis_id = _axis_id(node, draft) if node.type != "sweep" else _axis_id(node)
        # Explicit nodes use their source ID. Legacy nodes derive a stable ID
        # from the owner and parameter, as this is independent of list order.
        if node.type != "sweep":
            axis_id = f"{draft.owner_node_id}.axis.{draft.parameter_id.replace('.', '-').replace('/', '-') }"
        binding = _typed_binding(draft, axis_id, node.id)
        add_id(axis_id)
        roi_id = f"{axis_id}.set-roi-value"
        loop_id = f"{axis_id}.loop"
        add_id(loop_id)
        add_id(roi_id)
        roi = SemanticTreeNode(
            roi_id,
            SemanticNodeKind.SET_ROI_VALUE,
            node.id,
            f"Set ROI value · {_binding_device_prefix(binding, recipe_nodes)} · {_parameter_tail(binding.target).lower()}",
            _mapping({"target": binding.target, "dimension": binding.dimension}),
            None,
            (),
            False,
            False,
        )
        provider = resolvers.get(binding.device_module)
        represented_kinds: frozenset[str] = frozenset()
        action_kinds = getattr(provider, "axis_action_kinds", None)
        if callable(action_kinds):
            represented_kinds = frozenset(
                str(kind) for kind in action_kinds(binding)
            )
        # A provider-generated Set ROI value is the single semantic operation
        # for the axis.  Its authored technical update (when present in a
        # legacy recipe) is intentionally omitted from the operator tree; it
        # remains available in the compiled plan/event log for diagnostics.
        body_children = tuple(
            convert(child)
            for child in node.children
            if child.type not in represented_kinds
        )
        body = SemanticTreeNode(
            loop_id,
            SemanticNodeKind.LOOP_BODY,
            node.id,
            f"For each {_axis_slug(binding.target)} point",
            _mapping({"point_count": len(binding.points)}),
            None,
            (roi, *body_children),
        )
        return SemanticTreeNode(
            axis_id,
            SemanticNodeKind.SWEEP_AXIS,
            node.id,
            f"Sweep axis · {_parameter_tail(binding.target)}",
            _mapping({"target": binding.target, "dimension": binding.dimension}),
            binding,
            (body,),
        )

    def convert(node: RecipeNode, force_kind: SemanticNodeKind | None = None) -> SemanticTreeNode:
        if node.type == "sweep":
            if "binding" in node.data:
                draft = _binding_for_explicit(node, recipe_nodes, resolvers)
            else:
                target = _as_nonempty(node.data.get("target"), f"sweep {node.id}.target")
                try:
                    descriptor = parameter_descriptor(target)
                except KeyError as exc:
                    raise ConfigurationError(f"Unknown sweep target {target!r}.") from exc
                draft = SweepBindingDraft(
                    owner_node_id=node.id,
                    device_module=descriptor.device_module,
                    endpoint=_endpoint_for_target(target),
                    parameter_id=_canonical_parameter_id(target),
                    target=target,
                    dimension=descriptor.dimension,
                    stages=_raw_stages(node.data),
                )
            return make_axis(node, draft)

        data = node.data
        legacy_sweeps: list[Mapping[str, object]] = []
        if data.get("parameter_actions") is not None:
            actions = data["parameter_actions"]
            if not isinstance(actions, (list, tuple)):
                raise ConfigurationError(f"node {node.id}.parameter_actions must be a list.")
            for action in actions:
                mapping = _as_mapping(action, f"node {node.id}.parameter_actions entry")
                if mapping.get("mode") == "sweep":
                    legacy_sweeps.append(mapping)
        if len(legacy_sweeps) > 1:
            raise ConfigurationError(f"node {node.id}: multiple legacy local sweeps are ambiguous.")

        kind = force_kind
        if kind is None:
            kind = (
                SemanticNodeKind.DEVICE
                if data.get("device_module") or node.type.startswith("configure_")
                else SemanticNodeKind.SEQUENCE
                if node.type in {"sequence", "repeat", "if"}
                else SemanticNodeKind.ACTION
            )
        if legacy_sweeps:
            draft = _binding_for_legacy(node, legacy_sweeps[0], resolvers)
            axis = make_axis(node, draft)
            children = (axis,)
        else:
            children = tuple(convert(child) for child in node.children)
            children += tuple(convert(child) for child in node.else_children)
        add_id(node.id)
        return SemanticTreeNode(node.id, kind, node.id, _label(node), _mapping(data), None, children)

    # Finally is a first-class semantic branch shared by Builder and Execution.
    # Its generated safety actions are immutable presentation rows; the
    # concrete Run Engine shutdown manifest remains authoritative at runtime.
    generated_shutdown = (
        ("keithley.outputs_off", "Keithley A + B OUTPUT OFF"),
        ("rigol.outputs_off", "Rigol CH1 + CH2 OUTPUT OFF"),
        ("anritsu.rf_off_and_abort", "Anritsu RF OUTPUT OFF + abort"),
        ("storage.flush_checkpoint", "Measurement checkpoint flush"),
    )
    # The cleanup manifest is kept as metadata on the single final row. It is
    # deliberately not expanded into technical action rows in the operator
    # tree; the detailed, durable shutdown trace remains in the event log.
    cleanup_ids = tuple(str(node.id) for node in recipe.finally_nodes)
    generated_ids = tuple(action_id for action_id, _label_text in generated_shutdown)
    finally_root = SemanticTreeNode(
        "__finally__",
        SemanticNodeKind.FINALLY,
        None,
        "Finally — safe shutdown",
        _mapping({
            "detail": "Guaranteed safe shutdown",
            "operator_cleanup_ids": cleanup_ids,
            "generated_actions": generated_ids,
        }),
        None,
        (),
        False,
        False,
    )
    roots = (convert(recipe.root), finally_root)

    parent_by_id: dict[str, str] = {}
    children_by_id: dict[str, tuple[str, ...]] = {}
    by_id: dict[str, SemanticTreeNode] = {}

    def index(node: SemanticTreeNode, parent: str | None = None) -> None:
        if node.semantic_id in by_id:
            raise ConfigurationError(f"Duplicate semantic node identifier: {node.semantic_id!r}.")
        by_id[node.semantic_id] = node
        if parent is not None:
            parent_by_id[node.semantic_id] = parent
        children_by_id[node.semantic_id] = tuple(child.semantic_id for child in node.children)
        for child in node.children:
            index(child, node.semantic_id)

    for root in roots:
        index(root)

    def validate_active(node: SemanticTreeNode, active_targets: tuple[str, ...] = ()) -> None:
        current = active_targets
        if node.kind is SemanticNodeKind.SWEEP_AXIS and node.axis is not None:
            if node.axis.target in current:
                raise ConfigurationError(
                    f"duplicate active sweep binding for target {node.axis.target!r}."
                )
            current = (*current, node.axis.target)
        for child in node.children:
            validate_active(child, current)

    for root in roots:
        validate_active(root)

    contexts: dict[str, tuple[AxisPointContext, ...]] = {}

    def collect_contexts(node: SemanticTreeNode, active: tuple[SweepAxisBinding, ...] = ()) -> None:
        next_active = active
        if node.kind is SemanticNodeKind.SWEEP_AXIS and node.axis is not None:
            axis = node.axis
            if any(previous.target == axis.target for previous in active):
                raise ConfigurationError(f"duplicate active sweep binding for target {axis.target!r}.")
            records: list[AxisPointContext] = []
            combinations = product(*(previous.points for previous in active), axis.points)
            for combination in combinations:
                own = combination[-1]
                point_index = axis.points.index(own)
                stage_index = next(
                    stage.stage_index for stage in axis.stages if own in stage.points
                )
                setpoints = {
                    previous.target: value.si_value
                    for previous, value in zip(active, combination[:-1], strict=True)
                }
                setpoints[axis.target] = own.si_value
                records.append(
                    AxisPointContext(
                        axis.axis_id,
                        point_index,
                        len(axis.points),
                        stage_index,
                        own.si_value,
                        MappingProxyType(setpoints),
                        tuple(previous.source_node_id for previous in active) + (axis.source_node_id,),
                    )
                )
            contexts[axis.source_node_id] = tuple(records)
            next_active = (*active, axis)
        for child in node.children:
            collect_contexts(child, next_active)

    for root in roots:
        collect_contexts(root)
    return SemanticMeasurementTree(
        roots,
        MappingProxyType(by_id),
        MappingProxyType(parent_by_id),
        MappingProxyType(children_by_id),
        MappingProxyType(contexts),
        recipe.source_text,
    )


__all__ = [
    "AxisBindingResolver", "AxisPointContext", "SemanticMeasurementTree",
    "SemanticNodeKind", "SemanticTreeNode", "SweepAxisBinding", "SweepBindingDraft",
    "SweepStageSpec", "normalize_recipe_tree",
]
