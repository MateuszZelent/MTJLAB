"""MOKE-owned voltage axis. Field is a calibrated result, never a guessed axis."""

from __future__ import annotations

import re

from app.contracts.sweep_provider import CompiledAxisSetpoint
from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_VOLTAGE
from app.recipes.parameter_registry import parameter_descriptor
from app.recipes.semantic_tree import SweepAxisBinding, SweepBindingDraft
from app.safety.moke_box import MokeVoltagePlan, control_profile_from_settings


def voltage_channel(target: str) -> int:
    match = re.fullmatch(r"moke_box\.vout([0-7])\.voltage", target)
    if match is None:
        raise ConfigurationError(f"Unsupported MOKE voltage target {target!r}.")
    return int(match.group(1))


class MokeSweepProvider:
    module_key = "moke_box"

    @staticmethod
    def axis_action_kinds(binding):
        return frozenset({"update_moke_voltage"})

    def binding_for_target(self, node, target):
        channel = voltage_channel(target)
        descriptor = parameter_descriptor(target)
        return SweepAxisBinding(
            axis_id=f"{node.id}.axis.output-voltage", source_node_id=node.id,
            owner_node_id=node.id, device_module=self.module_key, endpoint=f"vout{channel}",
            parameter_id="output.voltage", target=target, dimension=descriptor.dimension,
            stages=(), points=(),
        )

    def bind_legacy_action(self, node, action):
        channel = node.data.get("channel")
        if type(channel) is not int or channel not in range(8) or action.get("parameter_id") != "output.voltage":
            raise ConfigurationError("MOKE voltage sweep requires an explicit channel and output.voltage binding.")
        target = f"moke_box.vout{channel}.voltage"
        stages = action.get("segments")
        if not isinstance(stages, (list, tuple)) or not stages:
            raise ConfigurationError("MOKE voltage sweep requires non-empty ROI stages.")
        return SweepBindingDraft(
            owner_node_id=node.id, device_module=self.module_key, endpoint=f"vout{channel}",
            parameter_id="output.voltage", target=target, dimension=DIMENSION_VOLTAGE,
            stages=tuple(stages),
        )

    def validate_binding(self, node, binding):
        channel = voltage_channel(binding.target)
        if binding.device_module != self.module_key or binding.endpoint != f"vout{channel}" or binding.parameter_id != "output.voltage":
            raise ConfigurationError("MOKE voltage axis does not match its device/channel/parameter binding.")

    def compile_point(self, node, binding, value, context, settings):
        self.validate_binding(node, binding)
        value.require_dimension(DIMENSION_VOLTAGE)
        simulation = bool((settings.moke_box.endpoint or "").startswith("SIM::MOKE"))
        profile = control_profile_from_settings(settings, simulation=simulation)
        if voltage_channel(binding.target) != profile.channel:
            raise ConfigurationError("MOKE sweep channel is not the qualified electromagnet channel.")
        plan = MokeVoltagePlan(profile.fingerprint, profile.channel,
                               profile.minimum_v, profile.maximum_v, (value.si_value,))
        plan.validate(profile)
        return CompiledAxisSetpoint(
            "update_moke_voltage", {"channel": profile.channel, "voltage_v": value.si_value},
            value.si_value, plan.applied_voltage(value.si_value),
        )


PROVIDER = MokeSweepProvider()
