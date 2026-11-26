"""Unit tests for the Rigol MTJ reference measurement library (HDF5 & CSV)."""

from __future__ import annotations

import json
from pathlib import Path

import h5py
import pytest

from app.devices.keithley_2600.characterization.export import KeithleyDataExporter
from app.devices.keithley_2600.characterization.models import (
    CharacterizationDataset,
    CharacterizationPoint,
    CharacterizationSweepConfig,
    FieldLineObservation,
    SampleMetadata,
)
from app.devices.rigol_dg1000z.reference_library import (
    ReferenceFilter,
    ReferenceLibraryScanner,
    filter_reference_curves,
    load_csv_reference_curves,
    load_hdf5_reference_curves,
)
from app.inventory.models import Sample, SampleRunRecord
from app.inventory.store import InventoryStore

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SWEEP_CSV = REPO_ROOT / "artifacts" / "sweep-r19c7-02" / "keithley-a.csv"


def _write_synthetic_hdf5(
    path: Path,
    *,
    sample_id: str = "S1",
    sample_name: str = "INL-W1",
    row: str = "19",
    col: str = "7",
    slices: list[tuple[float, float, list[tuple[float, float, bool]]]],
) -> Path:
    """Create a minimal committed HDF5 run compatible with Hdf5RunReader."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as handle:
        run = handle.create_group("run")
        run.attrs["status"] = "completed"
        run.attrs["created_at_utc"] = "2026-11-25T10:00:00Z"
        run.attrs["sample_id"] = sample_id
        run.attrs["sample_name"] = sample_name
        run.attrs["sample_row"] = row
        run.attrs["sample_col"] = col
        run.attrs["sample_coordinate_label"] = f"R{row}C{col}"
        points_grp = handle.create_group("points")
        idx = 0
        for kb_a, moke_v, iv_rows in slices:
            for current_a, voltage_v, compliance in iv_rows:
                grp = points_grp.create_group(str(idx))
                grp.attrs["complete"] = True
                grp.attrs["status"] = "ok"
                grp.attrs["timestamp_utc"] = "2026-11-25T10:00:01Z"
                grp.create_dataset(
                    "setpoints_json",
                    data=json.dumps(
                        {
                            "keithley.A.current": current_a,
                            "keithley.B.current": kb_a,
                            "moke_box.vout0.voltage": moke_v,
                        }
                    ),
                )
                resistance = abs(voltage_v / current_a) if abs(current_a) > 1e-12 else 0.0
                grp.create_dataset(
                    "measurements_json",
                    data=json.dumps(
                        {
                            "keithley.A.current_a": current_a,
                            "keithley.A.voltage_v": voltage_v,
                            "keithley.A.resistance_ohm": resistance,
                            "keithley.A.resistance_valid": float(abs(current_a) > 1e-12),
                            "keithley.A.compliance_detected": float(compliance),
                            "keithley.B.current_a": kb_a,
                            "moke_box.vout_voltage_v": moke_v,
                            "moke_box.hall1_voltage_v": -0.012,
                        }
                    ),
                )
                grp.create_dataset("metadata_json", data="{}")
                grp.create_dataset("device_states_json", data="{}")
                idx += 1
    return path


def test_load_real_keithley_a_csv_artifact() -> None:
    assert SAMPLE_SWEEP_CSV.is_file(), f"Missing sample CSV at {SAMPLE_SWEEP_CSV}"
    curves = load_csv_reference_curves(SAMPLE_SWEEP_CSV)
    assert len(curves) == 1
    curve = curves[0]
    assert curve.source_kind == "sweep_csv"
    assert curve.keithley_b_setpoint_a == pytest.approx(54e-3)
    assert curve.keithley_b_measured_a == pytest.approx(54.03e-3, abs=0.5e-3)
    assert curve.moke_vout_v == pytest.approx(-75.0732421875e-3)
    assert len(curve.passes) == 1
    assert len(curve.passes[0].points) == 101
    stats = curve.passes[0].resistance_stats
    assert stats is not None
    # Near-zero 105.3 ohm artifact excluded from resistance summary
    assert 108.0 <= stats.min_ohm <= 109.0
    assert 118.0 <= stats.max_ohm <= 119.5


def test_load_characterization_csv_with_field_metadata(tmp_path: Path) -> None:
    cfg = CharacterizationSweepConfig(
        channel="A",
        mode="current",
        start_level_si=-1e-3,
        stop_level_si=1e-3,
        points_count=4,
        compliance_si=0.5,
        nplc=1.0,
        dwell_time_s=0.01,
        sense_mode="2wire",
        metadata=SampleMetadata(sample_id="R19C7"),
    )
    obs = FieldLineObservation(
        demanded_current_a=0.054,
        measured_current_a=0.05401,
        measured_voltage_v=0.41,
        power_w=0.022,
        timestamp_epoch=1.0,
        compliance_active=False,
    )
    pts = tuple(
        CharacterizationPoint(
            index=i,
            demanded_si=curr,
            measured_voltage_v=curr * 116.0,
            measured_current_a=curr,
            true_resistance_ohm=116.0,
            apparent_resistance_ohm=116.0,
            power_w=curr * curr * 116.0,
            compliance_active=False,
            timestamp_epoch=float(i),
            valid=True,
            field_before=obs,
            field_after=obs,
        )
        for i, curr in enumerate((-1e-3, -0.5e-3, 0.5e-3, 1e-3))
    )
    dataset = CharacterizationDataset(
        config=cfg,
        points=pts,
        started_at_iso="2026-11-25T10:00:00Z",
        completed_at_iso="2026-11-25T10:00:10Z",
        checksum_sha256=CharacterizationDataset.calculate_checksum(pts),
        field_line_current_a=0.054,
    )
    csv_path = KeithleyDataExporter.export_csv(dataset, tmp_path / "characterization.csv")

    curves = load_csv_reference_curves(csv_path)
    assert len(curves) == 1
    curve = curves[0]
    assert curve.source_kind == "characterization_csv"
    assert curve.sample_id == "R19C7"
    assert curve.keithley_b_setpoint_a == pytest.approx(0.054)
    assert curve.sense_mode == "2wire"
    assert len(curve.passes[0].points) == 4


def test_load_csv_fails_closed_without_explicit_units(tmp_path: Path) -> None:
    bad_csv = tmp_path / "missing_units.csv"
    bad_csv.write_text("Voltage,Current\n100,1\n200,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unit"):
        load_csv_reference_curves(bad_csv)


def test_load_hdf5_splits_multi_slice_keithley_b_and_moke_and_excludes_compliance(
    tmp_path: Path,
) -> None:
    h5_path = _write_synthetic_hdf5(
        tmp_path / "2_20261125T100000Z_sweep" / "data.h5",
        slices=[
            (
                0.054,
                -0.075,
                [
                    (-2e-3, -220e-3, False),
                    (-1e-3, -112e-3, False),
                    (0.0, 0.0, False),
                    (1e-3, 115e-3, False),
                    (2e-3, 230e-3, True),  # compliance -> excluded
                ],
            ),
            (
                0.020,
                -0.030,
                [
                    (-2e-3, -240e-3, False),
                    (0.0, 0.0, False),
                    (2e-3, 240e-3, False),
                ],
            ),
        ],
    )
    curves = load_hdf5_reference_curves(h5_path)
    assert len(curves) == 2

    by_kb = {round(c.keithley_b_setpoint_a or 0.0, 4): c for c in curves}
    assert set(by_kb) == {0.054, 0.020}
    c54 = by_kb[0.054]
    assert c54.moke_vout_v == pytest.approx(-0.075)
    assert c54.excluded_point_count == 1
    assert len(c54.passes[0].points) == 4

    # Filter by Keithley B and MOKE VOUT0
    matched = filter_reference_curves(
        curves,
        ReferenceFilter(
            keithley_b_target_a=0.054,
            keithley_b_tol_a=1e-3,
            moke_vout_target_v=-0.075,
            moke_vout_tol_v=1e-3,
        ),
    )
    assert len(matched) == 1
    assert matched[0].curve_id == c54.curve_id


def test_scanner_discovers_inventory_and_directory_and_caches_by_mtime(tmp_path: Path) -> None:
    store = InventoryStore(tmp_path / "inventory.db")
    try:
        store.save_sample(
            Sample(sample_id="S1", name="INL-W1", rows=("19",), cols=("7",))
        )
        h5_path = _write_synthetic_hdf5(
            tmp_path / "measurements" / "1_20261125T100000Z_sweep" / "data.h5",
            slices=[(0.054, -0.075, [(-1e-3, -110e-3, False), (1e-3, 110e-3, False)])],
        )
        store.register_run_artifacts(
            SampleRunRecord(
                sample_id="S1",
                sample_name="INL-W1",
                row="19",
                col="7",
                device_label="R19C7",
                run_path=str(h5_path),
                run_sha256="abc",
                created_at_utc="2026-11-25T10:00:00Z",
                status="completed",
                point_count=2,
                spectrum_count=0,
                recipe_name="Sweep 2",
            )
        )
        # Also drop an unreadable HDF5 file to verify graceful skipping
        broken = tmp_path / "measurements" / "broken.h5"
        broken.write_bytes(b"not an hdf5 file")

        scanner = ReferenceLibraryScanner()
        first = scanner.scan(roots=(tmp_path / "measurements",), inventory_store=store)
        assert len(first.curves) == 1
        assert len(first.skipped_files) == 1
        assert first.curves[0].coordinate_label == "R19C7"

        # Second scan uses cache and produces identical result
        second = scanner.scan(roots=(tmp_path / "measurements",), inventory_store=store)
        assert len(second.curves) == 1
        assert second.curves[0].curve_id == first.curves[0].curve_id
    finally:
        store.close()


def test_load_hdf5_extracts_fixed_conditions_from_safety_context_and_splits_revisits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hysteresis" / "data.h5"
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as handle:
        run = handle.create_group("run")
        run.attrs["status"] = "completed"
        run.attrs["created_at_utc"] = "2026-11-25T11:00:00Z"
        run.attrs["sample_coordinate_label"] = "R19C7"
        pts = handle.create_group("points")
        # Contiguous visit 1 (KB=54 mA, R=110 ohm), middle (KB=20 mA, R=120 ohm),
        # revisit 2 (KB=54 mA, R=140 ohm) + one disabled/field-compliance point.
        schedule = [
            (0.054, -0.075, -1e-3, -0.110, 1.0, 0.0, True),
            (0.054, -0.075, 1e-3, 0.110, 1.0, 0.0, False),
            (0.054, -0.075, 2e-3, 0.220, 0.0, 0.0, False),  # output_enabled=0 -> excluded
            (0.020, -0.075, -1e-3, -0.120, 1.0, 0.0, False),
            (0.020, -0.075, 1e-3, 0.120, 1.0, 0.0, False),
            (0.054, -0.075, -1e-3, -0.140, 1.0, 0.0, False),
            (0.054, -0.075, 1e-3, 0.140, 1.0, 0.0, False),
            (0.054, -0.075, 2e-3, 0.280, 1.0, 1.0, False),  # keithley.B compliance -> excluded
        ]
        for idx, (kb, moke, curr, volt, out_en, b_comp, write_moke_meas) in enumerate(schedule):
            grp = pts.create_group(str(idx))
            grp.attrs["complete"] = True
            grp.attrs["status"] = "ok"
            grp.create_dataset("setpoints_json", data=json.dumps({"keithley.A.current": curr}))
            meas = {
                "keithley.A.current_a": curr,
                "keithley.A.voltage_v": volt,
                "keithley.A.resistance_ohm": abs(volt / curr),
                "keithley.A.resistance_valid": 1.0,
                "keithley.A.output_enabled": out_en,
                "keithley.A.measurement_path_connected": 1.0,
                "keithley.A.compliance_detected": 0.0,
                "keithley.B.compliance_detected": b_comp,
            }
            if write_moke_meas:
                meas["moke_box.vout_voltage_v"] = moke
            grp.create_dataset("measurements_json", data=json.dumps(meas))
            grp.create_dataset(
                "metadata_json",
                data=json.dumps(
                    {
                        "safety_context": {
                            "moke_box": {"actual_v": moke, "requested_v": moke},
                            "keithley.A": {"mode": "current", "sense_mode": "4wire"},
                            "keithley.B": {"mode": "current", "level_si": kb},
                        }
                    }
                ),
            )
            grp.create_dataset("device_states_json", data="{}")

    curves = load_hdf5_reference_curves(path)
    assert len(curves) == 3
    assert curves[0].sense_mode == "4wire"
    assert curves[0].moke_vout_v == pytest.approx(-0.075)
    assert curves[0].keithley_b_setpoint_a == pytest.approx(0.054)
    assert curves[0].resistance_stats is not None
    assert curves[0].resistance_stats.mean_ohm == pytest.approx(110.0)
    assert curves[2].resistance_stats is not None
    assert curves[2].resistance_stats.mean_ohm == pytest.approx(140.0)
    assert "Visit 1" in curves[0].title and "Visit 2" in curves[2].title
    assert curves[0].excluded_point_count == 2
