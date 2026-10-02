"""Manifest for the MOKE Box vertical module."""

from __future__ import annotations

from app.contracts import DeviceModule, RecipeExtension
from app.devices.base import DeviceAdapter
from app.devices.moke_box.adapter import MokeBoxAdapter, UnavailableMokeBoxAdapter
from app.devices.moke_box.models import MokeBoxConfig
from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
from app.devices.moke_box.transport import MokeBoxTcpTransport
from app.devices.simulation import SimulationContext
from app.devices.moke_box.ui import MokeBoxPage
from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.settings.models import StationSettings
from app.recipes.parameter_registry import parameter_definitions_for_module
from app.safety.moke_box import MokeControlProfile, MokeVoltagePlan, control_profile_from_settings
from app.devices.moke_box.sweep_provider import PROVIDER


def create_simulated_moke_adapter(
    context: SimulationContext | None = None, settings: StationSettings | None = None,
) -> MokeBoxAdapter:
    """Build the protocol-faithful in-memory MOKE adapter for a synthetic run."""

    profile = control_profile_from_settings(settings, simulation=True) if settings is not None else MokeControlProfile(
        2, "SIM::MOKE::COIL", -1, 1, 0, 0.05, 1, 0.05, 30, "simulation-only", True,
        "SIM::MOKE::INSTR",
    )
    return MokeBoxAdapter(
        MokeBoxConfig(endpoint="SIM::MOKE::INSTR", expected_model="MOKE SIM",
                      allow_vout_control=True, allowed_vout_channels=(profile.channel,),
                      control_profile=profile),
        SimulatedMokeBoxTransport(context or SimulationContext(seed=0), field_channel=profile.channel),
    )


def _simulation_adapter(settings: StationSettings, context: SimulationContext) -> DeviceAdapter:
    return create_simulated_moke_adapter(context, settings)


def _adapter(settings: StationSettings, simulation: bool) -> DeviceAdapter:
    if settings is None:
        raise ConfigurationError("MOKE Box requires a qualified station profile.")
    profile = settings.moke_box
    if simulation:
        return create_simulated_moke_adapter(settings=settings)
    if not profile.enabled or not profile.protocol_qualified or not profile.endpoint:
        return UnavailableMokeBoxAdapter(
            "Configure enabled=true, endpoint=host:port and protocol_qualified=true "
            "for MOKE Box in Station settings, then reconnect."
        )
    return MokeBoxAdapter(
        MokeBoxConfig(
            endpoint=profile.endpoint,
            timeout_s=parse_quantity(profile.timeout, DIMENSION_TIME).si_value,
            expected_model=profile.expected_model,
            allow_vout_control=profile.allow_vout_control,
            allowed_vout_channels=profile.allowed_vout_channels,
            control_profile=control_profile_from_settings(settings) if profile.allow_vout_control else None,
        ),
        MokeBoxTcpTransport(),
    )


def _dispatch(adapter: DeviceAdapter, operation: str, _payload: object) -> object:
    if operation in {"configure_voltage_plan", "arm_voltage_plan"}:
        if not isinstance(_payload, MokeVoltagePlan):
            raise ValueError("MOKE voltage control requires a typed immutable plan.")
        return getattr(adapter, operation)(_payload)
    if operation == "ramp_vout":
        if not isinstance(_payload, dict) or set(_payload) != {"channel", "voltage_v", "cancel"}:
            raise ValueError("MOKE ramp requires channel, SI voltage and cancellation event.")
        return getattr(adapter, operation)(_payload["channel"], _payload["voltage_v"], cancel=_payload["cancel"])
    if operation in {"stop_vout", "get_control_profile", "disarm_voltage_plan"}:
        return getattr(adapter, operation)()
    if operation not in {"read_signal", "read_vouts", "read_hall_voltage"}:
        raise ValueError(f"Unsupported MOKE Box operation {operation!r}.")
    method = getattr(adapter, operation, None)
    if not callable(method):
        raise TypeError("MOKE Box module received an incompatible adapter.")
    if operation == "read_hall_voltage":
        if not isinstance(_payload, dict):
            raise ValueError("MOKE Hall read requires a payload mapping.")
        return method(int(_payload["count"]))
    return method()


def _page(controller: object, settings: StationSettings) -> object:
    return MokeBoxPage(controller, settings)  # type: ignore[arg-type]


MODULE = DeviceModule(
    key="moke_box",
    implementation_key="moke_box",
    display_name="MOKE Box",
    settings_key="moke_box",
    adapter_factory=_adapter,
    simulation_adapter_factory=_simulation_adapter,
    dispatch=_dispatch,
    capabilities=frozenset({"qualified_voltage_control", "field_calibration", "vout_readback", "hall_voltage_readback"}),
    enabled_by_default=False,
    recipe_extension=RecipeExtension(
        module_key="moke_box",
        parameter_definitions=parameter_definitions_for_module("moke_box"),
        library_block_keys=("moke_box",),
        sweep_provider=PROVIDER,
    ),
    page_factory=_page,
)
