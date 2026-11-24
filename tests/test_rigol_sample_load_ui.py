"""Rendered sample estimate, unit validation, isolation and per-channel defaults."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFont, QFontDatabase, QKeyEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from app.devices.rigol_dg1000z.adapter import RigolAdapter
from app.devices.rigol_dg1000z.module import _dispatch
from app.devices.rigol_dg1000z.ui.page import RigolPage
from app.devices.visa import FakeVisaSessionFactory
from app.domain.quantities import parse_quantity
from app.settings.models import StationSettings
from app.settings.repository import SettingsRepository
from app.ui.design_system import apply_application_theme
from app.ui.quick_controls import QuickControlCoordinator
from app.ui.workers import DeviceController
from tests.helpers import simulation_settings
from tests.test_quick_controls import _FakeController
from tests.test_rigol_dc_firmware_offset import DcOffsetIgnoringFirmware
from tests.test_spectrum_correction_controller import wait_until


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


def _repeat_up(editor):
    QApplication.sendEvent(editor, QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier, "", True,
    ))


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


def test_live_voltage_typing_sends_only_on_explicit_commit(page, application):
    page.offset.setText("1 mV")
    page._cache_applied_carrier(page._visible_channel_config())
    page._device_state_changed("output_on")
    page._set_rigol_channel_output(1, True)
    page.set_live_control_enabled(True)
    page.resize(1360, 880)
    page.show()
    application.processEvents()
    editor = page.offset.number
    editor.setFocus()
    editor.selectAll()
    spy = QSignalSpy(page.quick_setpoint_requested)
    QTest.keyClicks(editor, "2")
    QTest.qWait(450)
    QTest.keyClicks(editor, "0")
    QTest.qWait(450)
    page.channel.setFocus()
    application.processEvents()
    assert spy.count() == 0
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert spy.count() == 1
    assert spy.at(0) == ["rigol.1.offset", "20 mV"]


def test_waveform_apply_on_routes_one_voltage_change_without_reconfiguration(page, application):
    page._cache_applied_carrier(page._visible_channel_config())
    page._device_state_changed("output_on")
    page._set_rigol_channel_output(1, True)
    page.set_live_control_enabled(True)
    page.show()
    application.processEvents()
    spy = QSignalSpy(page.quick_setpoint_requested)
    page.offset.setText("94 mV")
    page.waveform_apply_button.click()
    assert spy.count() == 1
    assert spy.at(0) == ["rigol.1.offset", "94 mV"]
    page._controller.call.assert_not_called()
    assert page.output_state_snapshot()["1"] == "on"


def test_waveform_apply_on_rejects_changes_to_shape_or_both_voltage_controls(page, application):
    page.waveform.setCurrentText("SQU")
    page.vpp.setText("8 mV")
    page._cache_applied_carrier(page._visible_channel_config())
    page._device_state_changed("output_on")
    page._set_rigol_channel_output(1, True)
    page.set_live_control_enabled(True)
    page.show()
    application.processEvents()
    spy = QSignalSpy(page.quick_setpoint_requested)
    page.duty.setText("40")
    page.waveform_apply_button.click()
    page.duty.setText("50")
    page.vpp.setText("10 mV")
    page.offset.setText("84 mV")
    page.waveform_apply_button.click()
    assert spy.count() == 0
    page._controller.call.assert_not_called()
    assert page.output_state_snapshot()["1"] == "on"


def test_pressed_voltage_arrow_steps_one_mv_in_either_unit(page, application):
    page.offset.setText("0.082 V")
    page._cache_applied_carrier(page._visible_channel_config())
    page._device_state_changed("output_on")
    page._set_rigol_channel_output(1, True)
    page.set_live_control_enabled(True)
    page.show()
    application.processEvents()
    spy = QSignalSpy(page.quick_setpoint_requested)
    QTest.keyClick(page.offset.number, Qt.Key.Key_Up)
    assert spy.count() == 1
    assert parse_quantity(spy.at(0)[1]).si_value == pytest.approx(.083)
    QTest.keyClick(page.offset.number, Qt.Key.Key_Up)
    assert spy.count() == 1  # Do not step past an outstanding, unconfirmed value.
    page.quick_setpoint_value_read("rigol.1.offset", .083)
    page.offset.unit.setCurrentText("mV")
    assert spy.count() == 1
    QTest.keyClick(page.offset.number, Qt.Key.Key_Down, Qt.KeyboardModifier.KeypadModifier)
    assert spy.count() == 2
    assert parse_quantity(spy.at(1)[1]).si_value == pytest.approx(.082)


def test_held_voltage_arrow_skips_busy_repeats_and_stops_at_limit(page, application):
    page.offset.setText("98 mV")
    page._cache_applied_carrier(page._visible_channel_config())
    page._device_state_changed("output_off")
    page.set_live_control_enabled(True)
    page.show()
    editor = page.offset.number
    editor.setFocus()
    application.processEvents()
    spy = QSignalSpy(page.quick_setpoint_requested)
    target = "rigol.1.offset"

    QTest.keyPress(editor, Qt.Key.Key_Up)
    assert spy.at(0) == [target, "99 mV"]
    page.quick_setpoint_state_changed(target, "applying", "Applying live setpoint")
    page.quick_setpoint_value_read(target, .099)
    _repeat_up(editor)
    assert spy.count() == 1
    page.quick_setpoint_state_changed(target, "applied", "Applied 99 mV")
    _repeat_up(editor)
    assert spy.count() == 2
    assert spy.at(1) == [target, "100 mV"]

    page.quick_setpoint_value_read(target, .100)
    _repeat_up(editor)
    assert spy.count() == 2
    assert editor.text() == "100"
    assert "limit" in page.banner.last_message.lower()
    QTest.keyRelease(editor, Qt.Key.Key_Up)


@pytest.mark.parametrize("interrupt", ["focus", "channel", "live_control"])
def test_held_voltage_arrow_does_not_resume_after_context_changes(page, application, interrupt):
    page._cache_applied_carrier(page._visible_channel_config())
    page._device_state_changed("output_off")
    page.set_live_control_enabled(True)
    page.show()
    editor = page.offset.number
    editor.setFocus()
    application.processEvents()
    spy = QSignalSpy(page.quick_setpoint_requested)
    QTest.keyPress(editor, Qt.Key.Key_Up)
    assert spy.count() == 1
    page.quick_setpoint_value_read("rigol.1.offset", .083)

    if interrupt == "focus":
        page.channel.setFocus()
        editor.setFocus()
    elif interrupt == "channel":
        page.channel.setCurrentText("2")
        page.channel.setCurrentText("1")
    else:
        page.set_live_control_enabled(False)
        page.set_live_control_enabled(True)

    _repeat_up(editor)
    assert spy.count() == 1
    QTest.keyRelease(editor, Qt.Key.Key_Up)
    QTest.keyClick(editor, Qt.Key.Key_Up)
    assert spy.at(1) == ["rigol.1.offset", "84 mV"]


@pytest.mark.parametrize(
    ("mode", "stepped_text", "expected_hz"),
    [("Frequency", "3 kHz", 3000), ("Period", "0.6 ms", 1 / .0006)],
)
def test_time_arrows_submit_one_value_after_channel_confirmation(page, application, mode, stepped_text, expected_hz):
    page.waveform.setCurrentText("SIN")
    page.frequency.setText("2 kHz")
    page.time_mode.setCurrentText(mode)
    editor = page.frequency if mode == "Frequency" else page.period
    before = editor.text()
    page._device_state_changed("output_off")
    page.resize(1360, 880)
    page.show()
    application.processEvents()
    assert editor.isVisibleTo(page)
    editor.number.setFocus()
    spy = QSignalSpy(page.quick_setpoint_requested)

    QTest.keyClick(editor.number, Qt.Key.Key_Up)
    page.set_live_control_enabled(True)
    QTest.keyClick(editor.number, Qt.Key.Key_Up)
    assert spy.count() == 0 and editor.text() == before

    page._cache_applied_carrier(page._visible_channel_config())
    QTest.keyClick(editor.number, Qt.Key.Key_Up)
    assert editor.text() == stepped_text
    assert spy.count() == 1
    target, quantity = spy.at(0)
    assert target == "rigol.1.frequency"
    assert parse_quantity(quantity).si_value == pytest.approx(expected_hz)
    QTest.keyClick(editor.number, Qt.Key.Key_Up)
    assert spy.count() == 1
    page.quick_setpoint_value_read(target, round(expected_hz, 6))
    QTest.keyClick(editor.number, Qt.Key.Key_Down)
    assert spy.count() == 2
    assert parse_quantity(spy.at(1)[1]).si_value == pytest.approx(2000)


@pytest.mark.parametrize("waveform", ["DC", "SIN"])
def test_rejected_unit_characters_cannot_send_a_truncated_voltage(page, application, monkeypatch, waveform):
    page.waveform.setCurrentText(waveform)
    if waveform == "SIN":
        page.vpp.setText("8 mV")
    page._cache_applied_carrier(page._visible_channel_config())
    page._device_state_changed("output_off")
    page.set_live_control_enabled(True)
    page.show()
    application.processEvents()
    editor = page.offset.number
    editor.setFocus()
    editor.selectAll()
    spy = QSignalSpy(page.quick_setpoint_requested)

    QTest.keyClicks(editor, "20V")
    assert editor.text() == "20"
    assert "unit" in page.live_control_hint.text().lower()
    QTest.keyClick(editor, Qt.Key.Key_Return)
    QTest.keyClick(editor, Qt.Key.Key_Up)
    page.offset.apply.click()
    page.channel.setCurrentText("2")
    page.channel.setCurrentText("1")
    assert page.offset.input_was_rejected
    if waveform == "SIN":
        assert page.vpp.text() == "8 mV"
        assert not page.vpp.is_draft
    page.configure()
    monkeypatch.setattr("app.devices.rigol_dg1000z.ui.page.QMessageBox.warning", Mock())
    page.request_output(True)
    assert spy.count() == 0
    page._controller.call.assert_not_called()

    editor.selectAll()
    QTest.keyClicks(editor, "20")
    QTest.keyClick(editor, Qt.Key.Key_Return)
    assert spy.count() == 1
    assert spy.at(0) == ["rigol.1.offset", "20 mV"]


def test_live_control_hint_names_missing_evidence_and_next_action(page):
    page.set_live_control_enabled(True)
    assert "Connect" in page.live_control_hint.text()
    page._device_state_changed("output_on")
    assert "OUTPUT OFF" in page.live_control_hint.text()
    assert "CH1" in page.live_control_hint.text()
    page._set_rigol_channel_output(1, True)
    assert "OUTPUT OFF" in page.live_control_hint.text()
    assert "Validate and apply waveform" in page.live_control_hint.text()
    page._device_state_changed("output_off")
    assert "Validate and apply waveform" in page.live_control_hint.text()
    page._cache_applied_carrier(page._visible_channel_config())
    assert page.offset.apply.isEnabled()


@pytest.mark.parametrize("waveform", ["DC", "SIN"])
def test_live_voltage_on_via_worker_preserves_output_and_other_channel(application, waveform, monkeypatch):
    settings_data = simulation_settings().model_dump()
    settings_data["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    settings_data["devices"]["rigol"]["safety"]["channels"]["1"]["enabled"] = True
    settings = StationSettings.model_validate(settings_data)
    session = DcOffsetIgnoringFirmware()
    adapter = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    controller = DeviceController(adapter, dispatcher=_dispatch)
    page = RigolPage(controller, settings)
    monkeypatch.setattr("app.devices.rigol_dg1000z.ui.page.QMessageBox.warning", Mock())
    coordinator = QuickControlCoordinator(
        {"rigol": controller, "keithley": _FakeController("disconnected")}, page, settings=settings)
    coordinator.set_hardware_request_builder(
        "rigol", lambda request, state: page.quick_control_hardware_request(
            request.target, request.text, state, live_only=request.live_only))
    page.quick_setpoint_requested.connect(lambda target, text: coordinator.submit(target, text, live_only=True))
    coordinator.value_read.connect(page.quick_setpoint_value_read)
    coordinator.state_changed.connect(page.quick_setpoint_state_changed)
    page.resize(1360, 880)
    page.show()
    try:
        controller.call("connect")
        wait_until(application, lambda: page.output_state_snapshot()["1"] == "off")
        page.waveform.setCurrentText(waveform)
        page.vpp.setText("8 mV" if waveform == "SIN" else "0 mV")
        page.offset.setText("81 mV")
        page.request_output(True)
        wait_until(application, lambda: page.output_state_snapshot()["1"] == "on")
        page.set_live_control_enabled(True)
        session.commands.clear()
        editor = page.offset.number
        editor.selectAll()
        QTest.keyClicks(editor, "90V")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        QTest.keyClick(editor, Qt.Key.Key_Down)
        assert not [command for command in session.commands if "?" not in command]
        editor.selectAll()
        QTest.keyClicks(editor, "82")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        wait_until(application, lambda: "82 mV" in page.live_control_hint.text())
        editor.setFocus()
        QTest.keyPress(editor, Qt.Key.Key_Up)
        wait_until(application, lambda: "83 mV" in page.live_control_hint.text())
        _repeat_up(editor)
        wait_until(application, lambda: "84 mV" in page.live_control_hint.text())
        QTest.keyRelease(editor, Qt.Key.Key_Up)
        _repeat_up(editor)
        page.offset.setText("86 mV")
        page.waveform_apply_button.click()
        wait_until(application, lambda: "86 mV" in page.live_control_hint.text())
        if waveform == "DC":
            assert "readback unavailable" in page.live_control_hint.text()
        page.offset.setText("110 mV")
        QTest.keyClick(page.offset.number, Qt.Key.Key_Return)
        assert "limit" in page.banner.last_message.lower()
        page.set_live_control_enabled(False)
        page.offset.setText("87 mV")
        page.waveform_apply_button.click()
        assert [c for c in session.commands if "?" not in c] == [
            ":SOUR1:VOLT:OFFS 0.082", ":SOUR1:VOLT:OFFS 0.083", ":SOUR1:VOLT:OFFS 0.084",
            ":SOUR1:VOLT:OFFS 0.086",
        ]
        assert page.output_state_snapshot() == {"1": "on", "2": "off"}
        assert session.output == {1: True, 2: False}
        if waveform == "DC":
            assert session.dc_panel[1] == .086
        else:
            assert adapter.last_channel_config(1).high_level_v == pytest.approx(.090)
            assert adapter.last_channel_config(1).low_level_v == pytest.approx(.082)
    finally:
        page.close()
        assert controller.close()
        page.deleteLater()
        application.processEvents()


def test_live_voltage_over_budget_stays_visible_and_is_rejected(page, application):
    page._cache_applied_carrier(page._visible_channel_config())
    page._device_state_changed("output_on")
    page._set_rigol_channel_output(1, True)
    page.set_live_control_enabled(True)
    page.show()
    application.processEvents()
    spy = QSignalSpy(page.quick_setpoint_requested)
    page.offset.setText("2000 mV")
    QTest.keyClick(page.offset.number, Qt.Key.Key_Return)
    assert spy.count() == 0
    assert page.offset.text() == "2000 mV"
    assert "limit" in page.banner.last_message.lower()


def test_enter_in_floating_window_cannot_trigger_full_waveform_apply(page, application):
    page._cache_applied_carrier(page._visible_channel_config())
    page._device_state_changed("output_off")
    page.set_live_control_enabled(True)
    page.show()
    page._toggle_floating_controls()
    application.processEvents()
    try:
        page.waveform_apply_button.setDefault(True)
        full_apply = QSignalSpy(page.waveform_apply_button.clicked)
        live_apply = QSignalSpy(page.quick_setpoint_requested)
        page.offset.setText("21 mV")
        page.offset.number.setFocus()
        QTest.keyClick(page.offset.number, Qt.Key.Key_Return)
        assert live_apply.count() == 1
        assert full_apply.count() == 0
        page._controller.call.assert_not_called()
    finally:
        page._dock_controls()


def test_period_draft_and_unit_are_used_only_after_apply(page, application):
    page.waveform.setCurrentText("SIN")
    page._cache_applied_carrier(page._visible_channel_config())
    page._device_state_changed("output_off")
    page.set_live_control_enabled(True)
    page.show()
    application.processEvents()
    spy = QSignalSpy(page.quick_setpoint_requested)
    page.frequency.unit.setCurrentText("MHz")
    assert page.frequency.number.text() == "0.001"
    assert parse_quantity(page.frequency.text()).si_value == 1000
    page.time_mode.setCurrentText("Period")
    page.period.unit.setCurrentText("us")
    assert page.period.number.text() == "1000"
    page.period.number.selectAll()
    QTest.keyClicks(page.period.number, "50")
    QTest.qWait(450)
    assert spy.count() == 0
    page.period.apply.click()
    assert spy.count() == 1
    target, text = spy.at(0)
    assert target == "rigol.1.frequency"
    assert parse_quantity(text).si_value == 20_000
    page._controller.call.assert_not_called()


def test_readback_uses_compact_digits_and_preserves_next_draft(page):
    settings = page._station_settings.model_dump()
    settings["devices"]["rigol"]["safety"]["channels"]["1"]["lab_limits"]["combined_voltage_limit"] = "1 V"
    page.set_settings(StationSettings.model_validate(settings))
    page.offset.setText("200.00000000000000000000 mV")
    page.quick_setpoint_value_read("rigol.1.offset", 0.2)
    assert page.offset.text() == "200 mV"
    assert page.combined_voltage_usage.text() == "200 mV"
    assert page._limit_fields[page.combined_voltage_usage].validation_warning.isHidden()
    page.offset.number.selectAll()
    QTest.keyClicks(page.offset.number, "201")
    page.quick_control_draft_changed_from_coordinator("rigol.1.offset", "200 mV", "readback")
    page.quick_setpoint_value_read("rigol.1.offset", 0.2)
    assert page.offset.text() == "201 mV"


def test_unverified_channel_keeps_draft_and_sends_nothing(page, application):
    page._device_state_changed("output_off")
    page.set_live_control_enabled(True)
    page.show()
    application.processEvents()
    assert not page.offset.apply.isEnabled()
    page.offset.number.selectAll()
    QTest.keyClicks(page.offset.number, "1e-")
    page.channel.setCurrentText("2")
    QTest.qWait(450)
    page.channel.setCurrentText("1")
    assert page.offset.number.text() == "1e-"
    QTest.keyClick(page.offset.number, Qt.Key.Key_Return)
    page._controller.call.assert_not_called()


@pytest.mark.parametrize("width,theme,waveform", [(1360, "light", "SIN"), (980, "dark", "SIN"), (980, "dark", "DC")])
def test_live_value_controls_render(page, application, tmp_path, width, theme, waveform):
    apply_application_theme(application, theme)
    page.waveform.setCurrentText(waveform)
    page._cache_applied_carrier(page._visible_channel_config())
    page._device_state_changed("output_off")
    page.set_live_control_enabled(True)
    page.resize(width, 880)
    page.show()
    application.processEvents()
    page.basic_scroll.ensureWidgetVisible(page.offset)
    application.processEvents()
    viewport = page.basic_scroll.viewport()
    editors = (page.frequency, page.vpp, page.offset) if waveform == "SIN" else (page.offset,)
    for control in (control for editor in editors for control in (editor.number, editor.unit, editor.apply)):
        assert control.isVisibleTo(page) and control.isEnabled()
        assert control.width() >= 50 and control.height() >= 25
        assert viewport.rect().contains(control.rect().translated(control.mapTo(viewport, QPoint())))
    assert page.output_off.isVisibleTo(page)
    assert page.width() == width
    assert page.grab().save(str(tmp_path / f"rigol-live-{width}-{theme}-{waveform}.png"))


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
