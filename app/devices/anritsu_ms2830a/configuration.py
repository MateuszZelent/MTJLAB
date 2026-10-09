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

    @property
    def is_complete_configuration(self) -> bool:
        """An authored basic baseline remains complete even with a field mask."""
        return self.changed_fields is None or {"start_hz", "stop_hz", "reference_level_dbm", "points"} <= set(self.changed_fields)


@dataclass(frozen=True, slots=True)
class SignalGeneratorConfig:
    frequency_hz: float
    power_dbm: float
    changed_fields: tuple[str, ...] | None = None


