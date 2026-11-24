"""Provenance for a completed Anritsu linear-power average."""

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
from sys import float_info


ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS = 2
ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS = 9999


@dataclass(frozen=True, slots=True)
class InstrumentAverageProvenance:
    sweep_count: int
    sweep_time_s: float
    requested_sweep_count: int | None = None
    target_duration_s: float | None = None
    elapsed_s: float | None = None

    def __post_init__(self) -> None:
        if (type(self.sweep_count) is not int
                or not ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS <= self.sweep_count <= ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS):
            raise ValueError("Anritsu average requires an integer sweep count in 2..9999.")
        if (type(self.sweep_time_s) not in (int, float)
                or not 0 < self.sweep_time_s <= float_info.max / self.sweep_count):
            raise ValueError("Anritsu average requires a positive finite nominal sweep time in seconds.")
        if self.requested_sweep_count is not None and (
            type(self.requested_sweep_count) is not int
            or self.requested_sweep_count != self.sweep_count
            or self.target_duration_s is not None
        ):
            raise ValueError("Anritsu exact-count completion must match its only requested target.")
        if self.target_duration_s is not None and (
            type(self.target_duration_s) not in (int, float)
            or not 0 < self.target_duration_s <= float_info.max
        ):
            raise ValueError("Anritsu average target duration must be positive finite seconds.")
        if self.elapsed_s is not None and (
            type(self.elapsed_s) not in (int, float) or not 0 <= self.elapsed_s <= float_info.max
        ):
            raise ValueError("Anritsu average elapsed time must be nonnegative finite seconds.")

    @property
    def nominal_duration_s(self) -> float:
        return self.sweep_count * self.sweep_time_s

    def metadata(self) -> dict[str, str | int | float]:
        return {
            "trace_storage_mode": "LAV",
            "sweep_count": self.sweep_count,
            "sweep_time_s": self.sweep_time_s,
            "nominal_duration_s": self.nominal_duration_s,
            **{
                name: getattr(self, name)
                for name in ("requested_sweep_count", "target_duration_s", "elapsed_s")
                if getattr(self, name) is not None
            },
        }

    @classmethod
    def from_metadata(cls, metadata: object) -> "InstrumentAverageProvenance":
        if not isinstance(metadata, Mapping) or metadata.get("trace_storage_mode") != "LAV":
            raise ValueError("Anritsu average evidence requires Lin Average storage mode (LAV).")
        result = cls(
            sweep_count=metadata.get("sweep_count"),
            sweep_time_s=metadata.get("sweep_time_s"),
            requested_sweep_count=metadata.get("requested_sweep_count"),
            target_duration_s=metadata.get("target_duration_s"),
            elapsed_s=metadata.get("elapsed_s"),
        )
        if "nominal_duration_s" in metadata:
            nominal = metadata["nominal_duration_s"]
            if (type(nominal) not in (int, float)
                    or not 0 < nominal <= float_info.max
                    or not math.isclose(nominal, result.nominal_duration_s, rel_tol=1e-12)):
                raise ValueError("Anritsu nominal duration must match sweep count × nominal sweep time.")
        return result


def legacy_instrument_average_count(trace_name: str) -> int | None:
    """Identify the exact name written by older manual Lin Average captures."""
    match = re.fullmatch(r"TRAC1_ANRITSU_LAV([1-9][0-9]{0,3})", trace_name)
    if match:
        count = int(match.group(1))
        if ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS <= count <= ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS:
            return count
    return None


def instrument_average_metadata(point_metadata: Mapping) -> object | None:
    """Read the evidence used by recipe checkpoints or standalone artifacts."""
    recipe = point_metadata.get("spectrum_processing_v1")
    if isinstance(recipe, Mapping) and "instrument_average" in recipe:
        return recipe["instrument_average"]
    if "instrument_average" in point_metadata:
        return point_metadata["instrument_average"]
    if point_metadata.get("artifact_schema") == "lab-control-sweep-baseline-v1":
        acquisition = point_metadata.get("acquisition_metadata")
        if isinstance(acquisition, Mapping):
            return acquisition.get("instrument_average")
    if point_metadata.get("background_averaging_source") == "instrument":
        # Older standalone backgrounds already carried these two observed values.
        return {
            "trace_storage_mode": "LAV",
            "sweep_count": point_metadata.get("background_sweep_count"),
            "sweep_time_s": point_metadata.get("nominal_sweep_time_s"),
        }
    return None
