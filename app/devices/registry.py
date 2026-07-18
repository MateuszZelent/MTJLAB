"""Composition registry for all built-in device families."""

from __future__ import annotations

from app.contracts import DeviceModuleRegistry
from app.devices.anritsu_ms2830a.module import MODULE as ANRITSU_MS2830A_MODULE
from app.devices.keithley_2600.module import MODULE as KEITHLEY_2600_MODULE
from app.devices.lakeshore_gaussmeter.module import MODULE as LAKESHORE_GAUSSMETER_MODULE
from app.devices.moke_box.module import MODULE as MOKE_BOX_MODULE
from app.devices.rigol_dg1000z.module import MODULE as RIGOL_DG1000Z_MODULE


def built_in_device_registry() -> DeviceModuleRegistry:
    """Return all reviewed modules; experimental hardware starts disabled."""

    return DeviceModuleRegistry(
        (
            RIGOL_DG1000Z_MODULE,
            KEITHLEY_2600_MODULE,
            ANRITSU_MS2830A_MODULE,
            MOKE_BOX_MODULE,
            LAKESHORE_GAUSSMETER_MODULE,
        )
    )
