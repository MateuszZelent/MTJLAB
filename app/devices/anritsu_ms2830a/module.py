"""Anritsu MS2830A module manifest and worker-thread operation mapping."""

from __future__ import annotations

from app.contracts import DeviceModule, RecipeExtension
from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.anritsu_ms2830a.ui import AnritsuPage
from app.devices.base import DeviceAdapter
from app.devices.simulators import SimulatedVisaFactory
from app.settings.models import StationSettings
from app.recipes.parameter_registry import parameter_definitions_for_module


def _adapter(settings: StationSettings, simulation: bool) -> DeviceAdapter:
    return AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu") if simulation else None)


def _dispatch(adapter: DeviceAdapter, operation: str, payload: object) -> object:
    if not isinstance(adapter, AnritsuAdapter):
        raise TypeError("Anritsu module received an incompatible adapter.")
    actions = {
        "read_configuration": adapter.read_current_configuration,
        "read_advanced_spectrum": adapter.read_advanced_spectrum_configuration,
        "configure_advanced_spectrum": lambda: adapter.configure_advanced_spectrum(payload),
        "configure": lambda: adapter.configure_spectrum(payload),
        "start_live": lambda: adapter.start_live(bool(payload)),
        "stop_live": adapter.stop_live,
        "fetch_trace": lambda: adapter.fetch_trace(str(payload or "TRAC1")),
        "fetch_current_trace": lambda: adapter.fetch_current_trace(str(payload or "TRAC1")),
        "single_sweep": lambda: adapter.acquire_single_sweep(str(payload or "TRAC1")),
        "read_signal_generator": adapter.read_signal_generator_configuration,
        "configure_signal_generator": lambda: adapter.configure_signal_generator(payload),
        "update_signal_generator": lambda: adapter.update_signal_generator(payload),
        "set_signal_generator_output": lambda: adapter.set_signal_generator_output(bool(payload)),
    }
    try:
        return actions[operation]()
    except KeyError as exc:
        raise ValueError(f"Unsupported Anritsu operation {operation!r}.") from exc


def _page(controller: object, settings: StationSettings) -> object:
    return AnritsuPage(
        controller,  # type: ignore[arg-type]
        settings,
        single_sweep_available=(
            settings.anritsu.acquisition.single_sweep_mode == "standard_scpi_opc"
        ),
    )


MODULE = DeviceModule(
    key="anritsu",
    implementation_key="anritsu_ms2830a",
    display_name="Anritsu MS2830A",
    settings_key="anritsu",
    adapter_factory=_adapter,
    dispatch=_dispatch,
    capabilities=frozenset({"spectrum_analysis", "signal_generator"}),
    recipe_extension=RecipeExtension(
        module_key="anritsu",
        parameter_definitions=parameter_definitions_for_module("anritsu"),
        library_block_keys=("anritsu", "anritsu_sg"),
    ),
    page_factory=_page,
)
