"""Nominal DC equivalents. No inversion, extrapolation or hardware access."""

from __future__ import annotations

from dataclasses import dataclass
import math

from app.devices.keithley_2600.characterization.models import CharacterizationDataset
from app.safety.rigol_current import rigol_display_voltage_from_open_circuit


@dataclass(frozen=True, slots=True)
class RigolEquivalentPoint:
    source_index: int
    current_a: float
    measured_voltage_v: float
    high_z_voltage_v: float | None
    load_50_voltage_v: float | None
    exclusion_reason: str = ""


def rigol_equivalence_context(dataset: CharacterizationDataset) -> dict:
    """Explicit assumptions; missing calibration is not a zero correction."""
    return {
        "model_version": "nominal_dc_v1",
        "topology_kind": "nominal_direct_connection",
        "source_resistance_ohm": 50.0,
        "sense_mode": dataset.config.sense_mode,
        "voltage_reference_plane": "keithley_sense_terminals" if dataset.config.sense_mode == "4wire" else "keithley_force_terminals",
        "equivalence_status": "conditional_external_drops_unknown" if dataset.config.sense_mode == "4wire" else "nominal_same_external_path",
        "external_series_resistance_ohm": None,
        "calibration_id": None,
        "frequency_validity_hz": None,
        "measurement_uncertainty": None,
        "field_current_a": dataset.field_line_current_a,
        "field_sequence_index": dataset.field_sequence_index,
        "history_segment": dataset.field_history_segment,
        "source_checksum": dataset.checksum_sha256 or dataset.calculate_checksum(dataset.points),
        "interpolation": "none",
    }


def rigol_equivalent_points(dataset: CharacterizationDataset) -> tuple[RigolEquivalentPoint, ...]:
    """Retain source order and exclusions; use measured I/V, never fitted R."""
    rows = []
    for point in dataset.points:
        current, voltage = point.measured_current_a, point.measured_voltage_v
        reason = ""
        if not point.valid:
            reason = "invalid_measurement_conditions"
        elif dataset.field_line_current_a is not None and (point.field_before is None or point.field_after is None):
            reason = "field_conditions_undocumented"
        elif any(observation is not None and observation.compliance_active
                 for observation in (point.field_before, point.field_after)):
            reason = "field_compliance"
        elif any(observation is not None and not all(math.isfinite(value) for value in (
                observation.measured_current_a, observation.measured_voltage_v, observation.power_w))
                for observation in (point.field_before, point.field_after)):
            reason = "invalid_field_observation"
        elif point.compliance_active:
            reason = "compliance"
        elif not math.isfinite(current) or not math.isfinite(voltage):
            reason = "nonfinite_measurement"
        elif current == 0 or voltage == 0:
            reason = "zero_measurement"
        elif (current > 0) != (voltage > 0):
            reason = "opposite_sign_iv"
        open_v = voltage + current * 50.0
        if not reason and not math.isfinite(open_v):
            reason = "nonfinite_equivalent"
        rows.append(RigolEquivalentPoint(
            source_index=point.index, current_a=current, measured_voltage_v=voltage,
            high_z_voltage_v=None if reason else rigol_display_voltage_from_open_circuit(open_v, "HIGHZ"),
            load_50_voltage_v=None if reason else rigol_display_voltage_from_open_circuit(open_v, 50.0),
            exclusion_reason=reason,
        ))
    return tuple(rows)
