from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
