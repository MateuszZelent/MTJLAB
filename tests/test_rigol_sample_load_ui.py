"""Rendered sample estimate, unit validation, isolation and per-channel defaults."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from app.devices.rigol_dg1000z.ui.page import RigolPage
from app.settings.repository import SettingsRepository
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        font = Path("C:/Windows/Fonts") / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont("Segoe UI", 10))
    return app


@pytest.fixture
def page(application):
    widget = RigolPage(Mock(), simulation_settings())
    widget.waveform.setCurrentText("DC")
    widget.offset.setText("82 mV")
    widget.load.setText("HIGHZ")
    yield widget
    widget.close()
    widget.deleteLater()
    application.processEvents()


def set_resistances(page):
    widget = page.sample_load_estimate
    widget.resistance.setText("0.175 kohm")
    widget.minimum.setText("150 ohm")
    widget.maximum.setText("200 ohm")
    return widget


def test_live_preview_tracks_dc_ac_load_and_clears_invalid_inputs(page):
    widget = set_resistances(page)
    assert widget.estimate.resistance_range.peak_current_a == pytest.approx(.000410)
    page.load.setText("50")
    assert widget.estimate.resistance_range.peak_current_a == pytest.approx(.000820)
    page.load.setText("HIGHZ")
    page.waveform.setCurrentText("SIN")
    page.vpp.setText("600 mV")
    page.offset.setText("-100 mV")
    assert widget.estimate.resistance_range.peak_current_a == pytest.approx(.002)
    assert widget.estimate.resistance_range.peak_voltage_v == pytest.approx(.32)
    page.offset.setText("bad voltage")
    assert widget.estimate is None
    assert all(label.text() == "—" for label in widget.values.values())
    page.offset.setText("0 V")
    assert widget.estimate is not None
    page.load.setText("not a load")
    assert widget.estimate is None
    page._controller.call.assert_not_called()


@pytest.mark.parametrize("value", ["175", "175 mV", "0 ohm", "-1 ohm", "nan ohm", "inf ohm"])
def test_resistance_validation_clears_previous_results(page, value):
    widget = set_resistances(page)
    assert widget.estimate is not None
    widget.resistance.setText(value)
    assert widget.estimate is None
    assert all(label.text() == "—" for label in widget.values.values())


def test_optional_range_and_reversed_bounds(page):
    widget = page.sample_load_estimate
    widget.resistance.setText("175 ohm")
    assert widget.estimate.nominal == widget.estimate.resistance_range
    widget.minimum.setText("150 ohm")
    assert widget.estimate is None
    widget.maximum.setText("100 ohm")
    assert widget.estimate is None
    widget.maximum.setText("200 ohm")
    assert widget.estimate is not None


def test_resistance_edits_cannot_dispatch_live_setpoints_or_change_limits(page):
    page._device_state_changed("output_on")
    page._set_rigol_channel_output(1, True)
    page.set_live_control_enabled(True)
    before = page._station_settings.model_dump()
    spy = QSignalSpy(page.quick_setpoint_requested)
    page._controller.reset_mock()
    widget = set_resistances(page)
    for field in (widget.resistance, widget.minimum, widget.maximum):
        field.editingFinished.emit()
        field.returnPressed.emit()
    assert widget.estimate is not None
    assert spy.count() == 0
    assert page._station_settings.model_dump() == before
    page._controller.call.assert_not_called()


@pytest.mark.parametrize("mode", ["modulation", "sweep", "burst", "gated", "NOIS", "USER"])
def test_unsupported_modes_clear_estimates(page, mode):
    widget = set_resistances(page)
    if mode in {"NOIS", "USER"}:
        page.waveform.setCurrentText(mode)
    elif mode == "gated":
        page.output_mode.setCurrentText("GAT")
    else:
        checkbox = getattr(page, {"modulation": "mod_enabled"}.get(mode, f"{mode}_enabled"))
        checkbox.setChecked(True)
        assert widget.estimate is None
        checkbox.setChecked(False)
        assert widget.estimate is not None
        page._confirmed_advanced_states[1][mode] = True
        page._refresh_confirmed_advanced_controls()
    assert widget.estimate is None
    page._controller.call.assert_not_called()


def test_channel_inputs_survive_settings_save_reload_without_entering_commands(page, application, tmp_path):
    widget = set_resistances(page)
    page.channel.setCurrentText("2")
    assert widget.resistance.text() == ""
    widget.resistance.setText("300 ohm")
    page.channel.setCurrentText("1")
    assert widget.resistance.text() == "0.175 kohm"
    defaults = page.settings_defaults()
    raw = simulation_settings().model_dump(mode="python")
    for channel, values in defaults.items():
        raw["devices"]["rigol"]["safety"]["channels"][str(channel)]["defaults"].update(values)
    repository = SettingsRepository(tmp_path / "settings.yml")
    repository.save_raw(raw)
    reloaded = RigolPage(Mock(), repository.load().settings)
    try:
        assert reloaded.sample_load_estimate.input_defaults() == widget.input_defaults()
        reloaded.channel.setCurrentText("2")
        assert reloaded.sample_load_estimate.resistance.text() == "300 ohm"
        assert not hasattr(reloaded.configuration_snapshot(), "estimate_resistance")
        reloaded._controller.call.assert_not_called()
        page._controller.call.assert_not_called()
    finally:
        reloaded.close()
        reloaded.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("width,theme,waveform", [
    (1360, "light", "DC"), (1360, "dark", "DC"), (900, "light", "DC"),
    (1360, "light", "SIN"),
])
def test_estimate_rendered_and_readable(page, application, tmp_path, width, theme, waveform):
    apply_application_theme(application, theme)
    widget = set_resistances(page)
    page.waveform.setCurrentText(waveform)
    if waveform == "SIN":
        page.vpp.setText("60 mV")
        page.offset.setText("-10 mV")
    assert widget.estimate is not None
    page.resize(width, 1000)
    page.show()
    application.processEvents()
    page.basic_scroll.ensureWidgetVisible(widget)
    application.processEvents()
    assert widget.isVisibleTo(page)
    viewport = page.basic_scroll.viewport()
    for label in widget.values.values():
        assert label.width() > 45
        assert label.height() >= label.heightForWidth(label.width())
        rect = label.rect().translated(label.mapTo(viewport, QPoint()))
        assert viewport.rect().contains(rect)
    assert page.output_off.isVisibleTo(page)
    assert page.grab().save(str(tmp_path / f"sample-estimate-{width}-{theme}-{waveform}.png"))
    page._controller.call.assert_not_called()
