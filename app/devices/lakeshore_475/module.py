"""Manifest and factories for the read-only Lake Shore Model 475 module."""

from __future__ import annotations

from app.contracts import DeviceModule, RecipeExtension
from app.devices.base import DeviceAdapter
from app.devices.lakeshore_475.adapter import LakeShore475Adapter, UnavailableLakeShoreAdapter
from app.devices.lakeshore_475.models import GaussmeterConfig
from app.devices.lakeshore_475.simulator import simulated_475_session
from app.devices.lakeshore_475.ui import LakeShore475Page
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.settings.models import StationSettings


def _adapter(settings: StationSettings, simulation: bool) -> DeviceAdapter:
    if settings is None:
        raise ConfigurationError("Lake Shore 475 requires a station profile.")
    profile = settings.lakeshore_gaussmeter
    if not profile.enabled or not profile.resource:
        return UnavailableLakeShoreAdapter(
            "Lake Shore 475 is not assigned yet. Open Discovery, run Scan VISA, "
            "choose Lake Shore 475 for the detected LSCI,MODEL475 resource, click "
            "Assign, confirm the change, and then reconnect."
        )
    config = GaussmeterConfig(
        resource=profile.resource,
        visa_backend=profile.visa_backend,
        timeout_ms=round(parse_quantity(profile.timeout, DIMENSION_TIME).si_value * 1000),
        baud_rate=profile.baud_rate,
        expected_serial=profile.expected_serial,
        require_serial_match=profile.require_serial_match,
    )
    if simulation:
        return LakeShore475Adapter(
            config,
            session_factory=FakeVisaSessionFactory(simulated_475_session()),
            official_model_factory=lambda connection: connection,
        )
    return LakeShore475Adapter(config)


def _dispatch(adapter: DeviceAdapter, operation: str, _payload: object) -> object:
    if operation not in {"read_snapshot", "read_measurement", "read_field"}:
        raise ValueError(f"Unsupported Lake Shore operation {operation!r}.")
    method = getattr(adapter, operation, None)
    if not callable(method):
        raise TypeError("Lake Shore module received an incompatible adapter.")
    return method()


def _page(controller: object, settings: StationSettings) -> object:
    return LakeShore475Page(controller, settings)  # type: ignore[arg-type]


MODULE = DeviceModule(
    key="lakeshore_gaussmeter",
    implementation_key="lakeshore_475",
    display_name="Lake Shore 475",
    settings_key="lakeshore_gaussmeter",
    execution_state_key="lakeshore",
    adapter_factory=_adapter,
    dispatch=_dispatch,
    capabilities=frozenset({"field_reading", "dc", "rms", "peak", "read_only"}),
    enabled_by_default=False,
    recipe_extension=RecipeExtension(module_key="lakeshore_gaussmeter"),
    page_factory=_page,
)
