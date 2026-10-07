"""Confirmed OFF evidence shared by characterization and its recovery paths."""
from app.domain.errors import DeviceError


def require_output_off(device, channel) -> None:
    confirm = getattr(device, "confirm_output_off", None)
    if callable(confirm):
        # The adapter's confirmation contract returns True for proven OFF.
        if confirm(channel) is not True:
            raise DeviceError(f"Keithley channel {channel} OUTPUT OFF was not confirmed.")
    elif device.assert_output_state(channel, expected_enabled=False) is False:
        # Legacy assertion methods communicate success by returning None;
        # an explicit failure result must never be discarded.
        raise DeviceError(f"Keithley channel {channel} OUTPUT OFF assertion failed.")
