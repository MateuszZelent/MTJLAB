from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

import h5py

from app.devices.anritsu import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.storage import Hdf5RunWriter, ThatecCompatibilityValidator
from tests.helpers import ROOT


class ThatecCompatibilityValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = ThatecCompatibilityValidator()

    @staticmethod
    def _write_run(path: Path, status: str) -> None:
        writer = Hdf5RunWriter(
            path,
            recipe_source="schema_version: 1\nname: validator-fixture\n",
            settings_source="schema_version: 1\n",
            plan_hash="validator-fixture",
            device_idn={"anritsu": "ANRITSU,MS2830A,SIM,1.0"},
            expected_points=1,
        )
        writer.append(
            MeasurementPoint(
                index=0,
                setpoints={"keithley.B.current": 0.001},
                measurements={"keithley.B.voltage_v": 0.01},
            ),
            SpectrumTrace(
                frequencies_hz=(1e6, 2e6, 3e6),
                powers_dbm=(-60.0, -50.0, -55.0),
                acquired_at_utc=datetime.now(timezone.utc),
                trace_name="TRAC1",
            ),
        )
        writer.close(status)

    def test_packaged_manifest_matches_supplied_golden_file(self) -> None:
        golden = ROOT / self.validator.manifest["golden_reference"]["filename"]
        if not golden.is_file():
            self.skipTest("The laboratory golden HDF5 file is not present in this checkout.")
        report = self.validator.verify_golden_reference(golden)
        self.assertTrue(report.valid, report.errors)
        self.assertEqual(report.pythat_version, "0.2.14")
        self.assertTrue(report.dimensions)
        self.assertTrue(report.data_variables)

    def test_completed_aborted_and_faulted_runs_pass_manifest_and_pythat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for status in ("completed", "aborted", "faulted"):
                with self.subTest(status=status):
                    path = root / f"{status}.h5"
                    self._write_run(path, status)
                    report = self.validator.validate(path, require_pythat=True)
                    self.assertTrue(report.valid, report.errors)
                    self.assertIn("Spectrum", report.data_variables)
                    self.assertEqual(dict(report.dimensions)["Frequency"], 3)

    def test_cross_schema_corruption_is_reported_with_exact_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "corrupt.h5"
            self._write_run(path, "completed")
            with h5py.File(path, "r+") as h5:
                spectrum_row = next(
                    name
                    for name, definition in h5["scan_definition"].items()
                    if name.startswith("row_")
                    and dict(definition.asstr()[()]).get("control name") == "Spectrum (dBm)"
                )
                del h5[f"measurement/{spectrum_row}/data"].attrs["dim of data"]

            report = self.validator.validate(path)
            self.assertFalse(report.valid)
            self.assertTrue(
                any(
                    issue.path.endswith(f"measurement/{spectrum_row}/data")
                    and "dim of data" in issue.message
                    for issue in report.errors
                )
            )

    def test_validator_rejects_tree_view_definition_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tree-mismatch.h5"
            self._write_run(path, "completed")
            with h5py.File(path, "r+") as h5:
                tree = h5["scan_definition/tree_view"]
                tree.resize((max(0, tree.shape[0] - 1), 3))

            report = self.validator.validate(path)
            self.assertFalse(report.valid)
            self.assertTrue(
                any(issue.path == "/scan_definition/tree_view" for issue in report.errors)
            )


if __name__ == "__main__":
    unittest.main()
