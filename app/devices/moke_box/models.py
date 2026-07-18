"""Protocol-neutral data model for MOKE Box integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import statistics
from typing import Mapping


@dataclass(frozen=True, slots=True)
class MokeBoxConfig:
    endpoint: str
    timeout_s: float = 3.0
    expected_model: str | None = None
    allow_vout_control: bool = False
    allowed_vout_channels: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.endpoint.strip():
            raise ValueError("MOKE endpoint cannot be empty.")
        if self.timeout_s <= 0:
            raise ValueError("MOKE timeout must be positive.")
        if any(channel not in range(8) for channel in self.allowed_vout_channels):
            raise ValueError("MOKE VOUT channels must be in 0..7.")
        if len(set(self.allowed_vout_channels)) != len(self.allowed_vout_channels):
            raise ValueError("MOKE VOUT channels must not be duplicated.")


@dataclass(frozen=True, slots=True)
class MokeReading:
    signal: float
    unit: str = "arb"
    timestamp_utc: datetime = datetime.min.replace(tzinfo=timezone.utc)

    @classmethod
    def now(cls, signal: float, unit: str = "arb") -> "MokeReading":
        return cls(signal=signal, unit=unit, timestamp_utc=datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class MokeSampleBatch:
    """One bounded acquisition returned by the confirmed SendData command."""

    samples_by_stream: Mapping[str, tuple[int, ...]]
    requested_samples: int
    timestamp_utc: datetime

    @classmethod
    def now(
        cls, samples_by_stream: Mapping[str, tuple[int, ...]], requested_samples: int
    ) -> "MokeSampleBatch":
        return cls(dict(samples_by_stream), requested_samples, datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class MokeHallVoltageReading:
    """Direct, read-only Hall-1 voltage from the live AD7734 response."""

    voltage_v: float
    stddev_v: float
    samples: int
    raw_codes: tuple[int, ...]
    timestamp_utc: datetime

    @classmethod
    def from_ad7734_codes(cls, codes: tuple[int, ...]) -> "MokeHallVoltageReading":
        from app.devices.moke_box.protocol import decode_ad7734_voltage

        if not codes:
            raise ValueError("MOKE Hall response must contain at least one AD7734 sample.")
        values = tuple(decode_ad7734_voltage(code) for code in codes)
        return cls(
            voltage_v=statistics.fmean(values),
            stddev_v=statistics.pstdev(values),
            samples=len(values),
            raw_codes=codes,
            timestamp_utc=datetime.now(timezone.utc),
        )


@dataclass(frozen=True, slots=True)
class MokeFieldReading:
    """Open-loop Hall-field measurement using the verified base calibration."""

    hall1_voltage_v: float
    hall1_field_t: float
    hall1_stddev_v: float
    hall2_voltage_v: float
    hall2_field_t: float
    hall2_stddev_v: float
    samples: int
    timestamp_utc: datetime

    @classmethod
    def from_hall_voltages(
        cls, hall1: tuple[float, ...], hall2: tuple[float, ...]
    ) -> "MokeFieldReading":
        if not hall1 or not hall2 or len(hall1) != len(hall2):
            raise ValueError("MOKE Hall streams must be non-empty and equal length.")
        mean1, mean2 = statistics.fmean(hall1), statistics.fmean(hall2)
        return cls(
            hall1_voltage_v=mean1,
            hall1_field_t=hall_field_from_voltage(mean1),
            hall1_stddev_v=statistics.pstdev(hall1),
            hall2_voltage_v=mean2,
            hall2_field_t=hall_field_from_voltage(mean2),
            hall2_stddev_v=statistics.pstdev(hall2),
            samples=len(hall1),
            timestamp_utc=datetime.now(timezone.utc),
        )


def hall_field_from_voltage(voltage_v: float) -> float:
    """Base Hall polynomial from the supplied calibration, output in tesla.

    The optional interpolation table is deliberately excluded: its X unit is
    not established by the reconstruction report.
    """

    if not math.isfinite(voltage_v):
        raise ValueError("MOKE Hall voltage must be finite.")
    c0, c1, c2, c3 = (
        -0.0007387072430926411,
        0.013032760125236825,
        0.0000027310986380390884,
        -0.00000868776802374576,
    )
    return c0 + voltage_v * (c1 + voltage_v * (c2 + voltage_v * c3))
