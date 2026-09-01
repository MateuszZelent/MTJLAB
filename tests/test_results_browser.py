from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import h5py
import numpy as np
from PySide6.QtWidgets import QApplication, QBoxLayout

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.storage import Hdf5RunReader, Hdf5RunWriter, ThatecRunReader
from app.ui.results import HeatmapResultsTab, ResultsPage
from tests.test_heatmap_coordinates import _write_three_axis_sweep


class ResultsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_private_result_tree_filters_and_switches_spectrum_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="name: browser\n",
                settings_source="schema_version: 1\n",
                plan_hash="browser",
                device_idn={},
                expected_points=2,
            )
            reference = SpectrumTrace(
                (1.0, 2.0, 3.0),
                (-70.0, -60.0, -65.0),
                datetime.now(timezone.utc),
                "TRAC1",
            )
            writer.store_reference(reference)
            for index in range(2):
                writer.append(
                    MeasurementPoint(
                        index=index,
                        setpoints={"source.level_v": float(index)},
                        measurements={},
                        metadata={"reference_index": 0},
                    ),
                    SpectrumTrace(
                        (1.0, 2.0, 3.0),
                        (-50.0 - index, -40.0 - index, -45.0 - index),
                        datetime.now(timezone.utc),
                        "TRAC1",
                    ),
                    processed_values=(-1.0 - index, -2.0 - index, -3.0 - index),
                    processed_unit="dB",
                    processing_operation="difference_db",
                )
            writer.close("completed")

            page = ResultsPage(temporary)
            try:
                page.runs.setCurrentItem(page.runs.topLevelItem(0))
                self.application.processEvents()
                self.assertEqual(page.points.topLevelItemCount(), 2)
                self.assertIn(
                    "Results",
                    [
                        page.experiment_tree.topLevelItem(index).text(0)
                        for index in range(page.experiment_tree.topLevelItemCount())
                    ],
                )

                page.points.setCurrentItem(page.points.topLevelItem(0))
                self.application.processEvents()
                variant = page.spectrum_tab.spectrum_variant_combo
                self.assertGreaterEqual(variant.findData("processed"), 0)
                self.assertGreaterEqual(variant.findData("reference"), 0)

                variant.setCurrentIndex(variant.findData("processed"))
                self.application.processEvents()
                self.assertEqual(
                    page.spectrum_plot.trace_point_count("Processed spectrum"),
                    3,
                )
                variant.setCurrentIndex(variant.findData("reference"))
                self.application.processEvents()
                self.assertEqual(
                    page.spectrum_plot.trace_point_count("Reference spectrum"),
                    3,
                )

                result_root = next(
                    page.experiment_tree.topLevelItem(index)
                    for index in range(page.experiment_tree.topLevelItemCount())
                    if page.experiment_tree.topLevelItem(index).text(0) == "Results"
                )
                checkpoint_group = result_root.child(0)
                point_item = checkpoint_group.child(0)
                page.experiment_tree.setCurrentItem(point_item)
                self.application.processEvents()
                self.assertTrue(page.sweep_tree.show_spectrum_button.isEnabled())
                page.sweep_tree.show_spectrum_button.click()
                self.application.processEvents()
                self.assertEqual(
                    page.spectrum_plot.trace_point_count("Stored spectrum"), 3
                )
                processed_item = next(
                    point_item.child(index)
                    for index in range(point_item.childCount())
                    if point_item.child(index).text(0) == "Processed spectrum"
                )
                page.experiment_tree.setCurrentItem(processed_item)
                self.application.processEvents()
                self.assertEqual(
                    page.spectrum_plot.trace_point_count("Processed spectrum"),
                    3,
                )
                references_group = next(
                    result_root.child(index)
                    for index in range(result_root.childCount())
                    if result_root.child(index).text(0).startswith("References")
                )
                page.experiment_tree.setCurrentItem(references_group.child(0))
                self.application.processEvents()
                self.assertEqual(
                    page.spectrum_plot.trace_point_count("Reference spectrum"),
                    3,
                )

                filters = page.spectrum_tab
                self.assertEqual(filters.parameter_set_combo.count(), 3)
                filters.parameter_set_combo.setCurrentIndex(1)
                self.application.processEvents()
                self.assertEqual(page.points.topLevelItemCount(), 1)
                filters.clear_parameter_filter()
                self.application.processEvents()
                filters.filter_parameter_combo.setCurrentIndex(
                    filters.filter_parameter_combo.findData("source.level_v")
                )
                self.application.processEvents()
                filters.filter_value_combo.setCurrentIndex(
                    filters.filter_value_combo.findData(1.0)
                )
                self.application.processEvents()
                self.assertEqual(page.points.topLevelItemCount(), 1)
                self.assertIn("1 of 2", filters.filter_summary.text())
            finally:
                page.close()

    def test_heatmap_selects_raw_or_processed_spectrum_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "variants.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="name: heatmap variants\n",
                settings_source="schema_version: 1\n",
                plan_hash="heatmap-variants",
                device_idn={},
                expected_points=2,
            )
            for index in range(2):
                writer.append(
                    MeasurementPoint(
                        index=index,
                        setpoints={"keithley.B.current": index * 0.001},
                        measurements={},
                    ),
                    SpectrumTrace(
                        (1.0, 2.0, 3.0),
                        (-50.0 - index, -40.0 - index, -45.0 - index),
                        datetime.now(timezone.utc),
                        "TRAC1",
                    ),
                    processed_values=(-1.0 - index, -2.0 - index, -3.0 - index),
                    processed_unit="dB",
                    processing_operation="difference_db",
                )
            writer.close("completed")

            tab = HeatmapResultsTab()
            try:
                tab.resize(1000, 700)
                tab.show()
                self.application.processEvents()
                tab.load(
                    path,
                    ThatecRunReader.describe(path),
                    Hdf5RunReader.points(path),
                )

                self.assertEqual(tab.variant_combo.currentData(), "raw")
                self.assertGreaterEqual(tab.variant_combo.findData("processed"), 0)
                self.assertEqual(tab.x_axis_combo.currentData(), "frequency")
                self.assertEqual(tab.y_axis_combo.currentData(), "keithley.B.current")
                tab.x_axis_combo.setCurrentIndex(
                    tab.x_axis_combo.findData("keithley.B.current")
                )
                self.application.processEvents()
                self.assertEqual(tab.y_axis_combo.currentData(), "frequency")
                tab.x_axis_combo.setCurrentIndex(tab.x_axis_combo.findData("frequency"))
                self.application.processEvents()
                tab.load_heatmap_for_row(str(tab.row_combo.currentData()))
                self.assertTrue(np.allclose(tab.heatmap._data[0], (-50.0, -40.0, -45.0)))
                self.assertIn(
                    "Keithley B current",
                    tab.heatmap.plot.getAxis("left").label.toPlainText(),
                )

                tab.variant_combo.setCurrentIndex(
                    tab.variant_combo.findData("processed")
                )
                self.application.processEvents()
                tab.load_heatmap_for_row(str(tab.row_combo.currentData()))
                self.assertTrue(np.allclose(tab.heatmap._data[0], (-1.0, -2.0, -3.0)))
                self.assertEqual(
                    tab.heatmap.color_bar.getAxis("right").label.toPlainText().strip(),
                    "Processed amplitude (dB)",
                )
            finally:
                tab.close()

    def test_heatmap_exposes_engineering_range_controls_at_narrow_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "range-controls.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="name: range controls\n",
                settings_source="schema_version: 1\n",
                plan_hash="range-controls",
                device_idn={},
                expected_points=3,
            )
            for index, current_a in enumerate((0.0, 0.001, 0.002)):
                writer.append(
                    MeasurementPoint(
                        index=index,
                        setpoints={"keithley.B.current": current_a},
                        measurements={},
                    ),
                    SpectrumTrace(
                        (1e6, 2e6),
                        (-60.0 - index, -50.0 - index),
                        datetime.now(timezone.utc),
                        "TRAC1",
                    ),
                )
            writer.close("completed")

            tab = HeatmapResultsTab()
            try:
                tab.resize(620, 700)
                tab.show()
                self.application.processEvents()
                tab.load(
                    path,
                    ThatecRunReader.describe(path),
                    Hdf5RunReader.points(path),
                )
                self.application.processEvents()

                self.assertIn("keithley.B.current", tab._range_combos)
                minimum, maximum = tab._range_combos["keithley.B.current"]
                self.assertEqual(minimum.currentData(), 0.0)
                self.assertEqual(maximum.currentData(), 0.002)
                self.assertIn("1 mA", minimum.itemText(1))
                self.assertTrue(tab.filter_host.isVisible())
                self.assertGreater(tab.filter_host.geometry().height(), 0)
                self.assertEqual(
                    tab.selector_layout.direction(),
                    QBoxLayout.Direction.TopToBottom,
                )
                self.assertLessEqual(
                    tab.filter_host.geometry().right(), tab.rect().right()
                )

                minimum.setCurrentIndex(1)
                tab.load_heatmap_for_row(str(tab.row_combo.currentData()))
                self.assertTrue(np.allclose(tab.heatmap._y_values, (0.001, 0.002)))
            finally:
                tab.close()

    def test_heatmap_defaults_unselected_dimensions_to_one_measured_slice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "three-axis-controls.h5"
            _write_three_axis_sweep(path)
            tab = HeatmapResultsTab()
            try:
                tab.resize(1280, 700)
                tab.show()
                self.application.processEvents()
                tab.load(
                    path,
                    ThatecRunReader.describe(path),
                    Hdf5RunReader.points(path),
                )
                tab.y_axis_combo.setCurrentIndex(
                    tab.y_axis_combo.findData("keithley.B.current")
                )
                self.application.processEvents()

                minimum, maximum = tab._range_combos["rigol.1.high_level"]
                self.assertEqual(minimum.currentData(), 1.0)
                self.assertEqual(maximum.currentData(), 1.0)
                tab.load_heatmap_for_row(str(tab.row_combo.currentData()))
                tab._read_pool.waitForDone(30_000)
                self.application.processEvents()
                self.assertEqual(tab.heatmap._data.shape, (2, 2))
            finally:
                tab.close()

    def test_public_result_browser_builds_checkpoint_index_and_navigates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "public.h5"
            _write_public_fixture(path)

            page = ResultsPage(temporary)
            try:
                page.runs.setCurrentItem(page.runs.topLevelItem(0))
                self.application.processEvents()
                self.assertEqual(page.points.topLevelItemCount(), 2)
                self.assertEqual(page.points.currentItem().text(0), "0")
                self.assertEqual(
                    page.spectrum_plot.trace_point_count("Spectrum (dBm)"), 3
                )
                self.assertTrue(page.spectrum_tab.next_button.isEnabled())

                page.spectrum_tab.next_button.click()
                self.application.processEvents()
                self.assertEqual(page.points.currentItem().text(0), "1")
                self.assertIn("2 / 2", page.spectrum_tab.position_label.text())
                self.assertFalse(page.spectrum_tab.next_button.isEnabled())

                filters = page.spectrum_tab
                parameter_index = filters.filter_parameter_combo.findData("Current (A)")
                self.assertGreaterEqual(parameter_index, 0)
                filters.filter_parameter_combo.setCurrentIndex(parameter_index)
                self.application.processEvents()
                self.assertIn("100 mA", filters.filter_value_combo.itemText(1))
                filters.filter_value_combo.setCurrentIndex(
                    filters.filter_value_combo.findData(0.1)
                )
                self.application.processEvents()
                self.assertEqual(page.points.topLevelItemCount(), 1)
                self.assertEqual(page.points.topLevelItem(0).text(0), "0")
            finally:
                page.close()


def _write_public_fixture(path: Path) -> None:
    text = h5py.string_dtype("utf-8")
    with h5py.File(path, "w") as file:
        scan = file.create_group("scan_definition")
        measurement = file.create_group("measurement")

        scan.create_dataset(
            "row_00",
            data=np.asarray(
                [
                    ("device name", "Source"),
                    ("control name", "Current (A)"),
                    ("dimensions", "0"),
                    ("tree indent level", "0"),
                    ("function", "indicator"),
                ],
                dtype=object,
            ),
            dtype=text,
        )
        scan.create_dataset(
            "row_01",
            data=np.asarray(
                [
                    ("device name", "Anritsu"),
                    ("control name", "Spectrum (dBm)"),
                    ("dimensions", "1"),
                    ("tree indent level", "0"),
                    ("function", "indicator"),
                ],
                dtype=object,
            ),
            dtype=text,
        )
        scan.create_dataset(
            "tree_view",
            data=np.asarray(
                [
                    ("row   0", "indicator", "Current (A)"),
                    ("row   1", "indicator", "Spectrum (dBm)"),
                ],
                dtype=object,
            ),
            dtype=text,
        )

        current = measurement.create_group("row_00")
        current.create_dataset("data", data=np.asarray([0.1, 0.2], dtype="f8"))
        current.create_dataset("timestamp", data=np.asarray([1.0, 2.0], dtype="f8"))

        spectrum = measurement.create_group("row_01")
        spectrum.create_dataset(
            "data",
            data=np.asarray(
                [[-10.0, -20.0, -15.0], [-11.0, -21.0, -16.0]], dtype="f8"
            ),
        )
        spectrum.create_dataset("timestamp", data=np.asarray([1.0, 2.0], dtype="f8"))
        spectrum.create_dataset(
            "scale",
            data=np.asarray([1.0, 1.0, 0.0, 1.0] * 2, dtype="f8"),
        )
        spectrum.create_dataset(
            "metadata",
            data=np.asarray(
                [
                    ("name", "Frequency"),
                    ("unit", "Hz"),
                    ("offset", "1"),
                    ("multiplier", "1"),
                    ("name", "Power"),
                    ("unit", "dBm"),
                    ("offset", "0"),
                    ("multiplier", "1"),
                ],
                dtype=object,
            ),
            dtype=text,
        )


if __name__ == "__main__":
    unittest.main()
