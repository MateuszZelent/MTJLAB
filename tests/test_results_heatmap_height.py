"""The heatmap canvas uses 70% of its former available vertical area."""

import numpy as np
import pytest

from app.ui.design_system import apply_application_theme
from app.ui.results.heatmap_tab import HeatmapPlotWidget
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414


@pytest.mark.parametrize("theme,size", [("light", (1100, 640)), ("dark", (760, 480))])
def test_rendered_heatmap_is_thirty_percent_shorter(shell_qt_application, tmp_path, theme, size):
    app = shell_qt_application
    apply_application_theme(app, theme)
    view = HeatmapPlotWidget()
    data = np.arange(120, dtype=float).reshape(10, 12) - 80
    try:
        view.resize(*size)
        view.show()
        view.set_data(data, x_values=np.linspace(1e9, 2e9, 12),
                      y_values=np.linspace(.002, .004, 10), z_label="Power (dBm)")
        for _ in range(3):
            app.processEvents()
        canvas = view.plot.geometry()
        available = view.contentsRect().bottom() - canvas.top() + 1
        assert canvas.height() / available == pytest.approx(.70, abs=.02)
        assert canvas.width() >= view.width() - 4
        assert canvas.height() >= 150
        assert view.rect().contains(canvas)
        assert view.range_button.isVisible() and view.export_button.isVisible()
        np.testing.assert_array_equal(view._data, data)
        assert view.grab().save(str(tmp_path / f"heatmap-{theme}.png"))
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()
        apply_application_theme(app, "dark")
