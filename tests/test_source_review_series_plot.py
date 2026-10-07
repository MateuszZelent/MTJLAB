"""Missing observations remain gaps in both inventory plot modes."""

import math

import pytest

from app.storage.hdf5_series_reader import MeasurementSeries
from app.ui.inventory.measurement_plot import MeasurementPlotWidget
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


@pytest.mark.parametrize("overlay", [False, True])
def test_shown_inventory_curve_does_not_connect_missing_observations(shell_qt_application, overlay):
    widget = MeasurementPlotWidget()
    series = MeasurementSeries("Missing observation", "Current", "A", (0, 1, 2, 3, 4),
                               "Voltage", "V", (1, 2, math.nan, 4, 5), 5)
    try:
        widget.resize(1000, 650)
        widget.show()
        if overlay:
            widget.set_multi_series([series, series])
        else:
            widget.set_series(series)
        shell_qt_application.processEvents()
        assert widget.plot.isVisible() and widget.plot.width() > 500
        for curve in widget._plot_items:
            assert curve.opts["connect"] == "finite"
            assert not curve.opts["autoDownsample"]
            assert not curve.opts["clipToView"]
            path = curve.curve.getPath()
            for i in range(1, path.elementCount()):
                before, after = path.elementAt(i - 1), path.elementAt(i)
                assert not (before.x == 1 and after.x == 3 and after.isLineTo())
    finally:
        widget.close()
        widget.deleteLater()
