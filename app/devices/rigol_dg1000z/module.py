"""Rigol module manifest and worker-thread operation mapping."""

from __future__ import annotations

from app.contracts import DeviceModule, RecipeExtension
from app.devices.base import DeviceAdapter
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
        "arm": lambda: adapter.arm_output(payload),
        "configure_modulation": lambda: adapter.configure_modulation(payload),
        "configure_sweep": lambda: adapter.configure_frequency_sweep(payload),
        "trigger_sweep": lambda: adapter.trigger_frequency_sweep(payload),
        "configure_burst": lambda: adapter.configure_burst(payload),
        "trigger_burst": lambda: adapter.trigger_burst(payload),
        "synchronize_phases": adapter.synchronize_phases,
    }
    if operation == "set_output":
        channel, enabled = payload  # type: ignore[misc]
        return adapter.set_output(channel, enabled)
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

