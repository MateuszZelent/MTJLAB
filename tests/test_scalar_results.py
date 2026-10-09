"""Scalar HDF5 round trips, nested/repeated curves, and rendered Results UI."""
import csv
import hashlib
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from app.domain.models import MeasurementPoint
from app.storage import Hdf5RunReader, Hdf5RunWriter, ThatecRunReader
from app.ui.results.scalar_data import ScalarColumn, read_scalar_columns, scalar_curves
from app.ui.results.scalar_tab import ScalarResultsTab
from tests.test_spectrum_correction_controller import wait_until


@pytest.fixture
def scalar_file(tmp_path):
    path = tmp_path / "scalars.h5"
    writer = Hdf5RunWriter(path, recipe_source="schema_version: 1\nname: scalars\nroot: {id: main, type: wait, duration: 1 ms}\n",
                          settings_source="profile: {id: test, name: Test}\n", plan_hash="a" * 64,
                          device_idn={"keithley": "KEITHLEY,2602A,1,1"})
    index = 0
    for _pass in range(2):
        for b in (-.004, -.005):
            for a in (.002, .003, .004):
                writer.append(MeasurementPoint(index=index, setpoints={"keithley.A.current": a, "keithley.B.current": b},
                                               measurements={"keithley.A.current_a": a, "keithley.A.voltage_v": 10 * a,
                                                             "keithley.A.power_w": 10 * a * a}))
                index += 1
    writer.close("completed")
    return path


@pytest.mark.parametrize("private", [False, True])
def test_scalar_reader_preserves_units_coordinates_and_repeated_passes(scalar_file, private):
    before = hashlib.sha256(scalar_file.read_bytes()).digest()
    run = ThatecRunReader.describe(scalar_file)
    points = Hdf5RunReader.points(scalar_file) if private else ()
    columns = {column.id: column for column in read_scalar_columns(scalar_file, run, points)}
    x, y, group = (columns[key] for key in ("setpoint:keithley.A.current", "measurement:keithley.A.voltage_v", "setpoint:keithley.B.current"))
    assert x.unit == "A" and y.unit == "V" and group.unit == "A"
    assert x.values.tolist() == [.002, .003, .004] * 4
    assert y.values.tolist() == pytest.approx([.02, .03, .04] * 4)
    curves = scalar_curves(x, y, group)
    assert len(curves) == 4
    assert sorted(len(indices) for _, _, indices in curves) == [3] * 4
    assert sorted(pass_index for _, pass_index, _ in curves) == [1, 1, 2, 2]
    assert hashlib.sha256(scalar_file.read_bytes()).digest() == before


def test_missing_samples_are_not_averaged_or_connected_across_gap():
    x = ScalarColumn("x", "X", "A", "setpoint", np.array([3., 2., 1., 0.]))
    y = ScalarColumn("y", "Y", "V", "measurement", np.array([1., 2., np.nan, 4.]))
    curves = scalar_curves(x, y)
    assert [indices.tolist() for _, _, indices in curves] == [[0, 1], [3]]


def test_results_page_hosts_scalar_tab_and_discards_stale_load(scalar_file, tmp_path):
    from app.ui.results.page import ResultsPage
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtTest import QTest
    from app.ui.design_system import apply_application_theme
    app = QApplication.instance() or QApplication([])
    if not QFontDatabase.families() and Path("C:/Windows/Fonts/arial.ttf").exists():
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/arial.ttf")
    app.setFont(QFont("Arial", 10))
    apply_application_theme(app, "light")
    page = ResultsPage(str(tmp_path))
    page.resize(1040, 820)
    page.show()
    try:
        page.open_result_file(scalar_file)
        wait_until(app, lambda: page.scalar_tab.y_axis.isEnabled())
        page.result_tabs.setCurrentIndex(page._scalar_index)
        QTest.qWait(250)  # Let the Fluent selection indicator finish its transition.
        app.processEvents()
        assert page.scalar_tab.isVisible()
        assert page.scalar_tab.plot.isVisible()
        assert page.scalar_tab.plot.width() > 0 and page.scalar_tab.plot.height() > 0
        controls = page.scalar_tab.controls
        button = page.scalar_tab.export_button
        assert button.mapTo(controls, button.rect().bottomRight()).y() <= controls.height()
        assert page.grab().save(str(tmp_path / "results-scalars-integrated.png"))
        old_request = page.scalar_tab._request_id
        old_columns = page.scalar_tab._columns
        page.scalar_tab.clear()
        page.scalar_tab._loaded(old_request, old_columns)
        assert not page.scalar_tab._columns and not page.scalar_tab.export_button.isEnabled()
    finally:
        assert page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("width,theme", [(1360, "light"), (820, "dark")])
def test_scalar_tab_renders_and_exports_values_in_recorded_units(scalar_file, tmp_path, width, theme):
    app = QApplication.instance() or QApplication([])
    from PySide6.QtGui import QFont, QFontDatabase
    from app.ui.design_system import apply_application_theme
    if not QFontDatabase.families() and Path("C:/Windows/Fonts/arial.ttf").exists():
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/arial.ttf")
    app.setFont(QFont("Arial", 10))
    apply_application_theme(app, theme)
    tab = ScalarResultsTab()
    tab.resize(width, 780)
    tab.apply_theme(theme)
    tab.show()
    try:
        tab.load(scalar_file, ThatecRunReader.describe(scalar_file), Hdf5RunReader.points(scalar_file))
        wait_until(app, lambda: tab.y_axis.isEnabled())
        tab.x_axis.setCurrentIndex(tab.x_axis.findData("setpoint:keithley.A.current"))
        tab.y_axis.setCurrentIndex(tab.y_axis.findData("measurement:keithley.A.voltage_v"))
        tab.group_by.setCurrentIndex(tab.group_by.findData("setpoint:keithley.B.current"))
        app.processEvents()
        assert len(tab._curves) == 4
        assert tab.plot.isVisible() and tab.plot.height() > 220
        assert tab.export_button.isVisible() and tab.export_button.isEnabled()
        assert tab.export_button.mapTo(tab.controls, tab.export_button.rect().bottomRight()).y() <= tab.controls.height()
        assert tab.grab().save(str(tmp_path / f"scalar-{width}-{theme}.png"))
        output = tmp_path / "plot.csv"
        tab.export_csv(output)
        with output.open(encoding="utf-8") as stream:
            records = list(csv.reader(stream))
        assert len(records) == 13
        assert "[A]" in records[0][1] and "[V]" in records[0][2]
        assert sorted(float(row[1]) for row in records[1:]) == sorted([.002, .003, .004] * 4)
        tab.clear()
        assert not tab.export_button.isEnabled() and not tab._columns
    finally:
        tab.cancel_read()
        tab._read_pool.waitForDone(5000)
        tab.close()
        tab.deleteLater()
        app.processEvents()
        apply_application_theme(app, "light")
