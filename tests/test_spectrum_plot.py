from __future__ import annotations

import csv
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.ui.design_system import plot_theme, tokens_for
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

    def test_frequency_readouts_use_engineering_si_prefixes(self) -> None:
        plot = SpectrumPlotWidget()
        try:
            plot.set_labels(x="Frequency", x_unit="Hz")
            self.assertEqual(plot._format_x_value(10_000.0), "10 kHz")
            self.assertEqual(plot._format_x_value(100_000_000.0), "100 MHz")
            self.assertEqual(plot._format_x_value(6_000_000_000.0), "6 GHz")
        finally:
            plot.close()

    def test_trace_can_render_a_visible_symbol_for_a_single_live_point(self) -> None:
        plot = SpectrumPlotWidget()
        try:
            plot.set_trace(
                "Live",
                [0.5],
                [12.0],
                color="#00a67d",
                primary=True,
                show_points=True,
            )
            curve = plot._curves["Live"]
            self.assertEqual(curve.opts["symbol"], "o")
            self.assertEqual(curve.opts["symbolSize"], 6)
            self.assertEqual(curve.opts["symbolBrush"].color().name(), "#00a67d")
        finally:
            plot.close()

    def test_apply_theme_uses_design_system_plot_palette(self) -> None:
        widget = SpectrumPlotWidget()
        try:
            widget.apply_theme("light")
            self.assertEqual(widget._theme_name, "light")
            self.assertEqual(widget.plot.backgroundBrush().color().name(), "#ffffff")
        finally:
            widget.close()

    def test_apply_theme_rethemes_token_owned_plot_items(self) -> None:
        widget = SpectrumPlotWidget()
        try:
            widget.apply_theme("dark")
            widget.set_trace("Raw", [1, 2], [-10, -20], primary=True)
            widget.set_trace("Reference", [1, 2], [-11, -21], color="#123456")
            widget.apply_theme("light")
            palette = plot_theme(tokens_for("light"))
            self.assertEqual(widget._curves["Raw"].opts["pen"].color().name(), palette.measurement)
            self.assertEqual(widget._curves["Reference"].opts["pen"].color().name(), "#123456")
            self.assertEqual(widget.marker.pen.color().name(), palette.reference)
            self.assertEqual(widget.crosshair_x.pen.color().name(), palette.grid)
            self.assertEqual(widget.crosshair_y.pen.color().name(), palette.grid)
            self.assertEqual(widget.plot.getAxis("left").pen().color().name(), palette.axes)
            self.assertEqual(widget.plot.getAxis("bottom").textPen().color().name(), palette.axes)
        finally:
            widget.close()

    def test_multiple_peak_markers_can_highlight_one_selected_peak(self) -> None:
        widget = SpectrumPlotWidget()
        try:
            widget.set_peak_markers([1e6, 2e6, 3e6], [-30.0, -20.0, -40.0])
            self.assertEqual(len(widget.peak_markers.points()), 3)
            widget.select_peak_marker(1)
            points = widget.peak_markers.points()
            self.assertEqual(points[1].size(), 14)
            self.assertEqual(points[0].size(), 10)
            widget.apply_theme("light")
            self.assertEqual(points[1].brush().color().name(), plot_theme(tokens_for("light")).reference)
        finally:
            widget.close()

    def test_csv_export_contains_each_visible_trace_and_honours_overwrite(self) -> None:
        plot = SpectrumPlotWidget()
        try:
            plot.set_trace("Raw", [1, 2], [-10, -20], primary=True)
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "trace.csv"
                plot._export_csv(path)
                with path.open("r", encoding="utf-8", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual([row["trace"] for row in rows], ["Raw", "Raw"])
                plot.set_trace("Raw", [3], [-30], primary=True)
                plot._export_csv(path)
                with path.open("r", encoding="utf-8", newline="") as stream:
                    overwritten = list(csv.DictReader(stream))
                self.assertEqual(len(overwritten), 1)
                self.assertEqual(overwritten[0]["frequency_Hz"], "3.0")
        finally:
            plot.close()


if __name__ == "__main__":
    unittest.main()
