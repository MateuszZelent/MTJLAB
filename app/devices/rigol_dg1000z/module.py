"""Rigol module manifest and worker-thread operation mapping."""

from __future__ import annotations

from app.contracts import DeviceModule, RecipeExtension
from app.devices.base import DeviceAdapter
from app.domain.quick_controls import QuickControlCommand
from app.domain.quantities import parse_quantity
from app.recipes.parameter_registry import QUICK_CONTROLS_BY_TARGET
from app.devices.rigol_dg1000z import RigolAdapter
from app.devices.rigol_dg1000z.ui import RigolPage
from app.devices.simulators import SimulatedVisaFactory
from app.settings.models import StationSettings
from app.recipes.parameter_registry import parameter_definitions_for_module


def _adapter(settings: StationSettings, simulation: bool) -> DeviceAdapter:
    return RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol") if simulation else None)


def _dispatch(adapter: DeviceAdapter, operation: str, payload: object) -> object:
    if not isinstance(adapter, RigolAdapter):
        raise TypeError("Rigol module received an incompatible adapter.")
    actions = {
        "configure": lambda: adapter.configure_channel(payload),
        "configure_output": lambda: adapter.configure_output(payload),
        "configure_modulation": lambda: adapter.configure_modulation(payload),
        "configure_sweep": lambda: adapter.configure_frequency_sweep(payload),
        "trigger_sweep": lambda: adapter.trigger_frequency_sweep(payload),
        "configure_burst": lambda: adapter.configure_burst(payload),
        "trigger_burst": lambda: adapter.trigger_burst(payload),
        "synchronize_phases": adapter.synchronize_phases,
        "configure_counter": lambda: adapter.configure_counter(payload),
        "read_counter": adapter.read_counter,
    }
    if operation == "set_output":
        channel, enabled = payload  # type: ignore[misc]
        return adapter.set_output(channel, enabled)
    if operation == "quick_setpoint":
        if not isinstance(payload, QuickControlCommand):
            raise ValueError("Rigol quick_setpoint requires an explicit-unit command.")
        descriptor = QUICK_CONTROLS_BY_TARGET.get(payload.target)
        if descriptor is None or descriptor.device_module != "rigol":
            raise ValueError(
                f"Unsupported Rigol quick-control target {payload.target!r}."
            )
        value_si = parse_quantity(
            payload.quantity_text, descriptor.dimension
        ).si_value
        _device, channel_text, field = payload.target.split(".")
        channel = int(channel_text)
        if field == "frequency":
            return adapter.update_frequency(channel, float(value_si))
        if field == "amplitude":
            return adapter.update_amplitude_vpp(channel, float(value_si))
        if field == "offset":
            return adapter.update_offset(channel, float(value_si))
        if field == "high_level":
            return adapter.update_high_level(channel, float(value_si))
        if field == "low_level":
            return adapter.update_low_level(channel, float(value_si))
        raise ValueError(
            f"Unsupported Rigol quick-control target {payload.target!r}."
        )
    if operation == "quick_readback":
        return adapter.quick_control_snapshot()
    try:
        return actions[operation]()
    except KeyError as exc:
        raise ValueError(f"Unsupported Rigol operation {operation!r}.") from exc


def _page(controller: object, settings: StationSettings) -> object:
    return RigolPage(controller, settings)  # type: ignore[arg-type]


MODULE = DeviceModule(
    key="rigol",
    implementation_key="rigol_dg1000z",
    display_name="Rigol DG1032Z",
    settings_key="rigol",
    adapter_factory=_adapter,
    dispatch=_dispatch,
    capabilities=frozenset({"signal_generation", "dual_channel"}),
    recipe_extension=RecipeExtension(
        module_key="rigol",
        parameter_definitions=parameter_definitions_for_module("rigol"),
        library_block_keys=("rigol",),
    ),
    page_factory=_page,
)

