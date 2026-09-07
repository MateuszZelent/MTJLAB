"""Unit tests for sample metadata integration with HDF5 and eLabFTW."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.integrations.elab.config import ElabCredentials, ElabIntegrationProfile
from app.integrations.elab.service import _experiment_body, upload_result, ElabUploadRequest
from app.inventory.models import ActiveSampleTarget
from app.inventory.store import InventoryStore
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.hdf5_reader import Hdf5RunReader, RunSummary


class _FakeElabClient:
    created_experiments: list[dict[str, object]] = []
    added_tags: list[tuple[int, str]] = []
    uploaded_files: list[dict[str, object]] = []

    def __init__(self, credentials: object = None, timeout_s: float = 30.0) -> None:
        pass

    def create_experiment(self, *, template_id: int, title: str, body: str) -> tuple[int, str]:
        exp_id = len(_FakeElabClient.created_experiments) + 1
        url = f"https://elab.example.org/experiments/{exp_id}"
        _FakeElabClient.created_experiments.append({
            "template_id": template_id,
            "title": title,
            "body": body,
            "id": exp_id,
            "url": url,
        })
        return exp_id, url

    def add_tag(self, *, experiment_id: int, tag: str) -> None:
        _FakeElabClient.added_tags.append((experiment_id, tag))

    def upload_file(self, *, experiment_id: int, path: Path, comment: str = "") -> str:
        _FakeElabClient.uploaded_files.append({
            "experiment_id": experiment_id,
            "path": path,
            "comment": comment,
        })
        return f"https://elab.example.org/uploads/{len(_FakeElabClient.uploaded_files)}"


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

    def test_hdf5_run_writer_and_reader_extended_sample_attributes(self) -> None:
        h5_path = self.root / "extended_sample_run.h5"
        stack_desc = (
            "Wafer stack: Si / SiO2(500nm) / Ta(5) / CuN(25) / Ta(5) / Ru(5) / IrMn(6) "
            "/ CoFe(2.1) / Ru(0.85) / CoFeB(2.4) / MgO(wedge 0.8-1.4 nm) / CoFeB(1.6) / Ta(5) / Ru(7)."
        )
        writer = Hdf5RunWriter(
            h5_path,
            recipe_source="recipe: test",
            settings_source="settings: test",
            plan_hash="hash123",
            device_idn={"keithley": "Keithley2600"},
            run_attributes={
                "sample_id": "INL_MTJ_02.2026",
                "sample_name": "INL MTJ Wedge Sample (02.2026)",
                "sample_row": "20",
                "sample_col": "1",
                "sample_coordinate_label": "100 nm (P1)",
                "sample_row_label": "l20 (Rep 1)",
                "sample_col_label": "100 nm (P1)",
                "sample_description": stack_desc,
                "sample_tags": ["wedge", "MgO", "INL", "sub-micron"],
                "sample_cell_notes": "Expected ~4.3 kOhm; RA = 34.00 Ohm*um^2",
            },
        )
        writer.close("completed")

        summary = Hdf5RunReader.summary(h5_path)
        self.assertEqual(summary.sample_id, "INL_MTJ_02.2026")
        self.assertEqual(summary.sample_name, "INL MTJ Wedge Sample (02.2026)")
        self.assertEqual(summary.sample_row, "20")
        self.assertEqual(summary.sample_col, "1")
        self.assertEqual(summary.sample_coordinate_label, "100 nm (P1)")
        self.assertEqual(summary.sample_row_label, "l20 (Rep 1)")
        self.assertEqual(summary.sample_col_label, "100 nm (P1)")
        self.assertEqual(summary.sample_description, stack_desc)
        self.assertEqual(summary.sample_tags, ("wedge", "MgO", "INL", "sub-micron"))
        self.assertEqual(summary.sample_cell_notes, "Expected ~4.3 kOhm; RA = 34.00 Ohm*um^2")

    def test_experiment_body_contains_rich_stack_and_metadata(self) -> None:
        summary_rich = RunSummary(
            path=self.root / "rich.h5",
            created_at_utc="2026-09-06T12:00:00Z",
            status="completed",
            point_count=100,
            spectrum_count=0,
            plan_sha256="sha-rich",
            application_version="2.0",
            sample_id="INL_MTJ_02.2026",
            sample_name="INL MTJ Wedge Sample (02.2026)",
            sample_row="20",
            sample_col="1",
            sample_coordinate_label="100 nm (P1)",
            sample_row_label="l20 (Rep 1)",
            sample_col_label="100 nm (P1)",
            sample_description="Wafer stack: Si / SiO2(500nm) / Ta(5) / MgO(wedge 0.8-1.4 nm)",
            sample_tags=("wedge", "MgO", "INL"),
            sample_cell_notes="Expected ~4.3 kOhm; RA = 34.00 Ohm*um^2",
        )
        body = _experiment_body(summary_rich, self.root / "rich.h5", "sha-rich")
        self.assertIn("Sample &amp; Coordinate Inventory", body)
        self.assertIn("INL_MTJ_02.2026", body)
        self.assertIn("Row 20 (l20 (Rep 1))", body)
        self.assertIn("Col 1 (100 nm (P1))", body)
        self.assertIn("100 nm (P1)", body)
        self.assertIn("Expected ~4.3 kOhm; RA = 34.00 Ohm*um^2", body)
        self.assertIn("wedge", body)
        self.assertIn("MgO", body)
        self.assertIn("INL", body)
        self.assertIn("Sample Fabrication Stack &amp; Research Notes", body)
        self.assertIn("Wafer stack: Si / SiO2(500nm) / Ta(5) / MgO(wedge 0.8-1.4 nm)", body)

    def test_upload_result_attaches_sample_tags_and_title(self) -> None:
        h5_path = self.root / "sample_run.h5"
        writer = Hdf5RunWriter(
            h5_path,
            recipe_source="recipe: test",
            settings_source="settings: test",
            plan_hash="hash123",
            device_idn={"keithley": "Keithley2600"},
            run_attributes={
                "sample_id": "INL_MTJ_02.2026",
                "sample_name": "INL MTJ Wedge Sample (02.2026)",
                "sample_row": "20",
                "sample_col": "1",
                "sample_coordinate_label": "100 nm (P1)",
                "sample_row_label": "l20 (Rep 1)",
                "sample_col_label": "100 nm (P1)",
                "sample_description": "Stack details for INL MTJ",
                "sample_tags": ["wedge", "MgO", "INL"],
            },
        )
        writer.close("completed")

        request = ElabUploadRequest(
            path=h5_path,
            credentials=ElabCredentials.from_values("https://elab.example.org", "secret"),
            profile=ElabIntegrationProfile(
                template_id=10,
                title_pattern="[{sample_id}] {sample_coord} ({device_label}) - {run_name}",
                tags=("station-sweep",),
            ),
            ledger_path=self.root / "ledger.json",
        )

        with patch("app.integrations.elab.service.ElabApiClient", _FakeElabClient):
            result = upload_result(request)

        self.assertEqual(result.record.status, "uploaded")
        self.assertEqual(len(_FakeElabClient.created_experiments), 1)
        created_exp = _FakeElabClient.created_experiments[0]
        self.assertEqual(created_exp["title"], "[INL_MTJ_02.2026] R20C1 (100 nm (P1)) - sample_run")
        self.assertIn("Sample Fabrication Stack &amp; Research Notes", str(created_exp["body"]))

        added_tag_strings = [tag for _, tag in _FakeElabClient.added_tags]
        self.assertIn("station-sweep", added_tag_strings)
        self.assertIn("sample:INL_MTJ_02.2026", added_tag_strings)
        self.assertIn("device:100 nm (P1)", added_tag_strings)
        self.assertIn("coord:R20C1", added_tag_strings)
        self.assertIn("wedge", added_tag_strings)
        self.assertIn("MgO", added_tag_strings)
        self.assertIn("INL", added_tag_strings)

        self.assertEqual(len(_FakeElabClient.uploaded_files), 1)
        comment = str(_FakeElabClient.uploaded_files[0]["comment"])
        self.assertIn("Sample: INL_MTJ_02.2026", comment)
        self.assertIn("INL MTJ Wedge Sample (02.2026)", comment)
        self.assertIn("R20C1 (100 nm (P1))", comment)

    def test_active_target_store_roundtrip_extended_fields(self) -> None:
        store = InventoryStore(self.root / "inventory.db")
        target = ActiveSampleTarget(
            sample_id="INL_MTJ_02.2026",
            sample_name="INL MTJ Wedge Sample (02.2026)",
            row="20",
            col="1",
            device_label="100 nm (P1)",
            notes="Device specific notes",
            row_label="l20 (Rep 1)",
            col_label="100 nm (P1)",
            description="Fabrication stack Si/SiO2/...",
            tags=("wedge", "MgO", "INL"),
        )
        store.set_active_target(target)

        restored = store.get_active_target()
        self.assertEqual(restored.sample_id, target.sample_id)
        self.assertEqual(restored.sample_name, target.sample_name)
        self.assertEqual(restored.row, target.row)
        self.assertEqual(restored.col, target.col)
        self.assertEqual(restored.device_label, target.device_label)
        self.assertEqual(restored.notes, target.notes)
        self.assertEqual(restored.row_label, target.row_label)
        self.assertEqual(restored.col_label, target.col_label)
        self.assertEqual(restored.description, target.description)
        self.assertEqual(restored.tags, ("wedge", "MgO", "INL"))
        store.close()


if __name__ == "__main__":
    unittest.main()
