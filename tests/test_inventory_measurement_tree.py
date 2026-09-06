"""Unit and rendering tests for the hierarchical measurement tree and browser view."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

import h5py
import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.inventory.models import Sample, SampleRunRecord
from app.ui.inventory.measurement_browser_view import MeasurementBrowserView
from app.ui.inventory.measurement_tree import MeasurementTreeWidget


def _create_synthetic_hdf5(path: Path, n_points: int = 20) -> None:
    """Create a minimal HDF5 measurement file with scalar sweep points."""
    with h5py.File(path, "w") as file:
        points = file.create_group("points")
        h_vals = np.linspace(-100, 100, n_points)
        for idx, h in enumerate(h_vals):
            grp = points.create_group(f"{idx:05d}")
            sp_data = json.dumps({"b_field_x": float(h)})
            grp.create_dataset("setpoints_json", data=sp_data)
            # R goes from 1000 to 2000
            r_val = 1000.0 if h < 0 else 2000.0
            meas_data = json.dumps({"resistance": r_val, "voltage": r_val * 1e-4})
            grp.create_dataset("measurements_json", data=meas_data)


class MeasurementTreeUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)

        # Create two synthetic H5 files
        self.h5_1 = self.root / "sweep_1.h5"
        self.h5_2 = self.root / "sweep_2.h5"
        _create_synthetic_hdf5(self.h5_1, 20)
        _create_synthetic_hdf5(self.h5_2, 20)

        self.sample = Sample(
            sample_id="SAMPLE-A",
            name="MTJ Wedge Sample A",
            rows=("1", "2"),
            row_labels={"1": "Top Strip", "2": "Bottom Strip"},
            cols=("1", "2"),
            col_labels={"1": "100 nm", "2": "200 nm"},
        )

        self.run1 = SampleRunRecord(
            sample_id="SAMPLE-A",
            row="1",
            col="2",
            device_label="200 nm Pillar",
            run_path=str(self.h5_1),
            run_sha256="sha111",
            created_at_utc="2026-09-06T10:00:00Z",
            status="completed",
            point_count=20,
            spectrum_count=0,
            recipe_name="EasyAxis_MajorLoop",
            elab_experiment_id=101,
            elab_status="uploaded",
        )

        self.run2 = SampleRunRecord(
            sample_id="SAMPLE-A",
            row="1",
            col="2",
            device_label="200 nm Pillar",
            run_path=str(self.h5_2),
            run_sha256="sha222",
            created_at_utc="2026-09-06T11:30:00Z",
            status="completed",
            point_count=20,
            spectrum_count=0,
            recipe_name="MinorLoop",
        )

        self.run3 = SampleRunRecord(
            sample_id="SAMPLE-A",
            row="2",
            col="1",
            device_label="100 nm Pillar",
            run_path=str(self.h5_1),
            run_sha256="sha333",
            created_at_utc="2026-09-05T09:00:00Z",
            status="failed",
            point_count=5,
            spectrum_count=0,
            recipe_name="EasyAxis_MajorLoop",
        )

        self.runs = [self.run1, self.run2, self.run3]

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_tree_grouping_modes_and_search(self) -> None:
        tree_widget = MeasurementTreeWidget()
        tree_widget.show()
        self.application.processEvents()

        # Fluent contract: non-zero geometry
        self.assertGreater(tree_widget.width(), 0)
        self.assertGreater(tree_widget.height(), 0)

        tree_widget.set_runs(self.runs, sample=self.sample)
        self.assertEqual(tree_widget.count_badge.text(), "3 sweeps")

        # 1. Default mode: Group by Device / Pillar
        self.assertEqual(tree_widget.grouping_combo.currentText(), MeasurementTreeWidget.VIEW_BY_DEVICE)
        # We have 2 devices: (1, 2) with 2 runs, and (2, 1) with 1 run
        self.assertEqual(tree_widget.tree.topLevelItemCount(), 2)

        # 2. Group by Recipe mode
        tree_widget.grouping_combo.setCurrentText(MeasurementTreeWidget.VIEW_BY_RECIPE)
        # We have 2 recipes: EasyAxis_MajorLoop (2 runs) and MinorLoop (1 run)
        self.assertEqual(tree_widget.tree.topLevelItemCount(), 2)

        # 3. Group by Date mode
        tree_widget.grouping_combo.setCurrentText(MeasurementTreeWidget.VIEW_BY_DATE)
        # We have 2 dates: 2026-09-06 (2 runs) and 2026-09-05 (1 run)
        self.assertEqual(tree_widget.tree.topLevelItemCount(), 2)

        # 4. Flat list mode
        tree_widget.grouping_combo.setCurrentText(MeasurementTreeWidget.VIEW_FLAT)
        self.assertEqual(tree_widget.tree.topLevelItemCount(), 3)

        # 5. Search filtering
        tree_widget.search_input.setText("MinorLoop")
        self.assertEqual(tree_widget.tree.topLevelItemCount(), 1)

        tree_widget.search_input.setText("R2:C1")
        self.assertEqual(tree_widget.tree.topLevelItemCount(), 1)

        tree_widget.search_input.clear()
        self.assertEqual(tree_widget.tree.topLevelItemCount(), 3)

        # 6. Status filter
        tree_widget.status_combo.setCurrentText("Completed Only")
        self.assertEqual(tree_widget.tree.topLevelItemCount(), 2)

        tree_widget.status_combo.setCurrentText("Failed / Aborted")
        self.assertEqual(tree_widget.tree.topLevelItemCount(), 1)

        tree_widget.status_combo.setCurrentText("With eLab Link")
        self.assertEqual(tree_widget.tree.topLevelItemCount(), 1)

        tree_widget.close()

    def test_tree_checkboxes_and_multi_selection(self) -> None:
        tree_widget = MeasurementTreeWidget()
        tree_widget.set_runs(self.runs, sample=self.sample)

        checked_emitted: list[list[SampleRunRecord]] = []
        tree_widget.runs_checked_changed.connect(lambda lst: checked_emitted.append(lst))

        # Check all
        tree_widget._check_all()
        self.assertEqual(len(tree_widget.get_checked_runs()), 3)
        self.assertIn("3 selected", tree_widget.checked_count_label.text())

        # Uncheck all
        tree_widget._uncheck_all()
        self.assertEqual(len(tree_widget.get_checked_runs()), 0)

        # Check top level group (Device R1:C2)
        top0 = tree_widget.tree.topLevelItem(0)
        top0.setCheckState(0, Qt.CheckState.Checked)
        tree_widget._on_item_changed(top0, 0)
        # Should have checked 2 child runs
        checked = tree_widget.get_checked_runs()
        self.assertEqual(len(checked), 2)

    def test_browser_view_full_lifecycle_and_signals(self) -> None:
        browser = MeasurementBrowserView()
        browser.resize(1000, 700)
        browser.show()
        self.application.processEvents()

        # Geometry contract
        self.assertGreater(browser.width(), 0)
        self.assertGreater(browser.height(), 0)
        self.assertGreater(browser.plot_widget.width(), 0)
        self.assertGreater(browser.analytics_card.width(), 0)

        browser.set_runs(self.runs, sample=self.sample)
        self.application.processEvents()

        # Check that first run was automatically selected and displayed in plot and analytics
        self.assertIn("EasyAxis_MajorLoop", browser.analytics_card.run_title.text())
        self.assertIn("R1:C2", browser.analytics_card.run_coord_label.text())
        # TMR and Rp tiles populated
        self.assertIsNotNone(browser.analytics_card.tile_rp.value_label)
        self.assertNotEqual(browser.analytics_card.tile_rp.value_label.text(), "")

        # Test selecting another run in the tree
        tree = browser.tree_widget
        top0 = tree.tree.topLevelItem(0)
        child1 = top0.child(1)  # MinorLoop
        tree.tree.setCurrentItem(child1)
        self.application.processEvents()

        self.assertIn("MinorLoop", browser.analytics_card.run_title.text())

        # Test filter_by_device helper
        browser.filter_by_device("2", "1")
        self.application.processEvents()
        self.assertIn("R2:C1", tree.search_input.text())

        # Test open in results signal
        results_requested: list[str] = []
        browser.open_in_results_requested.connect(lambda p: results_requested.append(p))
        browser.analytics_card.open_results_btn.clicked.emit()
        self.assertEqual(len(results_requested), 1)
        self.assertIn("sweep", results_requested[0])

        browser.close()


if __name__ == "__main__":
    unittest.main()
