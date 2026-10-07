"""Live characterization never fabricates resistance or destroys active workers."""

import math
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app.devices.keithley_2600.characterization.models import CharacterizationPoint
from app.devices.keithley_2600.ui.characterization_card import KeithleyCharacterizationCard
from app.devices.keithley_2600.ui.twin_axis_plot import KeithleyTwinAxisPlotWidget
from app.devices.simulators import simulated_station_settings
from tests.helpers import loaded_settings


@pytest.fixture(scope="module")
def qt_application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def card(monkeypatch, tmp_path, qt_application):
    monkeypatch.setattr("app.devices.keithley_2600.ui.characterization_card.QSettings", lambda *_: QSettings(str(tmp_path / "drafts.ini"), QSettings.Format.IniFormat))
    widget = KeithleyCharacterizationCard(Mock(), simulated_station_settings(loaded_settings()))
    yield widget
    widget._worker = widget._field_worker = widget._field_report_worker = widget._field_recovery_worker = None
    widget._field_lease = None
    widget._temporary_policy_phase = "idle"
    widget.close()
    widget.deleteLater()
    qt_application.processEvents()


@pytest.mark.parametrize("mode", ["current", "voltage"])
def test_live_curves_preserve_undefined_and_invalid_points(card, qt_application, mode):
    card._worker = SimpleNamespace(_config=SimpleNamespace(mode=mode))
    expected = [math.nan, 100.0, math.nan, math.nan, 200.0]
    for index, (resistance, valid) in enumerate([(math.nan, True), (100., True), (math.inf, True), (150., False), (200., True)]):
        card._on_point_acquired(CharacterizationPoint(
            index=index, demanded_si=index * .001,
            measured_voltage_v=.1, measured_current_a=.001,
            true_resistance_ohm=resistance, apparent_resistance_ohm=resistance,
            power_w=.0001, compliance_active=False, timestamp_epoch=float(index), valid=valid,
        ))
    card._worker = None
    card.resize(1366, 768)
    card.show()
    qt_application.processEvents()
    assert card.isVisible() and card.width() > 0
    for curve in (card.curve_r_true, card.curve_r_app):
        x, y = curve.getData()
        assert len(x) == 5
        np.testing.assert_allclose(y, expected, equal_nan=True)
        assert curve.opts["connect"] == "finite"
    _, y = card.curve_iv.getData()
    assert math.isnan(y[3])
    assert np.isfinite(y[[0, 1, 2, 4]]).all()
    assert card.curve_iv.opts["connect"] == "finite"


@pytest.mark.parametrize("active", ["_worker", "_field_worker", "_field_report_worker", "_field_recovery_worker", "_field_lease", "policy"])
def test_close_retains_card_until_all_work_and_restoration_finish(card, qt_application, active):
    card.show()
    qt_application.processEvents()
    worker = Mock()
    worker.isRunning.return_value = True
    if active == "policy":
        card._temporary_policy_phase = "restoring"
    elif active == "_field_lease":
        card._field_lease = object()
    else:
        setattr(card, active, worker)
    assert not card.close()
    assert card.isVisible()
    worker.wait.assert_not_called()
    if active in {"_worker", "_field_worker"}:
        worker.request_stop.assert_called_once()
    if active == "policy":
        card._temporary_policy_phase = "idle"
    elif active == "_field_lease":
        card._field_lease = None
    else:
        worker.isRunning.return_value = False
    assert card.close()


def test_twin_axis_preserves_each_quantity_and_missing_latest_readout(qt_application):
    plot = KeithleyTwinAxisPlotWidget("A")
    try:
        plot.resize(800, 400)
        plot.show()
        plot.set_data([0, 1, 2, 3, 4], [.1, .2, math.nan, .4, .5], [.001, math.nan, .003, .004, math.nan], [False, True, True, False, False])
        qt_application.processEvents()
        np.testing.assert_allclose(plot._voltage_curve.getData()[1], [.1, .2, math.nan, .4, .5], equal_nan=True)
        np.testing.assert_allclose(plot._current_curve.getData()[1], [.001, math.nan, .003, .004, math.nan], equal_nan=True)
        assert plot._voltage_curve.opts["connect"] == plot._current_curve.opts["connect"] == "finite"
        assert plot._voltage_compliance_scatter.getData()[0].tolist() == [1.]
        assert plot._current_compliance_scatter.getData()[0].tolist() == [2.]
        assert "I: —" in plot.readout_label.text()
        assert "V: —" not in plot.readout_label.text()
        assert plot.isVisible() and plot.plot.height() > 0
        plot.set_data([math.nan], [1], [1])
        assert plot.readout_label.text() == "V: —   I: —"
        with pytest.raises(ValueError, match="aligned"):
            plot.set_data([1, 2], [1], [1, 2])
    finally:
        plot.close()
        plot.deleteLater()
