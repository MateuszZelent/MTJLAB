"""Shown geometry, theme contrast and frozen quantitative preview regression."""

import os
import csv
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.ui.page import AnritsuPage, AnritsuPageState
from app.settings import SettingsRepository
from app.ui.design_system import apply_application_theme
from app.ui.widgets import SpectrumPlotWidget
from tests.helpers import SETTINGS_TEMPLATE
from tests.test_spectrum_correction_store import fixture_profile, signal_fixture


def test_committed_view_arrives_without_render_timer_and_freeze_only_stops_drawing(tmp_path):
    import time
    from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
    from app.devices.anritsu_ms2830a.ui.correction_controller import CorrectionViewSnapshot
    from tests.helpers import simulation_settings

    application = QApplication.instance() or QApplication([])
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    if font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
        application.setFont(QFont("Segoe UI", 10))
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    context, profile = fixture_profile()
    raw, _envelope, result = signal_fixture(context, profile)
    try:
        workspace.resize(1000, 800)
        workspace.show()
        workspace._render_timer.stop()
        workspace.freeze.setChecked(True)
        workspace._context, workspace._profile = context, profile
        workspace._archive_path = tmp_path / "committed.h5"
        workspace._started_monotonic = time.monotonic()
        workspace._kind = "signal"
        workspace._request = MagicMock()
        workspace._processed("frame", {"committed_point_count": 1, "accepted": True,
            "view": CorrectionViewSnapshot(result, raw)})
        application.processEvents()
        assert workspace._latest_result is result and workspace._latest_raw is raw
        assert workspace._result_archive_path == workspace._archive_path
        assert workspace._dirty_view
        assert workspace.corrected_plot.trace_point_count("Signed residual") == 0
        workspace.freeze.setChecked(False)
        application.processEvents()
        assert not workspace._dirty_view
        assert workspace.corrected_plot.isVisible() and workspace.corrected_plot.height() > 70
        np.testing.assert_array_equal(workspace.corrected_plot._traces["Signed residual"][1], result.values_w)
        assert workspace.corrected_plot._traces["Signed residual"][1][1] < 0
    finally:
        assert workspace.shutdown()
        workspace.close()
        workspace.deleteLater()
        application.processEvents()






@pytest.mark.parametrize("values", [(-1e-18, 0, 1e-18), (1e-18, 1e-18, 1e-18),
                                   (-1e-18, -1e-18, -1e-18), (0, 0, 0)])
def test_signed_watt_plot_reset_fits_weak_values_instead_of_one_watt(values):
    application = QApplication.instance() or QApplication([])
    plot = SpectrumPlotWidget()
    try:
        plot.set_labels(y="Signed residual", y_unit="W")
        plot.resize(1000, 650)
        plot.show()
        plot.set_trace("Residual", [1e6, 2e6, 3e6], values, primary=True)
        plot.auto_range()
        application.processEvents()
        x_range, y_range = plot.plot.viewRange()
        assert x_range[0] < 1e6 < 3e6 < x_range[1]
        assert y_range[0] <= min(values) <= max(values) <= y_range[1]
        assert y_range[1] - y_range[0] < (3e-15 if not any(values) else 3e-18)
        np.testing.assert_array_equal(plot._traces["Residual"][1], values)
    finally:
        plot.close()
        plot.deleteLater()
        application.processEvents()




def test_dense_curve_preserves_single_bin_peaks_full_arrays_and_csv_across_theme_change(tmp_path):
    application = QApplication.instance() or QApplication([])
    plot = SpectrumPlotWidget(csv_value_column="signed_power_w")
    x = np.linspace(1e6, 6e9, 10001)
    y = np.zeros(10001)
    y[4311], y[7432] = 1e-12, -2e-12
    try:
        plot.set_labels(y="Signed residual", y_unit="W")
        plot.resize(1100, 650)
        plot.show()
        plot.set_trace("Dense", x, y, primary=True)
        plot.auto_range()
        application.processEvents()
        plot.apply_theme("dark")
        np.testing.assert_array_equal(plot._traces["Dense"][1], y)
        np.testing.assert_array_equal(plot._curves["Dense"].yData, y)
        plot.peak_search()
        assert plot.marker.value() == x[4311]
        path = tmp_path / "full-spectrum.csv"
        plot._export_csv(path)
        exported = np.loadtxt(path, delimiter=",", skiprows=1, usecols=(1, 2))
        np.testing.assert_array_equal(exported[:, 0], x)
        np.testing.assert_array_equal(exported[:, 1], y)
        assert plot._curves["Dense"].opts["pen"].widthF() == 1.0
    finally:
        plot.close()
        plot.deleteLater()
        application.processEvents()
