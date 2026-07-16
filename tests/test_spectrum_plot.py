from __future__ import annotations

import csv
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.ui.widgets import SpectrumPlotWidget


class SpectrumPlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_plot_preserves_finite_narrow_peak_and_peak_marker(self) -> None:
        plot = SpectrumPlotWidget()
        try:
            x = list(range(10_001))
            y = [-100.0] * len(x)
            y[5_003] = -1.0
            plot.set_trace("Raw", x, y, primary=True)
            plot.peak_search()
            self.assertEqual(plot.trace_point_count("Raw"), 10_001)
            self.assertTrue(plot.marker.isVisible())
            self.assertAlmostEqual(plot.marker.value(), 5_003.0)
        finally:
            plot.close()

    def test_max_and_min_hold_accumulate_without_storing_frames(self) -> None:
        plot = SpectrumPlotWidget()
        try:
            plot.set_trace("Raw", [1, 2, 3], [-10, -20, -30], primary=True)
            plot.toggle_max_hold()
            plot.toggle_min_hold()
            plot.set_trace("Raw", [1, 2, 3], [-15, -5, -40], primary=True)
            self.assertEqual(plot._traces["Max hold"][1].tolist(), [-10.0, -5.0, -30.0])
            self.assertEqual(plot._traces["Min hold"][1].tolist(), [-15.0, -20.0, -40.0])
        finally:
            plot.close()

    def test_csv_export_contains_each_visible_trace_and_refuses_overwrite(self) -> None:
        plot = SpectrumPlotWidget()
        try:
            plot.set_trace("Raw", [1, 2], [-10, -20], primary=True)
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "trace.csv"
                plot._export_csv(path)
                with path.open("r", encoding="utf-8", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual([row["trace"] for row in rows], ["Raw", "Raw"])
                with self.assertRaises(FileExistsError):
                    plot._export_csv(path)
        finally:
            plot.close()


if __name__ == "__main__":
    unittest.main()
