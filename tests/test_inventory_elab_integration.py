"""Unit tests for sample metadata integration with HDF5 and eLabFTW."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.integrations.elab.service import _experiment_body
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.hdf5_reader import Hdf5RunReader, RunSummary


class _FakeElabClient:
    def __init__(self) -> None:
        self.created_experiments: list[dict[str, object]] = []
        self.added_tags: list[tuple[int, str]] = []
        self.uploaded_files: list[dict[str, object]] = []

    def create_experiment(self, *, template_id: int, title: str, body: str) -> tuple[int, str]:
        exp_id = len(self.created_experiments) + 1
        url = f"https://elab.example.org/experiments/{exp_id}"
        self.created_experiments.append({
            "template_id": template_id,
            "title": title,
            "body": body,
            "id": exp_id,
            "url": url,
        })
        return exp_id, url

    def add_tag(self, *, experiment_id: int, tag: str) -> None:
        self.added_tags.append((experiment_id, tag))

    def upload_file(self, *, experiment_id: int, path: Path, comment: str = "") -> str:
        self.uploaded_files.append({
            "experiment_id": experiment_id,
            "path": path,
            "comment": comment,
        })
        return f"https://elab.example.org/uploads/{len(self.uploaded_files)}"


class InventoryElabIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_hdf5_run_writer_and_reader_sample_attributes(self) -> None:
        h5_path = self.root / "sample_run.h5"
        writer = Hdf5RunWriter(
            h5_path,
            recipe_source="recipe: test",
            settings_source="settings: test",
            plan_hash="hash123",
            device_idn={"rigol": "Rigol123"},
            run_attributes={
                "sample_id": "XYZ",
                "sample_name": "CoFeB Sample XYZ",
                "sample_row": "23",
                "sample_col": "3",
                "sample_coordinate_label": "200 nm",
            },
        )
        writer.close("completed")

        summary = Hdf5RunReader.summary(h5_path)
        self.assertEqual(summary.sample_id, "XYZ")
        self.assertEqual(summary.sample_name, "CoFeB Sample XYZ")
        self.assertEqual(summary.sample_row, "23")
        self.assertEqual(summary.sample_col, "3")
        self.assertEqual(summary.sample_coordinate_label, "200 nm")

    def test_experiment_body_contains_sample_section(self) -> None:
        summary_with_sample = RunSummary(
            path=self.root / "test.h5",
            created_at_utc="2026-09-06T12:00:00Z",
            status="completed",
            point_count=10,
            spectrum_count=10,
            plan_sha256="abc",
            application_version="1.0",
            sample_id="XYZ",
            sample_name="CoFeB Sample XYZ",
            sample_row="23",
            sample_col="3",
            sample_coordinate_label="200 nm",
        )
        body = _experiment_body(summary_with_sample, self.root / "test.h5", "dummy_sha")
        self.assertIn("Sample &amp; Coordinate Inventory", body)
        self.assertIn("XYZ", body)
        self.assertIn("CoFeB Sample XYZ", body)
        self.assertIn("23", body)
        self.assertIn("3", body)
        self.assertIn("200 nm", body)

        # And without sample
        summary_plain = RunSummary(
            path=self.root / "plain.h5",
            created_at_utc="2026-09-06T12:00:00Z",
            status="completed",
            point_count=5,
            spectrum_count=5,
            plan_sha256="abc",
            application_version="1.0",
        )
        plain_body = _experiment_body(summary_plain, self.root / "plain.h5", "dummy_sha")
        self.assertNotIn("Sample &amp; Coordinate Inventory", plain_body)


if __name__ == "__main__":
    unittest.main()
