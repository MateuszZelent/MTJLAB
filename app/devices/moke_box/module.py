"""Manifest for the MOKE Box vertical module."""

from __future__ import annotations

from collections.abc import Mapping

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


def create_simulated_moke_adapter(context: SimulationContext | None = None) -> MokeBoxAdapter:
    """Build the protocol-faithful in-memory MOKE adapter for a synthetic run."""

    return MokeBoxAdapter(
        MokeBoxConfig(endpoint="SIM::MOKE::INSTR", expected_model="MOKE SIM"),
        SimulatedMokeBoxTransport(context or SimulationContext(seed=0)),
    )


def _adapter(settings: StationSettings, simulation: bool) -> DeviceAdapter:
    if settings is None:
        raise ConfigurationError("MOKE Box requires a qualified station profile.")
    profile = settings.moke_box
    if simulation:
        return create_simulated_moke_adapter()
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
        ),
        MokeBoxTcpTransport(),
    )


def _dispatch(adapter: DeviceAdapter, operation: str, _payload: object) -> object:
    if operation not in {
        "read_signal", "read_vouts", "acquire_samples", "read_fields", "read_hall_voltage",
        "set_hall_gains", "set_kerr_gain", "set_vout", "ramp_vout",
    }:
        raise ValueError(f"Unsupported MOKE Box operation {operation!r}.")
    method = getattr(adapter, operation, None)
    if not callable(method):
        raise TypeError("MOKE Box module received an incompatible adapter.")
    if operation == "acquire_samples":
        if not isinstance(_payload, Mapping):
            raise ValueError("MOKE sample acquisition requires a payload mapping.")
        return method(
            int(_payload["count"]),
            active_streams=int(_payload.get("active_streams", 4)),
        )
    if operation in {"read_fields", "read_hall_voltage"}:
        if not isinstance(_payload, Mapping):
            raise ValueError("MOKE Hall read requires a payload mapping.")
        return method(int(_payload["count"]))
    if operation in {"set_hall_gains", "set_kerr_gain", "set_vout", "ramp_vout"}:
        if not isinstance(_payload, Mapping):
            raise ValueError(f"MOKE {operation} requires a payload mapping.")
        return method(**dict(_payload))
    return method()


def _page(controller: object, settings: StationSettings) -> object:
    return MokeBoxPage(controller, settings)  # type: ignore[arg-type]


MODULE = DeviceModule(
    key="moke_box",
    display_name="MOKE Box",
    settings_key="moke_box",
    adapter_factory=_adapter,
    dispatch=_dispatch,
    capabilities=frozenset({"vout_readback", "hall_field_readback", "sample_acquisition"}),
    enabled_by_default=False,
    recipe_extension=RecipeExtension(
        module_key="moke_box",
        parameter_definitions=parameter_definitions_for_module("moke_box"),
        library_block_keys=("moke_box",),
    ),
    page_factory=_page,
)
