"""Unit and rendering tests for sample inventory UI components."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.inventory import ActiveSampleTarget, InventoryStore, Sample, SampleRunRecord
from app.ui.inventory import (
    SampleInventoryPage,
    SampleMatrixWidget,
    SampleProgrammingDialog,
)


class SampleInventoryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.db_path = self.root / "inventory.db"
        self.store = InventoryStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._temp_dir.cleanup()

    def test_sample_matrix_widget_renders_cells_and_signals(self) -> None:
        widget = SampleMatrixWidget()
        sample = Sample(
            sample_id="SAMPLE-TEST",
            name="Test Matrix",
            rows=("1", "2"),
            cols=("1", "2", "3"),
            col_labels={"3": "200 nm"},
            device_states={"1,1": "completed", "1,2": "burned", "1,3": "good"},
        )
        active = ActiveSampleTarget(
            sample_id="SAMPLE-TEST",
            sample_name="Test Matrix",
            row="1",
            col="3",
            device_label="200 nm",
        )
        runs = (
            SampleRunRecord(
                sample_id="SAMPLE-TEST",
                row="1",
                col="3",
                device_label="200 nm",
                run_path=str(self.root / "run.h5"),
                run_sha256="abc",
                created_at_utc="2026-09-06T12:00:00Z",
                status="completed",
                point_count=10,
                spectrum_count=10,
                recipe_name="Sweep1",
            ),
        )

        widget.set_sample(sample, run_records=runs, active_target=active)
        self.assertEqual(widget.table.rowCount(), 2)
        self.assertEqual(widget.table.columnCount(), 3)

        # Assert completed and burned cells
        item_completed = widget.table.item(0, 0)
        assert item_completed is not None
        self.assertIn("COMPLETED", item_completed.text())

        item_burned = widget.table.item(0, 1)
        assert item_burned is not None
        self.assertIn("BURNED", item_burned.text())

        # Assert active cell contains indicator
        active_item = widget.table.item(0, 2)
        self.assertIsNotNone(active_item)
        assert active_item is not None
        self.assertIn("ACTIVE", active_item.text())
        self.assertIn("200 nm", active_item.text())
        self.assertIn("runs", active_item.text())

        # Assert selection signal
        clicked_coords: list[tuple[str, str]] = []
        widget.cell_selected.connect(lambda r, c: clicked_coords.append((r, c)))
        widget.table.cellClicked.emit(1, 0)
        self.assertEqual(clicked_coords, [("2", "1")])

    def test_sample_programming_dialog_generates_sample(self) -> None:
        dialog = SampleProgrammingDialog()
        dialog.id_input.setText("XYZ")
        dialog.name_input.setText("CoFeB Wedge")
        dialog.rows_count.setValue(5)
        dialog.cols_count.setValue(3)
        dialog.col_labels_input.setText("50 nm, 100 nm, 200 nm")

        sample = dialog.get_sample()
        self.assertEqual(sample.sample_id, "XYZ")
        self.assertEqual(sample.name, "CoFeB Wedge")
        self.assertEqual(len(sample.rows), 5)
        self.assertEqual(len(sample.cols), 3)
        self.assertEqual(sample.col_labels.get("3"), "200 nm")
        self.assertEqual(sample.cell_label("1", "3"), "200 nm")

    def test_sample_inventory_page_lifecycle_and_geometry(self) -> None:
        # Prepopulate sample
        sample = Sample(
            sample_id="XYZ",
            name="CoFeB Sample XYZ",
            rows=("22", "23", "24"),
            row_labels={"23": "Center"},
            cols=("1", "2", "3"),
            col_labels={"3": "200 nm"},
        )
        self.store.save_sample(sample)

        page = SampleInventoryPage(self.store)
        page.show()
        self.application.processEvents()

        # Geometry must be non-zero (AGENTS.md contract)
        self.assertGreater(page.width(), 0)
        self.assertGreater(page.height(), 0)
        self.assertGreater(page.matrix_widget.width(), 0)

        # Check sample is listed
        self.assertGreaterEqual(page.sample_list.count(), 1)
        self.assertIn("CoFeB Sample XYZ", page.current_sample_title.text())

        # Test selecting cell and setting active target
        target_emitted: list[ActiveSampleTarget] = []
        page.active_target_changed.connect(lambda t: target_emitted.append(t))

        page._on_cell_selected("23", "3")
        self.assertEqual(page.cell_label_input.text(), "200 nm")
        page._set_selected_as_active_target()

        self.assertEqual(len(target_emitted), 1)
        self.assertEqual(target_emitted[0].sample_id, "XYZ")
        self.assertEqual(target_emitted[0].row, "23")
        self.assertEqual(target_emitted[0].col, "3")
        self.assertIn("XYZ", page.active_target_label.text())

        # Test advance to next device
        page._advance_to_next_device()
        self.assertEqual(len(target_emitted), 2)
        # Advance from (23, 3) wraps to next row (24, 1)
        self.assertEqual(target_emitted[1].row, "24")
        self.assertEqual(target_emitted[1].col, "1")

        # Test clearing target
        page._clear_active_target()
        self.assertEqual(len(target_emitted), 3)
        self.assertFalse(target_emitted[2].is_active)

        # Test inspector row & col label editing
        page._on_cell_selected("23", "3")
        page.row_label_input.setText("Center Strip")
        page.col_label_input.setText("220 nm Pillar")
        page.cell_label_input.setText("220 nm Pillar A")
        page._save_cell_changes()

        saved_sample = self.store.get_sample("XYZ")
        assert saved_sample is not None
        self.assertEqual(saved_sample.row_labels.get("23"), "Center Strip")
        self.assertEqual(saved_sample.col_labels.get("3"), "220 nm Pillar")
        self.assertEqual(saved_sample.cell_label("23", "3"), "220 nm Pillar A")

        # Test structure callbacks directly
        page._on_delete_column_requested("1")
        sample_del_col = self.store.get_sample("XYZ")
        assert sample_del_col is not None
        self.assertEqual(sample_del_col.cols, ("2", "3"))

        page._on_delete_row_requested("22")
        sample_del_row = self.store.get_sample("XYZ")
        assert sample_del_row is not None
        # Test quick mark burned and completed
        page._on_cell_selected("23", "3")
        page._quick_mark_state("burned")
        sample_burned = self.store.get_sample("XYZ")
        assert sample_burned is not None
        self.assertEqual(sample_burned.cell_state("23", "3"), "burned")
        self.assertIn("Burned: 1", page.stats_burned_label.text())

        page._quick_mark_state("completed")
        sample_completed = self.store.get_sample("XYZ")
        assert sample_completed is not None
        self.assertEqual(sample_completed.cell_state("23", "3"), "completed")
        self.assertIn("Completed: 1", page.stats_completed_label.text())

        # Test batch, row, col callbacks
        page._on_batch_cell_state_change_requested([("23", "2"), ("24", "2")], "burned")
        s_batch = self.store.get_sample("XYZ")
        assert s_batch is not None
        self.assertEqual(s_batch.cell_state("23", "2"), "burned")
        self.assertEqual(s_batch.cell_state("24", "2"), "burned")

        page._on_row_state_change_requested("24", "completed")
        s_row = self.store.get_sample("XYZ")
        assert s_row is not None
        self.assertEqual(s_row.cell_state("24", "2"), "completed")
        self.assertEqual(s_row.cell_state("24", "3"), "completed")

        page._on_col_state_change_requested("3", "burned")
        s_col = self.store.get_sample("XYZ")
        assert s_col is not None
        self.assertEqual(s_col.cell_state("23", "3"), "burned")
        self.assertEqual(s_col.cell_state("24", "3"), "burned")

        page.close()

    def test_sample_matrix_header_signals(self) -> None:
        widget = SampleMatrixWidget()
        sample = Sample(
            sample_id="HDR-TEST",
            name="Header Test",
            rows=("1", "2"),
            row_labels={"1": "Top", "2": "Bottom"},
            cols=("1", "2"),
            col_labels={"1": "100 nm", "2": "200 nm"},
        )
        widget.set_sample(sample)

        col_renames: list[tuple[str, str]] = []
        row_renames: list[tuple[str, str]] = []
        widget.col_rename_requested.connect(lambda k, lbl: col_renames.append((k, lbl)))
        widget.row_rename_requested.connect(lambda k, lbl: row_renames.append((k, lbl)))

        # Simulate double-clicking column 1 (index 1 -> col "2")
        widget._on_col_header_double_clicked(1)
        self.assertEqual(col_renames, [("2", "200 nm")])

        # Simulate double-clicking row 0 (index 0 -> row "1")
        widget._on_row_header_double_clicked(0)
        self.assertEqual(row_renames, [("1", "Top")])

    def test_programming_dialog_fine_grained_table_editor(self) -> None:
        sample = Sample(
            sample_id="PREV",
            name="Previous Sample",
            rows=("1", "2"),
            row_labels={"1": "Row 1", "2": "Row 2"},
            cols=("1", "2"),
            col_labels={"1": "50 nm", "2": "100 nm"},
            device_states={"1,1": "good"},
        )
        dialog = SampleProgrammingDialog(sample=sample)
        # Check that detailed tables are populated
        self.assertEqual(dialog.rows_table.rowCount(), 2)
        self.assertEqual(dialog.cols_table.rowCount(), 2)

        # Add a new row via table editor
        dialog._on_add_row()
        self.assertEqual(dialog.rows_table.rowCount(), 3)
        dialog.rows_table.item(2, 1).setText("Row 3 Custom")

        # Add a new column via table editor
        dialog._on_add_col()
        self.assertEqual(dialog.cols_table.rowCount(), 3)
        dialog.cols_table.item(2, 1).setText("150 nm")

        updated = dialog.get_sample()
        self.assertEqual(len(updated.rows), 3)
        self.assertEqual(len(updated.cols), 3)
        self.assertEqual(updated.row_labels.get("3"), "Row 3 Custom")
        self.assertEqual(updated.col_labels.get("3"), "150 nm")
        # Existing device state for (1, 1) is retained!
        self.assertEqual(updated.cell_state("1", "1"), "good")


if __name__ == "__main__":
    unittest.main()
