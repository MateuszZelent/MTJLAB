"""Immutable experiment-specific DUT safety declarations in SI units."""

from __future__ import annotations

from dataclasses import dataclass, field
import math


@dataclass(frozen=True, slots=True)
class DutRange:
    minimum_si: float
    maximum_si: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.minimum_si) or not math.isfinite(self.maximum_si):
            raise ValueError("DUT range boundaries must be finite.")
        if self.minimum_si > self.maximum_si:
            raise ValueError("DUT range minimum must not exceed its maximum.")


@dataclass(frozen=True, slots=True)
class KeithleyDutLimits:
    current: DutRange | None = None
    voltage: DutRange | None = None
    max_abs_power_w: float | None = None

    def __post_init__(self) -> None:
        if self.max_abs_power_w is not None and (
            not math.isfinite(self.max_abs_power_w) or self.max_abs_power_w <= 0
        ):
            raise ValueError("Keithley DUT power limit must be finite and positive.")


@dataclass(frozen=True, slots=True)
class RigolDutLimits:
    minimum_impedance_ohm: float | None = None
    max_abs_current_a: float | None = None
    max_abs_power_w: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("minimum impedance", self.minimum_impedance_ohm),
            ("maximum current", self.max_abs_current_a),
            ("maximum power", self.max_abs_power_w),
        ):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"Rigol DUT {name} must be finite and positive.")


@dataclass(frozen=True, slots=True)
class AnritsuDutLimits:
    max_expected_input_dbm: float | None = None
    max_signal_generator_output_dbm: float | None = None

    def __post_init__(self) -> None:
        if self.max_expected_input_dbm is not None and not math.isfinite(
            self.max_expected_input_dbm
        ):
            raise ValueError("Anritsu DUT input limit must be finite.")
        if self.max_signal_generator_output_dbm is not None and not math.isfinite(
            self.max_signal_generator_output_dbm
        ):
            raise ValueError("Anritsu DUT signal-generator output limit must be finite.")


@dataclass(frozen=True, slots=True)
class ExperimentDutLimits:
    keithley: dict[str, KeithleyDutLimits] = field(default_factory=dict)
    rigol: dict[int, RigolDutLimits] = field(default_factory=dict)
    anritsu: AnritsuDutLimits | None = None

    @property
    def declared(self) -> bool:
        return bool(self.keithley or self.rigol or self.anritsu is not None)
