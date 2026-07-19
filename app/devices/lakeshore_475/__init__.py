"""Read-only Lake Shore Model 475 module."""

from typing import Any

from app.devices.lakeshore_475.adapter import LakeShore475Adapter, UnavailableLakeShoreAdapter
from app.devices.lakeshore_475.models import GaussmeterConfig, GaussmeterReading, GaussmeterSnapshot, MeasurementMode

__all__ = ["GaussmeterConfig", "GaussmeterReading", "GaussmeterSnapshot", "LakeShore475Adapter", "MeasurementMode", "MODULE", "UnavailableLakeShoreAdapter"]


def __getattr__(name: str) -> Any:
    """Load the Qt-owning manifest only when composition requests it."""

    if name != "MODULE":
        raise AttributeError(name)
    from app.devices.lakeshore_475.module import MODULE

    return MODULE
