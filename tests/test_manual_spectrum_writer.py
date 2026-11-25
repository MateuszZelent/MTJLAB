from __future__ import annotations

import re
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.manual_metadata import ManualMetadataValue
from app.domain.quantities import DIMENSION_CURRENT
from app.storage import (
    Hdf5RunReader,
    ManualSpectrumArchive,
    ManualSpectrumSaveMode,
    ThatecCompatibilityValidator,
)


class ManualSpectrumWriterTests(unittest.TestCase):
    @staticmethod
    def _trace(
        *,
        acquired_at: datetime,
        frequencies: tuple[float, ...] = (1e6, 2e6, 3e6),
        offset: float = 0.0,
    ) -> SpectrumTrace:
        return SpectrumTrace(
            frequencies_hz=frequencies,
            powers_dbm=(-60.0 + offset, -50.0 + offset, -55.0 + offset),
            acquired_at_utc=acquired_at,
            trace_name="TRAC1",
        )

    @staticmethod
    def _metadata(value: float = 0.001) -> ManualMetadataValue:
        return ManualMetadataValue(
            key="keithley.B.current_a",
            device="Keithley 2600",
            label="Keithley B · current",
            dimension=DIMENSION_CURRENT,
            unit="A",
            value_si=value,
        )

    def test_append_close_and_resume_preserves_checkpoints_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manual.h5"
            acquired = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
            first = ManualSpectrumArchive(
                settings_source="schema_version: 1\nsettings: test\n",
                device_idn={"anritsu": "ANRITSU,MS2830A,SIM,1.0"},
            )
            first.save(
                self._trace(acquired_at=acquired),
                destination=path,
                mode=ManualSpectrumSaveMode.APPEND,
                metadata_values=(self._metadata(),),
                metadata_scope="selected",
            )
            first.close()

            resumed = ManualSpectrumArchive(
                settings_source="this is ignored for a verified existing archive",
            )
            result = resumed.save(
                self._trace(acquired_at=acquired + timedelta(seconds=1), offset=2.0),
                destination=path,
                mode="append",
                metadata_values=(self._metadata(0.002),),
                metadata_scope="selected",
            )
            self.assertEqual(result.point_index, 1)
            self.assertEqual(result.point_count, 2)
            resumed.close()

            summary = Hdf5RunReader.summary(path)
            self.assertEqual(summary.status, "aborted")
            self.assertEqual(summary.point_count, 2)
            self.assertEqual(summary.spectrum_count, 2)
            points = Hdf5RunReader.points(path)
            self.assertEqual(len(points), 2)
            self.assertEqual(points[0].measurements["keithley.B.current_a"], 0.001)
            self.assertEqual(points[1].measurements["keithley.B.current_a"], 0.002)
            self.assertEqual(points[0].metadata["metadata_scope"], "selected")

            report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
            self.assertTrue(report.valid, report.errors)
            self.assertIn("Spectrum", report.data_variables)
            self.assertEqual(dict(report.dimensions)["Frequency"], 3)

    def test_append_rejects_a_changed_frequency_grid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manual.h5"
            archive = ManualSpectrumArchive()
            acquired = datetime.now(UTC)
            try:
                archive.save(
                    self._trace(acquired_at=acquired),
                    destination=path,
                    mode=ManualSpectrumSaveMode.APPEND,
                )
                with self.assertRaises(ExecutionError):
                    archive.save(
                        self._trace(
                            acquired_at=acquired + timedelta(seconds=1),
                            frequencies=(1e6, 2.1e6, 3e6),
                        ),
                        destination=path,
                        mode=ManualSpectrumSaveMode.APPEND,
                    )
            finally:
                archive.close()

    def test_append_reuses_one_writer_for_path_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "manual.h5"
            archive = ManualSpectrumArchive()
            acquired = datetime.now(UTC)
            try:
                archive.save(
                    self._trace(acquired_at=acquired),
                    destination=path,
                    mode=ManualSpectrumSaveMode.APPEND,
                )
                result = archive.save(
                    self._trace(acquired_at=acquired + timedelta(seconds=1), offset=1.0),
                    destination=path.absolute(),
                    mode=ManualSpectrumSaveMode.APPEND,
                )
                self.assertEqual(result.point_index, 1)
                self.assertEqual(archive.point_count, 2)
            finally:
                archive.close()

    def test_timestamped_mode_creates_a_completed_collision_safe_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "capture.h5"
            archive = ManualSpectrumArchive()
            result = archive.save(
                self._trace(acquired_at=datetime.now(UTC)),
                destination=base,
                mode=ManualSpectrumSaveMode.TIMESTAMPED,
            )
            self.assertTrue(result.path.is_file())
            self.assertRegex(
                result.path.name,
                re.compile(r"^capture_\d{8}T\d{6}\.\d{6}Z\.h5$"),
            )
            self.assertIsNone(archive.active_path)
            self.assertEqual(Hdf5RunReader.summary(result.path).status, "completed")
            report = ThatecCompatibilityValidator().validate(
                result.path, require_pythat=True
            )
            self.assertTrue(report.valid, report.errors)

    def test_timestamped_failure_marks_faulted_cleans_writer_and_allows_next_save(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = ManualSpectrumArchive()
            acquired = datetime.now(UTC)
            bad_base = Path(temporary) / "failed.h5"
            with self.assertRaises(ExecutionError):
                archive.save(
                    self._trace(acquired_at=acquired),
                    destination=bad_base,
                    mode=ManualSpectrumSaveMode.TIMESTAMPED,
                    processed_values=(1.0, 2.0),
                    processed_unit="dB",
                    processing_operation="difference_db",
                )
            self.assertIsNone(archive.active_path)
            self.assertIsNone(archive._writer)
            created = sorted(Path(temporary).glob("failed_*.h5"))
            self.assertEqual(len(created), 1)
            self.assertEqual(Hdf5RunReader.summary(created[0]).status, "faulted")

            good_result = archive.save(
                self._trace(acquired_at=acquired + timedelta(seconds=1)),
                destination=Path(temporary) / "good.h5",
                mode=ManualSpectrumSaveMode.TIMESTAMPED,
            )
            self.assertEqual(Hdf5RunReader.summary(good_result.path).status, "completed")
            self.assertIsNone(archive.active_path)

    def test_switch_to_mismatched_target_preserves_open_append_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first_path = Path(temporary) / "first.h5"
            second_path = Path(temporary) / "second.h5"
            acquired = datetime.now(UTC)

            other = ManualSpectrumArchive()
            other.save(
                self._trace(acquired_at=acquired, frequencies=(4e6, 5e6, 6e6)),
                destination=second_path,
                mode=ManualSpectrumSaveMode.APPEND,
            )
            other.close()

            archive = ManualSpectrumArchive()
            try:
                archive.save(
                    self._trace(acquired_at=acquired, frequencies=(1e6, 2e6, 3e6)),
                    destination=first_path,
                    mode=ManualSpectrumSaveMode.APPEND,
                )
                open_writer = archive._writer
                with self.assertRaises(ExecutionError):
                    archive.save(
                        self._trace(
                            acquired_at=acquired + timedelta(seconds=1),
                            frequencies=(1e6, 2e6, 3e6),
                        ),
                        destination=second_path,
                        mode=ManualSpectrumSaveMode.APPEND,
                    )
                self.assertIs(archive._writer, open_writer)
                self.assertEqual(archive.active_path, first_path.resolve())
                follow_up = archive.save(
                    self._trace(
                        acquired_at=acquired + timedelta(seconds=2),
                        frequencies=(1e6, 2e6, 3e6),
                        offset=1.5,
                    ),
                    destination=first_path,
                    mode=ManualSpectrumSaveMode.APPEND,
                )
                self.assertEqual(follow_up.point_count, 2)
            finally:
                archive.close()

    def test_dotted_non_hdf5_names_append_h5_without_silent_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            acquired = datetime.now(UTC)
            archive = ManualSpectrumArchive()
            try:
                first = archive.save(
                    self._trace(acquired_at=acquired),
                    destination=Path(temporary) / "run.01.data",
                    mode=ManualSpectrumSaveMode.APPEND,
                )
                second = archive.save(
                    self._trace(acquired_at=acquired + timedelta(seconds=1)),
                    destination=Path(temporary) / "run.01.bak",
                    mode=ManualSpectrumSaveMode.APPEND,
                )
                self.assertEqual(first.path.name, "run.01.data.h5")
                self.assertEqual(second.path.name, "run.01.bak.h5")
                self.assertNotEqual(first.path, second.path)
            finally:
                archive.close()

            ts = archive.save(
                self._trace(acquired_at=acquired + timedelta(seconds=2)),
                destination=Path(temporary) / "sample.2.5GHz",
                mode=ManualSpectrumSaveMode.TIMESTAMPED,
            )
            self.assertTrue(ts.path.name.startswith("sample.2.5GHz_"))
            self.assertTrue(ts.path.name.endswith(".h5"))


if __name__ == "__main__":
    unittest.main()
