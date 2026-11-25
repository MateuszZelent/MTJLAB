"""Anritsu MS2830A module manifest and worker-thread operation mapping."""

from __future__ import annotations

from app.contracts import DeviceModule, RecipeExtension
from app.devices.anritsu_ms2830a import AnritsuAdapter, InstrumentAverageTarget
from app.devices.anritsu_ms2830a.sweep_provider import PROVIDER as SWEEP_PROVIDER
from app.devices.anritsu_ms2830a.ui import AnritsuPage
from app.devices.base import DeviceAdapter
from app.devices.simulators import SimulatedVisaFactory
from app.recipes.parameter_registry import parameter_definitions_for_module
from app.settings.models import StationSettings


def _adapter(settings: StationSettings, simulation: bool) -> DeviceAdapter:
    return AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu") if simulation else None)


def _dispatch_unaveraged_sweep(adapter: AnritsuAdapter, payload: object) -> object:
    if isinstance(payload, dict):
        trace = str(payload.get("trace") or "TRAC1")
        restore_continuous = bool(payload.get("restore_continuous", False))
        binary = bool(payload.get("binary", False))
        return adapter.acquire_unaveraged_sweep(
            trace,
            restore_continuous=restore_continuous,
            binary=binary,
        )
    if isinstance(payload, tuple) and len(payload) == 2:
        trace, restore_continuous = payload
        return adapter.acquire_unaveraged_sweep(
            str(trace or "TRAC1"),
            restore_continuous=bool(restore_continuous),
        )
    return adapter.acquire_unaveraged_sweep(str(payload or "TRAC1"))


def _dispatch(adapter: DeviceAdapter, operation: str, payload: object) -> object:
    if not isinstance(adapter, AnritsuAdapter):
        raise TypeError("Anritsu module received an incompatible adapter.")
    if operation.startswith("read_background_filter_configuration:"):
        return adapter.read_acquisition_configuration()
    if operation in {"read_sweep_timing", "configure_sweep_timing"}:
        timing = (adapter.read_sweep_timing() if operation == "read_sweep_timing"
                  else adapter.configure_sweep_timing(payload))
        return timing, adapter.read_advanced_spectrum_configuration()
    if operation in {"instrument_average", "instrument_average_live"}:
        if not isinstance(payload, InstrumentAverageTarget):
            raise TypeError("Anritsu instrument averaging requires a sweep count or duration target.")
        return adapter.acquire_manual_instrument_average(payload, live=operation == "instrument_average_live")
    actions = {
        "read_configuration": adapter.read_current_configuration,
        "read_advanced_spectrum": adapter.read_advanced_spectrum_configuration,
        "configure_advanced_spectrum": lambda: adapter.configure_advanced_spectrum(payload),
        "configure": lambda: adapter.configure_spectrum(payload),
        "start_live": lambda: adapter.start_live(bool(payload), allow_synchronized=True),
        "stop_live": adapter.stop_live,
        "abort_acquisition": adapter.abort_acquisition,
        "fetch_trace": lambda: adapter.fetch_trace(str(payload or "TRAC1")),
        "read_full_configuration": adapter.read_full_configuration,
        "read_acquisition_configuration": adapter.read_acquisition_configuration,
        "fetch_current_trace": lambda: adapter.fetch_current_trace(str(payload or "TRAC1")),
        "fetch_current_trace_fast": lambda: adapter.fetch_current_trace_fast(str(payload or "TRAC1")),
        "poll_synchronized_live": lambda: adapter.poll_synchronized_live(str(payload or "TRAC1")),
        "acquire_fresh_trace": lambda: adapter.acquire_fresh_trace(str(payload or "TRAC1")),
        "single_sweep": lambda: _dispatch_unaveraged_sweep(adapter, payload),
        "unaveraged_sweep": lambda: _dispatch_unaveraged_sweep(adapter, payload),
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
        sweep_provider=SWEEP_PROVIDER,
    ),
    page_factory=_page,
)
