"""Versioned, branch-aware B(U_DAC) calibration, independent of Qt and I/O.

Only measured DC reference data may create a model. A legacy MCAL has neither
units nor sufficient provenance to pass this contract and is not auto-imported.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

from app.domain.errors import ConfigurationError
from app.safety.moke_box import MokeVoltagePlan, finite_number

Direction = Literal["ascending", "descending"]


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True, slots=True)
class CalibrationContext:
    profile_fingerprint: str
    binding_id: str
    channel: int
    simulation: bool
    reference_idn: str
    probe_id: str
    orientation: str
    geometry: str
    reference_uncertainty_t: float
    hall_gain_description: str = "not recorded"

    def __post_init__(self) -> None:
        for name in ("profile_fingerprint", "binding_id", "reference_idn", "probe_id", "orientation", "geometry"):
            if not getattr(self, name).strip():
                raise ConfigurationError(f"Calibration requires {name}.")
        if type(self.channel) is not int or self.channel not in range(8):
            raise ConfigurationError("Calibration channel must be an integer in 0..7.")
        finite_number(self.reference_uncertainty_t, "Reference uncertainty [T]")
        if self.reference_uncertainty_t <= 0:
            raise ConfigurationError("Calibration requires a positive reference uncertainty.")


@dataclass(frozen=True, slots=True)
class CalibrationRequest:
    plan: MokeVoltagePlan
    context: CalibrationContext
    samples_per_point: int = 3
    settling_s: float = 1.0
    sample_interval_s: float = 0.1
    point_timeout_s: float = 30.0
    repetitions: int = 2
    warmup_cycles: int = 1
    acquire_hall: bool = True
    maximum_stddev_t: float = 0.0001

    def __post_init__(self) -> None:
        for name, lower, upper in (("samples_per_point", 2, 1000), ("repetitions", 1, 100),
                                   ("warmup_cycles", 1, 10)):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ConfigurationError(f"Calibration {name} must be in {lower}..{upper}.")
        for name in ("settling_s", "sample_interval_s", "point_timeout_s", "maximum_stddev_t"):
            finite_number(getattr(self, name), name)
        if not 0 <= self.settling_s < self.point_timeout_s <= 3600 or self.maximum_stddev_t <= 0:
            raise ConfigurationError("Invalid calibration settling, point deadline or stability threshold.")
        if self.sample_interval_s < 0.01 or self.settling_s + self.samples_per_point * self.sample_interval_s >= self.point_timeout_s:
            raise ConfigurationError("Calibration sampling interval must be at least 10 ms and fit the point deadline.")
        if self.context.profile_fingerprint != self.plan.profile_fingerprint or self.context.channel != self.plan.channel:
            raise ConfigurationError("Calibration context and voltage plan do not match.")
        if len(self.plan.targets_v) < 3 or any(
            right <= left for left, right in zip(self.plan.targets_v, self.plan.targets_v[1:])
        ):
            raise ConfigurationError("Calibration requires at least three strictly ascending voltage grid points.")
        applied = tuple(self.plan.applied_voltage(value) for value in self.plan.targets_v)
        if any(right <= left for left, right in zip(applied, applied[1:])):
            raise ConfigurationError("Calibration grid must contain distinct representable DAC voltages.")

    @property
    def trajectory(self) -> MokeVoltagePlan:
        up = self.plan.targets_v
        cycle = up + tuple(reversed(up))
        return MokeVoltagePlan(
            self.plan.profile_fingerprint, self.plan.channel,
            self.plan.minimum_v, self.plan.maximum_v,
            cycle * (self.warmup_cycles + self.repetitions),
            max(self.plan.settling_s, self.settling_s),
        )


@dataclass(frozen=True, slots=True)
class CalibrationPoint:
    index: int
    cycle: int
    direction: Direction
    requested_v: float
    applied_v: float
    actual_v: float
    reference_samples_t: tuple[float, ...]
    reference_timestamps_utc: tuple[str, ...]
    reference_snapshots: tuple[dict[str, object], ...]
    hall_samples_v: tuple[float, ...] = ()
    hall_raw_codes: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.direction not in {"ascending", "descending"}:
            raise ConfigurationError("Unknown calibration direction.")
        if type(self.index) is not int or self.index < 0 or type(self.cycle) is not int or self.cycle < 0:
            raise ConfigurationError("Calibration indices must be nonnegative integers.")
        for name in ("requested_v", "applied_v", "actual_v"):
            finite_number(getattr(self, name), name)
        if not -10 <= self.actual_v <= 10 or not self.reference_samples_t:
            raise ConfigurationError("Calibration needs an actual DAC voltage and reference samples.")
        if len(self.reference_samples_t) != len(self.reference_timestamps_utc) or len(self.reference_samples_t) != len(self.reference_snapshots):
            raise ConfigurationError("Calibration reference sample metadata must have matching lengths.")
        for timestamp in self.reference_timestamps_utc:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
                raise ConfigurationError("Calibration timestamps must include UTC timezone.")
        for value in (*self.reference_samples_t, *self.hall_samples_v):
            finite_number(value, "Calibration sample")
        if len(self.hall_samples_v) != len(self.hall_raw_codes):
            raise ConfigurationError("Hall voltages and raw AD7734 codes must have matching lengths.")
        if any(type(code) is not int or not 0 <= code <= 0xFFFFFF for code in self.hall_raw_codes):
            raise ConfigurationError("Hall raw codes must be unsigned 24-bit integers.")


@dataclass(frozen=True, slots=True)
class CalibrationBranch:
    direction: Direction
    voltage_v: tuple[float, ...]
    field_t: tuple[float, ...]
    stddev_t: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.direction not in {"ascending", "descending"}:
            raise ConfigurationError("Unknown calibration branch.")
        if len(self.voltage_v) < 3 or len(self.voltage_v) != len(self.field_t) or len(self.field_t) != len(self.stddev_t):
            raise ConfigurationError("Calibration branch requires at least three equal-length arrays.")
        for value in (*self.voltage_v, *self.field_t, *self.stddev_t):
            finite_number(value, "Calibration branch value")
        if any(right <= left for left, right in zip(self.voltage_v, self.voltage_v[1:])):
            raise ConfigurationError("Calibration branch voltages must be strictly increasing.")
        if min(self.stddev_t) < 0:
            raise ConfigurationError("Calibration standard deviation cannot be negative.")

    def estimate(self, voltage_v: float) -> float:
        finite_number(voltage_v, "Estimated DAC voltage")
        if not self.voltage_v[0] <= voltage_v <= self.voltage_v[-1]:
            raise ConfigurationError("Voltage is outside the calibrated branch; extrapolation is disabled.")
        index = bisect_left(self.voltage_v, voltage_v)
        if index == 0 or self.voltage_v[index] == voltage_v:
            return self.field_t[index]
        lower, upper = self.voltage_v[index - 1], self.voltage_v[index]
        fraction = (voltage_v - lower) / (upper - lower)
        return self.field_t[index - 1] + fraction * (self.field_t[index] - self.field_t[index - 1])


@dataclass(frozen=True, slots=True)
class MokeCalibration:
    context: CalibrationContext
    created_utc: str
    ascending: CalibrationBranch
    descending: CalibrationBranch
    raw_run_id: str
    raw_sha256: str
    conditioning: str = "full-range-cycle"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.conditioning != "full-range-cycle":
            raise ConfigurationError("Unsupported MOKE calibration schema or conditioning protocol.")
        if self.ascending.direction != "ascending" or self.descending.direction != "descending":
            raise ConfigurationError("Calibration requires both correctly named branches.")
        if not self.raw_run_id or len(self.raw_sha256) != 64:
            raise ConfigurationError("Calibration requires its durable raw-run identity and hash.")
        parsed = datetime.fromisoformat(self.created_utc)
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ConfigurationError("Calibration creation time must include UTC timezone.")

    @property
    def calibration_id(self) -> str:
        return hashlib.sha256(canonical_json(asdict(self)).encode()).hexdigest()

    def estimate(self, voltage_v: float, direction: Direction, *, profile_fingerprint: str,
                 simulation: bool, conditioned: bool) -> float:
        if profile_fingerprint != self.context.profile_fingerprint or simulation != self.context.simulation:
            raise ConfigurationError("Calibration does not match the active physical/simulation profile.")
        if not conditioned:
            raise ConfigurationError("Field estimate requires the recorded full-range conditioning history.")
        if direction not in {"ascending", "descending"}:
            raise ConfigurationError("Field estimate requires a known calibrated branch.")
        branch = self.ascending if direction == "ascending" else self.descending
        return branch.estimate(voltage_v)

    @classmethod
    def from_document(cls, document: dict[str, object]) -> MokeCalibration:
        try:
            data = dict(document)
            data["context"] = CalibrationContext(**data["context"])
            for name in ("ascending", "descending"):
                branch = dict(data[name])
                for key in ("voltage_v", "field_t", "stddev_t"):
                    branch[key] = tuple(branch[key])
                data[name] = CalibrationBranch(**branch)
            return cls(**data)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"Invalid MOKE calibration document: {exc}") from exc


def build_branches(points: tuple[CalibrationPoint, ...]) -> tuple[CalibrationBranch, CalibrationBranch]:
    """Average repeat cycles per branch without merging hysteresis branches."""
    import statistics

    branches = []
    for direction in ("ascending", "descending"):
        buckets: dict[float, list[float]] = {}
        for point in points:
            if point.direction == direction:
                buckets.setdefault(point.actual_v, []).extend(point.reference_samples_t)
        voltages = tuple(sorted(buckets))
        branches.append(CalibrationBranch(
            direction, voltages, tuple(statistics.fmean(buckets[v]) for v in voltages),
            tuple(statistics.pstdev(buckets[v]) for v in voltages),
        ))
    return branches[0], branches[1]
