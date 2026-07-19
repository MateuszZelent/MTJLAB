"""Complete vertical module for the Keithley 2600 family."""

from typing import Any

from app.devices.keithley_2600.adapter import (
    KeithleyAdapter,
    KeithleyRampRequest,
    KeithleyRampResult,
    build_keithley_ramp_levels,
)
from app.safety.keithley import KeithleySourceRequest

__all__ = [
    "MODULE",
    "KeithleyAdapter",
    "KeithleyRampRequest",
    "KeithleyRampResult",
    "KeithleySourceRequest",
    "build_keithley_ramp_levels",
]


def __getattr__(name: str) -> Any:
    """Load the Qt-owning manifest only when composition requests it."""

    if name != "MODULE":
        raise AttributeError(name)
    from app.devices.keithley_2600.module import MODULE

    return MODULE
