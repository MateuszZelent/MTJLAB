from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import json
import tempfile
import unittest

import h5py
import csv

from app.devices.anritsu import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.recipes import generate_sweep_points
from app.storage import Hdf5RunReader, Hdf5RunWriter


class Hdf5WriterTests(unittest.TestCase):
    def test_writer_flushes_a_point_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source=(
                    "schema_version: 1\n"
                    "dut_limits:\n"
                    "  anritsu:\n"
                    "    max_expected_input: '-10 dBm'\n"
                ),
                settings_source="schema_version: 1\n",
                plan_hash="abc",
                device_idn={"rigol": "RIGOL,DG1032Z"},
                device_capabilities={"rigol": {"features": ["basic_waveform"]}},
                operator_context={
                    "username": "LAB\\alice",
                    "provider": "operating_system",
                    "roles": ["operator"],
                },
            )
            point = MeasurementPoint(
                index=0,
                setpoints={"keithley.B.current": 0.001},
                measurements={"keithley.B.voltage_v": 0.01},
            )
            trace = SpectrumTrace(
                frequencies_hz=(1e6, 2e6, 3e6),
                powers_dbm=(-60.0, -50.0, -55.0),
                acquired_at_utc=datetime.now(timezone.utc),
                trace_name="TRAC1",
            )
            writer.append_event("run_started", {"timestamp_utc": "2026-01-01T00:00:00+00:00", "recipe": "test"})
            self.assertEqual(writer.append(point, trace), 0)
            writer.close("completed")
            with h5py.File(path, "r") as file:
                self.assertEqual(file["run"].attrs["status"], "completed")
                self.assertIn("application_version", file["run"].attrs)
                self.assertEqual(len(file["run"].attrs["settings_sha256"]), 64)
                self.assertIn("basic_waveform", file["run/capabilities_json"].asstr()[()])
                self.assertEqual(
                    json.loads(file["run/operator_context_json"].asstr()[()])["username"],
                    "LAB\\alice",
                )
                self.assertEqual(
                    json.loads(file["run/dut_limits_json"].asstr()[()])["anritsu"][
                        "max_expected_input"
                    ],
                    "-10 dBm",
                )
                labbook_metadata = dict(file["labbook/metadata"].asstr()[()])
                self.assertIn("-10 dBm", labbook_metadata["DUT limits"])
                self.assertEqual(file["events/name"].asstr()[0], "run_started")
                self.assertEqual(tuple(file["spectra/0/frequency_hz"][:]), trace.frequencies_hz)
                self.assertEqual(tuple(file["spectra/0/power_dbm"][:]), trace.powers_dbm)
                self.assertEqual(int(file.attrs["measurement running"]), 0)
                self.assertIn("scan_definition/row_00", file)
                definitions = {
                    name: dict(dataset.asstr()[()])
                    for name, dataset in file["scan_definition"].items()
                    if name.startswith("row_")
                }
                spectrum_row = next(
                    name
                    for name, definition in definitions.items()
                    if definition.get("control name") == "Spectrum (dBm)"
                )
                self.assertIn(f"measurement/{spectrum_row}/data", file)

    def test_writer_persists_simulation_and_full_device_state_per_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "simulation.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="schema_version: 1\n",
                settings_source="schema_version: 1\n",
                plan_hash="simulation",
                device_idn={"rigol": "SIM"},
                simulation_metadata={"enabled": True, "seed": 17, "model_version": "1"},
            )
            writer.append(
                MeasurementPoint(index=0, setpoints={}, measurements={}),
                device_states={
                    "rigol": {"channel_1": {"frequency_hz": 1_000.0, "output": True}},
                    "keithley": {"channel_B": {"current_a": 0.001}},
                    "anritsu": {"spectrum": {"rbw_hz": 1_000.0}},
                    "moke_box": {"hall": {"field_t": 0.02}},
                },
            )
            writer.close("completed")

            detail = Hdf5RunReader.detail(path)
            point = Hdf5RunReader.points(path)[0]
            self.assertEqual(detail.simulation_metadata["seed"], 17)
            self.assertTrue(point.device_states["rigol"]["channel_1"]["output"])
            self.assertIn("moke_box", point.device_states)

    def test_generated_spectrum_round_trips_through_qualified_pythat(self) -> None:
        from PyThat import MeasurementTree

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pythat.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="schema_version: 1\n",
                settings_source="schema_version: 1\n",
                plan_hash="abc",
                device_idn={"anritsu": "ANRITSU,MS2830A,SIM,1.0"},
            )
            writer.append(
                MeasurementPoint(index=0, setpoints={}, measurements={}),
                SpectrumTrace(
                    frequencies_hz=(1e6, 2e6, 3e6),
                    powers_dbm=(-60.0, -50.0, -55.0),
                    acquired_at_utc=datetime.now(timezone.utc),
                    trace_name="TRAC1",
                ),
            )
            writer.close("completed")
            with redirect_stdout(StringIO()):
                tree = MeasurementTree(path, index=True, override=True)
            self.assertEqual(tuple(tree.dataset.sizes), ("Checkpoint", "Frequency"))
            self.assertEqual(tree.dataset.sizes["Checkpoint"], 1)
            self.assertEqual(tree.dataset.sizes["Frequency"], 3)
            self.assertIn("Spectrum", tree.dataset.data_vars)
            self.assertEqual(tree.dataset["Frequency"].attrs["units"], "Hz")
            # PyThat 0.2.14 strips indicator units while normalising control
            # names; the source definition still carries "Spectrum (dBm)".
            spectrum_definition = next(
                definition
                for definition in tree.definition.values()
                if definition.get("control name") == "Spectrum"
            )
            self.assertEqual(spectrum_definition["units"], "dBm")

    def test_reference_raw_and_processed_spectra_are_all_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reference-processed.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="schema_version: 1\n",
                settings_source="schema_version: 1\n",
                plan_hash="reference-processed",
                device_idn={"anritsu": "ANRITSU,MS2830A,SIM,1.0"},
            )
            acquired = datetime.now(timezone.utc)
            reference = SpectrumTrace(
                frequencies_hz=(1e6, 2e6, 3e6),
                powers_dbm=(-70.0, -60.0, -65.0),
                acquired_at_utc=acquired,
                trace_name="TRAC1",
            )
            raw = SpectrumTrace(
                frequencies_hz=reference.frequencies_hz,
                powers_dbm=(-60.0, -55.0, -60.0),
                acquired_at_utc=acquired,
                trace_name="TRAC1",
            )
            writer.store_reference(reference, kind="single", average_count=1)
            writer.append(
                MeasurementPoint(index=0, setpoints={}, measurements={}),
                raw,
                processed_values=(10.0, 5.0, 5.0),
                processed_unit="dB",
                processing_operation="difference_db",
            )
            writer.close("completed")

            with h5py.File(path, "r") as file:
                self.assertEqual(tuple(file["reference/frequency_hz"][:]), reference.frequencies_hz)
                self.assertEqual(tuple(file["reference/power_dbm"][:]), reference.powers_dbm)
                self.assertEqual(tuple(file["spectra/0/power_dbm"][:]), raw.powers_dbm)
                self.assertEqual(
                    tuple(file["spectra/0/processed_values"][:]), (10.0, 5.0, 5.0)
                )
                self.assertEqual(file["spectra/0"].attrs["processed_unit"], "dB")
                self.assertEqual(
                    file["spectra/0"].attrs["processing_operation"], "difference_db"
                )
                definitions = {
                    name: dict(dataset.asstr()[()])
                    for name, dataset in file["scan_definition"].items()
                    if name.startswith("row_")
                }
                self.assertIn(
                    "Spectrum raw-reference (dB)",
                    {
                        definition.get("control name")
                        for definition in definitions.values()
                    },
                )
            stored = Hdf5RunReader.spectrum(path, 0)
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored.processed_values, (10.0, 5.0, 5.0))
            self.assertEqual(stored.processed_unit, "dB")
            self.assertEqual(stored.processing_operation, "difference_db")
            from PyThat import MeasurementTree

            with redirect_stdout(StringIO()):
                tree = MeasurementTree(path, index=True, override=True)
            self.assertIn("Spectrum", tree.dataset.data_vars)
            self.assertIn("Spectrum raw-reference", tree.dataset.data_vars)

    def test_multi_point_run_round_trips_setpoints_measurements_and_spectrum(self) -> None:
        from PyThat import MeasurementTree

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "multi-point.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="schema_version: 1\n",
                settings_source="schema_version: 1\n",
                plan_hash="abc",
                device_idn={"anritsu": "ANRITSU,MS2830A,SIM,1.0"},
                expected_points=2,
            )
            for index, current in enumerate((0.001, 0.002)):
                writer.append(
                    MeasurementPoint(
                        index=index,
                        setpoints={"keithley.B.current": current},
                        measurements={"keithley.B.voltage_v": current * 10},
                    ),
                    SpectrumTrace(
                        frequencies_hz=(1e6, 2e6, 3e6),
                        powers_dbm=(-60.0 + index, -50.0 + index, -55.0 + index),
                        acquired_at_utc=datetime.now(timezone.utc),
                        trace_name="TRAC1",
                    ),
                )
            writer.close("completed")

            with redirect_stdout(StringIO()):
                tree = MeasurementTree(path, index=True, override=True)
            self.assertEqual(tree.dataset.sizes["Checkpoint"], 2)
            self.assertEqual(tree.dataset.sizes["Frequency"], 3)
            self.assertEqual(
                tree.dataset["Setpoint B current"].values.tolist(), [0.001, 0.002]
            )
            self.assertEqual(
                tree.dataset["Measured B voltage"].values.tolist(), [0.01, 0.02]
            )
            setpoint_definition = next(
                definition
                for definition in tree.definition.values()
                if definition.get("control name") == "Setpoint B current"
            )
            measurement_definition = next(
                definition
                for definition in tree.definition.values()
                if definition.get("control name") == "Measured B voltage"
            )
            # PyThat 0.2.14 strips indicator units from xarray attrs; its
            # parsed source definitions retain the qualified SI units.
            self.assertEqual(setpoint_definition["units"], "A")
            self.assertEqual(measurement_definition["units"], "V")
            self.assertEqual(tree.dataset["Spectrum"].shape, (2, 3))

    def test_piecewise_axis_writes_119_coordinates_and_fixed_control_without_axis(self) -> None:
        recipe_source = """\
schema_version: 1
name: piecewise-storage
root:
  id: root
  type: sequence
  children:
    - id: fixed-a
      type: configure_keithley
      channel: A
      mode: current
      level: 0.5 mA
      compliance: 67 mV
    - id: current-sweep
      type: sweep
      target: keithley.B.current
      segments:
        - {start: 10 mA, stop: 100 mA, points: 100}
        - {start: 100 mA, stop: 150 mA, points: 20}
      children:
        - id: spectrum
          type: acquire_spectrum
"""
        points = generate_sweep_points(
            [
                {"start": "10 mA", "stop": "100 mA", "points": 100},
                {"start": "100 mA", "stop": "150 mA", "points": 20},
            ],
            "current",
        )
        self.assertEqual(len(points), 119)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "piecewise.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source=recipe_source,
                settings_source="schema_version: 1\n",
                plan_hash="piecewise",
                device_idn={"anritsu": "ANRITSU,MS2830A,SIM,1.0"},
                expected_points=len(points),
            )
            trace = SpectrumTrace(
                frequencies_hz=(1e6, 2e6),
                powers_dbm=(-60.0, -50.0),
                acquired_at_utc=datetime.now(timezone.utc),
                trace_name="TRAC1",
            )
            for index, point in enumerate(points):
                writer.append(
                    MeasurementPoint(
                        index=index,
                        setpoints={
                            "keithley.A.current": 0.0005,
                            "keithley.B.current": point.si_value,
                        },
                        measurements={},
                    ),
                    trace,
                )
            writer.close("completed")
            with h5py.File(path, "r") as file:
                definitions = {
                    name: dict(dataset.asstr()[()])
                    for name, dataset in file["scan_definition"].items()
                    if name.startswith("row_")
                }
                axis_row, axis = next(
                    (name, item)
                    for name, item in definitions.items()
                    if item.get("control name") == "Keithley B current (A)"
                )
                fixed_row, fixed = next(
                    (name, item)
                    for name, item in definitions.items()
                    if item.get("control name") == "Setpoint A current (A)"
                )
                self.assertEqual(tuple(file["measurement"][axis_row]["data"][:]), tuple(point.si_value for point in points))
                self.assertEqual(axis["equation"], "x")
                self.assertNotIn("Keithley A current (A)", [item.get("control name") for item in definitions.values()])
                self.assertEqual(fixed["lab control role"], "setpoint")
                self.assertEqual(len(file["measurement"][fixed_row]["data"]), 119)

    def test_reader_exposes_metadata_points_and_decimated_spectrum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "result.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="name: verified\n",
                settings_source="profile:\n  state: approved\n",
                plan_hash="plan-hash",
                device_idn={"rigol": "RIGOL,DG1032Z"},
                device_capabilities={"rigol": {"features": ["basic_waveform"]}},
            )
            writer.append(
                MeasurementPoint(
                    index=0,
                    setpoints={"rigol.1.high_level": 0.001},
                    measurements={"keithley.B.current_a": 0.001},
                    metadata={"plan_node": "spectrum"},
                ),
                SpectrumTrace(
                    frequencies_hz=tuple(float(item) for item in range(100)),
                    powers_dbm=tuple(-80.0 + item / 10 for item in range(100)),
                    acquired_at_utc=datetime.now(timezone.utc),
                    trace_name="TRAC1",
                ),
            )
            writer.close("completed")

            summary = Hdf5RunReader.summary(path)
            self.assertEqual(summary.status, "completed")
            self.assertEqual(summary.point_count, 1)
            detail = Hdf5RunReader.detail(path)
            self.assertEqual(detail.recipe_yaml, "name: verified\n")
            self.assertIn("basic_waveform", detail.capabilities["rigol"]["features"])
            self.assertEqual(detail.events, ())
            points = Hdf5RunReader.points(path)
            self.assertEqual(points[0].measurements["keithley.B.current_a"], 0.001)
            self.assertTrue(points[0].has_spectrum)
            spectrum = Hdf5RunReader.spectrum(path, 0, max_points=10)
            self.assertIsNotNone(spectrum)
            assert spectrum is not None
            self.assertEqual(spectrum.source_point_count, 100)
            self.assertLessEqual(len(spectrum.powers_dbm), 11)
            self.assertEqual(spectrum.frequencies_hz[-1], 99.0)

    def test_csv_summary_is_checkpointed_with_hdf5(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csv_path = root / "summary.csv"
            writer = Hdf5RunWriter(
                root / "run.h5",
                recipe_source="schema_version: 1\n",
                settings_source="schema_version: 1\n",
                plan_hash="abc",
                device_idn={},
                csv_summary_path=csv_path,
            )
            writer.append(MeasurementPoint(index=0, setpoints={"source": 0.001}, measurements={"current": 0.001}))
            writer.close("completed")
            with csv_path.open("r", encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["point_index"], "0")
            self.assertEqual(rows[0]["measurements_json"], '{"current": 0.001}')

    def test_writer_refuses_to_overwrite_an_existing_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.h5"
            first = Hdf5RunWriter(
                path,
                recipe_source="schema_version: 1\n",
                settings_source="schema_version: 1\n",
                plan_hash="abc",
                device_idn={},
            )
            first.close("completed")
            with self.assertRaisesRegex(Exception, "already exists"):
                Hdf5RunWriter(
                    path,
                    recipe_source="schema_version: 1\n",
                    settings_source="schema_version: 1\n",
                    plan_hash="def",
                    device_idn={},
                )

    def test_writer_rejects_non_finite_spectrum_without_partial_point(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="schema_version: 1\n",
                settings_source="schema_version: 1\n",
                plan_hash="abc",
                device_idn={},
            )
            trace = SpectrumTrace(
                frequencies_hz=(1.0, 2.0),
                powers_dbm=(-50.0, float("nan")),
                acquired_at_utc=datetime.now(timezone.utc),
                trace_name="TRAC1",
            )
            with self.assertRaisesRegex(Exception, "NaN"):
                writer.append(MeasurementPoint(index=0, setpoints={}, measurements={}), trace)
            writer.close("faulted")
            with h5py.File(path, "r") as file:
                self.assertEqual(len(file["points"]), 0)
                self.assertEqual(len(file["_pending"]), 0)

    def test_writer_rejects_non_finite_scalar_without_partial_point(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="schema_version: 1\n",
                settings_source="schema_version: 1\n",
                plan_hash="abc",
                device_idn={},
            )
            with self.assertRaisesRegex(Exception, "invalid measurement"):
                writer.append(
                    MeasurementPoint(
                        index=0,
                        setpoints={},
                        measurements={"keithley.B.current_a": float("inf")},
                    )
                )
            writer.close("faulted")
            with h5py.File(path, "r") as file:
                self.assertEqual(len(file["points"]), 0)
                self.assertEqual(len(file["_pending"]), 0)

    def test_checkpoint_failure_rolls_back_point_and_leaves_durable_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="schema_version: 1\n",
                settings_source="schema_version: 1\n",
                plan_hash="abc",
                device_idn={},
            )
            original = writer._thatec.append
            writer._thatec.append = lambda *_args: (_ for _ in ()).throw(OSError("injected failure"))
            with self.assertRaisesRegex(Exception, "injected failure"):
                writer.append(MeasurementPoint(index=0, setpoints={"x": 1.0}, measurements={}))
            writer._thatec.append = original
            writer.close("faulted")

            detail = Hdf5RunReader.detail(path)
            self.assertEqual(Hdf5RunReader.points(path), ())
            self.assertTrue(any(event.name == "checkpoint_write_failed" for event in detail.events))

    def test_close_marks_contract_corruption_faulted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid-contract.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="schema_version: 1\n",
                settings_source="schema_version: 1\n",
                plan_hash="abc",
                device_idn={},
            )
            del writer._file.attrs["thaTEC:OS version"]

            with self.assertRaisesRegex(Exception, "contract validation failed"):
                writer.close("completed")

            with h5py.File(path, "r") as file:
                self.assertEqual(file["run"].attrs["status"], "faulted")
                self.assertIn("thaTEC:OS version", file["run"].attrs["storage_validation_error"])
                self.assertEqual(int(file.attrs["measurement running"]), 0)


if __name__ == "__main__":
    unittest.main()
