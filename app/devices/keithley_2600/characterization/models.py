"""Data models for Keithley sample characterization and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Literal


@dataclass(frozen=True, slots=True)
class SampleMetadata:
    """Provenance and device under test physical parameters."""

    sample_id: str
    structure_name: str = ""
    operator: str = ""
    junction_area_um2: float | None = None
    nominal_barrier_thickness_nm: float = 1.0
    temperature_k: float | None = None
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CharacterizationSweepConfig:
    """Configured sweep parameters for IV characterization."""

    channel: Literal["A", "B"] = "A"
    mode: Literal["current", "voltage"] = "current"
    start_level_si: float = -0.010
    stop_level_si: float = 0.010
    points_count: int = 101
    compliance_si: float = 0.670
    dwell_time_s: float = 0.05
    sense_mode: Literal["2wire", "4wire"] = "4wire"
    metadata: SampleMetadata = field(default_factory=lambda: SampleMetadata(sample_id="Sample-1"))


@dataclass(frozen=True, slots=True)
class CharacterizationPoint:
    """One acquired point in the characterization sweep."""

    index: int
    demanded_si: float
    measured_voltage_v: float
    measured_current_a: float
    true_resistance_ohm: float
    apparent_resistance_ohm: float
    power_w: float
    compliance_active: bool
    timestamp_epoch: float


@dataclass(frozen=True, slots=True)
class ExtractedScientificParameters:
    """Physical and analytical quantities derived from the dataset."""

    zero_bias_resistance_ohm: float
    zero_bias_conductance_s: float
    ra_product_ohm_um2: float | None
    compliance_detected: bool
    compliance_onset_point: tuple[float, float] | None  # (I, V)
    clamped_points_fraction: float
    max_power_dissipated_w: float
    rectification_ratio: float | None
    tunnel_barrier_height_ev: float | None
    tunnel_barrier_asymmetry_ev: float | None
    tunnel_barrier_thickness_nm: float | None
    linearity_r2: float
    max_voltage_v: float = 0.0
    max_current_a: float = 0.0
    bdr_coefficients: tuple[float, float, float] | None = None  # c0, c1, c2: G(V) = c0 + c1*V + c2*V^2
    differential_resistance_curve: list[tuple[float, float]] = field(default_factory=list)  # (V, dV/dI)
    differential_conductance_curve: list[tuple[float, float]] = field(default_factory=list)  # (V, dI/dV)


@dataclass(frozen=True, slots=True)
class CharacterizationDataset:
    """Complete dataset acquired during characterization."""

    config: CharacterizationSweepConfig
    points: tuple[CharacterizationPoint, ...]
    started_at_iso: str
    completed_at_iso: str
    checksum_sha256: str = ""

    @staticmethod
    def calculate_checksum(points: tuple[CharacterizationPoint, ...] | list[CharacterizationPoint]) -> str:
        """Compute deterministic SHA-256 over measured points."""
        hasher = hashlib.sha256()
        for p in points:
            hasher.update(
                f"{p.index}:{p.demanded_si:.9e}:{p.measured_voltage_v:.9e}:{p.measured_current_a:.9e}:{p.compliance_active}".encode("utf-8")
            )
        return hasher.hexdigest()
