"""Data export utilities (CSV) for Keithley characterization datasets."""

from __future__ import annotations

import csv
from pathlib import Path

from app.devices.keithley_2600.characterization.models import CharacterizationDataset


class KeithleyDataExporter:
    """Exports raw measurement points and metadata to CSV format."""

    @classmethod
    def export_csv(cls, dataset: CharacterizationDataset, file_path: str | Path) -> Path:
        """Write characterization dataset to a standardized CSV file."""
        target = Path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        config = dataset.config
        meta = config.metadata

        with target.open("w", newline="", encoding="utf-8") as f:
            # Metadata header
            f.write("# MTJLAB - Keithley Sample Characterization Dataset\n")
            f.write(f"# Sample ID: {meta.sample_id}\n")
            if meta.structure_name:
                f.write(f"# Structure: {meta.structure_name}\n")
            if meta.operator:
                f.write(f"# Operator: {meta.operator}\n")
            if meta.diameter_nm is not None:
                f.write(f"# Pillar Diameter [nm]: {meta.diameter_nm}\n")
            if meta.junction_area_um2 is not None:
                f.write(f"# Junction Area [um^2]: {meta.junction_area_um2}\n")
            f.write(f"# Channel: {config.channel}\n")
            f.write(f"# Mode: {config.mode}\n")
            sense_description = "Kelvin (4-wire)" if config.sense_mode == "4wire" else "Local (2-wire)"
            f.write(f"# Sense Mode: {sense_description}\n")
            f.write(f"# Compliance Policy: {config.compliance_policy}\n")
            f.write(f"# NPLC: {config.nplc}\n")
            f.write(f"# Dwell Time [s]: {config.dwell_time_s}\n")
            f.write(f"# Source Autorange: {config.source_autorange}\n")
            f.write(f"# Source Range [SI]: {config.source_range_si}\n")
            f.write(f"# Measure Voltage Autorange: {config.measure_voltage_autorange}\n")
            f.write(f"# Measure Voltage Range [V]: {config.measure_voltage_range_si}\n")
            f.write(f"# Measure Current Autorange: {config.measure_current_autorange}\n")
            f.write(f"# Measure Current Range [A]: {config.measure_current_range_si}\n")
            if meta.nominal_barrier_thickness_nm:
                f.write(f"# Nominal Barrier Thickness [nm]: {meta.nominal_barrier_thickness_nm}\n")
            f.write(f"# Sweep Range: {config.start_level_si} to {config.stop_level_si} (Points: {config.points_count})\n")
            f.write("# Zero Setpoint Policy: omitted from characterization\n")
            f.write(f"# Zero Setpoint Omitted: {dataset.zero_setpoint_omitted}\n")
            f.write(f"# Compliance Limit: {config.compliance_si}\n")
            f.write(f"# Started At: {dataset.started_at_iso}\n")
            f.write(f"# Ended At: {dataset.completed_at_iso}\n")
            f.write(f"# Completion Status: {dataset.completion_status}\n")
            f.write(f"# Acquired Points: {len(dataset.points)} of {config.points_count}\n")
            if dataset.termination_detail:
                f.write(f"# Termination Detail: {dataset.termination_detail}\n")
            f.write(f"# Checksum SHA-256: {dataset.checksum_sha256}\n")
            f.write("#\n")

            writer = csv.writer(f)
            writer.writerow([
                "Index",
                "Demanded_SI",
                "Voltage_V",
                "Current_A",
                "True_Resistance_Ohm",
                "Apparent_Resistance_Ohm",
                "Power_W",
                "Compliance_Active",
                "Timestamp_Epoch_s",
            ])

            for p in dataset.points:
                writer.writerow([
                    p.index,
                    f"{p.demanded_si:.9e}",
                    f"{p.measured_voltage_v:.9e}",
                    f"{p.measured_current_a:.9e}",
                    f"{p.true_resistance_ohm:.9e}",
                    f"{p.apparent_resistance_ohm:.9e}",
                    f"{p.power_w:.9e}",
                    1 if p.compliance_active else 0,
                    f"{p.timestamp_epoch:.4f}",
                ])

        return target
