"""Anritsu-owned spectrum and signal-generator sweep binding."""

from __future__ import annotations

from collections.abc import Mapping

from app.contracts.sweep_provider import CompiledAxisSetpoint
from app.devices.anritsu_ms2830a.configuration import SignalGeneratorConfig, SpectrumConfig
from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_DBM, DIMENSION_FREQUENCY, Quantity
from app.recipes.models import RecipeNode
from app.recipes.parameter_registry import parameter_descriptor
from app.recipes.semantic_tree import SweepAxisBinding, SweepBindingDraft
from app.safety.anritsu import validate_anritsu_signal_generator, validate_anritsu_spectrum
from app.settings.models import StationSettings


class AnritsuSweepProvider:
    module_key = "anritsu"

    @staticmethod
    def axis_action_kinds(binding: SweepAxisBinding) -> frozenset[str]:
        """Return authored update nodes represented by one semantic ROI row."""

        if binding.parameter_id.startswith(("signal_generator.", "sg.")):
            return frozenset({"update_anritsu_sg"})
        # Spectrum axes use configure_anritsu with a selected-field mask.
        if binding.parameter_id.startswith("spectrum."):
            return frozenset({"configure_anritsu"})
        return frozenset()

    def bind_legacy_action(self, node: RecipeNode, action: Mapping[str, object]) -> SweepBindingDraft:
        parameter_id = str(action.get("parameter_id", ""))
        configuration = node.data.get("configuration")
        configuration = configuration if isinstance(configuration, Mapping) else node.data
        if parameter_id in {"signal_generator.frequency", "sg.frequency"}:
            target, dimension = "anritsu.sg.frequency", DIMENSION_FREQUENCY
        elif parameter_id in {"signal_generator.power", "sg.power"}:
            target, dimension = "anritsu.sg.power", DIMENSION_DBM
        elif parameter_id in {"spectrum.start_frequency", "spectrum.stop_frequency", "spectrum.reference_level"}:
            target = f"anritsu.spectrum.{parameter_id.split('.', 1)[1]}"
            dimension = parameter_descriptor(target).dimension
        else:
            raise ConfigurationError(f"{node.id}: unsupported Anritsu sweep parameter {parameter_id!r}.")
        stages = action.get("segments", ())
        if not isinstance(stages, (list, tuple)) or not stages:
            raise ConfigurationError(f"{node.id}: {parameter_id} sweep requires a non-empty ROI.")
        endpoint = "SG" if target.startswith("anritsu.sg.") else "SPECTRUM"
        return SweepBindingDraft(
            owner_node_id=node.id,
            device_module=self.module_key,
            endpoint=endpoint,
            parameter_id=parameter_id,
            target=target,
            dimension=dimension,
            stages=tuple(item for item in stages if isinstance(item, Mapping)),
        )

    def binding_for_target(self, node: RecipeNode, target: str) -> SweepAxisBinding:
        descriptor = parameter_descriptor(target)
        if descriptor.device_module != self.module_key:
            raise ConfigurationError(f"{node.id}: target {target!r} is not owned by Anritsu.")
        parameter_id = target.removeprefix("anritsu.")
        endpoint = "SG" if target.startswith("anritsu.sg.") else "SPECTRUM"
        return SweepAxisBinding(f"{node.id}.axis.{parameter_id.replace('.', '-')}", node.id, node.id, self.module_key, endpoint, parameter_id, target, descriptor.dimension, (), ())

    def validate_binding(self, node: RecipeNode, binding: SweepAxisBinding) -> None:
        if binding.device_module != self.module_key:
            raise ConfigurationError(f"{node.id}: Anritsu binding has an incompatible module.")
        descriptor = parameter_descriptor(binding.target)
        if descriptor.device_module != self.module_key or descriptor.dimension != binding.dimension:
            raise ConfigurationError(f"{node.id}: Anritsu binding target/dimension mismatch.")
        expected_endpoint = "SG" if binding.target.startswith("anritsu.sg.") else "SPECTRUM"
        if binding.endpoint.upper() != expected_endpoint:
            raise ConfigurationError(f"{node.id}: Anritsu binding endpoint does not match target.")
        parameter = binding.parameter_id.replace("signal_generator.", "sg.", 1)
        if parameter != binding.target.removeprefix("anritsu."):
            raise ConfigurationError(f"{node.id}: Anritsu binding parameter does not match target.")

    def compile_point(self, node: RecipeNode, binding: SweepAxisBinding, value: Quantity, context: Mapping[str, Quantity], settings: StationSettings) -> CompiledAxisSetpoint:
        self.validate_binding(node, binding)
        descriptor = parameter_descriptor(binding.target)
        value.require_dimension(descriptor.dimension)
        if binding.target.startswith("anritsu.sg."):
            if any(key not in context for key in ("anritsu.sg.frequency", "anritsu.sg.power")):
                raise ConfigurationError("Anritsu SG sweep requires an explicit frequency and power baseline.")
            current_frequency = context["anritsu.sg.frequency"].si_value
            current_power = context["anritsu.sg.power"].si_value
            if binding.target.endswith("frequency"):
                current_frequency = value.si_value
            else:
                current_power = value.si_value
            validate_anritsu_signal_generator(settings.anritsu, frequency_hz=current_frequency, power_dbm=current_power)
            changed = "frequency_hz" if binding.target.endswith("frequency") else "power_dbm"
            config = SignalGeneratorConfig(current_frequency, current_power, changed_fields=(changed,))
            return CompiledAxisSetpoint("update_anritsu_sg", {"config": config}, value.si_value, value.si_value)
        # Keep the baseline for validation; changed_fields limits the mutation
        # to the selected axis instead of reapplying unrelated settings.
        required = ("start_frequency", "stop_frequency", "reference_level", "points")
        if any(f"anritsu.spectrum.{key}" not in context for key in required):
            raise ConfigurationError("Anritsu spectrum sweep requires an explicit frequency, reference and points baseline.")
        start = context["anritsu.spectrum.start_frequency"].si_value
        stop = context["anritsu.spectrum.stop_frequency"].si_value
        reference = context["anritsu.spectrum.reference_level"].si_value
        if binding.target.endswith("start_frequency"):
            start = value.si_value
        elif binding.target.endswith("stop_frequency"):
            stop = value.si_value
        else:
            reference = value.si_value
        points_value = context["anritsu.spectrum.points"]
        points_value.require_dimension("ratio")
        if not float(points_value.si_value).is_integer():
            raise ConfigurationError("Anritsu points baseline must be an integer without rounding.")
        points = int(points_value.si_value)
        validate_anritsu_spectrum(settings.anritsu.safety, start_hz=start, stop_hz=stop, reference_level_dbm=reference, points=points)
        changed = {"start_frequency": "start_hz", "stop_frequency": "stop_hz", "reference_level": "reference_level_dbm"}[binding.target.rsplit(".", 1)[-1]]
        config = SpectrumConfig(start, stop, reference, points, changed_fields=(changed,))
        return CompiledAxisSetpoint("configure_anritsu", {"config": config}, value.si_value, value.si_value)


PROVIDER = AnritsuSweepProvider()
