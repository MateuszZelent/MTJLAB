from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

import h5py
import csv

from app.devices.anritsu import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.storage import Hdf5RunReader, Hdf5RunWriter


class Hdf5WriterTests(unittest.TestCase):
    def test_writer_flushes_a_point_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="schema_version: 1\n",
                settings_source="schema_version: 1\n",
                plan_hash="abc",
                device_idn={"rigol": "RIGOL,DG1032Z"},
                device_capabilities={"rigol": {"features": ["basic_waveform"]}},
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
                self.assertEqual(file["events/name"].asstr()[0], "run_started")
                self.assertEqual(tuple(file["spectra/0/frequency_hz"][:]), trace.frequencies_hz)
                self.assertEqual(tuple(file["spectra/0/power_dbm"][:]), trace.powers_dbm)

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
            with self.assertRaisesRegex(Exception, "już istnieje"):
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


if __name__ == "__main__":
    unittest.main()
