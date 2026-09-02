"""Keithley-owned sweep binding and safe point compilation."""

from __future__ import annotations

from collections.abc import Mapping

from app.contracts.sweep_provider import CompiledAxisSetpoint
from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.quantities import DIMENSION_CURRENT, DIMENSION_TIME, DIMENSION_VOLTAGE, Quantity, parse_quantity
from app.recipes.models import RecipeNode
from app.recipes.parameter_registry import parameter_descriptor
from app.recipes.semantic_tree import SweepAxisBinding, SweepBindingDraft
from app.safety.keithley import quantize_keithley_value
from app.settings.models import StationSettings


class KeithleySweepProvider:
    module_key = "keithley"

    @staticmethod
    def _channel_mode(node: RecipeNode) -> tuple[str, str]:
        configuration = node.data.get("configuration")
        configuration = configuration if isinstance(configuration, Mapping) else node.data
        channel = str(configuration.get("channel", node.data.get("channel", ""))).upper()
        mode = str(
            configuration.get(
                "source_mode",
                configuration.get("mode", node.data.get("source_mode", node.data.get("mode", ""))),
            )
        ).lower()
        if channel not in {"A", "B"} or mode not in {"current", "voltage", "measure_only"}:
            raise ConfigurationError(f"{node.id}: invalid Keithley channel or source mode.")
        return channel, mode

    def bind_legacy_action(self, node: RecipeNode, action: Mapping[str, object]) -> SweepBindingDraft:
        channel, mode = self._channel_mode(node)
        parameter_id = str(action.get("parameter_id", ""))
        if parameter_id == "source.level":
            if mode == "measure_only":
                raise ConfigurationError(f"{node.id}: measure_only cannot sweep source.level.")
            target = f"keithley.{channel}.{mode}"
        elif parameter_id == "source.compliance":
            if mode == "measure_only":
                raise ConfigurationError(f"{node.id}: measure_only cannot sweep source.compliance.")
            target = f"keithley.{channel}.{'compliance_voltage' if mode == 'current' else 'compliance_current'}"
        elif parameter_id == "measurement.settling_time":
            target = f"keithley.{channel}.settling_time"
        else:
            raise ConfigurationError(f"{node.id}: unsupported Keithley sweep parameter {parameter_id!r}.")
        descriptor = parameter_descriptor(target)
        stages = action.get("segments", ())
        if not isinstance(stages, (list, tuple)) or not stages:
            raise ConfigurationError(f"{node.id}: {parameter_id} sweep requires a non-empty ROI.")
        return SweepBindingDraft(
            owner_node_id=node.id,
            device_module=self.module_key,
            endpoint=channel,
            parameter_id=parameter_id,
            target=target,
            dimension=descriptor.dimension,
            stages=tuple(item for item in stages if isinstance(item, Mapping)),
        )

    def binding_for_target(self, node: RecipeNode, target: str) -> SweepAxisBinding:
        channel, _mode = self._channel_mode(node)
        descriptor = parameter_descriptor(target)
        if descriptor.device_module != self.module_key or f".{channel}." not in target:
            raise ConfigurationError(f"{node.id}: target {target!r} is not owned by Keithley {channel}.")
        parameter_id = {
            "current": "source.level",
            "voltage": "source.level",
            "compliance_voltage": "source.compliance",
            "compliance_current": "source.compliance",
            "settling_time": "measurement.settling_time",
        }.get(target.rsplit(".", 1)[-1])
        if parameter_id is None:
            raise ConfigurationError(f"{node.id}: unsupported Keithley target {target!r}.")
        return SweepAxisBinding(
            axis_id=f"{node.id}.axis.{parameter_id.replace('.', '-')}",
            source_node_id=node.id,
            owner_node_id=node.id,
            device_module=self.module_key,
            endpoint=channel,
            parameter_id=parameter_id,
            target=target,
            dimension=descriptor.dimension,
            stages=(),
            points=(),
        )

    def validate_binding(self, node: RecipeNode, binding: SweepAxisBinding) -> None:
        channel, mode = self._channel_mode(node)
        if binding.endpoint != channel or binding.device_module != self.module_key:
            raise ConfigurationError(f"{node.id}: Keithley sweep binding endpoint does not match configuration.")
        if binding.target.startswith(f"keithley.{channel}.") is False:
            raise ConfigurationError(f"{node.id}: Keithley target {binding.target!r} does not match endpoint.")
        if mode == "measure_only" and binding.parameter_id in {"source.level", "source.compliance"}:
            raise ConfigurationError(f"{node.id}: measure_only cannot sweep {binding.parameter_id}.")

    def compile_point(
        self,
        node: RecipeNode,
        binding: SweepAxisBinding,
        value: Quantity,
        context: Mapping[str, Quantity],
        settings: StationSettings,
    ) -> CompiledAxisSetpoint:
        self.validate_binding(node, binding)
        descriptor = parameter_descriptor(binding.target)
        value.require_dimension(descriptor.dimension)
        channel = binding.endpoint
        mode = "current" if binding.target.rsplit(".", 1)[-1] in {"current", "compliance_voltage"} else "voltage"
        if binding.target.rsplit(".", 1)[-1] == "settling_time":
            if value.si_value < 0 or value.si_value > 3600:
                raise SafetyViolation(f"{node.id}: settling time is outside 0..3600 s.")
            applied = value.si_value
            return CompiledAxisSetpoint(
                "wait", {"duration_s": applied}, value.si_value, applied, "configured_unchanged"
            )
        limits = settings.keithley.safety.channels[channel].lab_limits
        limit = limits.source_current if descriptor.dimension == DIMENSION_CURRENT else limits.source_voltage
        if binding.target.rsplit(".", 1)[-1] in {"compliance_voltage", "compliance_current"}:
            limit = limits.voltage_compliance if descriptor.dimension == DIMENSION_VOLTAGE else limits.current_compliance
        if limit.enabled:
            minimum = parse_quantity(limit.min, descriptor.dimension).si_value
            maximum = parse_quantity(limit.max, descriptor.dimension).si_value
            if not minimum - 1e-12 <= value.si_value <= maximum + 1e-12:
                raise SafetyViolation(f"{node.id}: Keithley value {value.si_value:g} SI is outside station limits.")
        applied = quantize_keithley_value(value.si_value, descriptor.dimension)
        if binding.parameter_id == "source.level":
            return CompiledAxisSetpoint(
                "update_keithley_level",
                {"channel": channel, "mode": mode, "level_si": applied},
                value.si_value,
                applied,
            )
        return CompiledAxisSetpoint(
            "update_keithley_compliance",
            {"channel": channel, "mode": mode, "compliance_si": applied},
            value.si_value,
            applied,
        )


PROVIDER = KeithleySweepProvider()
