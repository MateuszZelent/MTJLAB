"""Safety validation shared by the Anritsu adapter and recipe preflight."""

from __future__ import annotations

import math

from app.domain.errors import SafetyViolation
from app.domain.quantities import DIMENSION_DBM, DIMENSION_FREQUENCY, parse_quantity
from app.settings.models import AnritsuSafety


ANRITSU_SWEEP_POINT_COUNTS = (11, 21, 41, 51, 101, 201, 251, 401, 501, 1001, 2001, 5001, 10001)


def validate_anritsu_trace_name(trace: str) -> str:
    """Accept only the explicitly qualified spectrum trace identifier."""

    normalized = trace.strip().upper()
    if normalized != "TRAC1":
        raise SafetyViolation("Only the Anritsu TRAC1 trace is qualified in version 1.")
    return normalized


def assert_anritsu_acquisition_allowed(safety: AnritsuSafety) -> None:
    """Require every RF limit before a trace can be acquired or configured."""

    if not safety.acquisition_allowed:
        raise SafetyViolation(
            "Anritsu acquisition is locked by the safety profile. "
            "Define the RF input, frequency, and reference-level limits in Settings > Anritsu, "
            "then enable acquisition."
        )
    if safety.require_rf_input_limit_definition and safety.rf_input.max_expected_power_at_connector is None:
        raise SafetyViolation("Define the maximum expected RF power at the Anritsu input connector first.")
    if safety.rf_input.max_expected_power_at_connector is not None:
        parse_quantity(safety.rf_input.max_expected_power_at_connector, DIMENSION_DBM)
    if safety.frequency.min is None or safety.frequency.max is None:
        raise SafetyViolation("Define the permitted Anritsu frequency range before acquisition.")
    if safety.reference_level.min is None or safety.reference_level.max is None:
        raise SafetyViolation("Define the permitted Anritsu reference-level range before acquisition.")


def validate_anritsu_spectrum(
    safety: AnritsuSafety,
    *,
    start_hz: float,
    stop_hz: float,
    reference_level_dbm: float,
    points: int,
) -> None:
    """Validate the effective range before any Anritsu SCPI is issued."""

    assert_anritsu_acquisition_allowed(safety)
    if not all(math.isfinite(value) for value in (start_hz, stop_hz, reference_level_dbm)):
        raise SafetyViolation("Anritsu spectrum parameters must be finite numbers.")
    if start_hz <= 0 or stop_hz <= start_hz:
        raise SafetyViolation("The Anritsu spectrum range must satisfy 0 < start < stop.")
    frequency_min = parse_quantity(safety.frequency.min, DIMENSION_FREQUENCY).si_value
    frequency_max = parse_quantity(safety.frequency.max, DIMENSION_FREQUENCY).si_value
    if start_hz < frequency_min or stop_hz > frequency_max:
        raise SafetyViolation(
            f"Anritsu range {start_hz:.9g}–{stop_hz:.9g} Hz is outside the approved range "
            f"of {frequency_min:.9g}–{frequency_max:.9g} Hz."
        )
    reference_min = parse_quantity(safety.reference_level.min, DIMENSION_DBM).si_value
    reference_max = parse_quantity(safety.reference_level.max, DIMENSION_DBM).si_value
    if reference_level_dbm < reference_min or reference_level_dbm > reference_max:
        raise SafetyViolation(
            f"Reference level {reference_level_dbm:.9g} dBm is outside the approved range "
            f"of {reference_min:.9g}–{reference_max:.9g} dBm."
        )
    if isinstance(points, bool) or points not in ANRITSU_SWEEP_POINT_COUNTS:
        raise SafetyViolation(
            "The Anritsu point count must be one of: "
            + ", ".join(str(value) for value in ANRITSU_SWEEP_POINT_COUNTS)
            + "."
        )
    if not safety.sweep_points.min <= points <= safety.sweep_points.max:
        raise SafetyViolation(
            f"The Anritsu point count must be between {safety.sweep_points.min} "
            f"and {safety.sweep_points.max}."
        )
