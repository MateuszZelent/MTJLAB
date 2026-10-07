"""Identity qualification is not channel-specific output evidence."""

from unittest.mock import Mock

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from app.devices.keithley_2600.ui.page import KeithleyPage
from app.devices.rigol_dg1000z.ui.page import RigolPage
from app.devices.simulators import simulated_station_settings
from tests.helpers import loaded_settings


@pytest.fixture(scope="module")
def qt_application():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("page_type,channels", [(KeithleyPage, ("A", "B")), (RigolPage, (1, 2))])
@pytest.mark.parametrize("initial", [None, False, True])
def test_verified_preserves_output_evidence(page_type, channels, initial, monkeypatch, tmp_path, qt_application):
    monkeypatch.setattr("app.devices.keithley_2600.ui.page.QSettings", lambda *_: QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    page = page_type(Mock(), simulated_station_settings(loaded_settings()))
    try:
        page._output_states = {channel: bool(initial) for channel in channels}
        page._output_state_known = {channel: initial is not None for channel in channels}
        before = (dict(page._output_states), dict(page._output_state_known))
        page._device_state_changed("VERIFIED")
        assert (page._output_states, page._output_state_known) == before
        page._device_state_changed("OUTPUT_OFF")
        assert all(page._output_state_known.values())
        assert not any(page._output_states.values())
    finally:
        page.close()
        page.deleteLater()
        qt_application.processEvents()


@pytest.mark.parametrize("operation", ["configure", "configure_output", "set_output"])
def test_rigol_failed_operation_cannot_confirm_off_from_verified(operation, monkeypatch, qt_application):
    monkeypatch.setattr("app.devices.rigol_dg1000z.ui.page.QMessageBox.warning", lambda *_: None)
    page = RigolPage(Mock(), simulated_station_settings(loaded_settings()))
    try:
        page._device_state_changed("VERIFIED")
        page._set_rigol_channel_output(1, True)
        page._error(operation, "Readback timed out")
        assert page._output_state_known[1] is False
    finally:
        page.close()
        page.deleteLater()
        qt_application.processEvents()
