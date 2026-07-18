"""Manifest for the future Lake Shore gaussmeter vertical module."""

from __future__ import annotations

from app.contracts import DeviceModule, RecipeExtension
from app.devices.base import DeviceAdapter
from app.domain.errors import ConfigurationError
from app.settings.models import StationSettings


def _not_configured(_settings: StationSettings, _simulation: bool) -> DeviceAdapter:
    raise ConfigurationError(
        "Lake Shore Gaussmeter is not yet configured in settings.yml. "
        "Create a GaussmeterConfig and inject LakeShore475Adapter during integration."
    )


def _dispatch(adapter: DeviceAdapter, operation: str, _payload: object) -> object:
    if operation != "read_field":
        raise ValueError(f"Unsupported Lake Shore operation {operation!r}.")
    read_field = getattr(adapter, "read_field", None)
    if not callable(read_field):
        raise TypeError("Lake Shore module received an incompatible adapter.")
    return read_field()


MODULE = DeviceModule(
    key="lakeshore_gaussmeter",
    display_name="Lake Shore Gaussmeter (425/475)",
    settings_key="lakeshore_gaussmeter",
    adapter_factory=_not_configured,
    dispatch=_dispatch,
    capabilities=frozenset({"field_reading", "read_only"}),
    enabled_by_default=False,
    recipe_extension=RecipeExtension(module_key="lakeshore_gaussmeter"),
)
