"""Safe instrument adapters and test simulators."""

from __future__ import annotations

from typing import Any

__all__ = ["AnritsuAdapter", "KeithleyAdapter", "RigolAdapter"]


def __getattr__(name: str) -> Any:
    """Keep the convenience exports without eagerly importing every adapter."""

    if name == "AnritsuAdapter":
        from app.devices.anritsu.adapter import AnritsuAdapter

        return AnritsuAdapter
    if name == "KeithleyAdapter":
        from app.devices.keithley.adapter import KeithleyAdapter

        return KeithleyAdapter
    if name == "RigolAdapter":
        from app.devices.rigol.adapter import RigolAdapter

        return RigolAdapter
    raise AttributeError(name)

