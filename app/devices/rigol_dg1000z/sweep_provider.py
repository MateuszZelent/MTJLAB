"""Rigol-owned sweep binding and point compilation."""

from __future__ import annotations

from collections.abc import Mapping

from app.contracts.sweep_provider import CompiledAxisSetpoint
from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.quantities import DIMENSION_FREQUENCY, DIMENSION_VOLTAGE, Quantity, parse_quantity
from app.recipes.models import RecipeNode
from app.recipes.parameter_registry import parameter_descriptor
from app.recipes.semantic_tree import SweepAxisBinding, SweepBindingDraft
from app.safety.rigol_current import quantize_rigol_frequency, quantize_rigol_voltage
from app.settings.models import StationSettings


class RigolSweepProvider:
    module_key = "rigol"

    @staticmethod
    def _channel(node: RecipeNode) -> int:
        configuration = node.data.get("configuration")
        configuration = configuration if isinstance(configuration, Mapping) else node.data
        try:
            channel = int(configuration.get("channel", node.data.get("channel", 0)))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"{node.id}: invalid Rigol channel.") from exc
        if channel not in {1, 2}:
            raise ConfigurationError(f"{node.id}: Rigol channel must be 1 or 2.")
        return channel

    def bind_legacy_action(self, node: RecipeNode, action: Mapping[str, object]) -> SweepBindingDraft:
        channel = self._channel(node)
        parameter_id = str(action.get("parameter_id", ""))
        targets = {
            "carrier.frequency": (f"rigol.{channel}.frequency", DIMENSION_FREQUENCY),
            "carrier.high_level": (f"rigol.{channel}.high_level", DIMENSION_VOLTAGE),
            "carrier.low_level": (f"rigol.{channel}.low_level", DIMENSION_VOLTAGE),
        }
        try:
            target, dimension = targets[parameter_id]
        except KeyError as exc:
            raise ConfigurationError(f"{node.id}: unsupported Rigol sweep parameter {parameter_id!r}.") from exc
        stages = action.get("segments", ())
        if not isinstance(stages, (list, tuple)) or not stages:
            raise ConfigurationError(f"{node.id}: {parameter_id} sweep requires a non-empty ROI.")
        return SweepBindingDraft(
            owner_node_id=node.id, device_module=self.module_key, endpoint=str(channel),
            parameter_id=parameter_id, target=target, dimension=dimension,
            stages=tuple(item for item in stages if isinstance(item, Mapping)),
        )

    def binding_for_target(self, node: RecipeNode, target: str) -> SweepAxisBinding:
        channel = self._channel(node)
        descriptor = parameter_descriptor(target)
        if descriptor.device_module != self.module_key or not target.startswith(f"rigol.{channel}."):
            raise ConfigurationError(f"{node.id}: target {target!r} is not owned by Rigol CH{channel}.")
        parameter = target.rsplit(".", 1)[-1]
        parameter_id = {"frequency": "carrier.frequency", "high_level": "carrier.high_level", "low_level": "carrier.low_level"}.get(parameter)
        if parameter_id is None:
            raise ConfigurationError(f"{node.id}: unsupported Rigol target {target!r}.")
        return SweepAxisBinding(f"{node.id}.axis.{parameter_id.replace('.', '-')}", node.id, node.id, self.module_key, str(channel), parameter_id, target, descriptor.dimension, (), ())

    def validate_binding(self, node: RecipeNode, binding: SweepAxisBinding) -> None:
        if binding.device_module != self.module_key or binding.endpoint != str(self._channel(node)):
            raise ConfigurationError(f"{node.id}: Rigol sweep binding endpoint does not match configuration.")

    def compile_point(self, node: RecipeNode, binding: SweepAxisBinding, value: Quantity, context: Mapping[str, Quantity], settings: StationSettings) -> CompiledAxisSetpoint:
        self.validate_binding(node, binding)
        descriptor = parameter_descriptor(binding.target)
        value.require_dimension(descriptor.dimension)
        channel = int(binding.endpoint)
        channel_settings = settings.rigol.safety.channels[str(channel)]
        limit = getattr(channel_settings.lab_limits, descriptor.axis_target.rsplit(".", 1)[-1], None)
        if limit is not None and limit.enabled:
            lower = parse_quantity(limit.min, descriptor.dimension).si_value
            upper = parse_quantity(limit.max, descriptor.dimension).si_value
            if not lower <= value.si_value <= upper:
                raise SafetyViolation(f"{node.id}: Rigol value {value.si_value:g} SI is outside station limits.")
        configuration = node.data.get("configuration")
        configuration = configuration if isinstance(configuration, Mapping) else node.data
        if not isinstance(configuration, Mapping) or not configuration.get("low_level"):
            # Explicit axes commonly own a configure_rigol child. Use that
            # authored fixed level for the non-swept side of a level pair.
            for child in node.children:
                if child.type == "configure_rigol":
                    configuration = child.data
                    break
        def level(target: str, key: str) -> float:
            item = context.get(target)
            if item is not None:
                item.require_dimension(DIMENSION_VOLTAGE)
                return item.si_value
            raw = configuration.get(key, "0 V")
            return parse_quantity(raw, DIMENSION_VOLTAGE).si_value
        if binding.parameter_id == "carrier.frequency":
            applied = quantize_rigol_frequency(value.si_value)
            return CompiledAxisSetpoint("update_rigol_frequency", {"channel": channel, "frequency_hz": applied}, value.si_value, applied)
        high = level(f"rigol.{channel}.high_level", "high_level")
        low = level(f"rigol.{channel}.low_level", "low_level")
        if binding.parameter_id == "carrier.high_level":
            high = value.si_value
        else:
            low = value.si_value
        applied_high = quantize_rigol_voltage(high)
        applied_low = quantize_rigol_voltage(low)
        return CompiledAxisSetpoint(
            "update_rigol_levels",
            {"channel": channel, "high_level_v": applied_high, "low_level_v": applied_low},
            value.si_value,
            applied_high if binding.parameter_id == "carrier.high_level" else applied_low,
        )


PROVIDER = RigolSweepProvider()
