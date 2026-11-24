"""Resolve nominal Anritsu linear averages to whole device sweeps."""

import math

from app.domain.errors import SafetyViolation
from app.domain.spectrum_averaging import (
    ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS,
    ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS,
)


def instrument_average_count_for_duration(duration_s: float, sweep_time_s: float) -> int:
    """Resolve a positive duration and sweep time in seconds to a supported count."""
    if duration_s > ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS * sweep_time_s:
        raise SafetyViolation(
            f"Instrument averaging target exceeds the Anritsu limit of "
            f"{ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS} sweeps."
        )
    count = max(ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS, math.floor(duration_s / sweep_time_s))
    # Compare in seconds so floating-point division cannot add a sweep at an exact multiple.
    if count * sweep_time_s < duration_s:
        count += 1
    return count
