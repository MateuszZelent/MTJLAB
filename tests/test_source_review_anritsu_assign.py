from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from app.devices.simulators import SimulatedVisaFactory
from tests.helpers import loaded_settings, simulation_settings


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    previous = application.font()
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
    application.setFont(QFont("Segoe UI", 10))
    yield application
    application.setFont(previous)


@pytest.fixture
def page(app):
    widget = AnritsuPage(Mock(), loaded_settings(), single_sweep_available=True)
    yield widget
    if widget._readback_dialog is not None:
        widget._readback_dialog.close()
    assert widget.shutdown_analysis()
    widget.close()
    widget.deleteLater()
    app.processEvents()


@pytest.fixture
def readback():
    adapter = AnritsuAdapter(simulation_settings(), session_factory=SimulatedVisaFactory("anritsu"))
    adapter.connect()
    try:
        return adapter.read_full_configuration()
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("representation", ["start_stop", "center_span"])
@pytest.mark.parametrize("parameter,value,expected", [
    ("start_hz", 2e6, (2e6, 10e6)),
    ("stop_hz", 12e6, (1e6, 12e6)),
    ("center_hz", 6e6, (1.5e6, 10.5e6)),
    ("span_hz", 4e6, (3.5e6, 7.5e6)),
])
def test_frequency_assignment_keeps_semantics_in_both_representations(page, representation, parameter, value, expected):
    page.frequency_representation.setCurrentIndex(page.frequency_representation.findData(representation))
    page._set_frequency_bounds(1e6, 10e6)
    page._controller.call.reset_mock()
    page._apply_readback_parameter(parameter, value)
    assert page._spectrum_frequency_bounds() == pytest.approx(expected)
    page._controller.call.assert_not_called()


def test_invalid_frequency_assignment_preserves_form(page):
    page._set_frequency_bounds(1e6, 10e6)
    before = (page.start.text(), page.stop.text())
    with pytest.raises(ValueError):
        page._apply_readback_parameter("start_hz", 20e6)
    assert (page.start.text(), page.stop.text()) == before


def test_dialog_uses_actual_form_after_each_request(page, readback, app):
    readback = replace(readback, points=123, stop_hz=12e6, center_hz=6.5e6, span_hz=11e6)
    page._set_frequency_bounds(1e6, 10e6)
    page._show_full_readback_dialog(readback)
    dialog = page._readback_dialog
    dialog.resize(1000, 720)
    app.processEvents()
    dialog._action_buttons["points"].click()
    assert dialog._status_items["points"].text() != "MATCH"
    assert dialog._action_buttons["points"].isEnabled()
    dialog._action_buttons["stop_hz"].click()
    assert dialog._status_items["stop_hz"].text() == "MATCH"
    assert dialog._status_items["center_hz"].text() == "MATCH"
    assert dialog._status_items["span_hz"].text() == "MATCH"
    assert dialog.table.isVisible()
    assert dialog.table.width() > 500
    assert dialog.table.height() > 200
    target = Path("artifacts/source-review/anritsu-readback.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    assert dialog.grab().save(str(target))


def test_use_all_does_not_claim_unsupported_point_count(page, readback):
    page._show_full_readback_dialog(replace(readback, points=123))
    dialog = page._readback_dialog
    dialog.use_all_button.click()
    assert dialog._status_items["points"].text() != "MATCH"
    assert dialog._action_buttons["points"].isEnabled()


@pytest.mark.parametrize("use_all", [False, True])
def test_vbw_off_is_copied_as_off(page, readback, use_all):
    page._show_full_readback_dialog(replace(readback, vbw_auto=False, vbw_hz=None))
    dialog = page._readback_dialog
    if use_all:
        dialog.use_all_button.click()
    else:
        dialog._action_buttons["vbw_hz"].click()
    assert page.vbw_auto.currentData() == "off"
    assert dialog._status_items["vbw_hz"].text() == "MATCH"
    assert dialog._status_items["vbw_auto"].text() == "MATCH"
