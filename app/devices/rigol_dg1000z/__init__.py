"""Vertical module facade for the Rigol DG1000Z family."""

from app.devices.rigol import *  # noqa: F403
from app.devices.rigol_dg1000z.module import MODULE

__all__ = ["MODULE"]
