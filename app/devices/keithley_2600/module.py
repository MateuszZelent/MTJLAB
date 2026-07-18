"""Keithley 2600 module manifest and worker-thread operation mapping."""

from __future__ import annotations

from app.contracts import DeviceModule, RecipeExtension
from app.devices.base import DeviceAdapter
from app.devices.keithley import KeithleyAdapter
from app.devices.keithley_2600.ui import KeithleyPage
from app.devices.simulators import SimulatedVisaFactory
from app.settings.models import StationSettings
from app.recipes.parameter_registry import parameter_definitions_for_module


def _adapter(settings: StationSettings, simulation: bool) -> DeviceAdapter:
    return KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley") if simulation else None)


def _dispatch(adapter: DeviceAdapter, operation: str, payload: object) -> object:
    if not isinstance(adapter, KeithleyAdapter):
        raise TypeError("Keithley module received an incompatible adapter.")
    actions = {
        "configure": lambda: adapter.configure_source(payload),
        "arm": lambda: adapter.arm_output(payload),
        "measure": lambda: adapter.measure(payload),
        "ramp_to_zero": lambda: adapter.ramp_to_zero(payload),
        "ramp_to_level": lambda: adapter.ramp_to_level(payload),
    }
    if operation == "set_output":
        channel, enabled = payload  # type: ignore[misc]
        return adapter.set_output(channel, enabled)
    try:
        return actions[operation]()
    except KeyError as exc:
        raise ValueError(f"Unsupported Keithley operation {operation!r}.") from exc


def _page(controller: object, settings: StationSettings) -> object:
    return KeithleyPage(controller, settings)  # type: ignore[arg-type]


MODULE = DeviceModule(
    key="keithley",
    display_name="Keithley 2600",
    settings_key="keithley",
    adapter_factory=_adapter,
    dispatch=_dispatch,
    capabilities=frozenset({"source_measure", "dual_channel"}),
    recipe_extension=RecipeExtension(
        module_key="keithley",
        parameter_definitions=parameter_definitions_for_module("keithley"),
        library_block_keys=("keithley",),
    ),
    page_factory=_page,
)
