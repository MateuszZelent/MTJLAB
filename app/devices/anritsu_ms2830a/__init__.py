"""Vertical module facade for the Anritsu MS2830A."""

from app.devices.anritsu import *  # noqa: F403
from app.devices.anritsu_ms2830a.module import MODULE

__all__ = ["MODULE"]
