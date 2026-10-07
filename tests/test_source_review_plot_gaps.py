"""Rendered gaps and hold frames retain the meaning of missing data and axes."""
import numpy as np

from app.ui.widgets import SpectrumPlotWidget
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


def test_missing_sample_stays_a_visible_gap_and_exports_as_missing(shell_qt_application, tmp_path):
    widget = SpectrumPlotWidget()
    try:
        widget.resize(1000, 650)
        widget.show()
        widget.set_trace("Raw", [0, 1, 2, 3, 4], [1, 2, np.nan, 4, 5], primary=True)
        shell_qt_application.processEvents()
        curve = widget._curves["Raw"]
        assert np.isnan(curve.yData[2]) and curve.xData.size == 5
        assert curve.opts["connect"] == "finite"
        path = curve.curve.getPath()
        for index in range(1, path.elementCount()):
            before, after = path.elementAt(index - 1), path.elementAt(index)
            assert not (before.x == 1 and after.x == 3 and after.isLineTo())
        assert widget.trace_point_count("Raw") == 4
        target = tmp_path / "gap.csv"
        widget._export_csv(target)
        assert "nan" in target.read_text(encoding="utf-8")
        widget.set_trace("Raw", [0, 1], [np.nan, np.nan], primary=True)
        widget.peak_search()  # Empty finite data must not raise nanargmax.
        assert widget.trace_point_count("Raw") == 0
        assert widget.width() >= 1000
    finally:
        widget.close()
        widget.deleteLater()


def test_base_hold_resets_for_changed_axis_of_same_length(shell_qt_application):
    widget = SpectrumPlotWidget()
    try:
        widget.set_trace("Raw", [1, 2, 3], [10, 20, 30], primary=True)
        widget.toggle_max_hold()
        widget.toggle_min_hold()
        widget.set_trace("Raw", [4, 5, 6], [1, 2, 3], primary=True)
        for name in ("Max hold", "Min hold"):
            np.testing.assert_array_equal(widget._traces[name][0], [4, 5, 6])
            np.testing.assert_array_equal(widget._traces[name][1], [1, 2, 3])
    finally:
        widget.close()
        widget.deleteLater()


def test_hold_resets_when_units_or_primary_source_change(shell_qt_application):
    widget = SpectrumPlotWidget()
    try:
        widget.set_trace("Raw", [1, 2, 3], [10, 20, 30], primary=True)
        widget.toggle_max_hold()
        widget.set_labels(y_unit="W")
        assert "Max hold" not in widget._traces
        widget.set_trace("Raw", [1, 2, 3], [1e-12, 2e-12, 3e-12], primary=True)
        widget.toggle_max_hold()
        np.testing.assert_array_equal(widget._traces["Max hold"][1], [1e-12, 2e-12, 3e-12])
        widget.set_trace("Residual", [1, 2, 3], [-1e-12, 0, 1e-12], primary=True)
        assert "Max hold" not in widget._traces
        widget.toggle_max_hold()
        np.testing.assert_array_equal(widget._traces["Max hold"][1], [-1e-12, 0, 1e-12])
    finally:
        widget.close()
        widget.deleteLater()
