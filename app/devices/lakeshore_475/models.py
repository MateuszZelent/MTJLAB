"""Typed read-only domain model for the Lake Shore Model 475."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math


class MeasurementMode(str, Enum):
    DC = "dc"
    RMS = "rms"
    PEAK = "peak"


class FieldUnit(str, Enum):
    GAUSS = "gauss"
    TESLA = "tesla"
    OERSTED = "oersted"
    AMPERE_PER_METER = "ampere_per_meter"


_UNIT_CODES = {
    "1": FieldUnit.GAUSS,
    "2": FieldUnit.TESLA,
    "3": FieldUnit.OERSTED,
    "4": FieldUnit.AMPERE_PER_METER,
}
_MODE_CODES = {"1": MeasurementMode.DC, "2": MeasurementMode.RMS, "3": MeasurementMode.PEAK}


def field_unit_from_code(value: str) -> FieldUnit:
    try:
        return _UNIT_CODES[value.strip()]
    except KeyError as exc:
        raise ValueError(f"Unknown Lake Shore UNIT? code {value!r}.") from exc


def measurement_mode_from_code(value: str) -> MeasurementMode:
    try:
        return _MODE_CODES[value.strip()]
    except KeyError as exc:
        raise ValueError(f"Unknown Lake Shore RDGMODE? code {value!r}.") from exc


@dataclass(frozen=True, slots=True)
class GaussmeterConfig:
    """VISA profile for one Model 475; it contains no writable settings."""

    resource: str
    visa_backend: str = "system"
    timeout_ms: int = 3_000
    baud_rate: int = 57_600
    expected_serial: str | None = None
    require_serial_match: bool = False

    def __post_init__(self) -> None:
        if not self.resource.strip():
            raise ValueError("Lake Shore resource must not be empty.")
        if self.timeout_ms <= 0:
            raise ValueError("Lake Shore timeout must be positive.")
        if self.baud_rate not in {9600, 19200, 38400, 57600}:
            raise ValueError("Lake Shore baud_rate must be 9600, 19200, 38400, or 57600.")
        if self.require_serial_match and not self.expected_serial:
            raise ValueError("Lake Shore serial matching requires expected_serial.")


@dataclass(frozen=True, slots=True)
class GaussmeterSnapshot:
    mode_code: str
    mode: MeasurementMode
    unit_code: str
    unit: FieldUnit
    range_code: str
    autorange_enabled: bool
    probe_type_code: str
    timestamp_utc: datetime


@dataclass(frozen=True, slots=True)
class GaussmeterReading:
    mode: MeasurementMode
    unit: FieldUnit
    snapshot: GaussmeterSnapshot
    timestamp_utc: datetime
    field_t: float | None = None
    frequency_hz: float | None = None
    negative_peak_t: float | None = None
    positive_peak_t: float | None = None

    def __post_init__(self) -> None:
        values = (self.field_t, self.frequency_hz, self.negative_peak_t, self.positive_peak_t)
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("Lake Shore readings must be finite.")
        if self.mode is MeasurementMode.DC and (self.field_t is None or any(values[index] is not None for index in (1, 2, 3))):
            raise ValueError("DC readings require exactly field_t.")
        if self.mode is MeasurementMode.RMS and (self.field_t is None or self.frequency_hz is None or self.negative_peak_t is not None or self.positive_peak_t is not None):
            raise ValueError("RMS readings require field_t and frequency_hz only.")
        if self.mode is MeasurementMode.PEAK and (self.negative_peak_t is None or self.positive_peak_t is None or self.field_t is not None or self.frequency_hz is not None):
            raise ValueError("Peak readings require negative_peak_t and positive_peak_t only.")

    @classmethod
    def now(cls, *, mode: MeasurementMode, unit: FieldUnit, snapshot: GaussmeterSnapshot, **values: float | None) -> "GaussmeterReading":
        return cls(mode=mode, unit=unit, snapshot=snapshot, timestamp_utc=datetime.now(timezone.utc), **values)
