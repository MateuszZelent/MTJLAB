"""MOKE Box integration boundary; importing runtime code never loads the Qt page."""

from typing import Any

from app.devices.moke_box.adapter import MokeBoxAdapter, MokeBoxBinaryTransport, MokeBoxTransport
from app.devices.moke_box.models import (
    MokeBoxConfig,
    MokeFieldReading,
    MokeHallVoltageReading,
    MokeReading,
    MokeSampleBatch,
    hall_field_from_voltage,
)
from app.devices.moke_box.protocol import MokeGain, MokeTarget
from app.devices.moke_box.transport import MokeBoxTcpTransport

__all__ = [
    "MODULE", "MokeBoxAdapter", "MokeBoxBinaryTransport", "MokeBoxConfig",
    "MokeBoxTcpTransport", "MokeBoxTransport", "MokeFieldReading",
    "MokeGain", "MokeHallVoltageReading", "MokeReading", "MokeSampleBatch",
    "MokeTarget", "hall_field_from_voltage",
]


def __getattr__(name: str) -> Any:
    """Load the manifest lazily because it owns the optional Qt UI package."""

    if name == "MODULE":
        from app.devices.moke_box.module import MODULE

        return MODULE
    raise AttributeError(name)
