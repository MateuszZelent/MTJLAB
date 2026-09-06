"""Unit tests for sample inventory models and SQLite store."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.inventory import (
    ActiveSampleTarget,
    InventoryStore,
    Sample,
    SampleRunRecord,
)


class InventoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.db_path = self.root / "test_inventory.db"
        self.store = InventoryStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._temp_dir.cleanup()

    def test_sample_crud(self) -> None:
        sample = Sample(
            sample_id="SAMPLE-XYZ",
            name="CoFeB Wedge A",
            description="Test MTJ stack with wedge",
            rows=("1", "2", "3"),
            row_labels={"2": "Middle region"},
            cols=("1", "2", "3"),
            col_labels={"1": "50 nm", "2": "100 nm", "3": "200 nm"},
            device_labels={"2,3": "200 nm Pillar"},
            device_states={"2,3": "untested"},
        )
        saved = self.store.save_sample(sample)
        self.assertEqual(saved.sample_id, "SAMPLE-XYZ")
        self.assertEqual(saved.name, "CoFeB Wedge A")

        retrieved = self.store.get_sample("SAMPLE-XYZ")
        self.assertIsNotNone(retrieved)
        assert retrieved is not None
        self.assertEqual(retrieved.name, "CoFeB Wedge A")
        self.assertEqual(retrieved.rows, ("1", "2", "3"))
        self.assertEqual(retrieved.cols, ("1", "2", "3"))
        self.assertEqual(retrieved.cell_label("2", "3"), "200 nm Pillar")
        self.assertEqual(retrieved.cell_label("1", "1"), "50 nm")
        self.assertEqual(retrieved.cell_state("2", "3"), "untested")

        # Update cell
        updated = retrieved.with_cell_update("2", "3", state="good", notes="R = 1.2 kOhm")
        self.store.save_sample(updated)

        retrieved_updated = self.store.get_sample("SAMPLE-XYZ")
        assert retrieved_updated is not None
        self.assertEqual(retrieved_updated.cell_state("2", "3"), "good")
        self.assertEqual(retrieved_updated.cell_notes("2", "3"), "R = 1.2 kOhm")

        # List samples
        all_samples = self.store.list_samples()
        self.assertEqual(len(all_samples), 1)
        self.assertEqual(all_samples[0].sample_id, "SAMPLE-XYZ")

        # Delete sample
        deleted = self.store.delete_sample("SAMPLE-XYZ")
        self.assertTrue(deleted)
        self.assertIsNone(self.store.get_sample("SAMPLE-XYZ"))
        self.assertEqual(len(self.store.list_samples()), 0)

    def test_attachments(self) -> None:
        sample = Sample(sample_id="SAMPLE-ATT", name="Attachment Test Sample")
        self.store.save_sample(sample)

        # Create dummy image and pdf
        dummy_img = self.root / "microscope.png"
        dummy_img.write_bytes(b"\x89PNG\r\n\x1a\n\x00fake image data")

        dummy_pdf = self.root / "datasheet.pdf"
        dummy_pdf.write_bytes(b"%PDF-1.4\nfake pdf data")

        att_img = self.store.add_attachment("SAMPLE-ATT", dummy_img, caption="Chip photo")
        self.assertTrue(att_img.is_image)
        self.assertEqual(att_img.filename, "microscope.png")
        self.assertEqual(att_img.caption, "Chip photo")
        img_path = self.store.get_attachment_path(att_img)
        self.assertTrue(img_path.is_file())

        att_pdf = self.store.add_attachment("SAMPLE-ATT", dummy_pdf, caption="Process PDF")
        self.assertTrue(att_pdf.is_pdf)
        pdf_path = self.store.get_attachment_path(att_pdf)
        self.assertTrue(pdf_path.is_file())

        sample_with_att = self.store.get_sample("SAMPLE-ATT")
        assert sample_with_att is not None
        self.assertEqual(len(sample_with_att.attachments), 2)

        # Delete attachment
        deleted = self.store.delete_attachment(att_img.id)
        self.assertTrue(deleted)
        self.assertFalse(img_path.is_file())

        sample_after = self.store.get_sample("SAMPLE-ATT")
        assert sample_after is not None
        self.assertEqual(len(sample_after.attachments), 1)
        self.assertEqual(sample_after.attachments[0].id, att_pdf.id)

    def test_runs_and_active_target(self) -> None:
        sample = Sample(
            sample_id="SAMPLE-RUNS",
            name="Runs Test",
            rows=("1", "2"),
            cols=("1", "2"),
            device_states={"1,2": "untested"},
        )
        self.store.save_sample(sample)

        # Active target
        target = ActiveSampleTarget(
            sample_id="SAMPLE-RUNS",
            sample_name="Runs Test",
            row="1",
            col="2",
            device_label="200 nm",
            notes="Center device",
        )
        self.store.set_active_target(target)
        current = self.store.get_active_target()
        self.assertTrue(current.is_active)
        self.assertEqual(current.sample_id, "SAMPLE-RUNS")
        self.assertEqual(current.row, "1")
        self.assertEqual(current.col, "2")
        self.assertIn("R1:C2 [200 nm]", current.display_text())

        # Record run
        run_record = SampleRunRecord(
            sample_id="SAMPLE-RUNS",
            sample_name="Runs Test",
            row="1",
            col="2",
            device_label="200 nm",
            run_path=str(self.root / "run_001.h5"),
            run_sha256="abc123456",
            created_at_utc="2026-09-06T12:00:00Z",
            status="completed",
            point_count=50,
            spectrum_count=50,
            recipe_name="IV_Sweep",
        )
        saved_run = self.store.record_run(run_record)
        self.assertIsNotNone(saved_run.id)

        # Check that cell state transitioned to 'measured'
        sample_after_run = self.store.get_sample("SAMPLE-RUNS")
        assert sample_after_run is not None
        self.assertEqual(sample_after_run.cell_state("1", "2"), "measured")

        # List runs for sample and cell
        runs = self.store.list_runs_for_sample("SAMPLE-RUNS")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].recipe_name, "IV_Sweep")

        cell_runs = self.store.list_runs_for_cell("SAMPLE-RUNS", "1", "2")
        self.assertEqual(len(cell_runs), 1)

        # Update eLab status
        self.store.update_run_elab_status(
            str(self.root / "run_001.h5"),
            elab_experiment_id=101,
            elab_url="https://elab.example.org/experiments/101",
            elab_status="uploaded",
        )
        updated_runs = self.store.list_runs_for_sample("SAMPLE-RUNS")
        self.assertEqual(updated_runs[0].elab_experiment_id, 101)
        self.assertEqual(updated_runs[0].elab_status, "uploaded")

        # Clear active target
        self.store.clear_active_target()
        self.assertFalse(self.store.get_active_target().is_active)

    def test_sample_structure_mutations(self) -> None:
        sample = Sample(
            sample_id="SAMPLE-MUT",
            name="Mutation Test",
            rows=("1", "2"),
            row_labels={"1": "Top Row", "2": "Bottom Row"},
            cols=("1", "2"),
            col_labels={"1": "100 nm", "2": "200 nm"},
            device_labels={"1,1": "Pillar 1", "2,2": "Pillar 4"},
            device_states={"1,1": "good", "2,2": "shorted"},
            device_notes={"1,1": "Low noise", "2,2": "Failed breakdown"},
        )

        # 1. Row/Col label update
        s1 = sample.with_row_label("1", "Row Alpha").with_col_label("2", "250 nm")
        self.assertEqual(s1.row_labels["1"], "Row Alpha")
        self.assertEqual(s1.col_labels["2"], "250 nm")

        # 2. Rename row with cell migration
        s2 = sample.with_renamed_row("2", "20", new_label="Row 20")
        self.assertEqual(s2.rows, ("1", "20"))
        self.assertEqual(s2.row_labels["20"], "Row 20")
        self.assertNotIn("2", s2.row_labels)
        self.assertEqual(s2.cell_state("20", "2"), "shorted")
        self.assertEqual(s2.cell_notes("20", "2"), "Failed breakdown")
        self.assertEqual(s2.cell_label("20", "2"), "Pillar 4")

        # 3. Rename col with cell migration
        s3 = sample.with_renamed_col("1", "A", new_label="Col A")
        self.assertEqual(s3.cols, ("A", "2"))
        self.assertEqual(s3.col_labels["A"], "Col A")
        self.assertEqual(s3.cell_state("1", "A"), "good")
        self.assertEqual(s3.cell_notes("1", "A"), "Low noise")

        # 4. Add row and col
        s4 = sample.with_added_row("3", label="Third Row", after_row="2")
        self.assertEqual(s4.rows, ("1", "2", "3"))
        self.assertEqual(s4.row_labels["3"], "Third Row")

        s5 = sample.with_added_col("3", label="500 nm", after_col="2")
        self.assertEqual(s5.cols, ("1", "2", "3"))
        self.assertEqual(s5.col_labels["3"], "500 nm")

        # 5. Delete row and col
        s6 = sample.with_deleted_row("2")
        self.assertEqual(s6.rows, ("1",))
        self.assertNotIn("2,2", s6.device_states)

        s7 = sample.with_deleted_col("1")
        self.assertEqual(s7.cols, ("2",))
        self.assertNotIn("1,1", s7.device_states)

        # 6. with_structure bulk update
        s8 = sample.with_structure(
            rows=("1", "99"),
            row_labels={"1": "Top", "99": "New Row"},
            cols=("1", "88"),
            col_labels={"1": "100 nm", "88": "New Col"},
        )
        self.assertEqual(s8.rows, ("1", "99"))
        self.assertEqual(s8.cols, ("1", "88"))
        self.assertEqual(s8.cell_state("1", "1"), "good")
        self.assertNotIn("2,2", s8.device_states)

        # 7. Batch state mutations (completed, burned)
        s9 = sample.with_cells_state([("1", "1"), ("1", "2")], state="completed")
        self.assertEqual(s9.cell_state("1", "1"), "completed")
        self.assertEqual(s9.cell_state("1", "2"), "completed")
        self.assertEqual(s9.cell_state("2", "2"), "shorted")

        s10 = sample.with_row_state("2", state="burned")
        self.assertEqual(s10.cell_state("2", "1"), "burned")
        self.assertEqual(s10.cell_state("2", "2"), "burned")
        self.assertEqual(s10.cell_state("1", "1"), "good")

        s11 = sample.with_col_state("1", state="completed")
        self.assertEqual(s11.cell_state("1", "1"), "completed")
        self.assertEqual(s11.cell_state("2", "1"), "completed")

    def test_row_renumbering_and_remapping(self) -> None:
        # Sample with rows 1..10 and measurement data
        sample = Sample(
            sample_id="SAMPLE-RENUMBER",
            name="Wedge 1..10",
            rows=tuple(str(i) for i in range(1, 11)),
            row_labels={str(i): f"l{i}" for i in range(1, 11)},
            cols=("1", "2", "3"),
            col_labels={"1": "100 nm", "2": "200 nm", "3": "300 nm"},
            device_states={"1,1": "completed", "4,2": "good", "10,3": "burned"},
            device_notes={"4,2": "R = 1.2 kOhm"},
        )
        self.store.save_sample(sample)

        # Record a run on row 4, col 2
        run = SampleRunRecord(
            sample_id="SAMPLE-RENUMBER",
            row="4",
            col="2",
            device_label="200 nm",
            run_path="measurements/run_test.h5",
            run_sha256="abc12345",
            created_at_utc="2026-09-06T12:00:00Z",
            status="completed",
            point_count=50,
            spectrum_count=0,
            recipe_name="IV_Sweep",
        )
        self.store.record_run(run)

        # Renumber rows 1..10 to 20..30 (11 rows)
        renumbered = sample.with_row_renumbering(start_row=20, count=11, row_prefix="l")
        self.assertEqual(len(renumbered.rows), 11)
        self.assertEqual(renumbered.rows[0], "20")
        self.assertEqual(renumbered.rows[-1], "30")
        self.assertEqual(renumbered.rows, tuple(str(i) for i in range(20, 31)))

        # Verify device states and notes were mapped from old row 1->20, 4->23, 10->29
        self.assertEqual(renumbered.cell_state("20", "1"), "completed")
        self.assertEqual(renumbered.cell_state("23", "2"), "good")
        self.assertEqual(renumbered.cell_notes("23", "2"), "R = 1.2 kOhm")
        self.assertEqual(renumbered.cell_state("29", "3"), "burned")
        self.assertNotIn("1,1", renumbered.device_states)
        self.assertNotIn("4,2", renumbered.device_states)

        # Test store.remap_sample_rows also updates SQLite sample_runs
        row_mapping = {str(i): str(20 + i - 1) for i in range(1, 11)}
        self.store.remap_sample_rows("SAMPLE-RENUMBER", row_mapping)
        stored_runs = self.store.list_runs_for_sample("SAMPLE-RENUMBER")
        self.assertEqual(len(stored_runs), 1)
        self.assertEqual(stored_runs[0].row, "23")  # old row 4 -> new row 23!


if __name__ == "__main__":
    unittest.main()
