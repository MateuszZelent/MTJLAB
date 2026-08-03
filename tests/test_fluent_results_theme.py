from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from app.ui.design_system import plot_theme, tokens_for
from app.ui.results.heatmap_tab import HeatmapPlotWidget
from app.ui.shell import MainWindow


class FluentResultsThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_heatmap_plot_uses_semantic_tokens_for_light_and_dark_axes(self) -> None:
        plot = HeatmapPlotWidget()
        try:
            for name in ("light", "dark"):
                plot.apply_theme(name)
                expected = plot_theme(tokens_for(name))
                axis_pen = plot.plot.getAxis("bottom").pen().color().name()
                self.assertEqual(axis_pen, expected.axes)
        finally:
            plot.close()

    def test_heatmap_colorbar_has_a_visible_interactive_gradient(self) -> None:
        plot = HeatmapPlotWidget()
        try:
            plot.show()
            self.application.processEvents()
            plot.set_data(
                np.asarray(((-80.0, -75.0), (-72.0, -70.0))),
                x_values=np.asarray((1.0, 2.0)),
                y_values=np.asarray((0.0, 1.0)),
                z_label="Power (dBm)",
            )
            self.assertIsNotNone(plot.color_bar._colorMap)
            self.assertFalse(plot.color_bar.bar.pixmap().isNull())
            self.assertEqual(plot.color_bar.values, (-80.0, -70.0))
            self.assertTrue(plot.color_bar.region.isVisible())
        finally:
            plot.close()

    def test_heatmap_readout_uses_physical_cell_boundaries_for_nonuniform_axes(self) -> None:
        plot = HeatmapPlotWidget()
        try:
            plot.set_data(
                np.arange(6, dtype=float).reshape(2, 3),
                x_values=np.asarray((0.0, 1.0, 3.0)),
                y_values=np.asarray((10.0, 20.0)),
            )

            self.assertTrue(np.allclose(plot._x_edges, (-0.5, 0.5, 2.0, 4.0)))
            self.assertEqual(plot._cell_indices(0.75, 14.0), (0, 1))
        finally:
            plot.close()

    def test_shell_live_theme_switch_rethemes_embedded_heatmap(self) -> None:
        window = MainWindow(".config/settings.yml", simulation=True)
        try:
            window.resize(1280, 720)
            window.show()
            window._navigate_to("results")
            self.application.processEvents()
            for name in ("light", "dark"):
                window._set_theme_mode(name, persist=False)
                self.application.processEvents()
                heatmap = window.results_page.heatmap_tab.heatmap
                self.assertEqual(heatmap._theme_name, name)
                self.assertEqual(
                    heatmap.plot.getAxis("bottom").pen().color().name(),
                    plot_theme(tokens_for(name)).axes,
                )
        finally:
            window._set_theme_mode("system", persist=False)
            window.close()


if __name__ == "__main__":
    unittest.main()
