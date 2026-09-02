"""Keithley 2600 module manifest and worker-thread operation mapping."""

from __future__ import annotations

from app.contracts import DeviceModule, RecipeExtension
from app.devices.base import DeviceAdapter
from app.devices.keithley_2600 import KeithleySourceRequest
from app.domain.quick_controls import QuickConfigureCommand, QuickControlCommand
from app.domain.quantities import parse_quantity
from app.recipes.parameter_registry import QUICK_CONTROLS_BY_TARGET
from app.devices.keithley_2600 import KeithleyAdapter
from app.devices.keithley_2600.ui import KeithleyPage
from app.devices.simulators import SimulatedVisaFactory
from app.settings.models import StationSettings
from app.recipes.parameter_registry import parameter_definitions_for_module
from app.devices.keithley_2600.sweep_provider import PROVIDER as SWEEP_PROVIDER


def _adapter(settings: StationSettings, simulation: bool) -> DeviceAdapter:
    return KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley") if simulation else None)


def _dispatch(adapter: DeviceAdapter, operation: str, payload: object) -> object:
    if not isinstance(adapter, KeithleyAdapter):
        raise TypeError("Keithley module received an incompatible adapter.")
    actions = {
        "configure": lambda: adapter.configure_source(payload),
        "read_configuration": adapter.read_configuration,
        "measure": lambda: adapter.measure(payload),
        "ramp_to_zero": lambda: adapter.ramp_to_zero(payload),
        "ramp_to_level": lambda: adapter.ramp_to_level(payload),
    }
    if operation == "set_output":
        channel, enabled = payload  # type: ignore[misc]
        return adapter.set_output(channel, enabled)
    if operation == "set_output_group":
        channels, enabled = payload  # type: ignore[misc]
        return adapter.set_output_group(tuple(channels), bool(enabled))
    if operation == "set_dut_output_off_mode":
        channel, mode = payload  # type: ignore[misc]
        return adapter.set_dut_output_off_mode(channel, mode)
    if operation == "recover_from_compliance":
        channel, choice = payload  # type: ignore[misc]
        return adapter.recover_from_compliance(channel, choice)
    if operation == "set_compliance_policy":
        channel, stop_on_compliance = payload  # type: ignore[misc]
        return adapter.set_compliance_policy(channel, bool(stop_on_compliance))
    if operation == "quick_setpoint":
        if not isinstance(payload, QuickControlCommand):
            raise ValueError("Keithley quick_setpoint requires an explicit-unit command.")
        descriptor = QUICK_CONTROLS_BY_TARGET.get(payload.target)
        if descriptor is None or descriptor.device_module != "keithley":
            raise ValueError(
                f"Unsupported Keithley quick-control target {payload.target!r}."
            )
        value_si = parse_quantity(
            payload.quantity_text, descriptor.dimension
        ).si_value
        _device, channel, mode = payload.target.split(".")
        if channel not in {"A", "B"} or mode not in {"current", "voltage"}:
            raise ValueError(
                f"Unsupported Keithley quick-control target {payload.target!r}."
            )
        return adapter.quick_update_source_level(
            channel, mode=mode, level_si=float(value_si)  # type: ignore[arg-type]
        )
    if operation == "quick_configure":
        if not isinstance(payload, QuickConfigureCommand):
            raise ValueError(
                "Keithley quick_configure requires a complete device-page configuration."
            )
        descriptor = QUICK_CONTROLS_BY_TARGET.get(payload.target)
        request = payload.configuration
        if (
            descriptor is None
            or descriptor.device_module != "keithley"
            or not isinstance(request, KeithleySourceRequest)
        ):
            raise ValueError(
                f"Unsupported Keithley quick configuration target {payload.target!r}."
            )
        _device, channel, mode = payload.target.split(".")
        if request.channel != channel or request.mode != mode:
            raise ValueError(
                "Keithley quick configuration target and source request do not match."
            )
        return adapter.configure_source(request).level_si
    if operation == "quick_readback":
        return adapter.quick_control_snapshot()
    try:
        return actions[operation]()
    except KeyError as exc:
        raise ValueError(f"Unsupported Keithley operation {operation!r}.") from exc


def _page(controller: object, settings: StationSettings) -> object:
    return KeithleyPage(controller, settings)  # type: ignore[arg-type]


MODULE = DeviceModule(
    key="keithley",
    implementation_key="keithley_2600",
    display_name="Keithley 2600",
    settings_key="keithley",
    adapter_factory=_adapter,
    dispatch=_dispatch,
    capabilities=frozenset({"source_measure", "dual_channel"}),
    recipe_extension=RecipeExtension(
        module_key="keithley",
        parameter_definitions=parameter_definitions_for_module("keithley"),
        library_block_keys=("keithley",),
        sweep_provider=SWEEP_PROVIDER,
    ),
    page_factory=_page,
)
