"""Safety validation shared by the Anritsu adapter and recipe preflight."""

from __future__ import annotations

import math

from app.devices.anritsu.hardware import ANRITSU_PREAMPLIFIER_OPTIONS
from app.domain.errors import SafetyViolation
from app.domain.quantities import (
    DIMENSION_DB,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    parse_quantity,
)
from app.settings.models import AnritsuSafety, AnritsuSettings


ANRITSU_SWEEP_POINT_COUNTS = (11, 21, 41, 51, 101, 201, 251, 401, 501, 1001, 2001, 5001, 10001)
ANRITSU_BASIC_DETECTORS = frozenset({"NORM", "POS", "SAMP", "NEG", "RMS"})
ANRITSU_CISPR_DETECTORS = frozenset({"QPE", "CAV", "CRMS"})
ANRITSU_CISPR_OPTIONS = frozenset({"016", "116"})


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


def validate_anritsu_dut_input(
    safety: AnritsuSafety, max_expected_input_dbm: float | None
) -> None:
    """Intersect the experiment RF declaration with the approved station input limit."""

    assert_anritsu_acquisition_allowed(safety)
    if max_expected_input_dbm is None:
        return
    if not math.isfinite(max_expected_input_dbm):
        raise SafetyViolation("The DUT maximum expected Anritsu input must be finite.")
    profile_value = safety.rf_input.max_expected_power_at_connector
    if profile_value is None:
        raise SafetyViolation("The station profile has no approved Anritsu RF input limit.")
    profile_max_dbm = parse_quantity(profile_value, DIMENSION_DBM).si_value
    if max_expected_input_dbm > profile_max_dbm:
        raise SafetyViolation(
            f"DUT expected input {max_expected_input_dbm:.9g} dBm exceeds the station limit "
            f"{profile_max_dbm:.9g} dBm."
        )


def validate_anritsu_spectrum(
    safety: AnritsuSafety,
    *,
    start_hz: float,
    stop_hz: float,
    reference_level_dbm: float,
    points: int,
    dut_max_expected_input_dbm: float | None = None,
) -> None:
    """Validate the effective range before any Anritsu SCPI is issued."""

    assert_anritsu_acquisition_allowed(safety)
    validate_anritsu_dut_input(safety, dut_max_expected_input_dbm)
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


def validate_anritsu_signal_generator(
    settings: AnritsuSettings,
    *,
    frequency_hz: float,
    power_dbm: float,
) -> None:
    """Validate a generator setpoint against the qualified station envelope."""

    generator = settings.signal_generator
    if generator.control_protocol != "basic_scpi":
        raise SafetyViolation(
            "Anritsu signal-generator control is unverified for this option and firmware."
        )
    if not all(math.isfinite(value) for value in (frequency_hz, power_dbm)):
        raise SafetyViolation("Anritsu SG frequency and power must be finite.")
    if frequency_hz <= 0:
        raise SafetyViolation("Anritsu SG frequency must be positive.")
    if generator.frequency.min is None or generator.frequency.max is None:
        raise SafetyViolation("Define the approved Anritsu SG frequency range first.")
    if generator.power.min is None or generator.power.max is None:
        raise SafetyViolation("Define the approved Anritsu SG power range first.")
    frequency_min = parse_quantity(generator.frequency.min, DIMENSION_FREQUENCY).si_value
    frequency_max = parse_quantity(generator.frequency.max, DIMENSION_FREQUENCY).si_value
    power_min = parse_quantity(generator.power.min, DIMENSION_DBM).si_value
    power_max = parse_quantity(generator.power.max, DIMENSION_DBM).si_value
    if not frequency_min <= frequency_hz <= frequency_max:
        raise SafetyViolation(
            f"Anritsu SG frequency {frequency_hz:.9g} Hz is outside the approved range "
            f"{frequency_min:.9g}–{frequency_max:.9g} Hz."
        )
    if not power_min <= power_dbm <= power_max:
        raise SafetyViolation(
            f"Anritsu SG power {power_dbm:.9g} dBm is outside the approved range "
            f"{power_min:.9g}–{power_max:.9g} dBm."
        )


def validate_anritsu_advanced_spectrum(
    settings: AnritsuSettings,
    *,
    rbw_auto: bool,
    rbw_hz: float | None,
    vbw_mode: str,
    vbw_hz: float | None,
    detector: str,
    attenuation_auto: bool,
    attenuation_db: float | None,
    preamplifier_enabled: bool,
    sweep_time_auto: bool,
    sweep_time_s: float | None,
    hardware_options: tuple[str, ...],
) -> None:
    """Validate documented MS2830A advanced controls before any write."""

    assert_anritsu_acquisition_allowed(settings.safety)
    if settings.advanced_spectrum.control_protocol != "standard_scpi":
        raise SafetyViolation(
            "Anritsu advanced Spectrum Analyzer control is unverified for this firmware."
        )

    normalized_detector = detector.strip().upper()
    allowed_detectors = set(ANRITSU_BASIC_DETECTORS)
    if ANRITSU_CISPR_OPTIONS.intersection(hardware_options):
        allowed_detectors.update(ANRITSU_CISPR_DETECTORS)
    if normalized_detector not in allowed_detectors:
        raise SafetyViolation(
            f"Detector {detector!r} is not qualified for the detected Anritsu options."
        )

    if not rbw_auto and (
        rbw_hz is None or not math.isfinite(rbw_hz) or not 1 <= rbw_hz <= 31.25e6
    ):
        raise SafetyViolation("Manual Anritsu RBW must be within 1 Hz..31.25 MHz.")

    normalized_vbw_mode = vbw_mode.strip().lower()
    if normalized_vbw_mode not in {"auto", "manual", "off"}:
        raise SafetyViolation("Anritsu VBW mode must be auto, manual, or off.")
    if normalized_vbw_mode == "manual" and (
        vbw_hz is None or not math.isfinite(vbw_hz) or not 1 <= vbw_hz <= 10e6
    ):
        raise SafetyViolation("Manual Anritsu VBW must be within 1 Hz..10 MHz.")

    minimum_attenuation = settings.safety.rf_input.minimum_internal_attenuation
    minimum_db = (
        parse_quantity(minimum_attenuation, DIMENSION_DB).si_value
        if minimum_attenuation is not None
        else None
    )
    if attenuation_auto:
        if minimum_db is not None and minimum_db > 0:
            raise SafetyViolation(
                "Automatic attenuation is forbidden because the safety profile requires a "
                "minimum internal attenuation. Select a manual value at or above that limit."
            )
    else:
        if attenuation_db is None or not math.isfinite(attenuation_db):
            raise SafetyViolation("Manual Anritsu attenuation requires a finite value.")
        if not 0 <= attenuation_db <= 60 or not math.isclose(
            attenuation_db % 2, 0.0, abs_tol=1e-9
        ):
            raise SafetyViolation("Anritsu attenuation must be 0..60 dB in 2 dB steps.")
        if minimum_db is not None and attenuation_db < minimum_db:
            raise SafetyViolation(
                f"Anritsu attenuation {attenuation_db:g} dB is below the approved minimum "
                f"{minimum_db:g} dB."
            )

    if preamplifier_enabled:
        if not settings.safety.rf_input.preamplifier_allowed:
            raise SafetyViolation("The Anritsu preamplifier is disabled by the safety profile.")
        if not ANRITSU_PREAMPLIFIER_OPTIONS.intersection(hardware_options):
            raise SafetyViolation("The connected Anritsu did not report a preamplifier option.")

    if not sweep_time_auto and (
        sweep_time_s is None
        or not math.isfinite(sweep_time_s)
        or not 1e-3 <= sweep_time_s <= 1000
    ):
        raise SafetyViolation(
            "Manual Anritsu frequency-domain sweep time must be within 1 ms..1000 s."
        )
