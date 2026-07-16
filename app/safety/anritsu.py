"""Safety validation shared by the Anritsu adapter and recipe preflight."""

from __future__ import annotations

import math

from app.domain.errors import SafetyViolation
from app.domain.quantities import DIMENSION_DBM, DIMENSION_FREQUENCY, parse_quantity
from app.settings.models import AnritsuSafety


def validate_anritsu_trace_name(trace: str) -> str:
    """Accept only the explicitly qualified spectrum trace identifier."""

    normalized = trace.strip().upper()
    if normalized != "TRAC1":
        raise SafetyViolation("W wersji v1 kwalifikowany jest wyłącznie trace Anritsu TRAC1.")
    return normalized


def assert_anritsu_acquisition_allowed(safety: AnritsuSafety) -> None:
    """Require every RF limit before a trace can be acquired or configured."""

    if not safety.acquisition_allowed:
        raise SafetyViolation("Akwizycja Anritsu jest zablokowana w settings.yml.")
    if safety.require_rf_input_limit_definition and safety.rf_input.max_expected_power_at_connector is None:
        raise SafetyViolation("Najpierw zdefiniuj bezpieczny limit wejścia RF Anritsu.")
    if safety.rf_input.max_expected_power_at_connector is not None:
        parse_quantity(safety.rf_input.max_expected_power_at_connector, DIMENSION_DBM)
    if safety.frequency.min is None or safety.frequency.max is None:
        raise SafetyViolation("Przed akwizycją Anritsu zdefiniuj zakres częstotliwości stanowiska.")
    if safety.reference_level.min is None or safety.reference_level.max is None:
        raise SafetyViolation("Przed akwizycją Anritsu zdefiniuj zakres reference level stanowiska.")


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
        raise SafetyViolation("Parametry widma Anritsu muszą być skończonymi liczbami.")
    if start_hz <= 0 or stop_hz <= start_hz:
        raise SafetyViolation("Zakres widma Anritsu musi spełniać 0 < start < stop.")
    frequency_min = parse_quantity(safety.frequency.min, DIMENSION_FREQUENCY).si_value
    frequency_max = parse_quantity(safety.frequency.max, DIMENSION_FREQUENCY).si_value
    if start_hz < frequency_min or stop_hz > frequency_max:
        raise SafetyViolation(
            f"Zakres Anritsu {start_hz:.9g}–{stop_hz:.9g} Hz jest poza zatwierdzonym zakresem "
            f"{frequency_min:.9g}–{frequency_max:.9g} Hz."
        )
    reference_min = parse_quantity(safety.reference_level.min, DIMENSION_DBM).si_value
    reference_max = parse_quantity(safety.reference_level.max, DIMENSION_DBM).si_value
    if reference_level_dbm < reference_min or reference_level_dbm > reference_max:
        raise SafetyViolation(
            f"Reference level {reference_level_dbm:.9g} dBm jest poza zatwierdzonym zakresem "
            f"{reference_min:.9g}–{reference_max:.9g} dBm."
        )
    if isinstance(points, bool) or not safety.sweep_points.min <= points <= safety.sweep_points.max:
        raise SafetyViolation(
            f"Liczba punktów Anritsu musi być w zakresie {safety.sweep_points.min}–{safety.sweep_points.max}."
        )
