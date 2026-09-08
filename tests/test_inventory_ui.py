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
    RenumberRowsDialog,
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
        dialog.folder_name_input.setText("1_CoFeBWedge")
        dialog.show()
        self.application.processEvents()
        self.assertGreater(dialog.folder_name_input.width(), 0)

        sample = dialog.get_sample()
        self.assertEqual(sample.sample_id, "XYZ")
        self.assertEqual(sample.name, "CoFeB Wedge")
        self.assertEqual(len(sample.rows), 5)
        self.assertEqual(len(sample.cols), 3)
        self.assertEqual(sample.col_labels.get("3"), "200 nm")
        self.assertEqual(sample.cell_label("1", "3"), "200 nm")
        self.assertEqual(sample.folder_name, "1_CoFeBWedge")
        dialog.close()

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
        self.assertTrue(page.catalogue_settings_btn.isVisible())
        self.assertIn(str(self.store.catalogue_root), page.catalogue_settings_btn.toolTip())

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

    def test_renumber_rows_dialog(self) -> None:
        sample = Sample(
            sample_id="SAMPLE-REN",
            name="INL Chip",
            rows=("1", "2", "3"),
            row_labels={"1": "Row 1", "2": "Row 2", "3": "Row 3"},
            cols=("1", "2"),
            device_states={"2,1": "completed"},
        )
        dialog = RenumberRowsDialog(sample=sample)
        dialog.start_spin.setValue(20)
        dialog.end_spin.setValue(30)
        self.assertEqual(dialog.count_spin.value(), 11)

        renumbered = dialog.get_renumbered_sample()
        self.assertEqual(len(renumbered.rows), 11)
        self.assertEqual(renumbered.rows[0], "20")
        self.assertEqual(renumbered.rows[-1], "30")
        # Check cell state preservation (old row 2 -> new row 21)
        self.assertEqual(renumbered.cell_state("21", "1"), "completed")

    def test_programming_dialog_custom_range_rows(self) -> None:
        dialog = SampleProgrammingDialog()
        dialog.id_input.setText("INL-20-30")
        dialog.row_scheme.setCurrentIndex(1)  # Custom Range
        dialog.row_start.setValue(20)
        dialog.row_end.setValue(30)
        self.assertEqual(dialog.rows_count.value(), 11)
        dialog.col_labels_input.setText("100 nm, 200 nm, 500 nm")

        sample = dialog.get_sample()
        self.assertEqual(len(sample.rows), 11)
        self.assertEqual(sample.rows[0], "20")
        self.assertEqual(sample.rows[-1], "30")

    def test_instant_toggle_buttons_and_state_toggling(self) -> None:
        sample = Sample(
            sample_id="TOGGLE-TEST",
            name="Toggle Sample",
            rows=("1", "2"),
            cols=("1", "2"),
        )
        self.store.save_sample(sample)
        page = SampleInventoryPage(self.store)
        page.show()
        self.application.processEvents()

        # Select (1, 1)
        page._on_cell_selected("1", "1")
        self.assertFalse(page.quick_completed_btn.isChecked())
        self.assertFalse(page.quick_burned_btn.isChecked())

        # Click Completed toggle button -> becomes completed
        page.quick_completed_btn.click()
        self.assertTrue(page.quick_completed_btn.isChecked())
        self.assertEqual(page.store.get_sample("TOGGLE-TEST").cell_state("1", "1"), "completed")

        # Click Completed toggle button again -> untoggles back to untested!
        page.quick_completed_btn.click()
        self.assertFalse(page.quick_completed_btn.isChecked())
        self.assertEqual(page.store.get_sample("TOGGLE-TEST").cell_state("1", "1"), "untested")

        # Click Burned toggle button -> becomes burned
        page.quick_burned_btn.click()
        self.assertTrue(page.quick_burned_btn.isChecked())
        self.assertEqual(page.store.get_sample("TOGGLE-TEST").cell_state("1", "1"), "burned")

        # Click Burned toggle button again -> untoggles back to untested!
        page.quick_burned_btn.click()
        self.assertFalse(page.quick_burned_btn.isChecked())
        self.assertEqual(page.store.get_sample("TOGGLE-TEST").cell_state("1", "1"), "untested")

        # Verify notes input has expanded height
        self.assertGreaterEqual(page.cell_notes_input.minimumHeight(), 80)
        page.close()

    def test_matrix_widget_in_place_update_and_keyboard_shortcuts(self) -> None:
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        widget = SampleMatrixWidget()
        sample = Sample(
            sample_id="KEY-TEST",
            name="Key Test",
            rows=("1", "2"),
            cols=("1", "2"),
        )
        widget.set_sample(sample)
        item_before = widget.table.item(0, 0)
        self.assertIsNotNone(item_before)

        # In-place update
        widget.update_cell("1", "1", state="completed")
        item_after = widget.table.item(0, 0)
        self.assertIs(item_before, item_after)  # Same item identity, NO table recreation!
        self.assertIn("COMPLETED", item_after.text())

        # Select item (0, 0)
        widget.table.setCurrentCell(0, 0)
        item_after.setSelected(True)

        # Press 'B' key -> marks as burned
        b_key = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_B, Qt.KeyboardModifier.NoModifier)
        state_changes: list[tuple[str, str, str]] = []
        widget.cell_state_change_requested.connect(lambda r, c, s: state_changes.append((r, c, s)))
        widget.table.keyPressEvent(b_key)
        self.assertEqual(state_changes, [("1", "1", "burned")])

        # Press 'C' key -> marks as completed
        c_key = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, Qt.KeyboardModifier.NoModifier)
        widget.table.keyPressEvent(c_key)
        self.assertEqual(state_changes[-1], ("1", "1", "completed"))

        # Press 'U' key -> marks as untested
        u_key = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_U, Qt.KeyboardModifier.NoModifier)
        widget.table.keyPressEvent(u_key)
        self.assertEqual(state_changes[-1], ("1", "1", "untested"))

    def test_device_inspector_clean_icons_and_target_state(self) -> None:
        from qfluentwidgets import TableWidget, ToggleButton

        sample = Sample(
            sample_id="INSP-CLEAN",
            name="Clean Inspector Test",
            rows=("1", "2"),
            cols=("1", "2"),
        )
        self.store.save_sample(sample)
        page = SampleInventoryPage(self.store)
        page.show()
        self.application.processEvents()

        # Check button types and clean text (no duplicate icons in text)
        self.assertIsInstance(page.quick_completed_btn, ToggleButton)
        self.assertIsInstance(page.quick_burned_btn, ToggleButton)
        self.assertEqual(page.quick_completed_btn.text(), "Completed")
        self.assertEqual(page.quick_burned_btn.text(), "Burned")
        self.assertNotIn("✔", page.quick_completed_btn.text())
        self.assertNotIn("🔥", page.quick_burned_btn.text())
        self.assertEqual(page.new_sample_btn.text(), "New Sample")
        self.assertNotIn("+", page.new_sample_btn.text())

        # Check TableWidget
        self.assertIsInstance(page.cell_runs_table, TableWidget)

        # Select (1, 1) - initially not target
        page._on_cell_selected("1", "1")
        self.assertEqual(page.set_target_btn.text(), "Set Target")
        self.assertNotIn("★", page.set_target_btn.text())

        # Set as active target
        page.set_target_btn.click()
        self.assertEqual(page.set_target_btn.text(), "Target Active")

        # Select another cell (1, 2)
        page._on_cell_selected("1", "2")
        self.assertEqual(page.set_target_btn.text(), "Set Target")

        # Select back (1, 1) -> should show Target Active
        page._on_cell_selected("1", "1")
        self.assertEqual(page.set_target_btn.text(), "Target Active")

        # Clear target
        page._clear_active_target()
        self.assertEqual(page.set_target_btn.text(), "Set Target")

        page.close()


if __name__ == "__main__":
    unittest.main()
