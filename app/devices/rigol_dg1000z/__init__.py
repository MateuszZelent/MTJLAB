"""Complete vertical module for the Rigol DG1000Z family."""

from typing import Any

from app.devices.rigol_dg1000z.adapter import (
    RigolAdapter,
    RigolBurstConfig,
    RigolChannelConfig,
    RigolFrequencySweepConfig,
    RigolModulationConfig,
    RigolOutputConfig,
)

__all__ = [
    "MODULE",
    "RigolAdapter",
    "RigolBurstConfig",
    "RigolChannelConfig",
    "RigolFrequencySweepConfig",
    "RigolModulationConfig",
    "RigolOutputConfig",
]


def __getattr__(name: str) -> Any:
    """Load the Qt-owning manifest only when composition requests it."""

    if name != "MODULE":
        raise AttributeError(name)
    from app.devices.rigol_dg1000z.module import MODULE

    return MODULE
