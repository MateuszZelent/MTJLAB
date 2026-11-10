"""Paired, rendered Anritsu/Results reference controls without device or file I/O."""

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtCore import QEvent
from PySide6.QtTest import QTest

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from app.settings import SettingsRepository
from app.storage.hdf5_reader import Hdf5RunReader, StoredPoint, StoredReference, StoredSpectrum
from app.ui.results.read_session import ResultReadSession
from app.ui.results.spectrum_tab import SpectrumResultsTab
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414


def wait_for_reads(app, tab):
    for _ in range(500):
        app.processEvents()
        if not tab._read_tasks:
            return
        QTest.qWait(10)
    pytest.fail(f"Results reference read did not finish: {tab.spectrum_info.text()}")


@pytest.fixture
def paired_pages(shell_qt_application, monkeypatch):
    app = shell_qt_application
    frequencies = (1e6, 2e6, 3e6, 4e6, 5e6)
    acquired = datetime(2026, 11, 3, tzinfo=UTC)
    raw = SpectrumTrace(frequencies, (-60., -59., -61., -58., -62.), acquired, "TRAC1")
    reference = SpectrumTrace(frequencies, (-60.,) * 5, acquired, "TRAC1")
    baseline = StoredReference(1, "TRAC1", acquired.isoformat(), "averaged", 4,
                               frequencies, reference.powers_dbm, purpose="reference")
    point = StoredPoint(0, acquired.isoformat(), "completed", {}, {}, {}, {}, True)
    stored = StoredSpectrum(0, "TRAC1", acquired.isoformat(), frequencies, raw.powers_dbm, 5)
    session = ResultReadSession(Path("in-memory-reference-parity.h5"), references=(baseline,))
    # Exercise the real session/worker/DSP path using its bounded cache.
    session.cached(("point", 0), lambda: point)
    session.cached(("raw", 0), lambda: stored)
    session.cached(("reference", 1), lambda: baseline)

    def unexpected_io(*_args, **_kwargs):
        raise AssertionError("Paired page regression must not read an archive")

    for method in ("point", "spectrum", "reference", "reference_sweep", "references"):
        monkeypatch.setattr(Hdf5RunReader, method, unexpected_io)
    controller = MagicMock()
    controller.is_connected = False
    controller.visa_address = "SIM::NO_DEVICE_IO"
    controller.call.side_effect = AssertionError("Paired page regression must not contact devices")
    settings = SettingsRepository("app/resources/settings.template.yml").load().settings
    main = AnritsuPage(controller, settings, single_sweep_available=True)
    results = SpectrumResultsTab()
    try:
        main._latest_trace = raw
        main._reference_trace = reference
        # A distinct average catches accidentally selecting it instead of raw.
        main._averaged_trace = SpectrumTrace(frequencies, (-40.,) * 5, acquired, "AVG")
        results._selected_path = session.path
        results._session = session
        results.view_controls.set_references((baseline,))
        main.resize(1600, 1000)
        results.resize(1600, 1000)
        main.show()
        results.show()
        app.processEvents()
        main._refresh_spectrum_display(auto_range=True)
        results._render_stored_spectrum(point, stored)
        app.processEvents()
        yield app, main, results
        controller.call.assert_not_called()
    finally:
        results._invalidate_pending_reads()
        assert results._read_pool.waitForDone(5000)
        # The page can defer close while its real CPU workers stop.
        for _ in range(500):
            if main.close():
                break
            QTest.qWait(10)
        else:
            pytest.fail("Anritsu page workers did not stop during teardown")
        assert results.close()
        main.deleteLater()
        results.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def assert_rendered_parity(app, main, results, unit):
    wait_for_reads(app, results)
    app.processEvents()
    assert main.spectrum_host.currentWidget() is main.spectrum_plot
    assert results.spectrum_view.currentWidget() is results.spectrum_plot, results.spectrum_info.text()
    assert main._display_state.available_keys == ("processed",)
    assert main._display_state.by_key["processed"].unit == unit
    assert not any(box.isChecked() for box in main.quick_curves.values())
    visible = []
    for plot in (main.spectrum_plot, results.spectrum_plot):
        assert plot.isVisible() and plot.width() > 300 and plot.height() > 100
        curves = {name: curve for name, curve in plot._curves.items()
                  if curve.isVisible() and name in plot._traces}
        assert len(curves) == 1, tuple(curves)
        assert plot._y_unit == plot.plot.getAxis("left").labelUnits == unit
        assert plot.plot.getAxis("bottom").labelUnits == "Hz"
        curve = next(iter(curves.values()))
        x, y = curve.getData()
        assert x is not None and y is not None
        visible.append((x, y))
        limits = plot.plot.viewRange()[1]
        finite = y[np.isfinite(y)]
        assert finite.size and limits[0] < finite.min() <= finite.max() < limits[1]
    np.testing.assert_array_equal(visible[0][0], visible[1][0])
    np.testing.assert_array_equal(visible[0][1], visible[1][1])
    np.testing.assert_allclose(main.spectrum_plot.plot.viewRange(),
                               results.spectrum_plot.plot.viewRange(), rtol=1e-12, atol=0)
    return visible[1][1]


def test_raw_analysis_reference_click_sequence_keeps_analyzer_raw_in_dbm(paired_pages):
    app, main, results = paired_pages
    main.show_average.setChecked(False)
    main.show_raw.setChecked(True)
    main._refresh_spectrum_display(auto_range=True)
    expected = np.asarray(main._latest_trace.powers_dbm)

    results.view_controls.boxes["show_raw"].click()
    wait_for_reads(app, results)
    assert results.spectrum_plot._y_unit == main._display_state.by_key["raw"].unit == "dBm"
    np.testing.assert_array_equal(results.spectrum_plot._traces["Raw"][1], expected)
    np.testing.assert_allclose(results.spectrum_plot.plot.viewRange(),
                               main.spectrum_plot.plot.viewRange(), rtol=1e-12, atol=0)

    results.view_controls.boxes["show_raw"].click()
    wait_for_reads(app, results)
    assert results.view_controls.boxes["show_analysis"].isChecked()
    assert results.spectrum_plot._y_unit == "dBm"
    np.testing.assert_array_equal(results.spectrum_plot._traces["Stored spectrum"][1], expected)

    main.correction_controls.reference.click()
    results.view_controls.boxes["show_reference"].click()
    np.testing.assert_array_equal(assert_rendered_parity(app, main, results, "dB"),
                                  (0., 1., -1., 2., -2.))


def test_reference_toggle_defaults_to_same_rendered_subtract_db(paired_pages):
    app, main, results = paired_pages
    main.correction_controls.reference.click()
    results.view_controls.boxes["show_reference"].click()
    assert main.reference_operation.currentData() == "difference_db"
    assert results.view_controls.reference_operation.currentData() == "difference_db"
    assert main.reference_operation.currentText() == "Subtract [dB]"
    assert results.view_controls.reference_operation.currentText() == "Subtract [dB]"
    values = assert_rendered_parity(app, main, results, "dB")
    np.testing.assert_array_equal(values, (0., 1., -1., 2., -2.))
    np.testing.assert_allclose(results.spectrum_plot.plot.viewRange()[1], (-2.2, 2.2),
                               rtol=1e-12, atol=0)
    for key in ("show_raw", "show_background", "show_background_spectrum", "show_reference_spectrum",
                "show_analysis"):
        assert not results.view_controls.boxes[key].isChecked()
        assert not results.view_controls.boxes[key].isEnabled()
    assert not results.view_controls.unit.isEnabled()


@pytest.mark.parametrize("operation,unit", [
    ("difference_db", "dB"),
    ("ratio_linear", "ratio"),
    ("add_power", "dBm"),
    ("subtract_power", "dBm"),
    ("multiply_linear", "mW²"),
])
def test_all_normal_reference_operations_match_on_actual_pages(paired_pages, operation, unit):
    app, main, results = paired_pages
    main.correction_controls.reference.click()
    results.view_controls.boxes["show_reference"].click()
    main_index = main.reference_operation.findData(operation)
    results_index = results.view_controls.reference_operation.findData(operation)
    assert main_index >= 0 and results_index >= 0
    main.reference_operation.setCurrentIndex(main_index)
    results.view_controls.reference_operation.setCurrentIndex(results_index)
    assert main.reference_operation.currentText() == results.view_controls.reference_operation.currentText()
    values = assert_rendered_parity(app, main, results, unit)
    if operation == "subtract_power":
        np.testing.assert_array_equal(np.isnan(values), (True, False, True, False, True))
