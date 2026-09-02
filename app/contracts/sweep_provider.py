"""Pure contracts for device-owned sweep-axis binding and point compilation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Protocol

from app.domain.quantities import Quantity

if TYPE_CHECKING:
    from app.recipes.models import RecipeNode
    from app.recipes.semantic_tree import SweepAxisBinding, SweepBindingDraft
    from app.settings.models import StationSettings


@dataclass(frozen=True, slots=True)
class CompiledAxisSetpoint:
    """One validated provider update, still independent of an adapter instance."""

    action_kind: str
    payload: Mapping[str, object]
    requested_si: float
    applied_si: float
    verification_field: str = "readback"


class DeviceSweepProvider(Protocol):
    """Device-owned binding and conversion surface used by the recipe compiler."""

    module_key: str

    def bind_legacy_action(
        self, node: "RecipeNode", action: Mapping[str, object]
    ) -> "SweepBindingDraft": ...

    def validate_binding(
        self, node: "RecipeNode", binding: "SweepAxisBinding"
    ) -> None: ...

    def compile_point(
        self,
        node: "RecipeNode",
        binding: "SweepAxisBinding",
        value: Quantity,
        context: Mapping[str, Quantity],
        settings: "StationSettings",
    ) -> CompiledAxisSetpoint: ...
