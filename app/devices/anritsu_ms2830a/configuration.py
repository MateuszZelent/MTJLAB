"""Shared immutable Anritsu requests without transport dependencies."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpectrumConfig:
    start_hz: float
    stop_hz: float
    reference_level_dbm: float
    points: int
    trace: str = "TRAC1"
    rbw_auto: bool | None = None
    rbw_hz: float | None = None
    vbw_auto: bool | None = None
    vbw_mode: str | None = None
    vbw_hz: float | None = None
    changed_fields: tuple[str, ...] | None = None
    prepare_current_buffer: bool = True


@dataclass(frozen=True, slots=True)
class SignalGeneratorConfig:
    frequency_hz: float
    power_dbm: float
    changed_fields: tuple[str, ...] | None = None


