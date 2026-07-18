"""Vertical module facade for the Keithley 2600 family."""

from app.devices.keithley import *  # noqa: F403
from app.devices.keithley_2600.module import MODULE

__all__ = ["MODULE"]
