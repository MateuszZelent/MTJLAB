"""Rendered Rigol screen and polling lifecycle regressions."""

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.devices.rigol_dg1000z.ui.page import RigolPage
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.test_rigol_screen_capture import bmp_bytes
from tests.test_spectrum_correction_controller import wait_until


class ScreenController(QObject):
    request = Signal(str, object)
    result = Signal(str, object)
    error = Signal(str, str)
    state_changed = Signal(str)
    reservation_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.calls = []

    def call(self, operation, payload=None):
        self.calls.append((operation, payload))
        self.request.emit(operation, payload)


@pytest.fixture
def screen_page():
    app = QApplication.instance() or QApplication([])
    controller = ScreenController()
    page = RigolPage(controller, simulation_settings())
    page.resize(1440, 960)
    page.show()
    app.processEvents()
    page.preview_tabs.setCurrentIndex(1)
    app.processEvents()
    yield app, controller, page, page.screen_preview
    page.close()
    page.deleteLater()
    app.processEvents()


def test_refresh_and_live_are_read_only_and_single_flight(screen_page):
    app, controller, page, screen = screen_page
    assert not screen.refresh_button.isEnabled()
    controller.state_changed.emit('output_on')
    page._set_rigol_channel_output(1, True)
    screen.live_switch.setChecked(True)
    app.processEvents()
    assert controller.calls == [('capture_screen', None)]
    screen.request_frame()
    screen.request_frame()
    assert len(controller.calls) == 1
    controller.result.emit('capture_screen', bmp_bytes())
    assert screen.has_frame
    assert screen._timer.isActive()
    screen.live_switch.setChecked(False)
    assert not screen._timer.isActive()
    screen.refresh_button.click()
    assert len(controller.calls) == 2
    controller.error.emit('capture_screen', 'Read timeout')
    assert 'Read timeout' in screen.status_label.text()
    assert not screen.live_switch.isChecked()
    assert not screen._timer.isActive()
    assert page.output_off.isEnabled()


def test_hide_sweep_and_reservation_suspend_polling(screen_page):
    app, controller, page, screen = screen_page
    controller.state_changed.emit('output_off')
    screen.live_switch.setChecked(True)
    app.processEvents()
    controller.result.emit('capture_screen', bmp_bytes())
    page.preview_tabs.setCurrentIndex(0)
    app.processEvents()
    assert not screen._timer.isActive()
    page.set_execution_controlled(True)
    page.preview_tabs.setCurrentIndex(1)
    app.processEvents()
    screen.request_frame()
    assert len(controller.calls) == 1
    assert 'sweep' in screen.status_label.text().lower()
    page.set_execution_controlled(False)
    controller.reservation_changed.emit(True)
    app.processEvents()
    screen.request_frame()
    assert len(controller.calls) == 1
    controller.reservation_changed.emit(False)
    app.processEvents()
    assert len(controller.calls) == 2


def test_late_frame_cannot_repopulate_after_disconnect(screen_page):
    _app, controller, _page, screen = screen_page
    controller.state_changed.emit('output_off')
    screen.refresh_button.click()
    controller.state_changed.emit('disconnected')
    controller.state_changed.emit('output_off')
    controller.result.emit('capture_screen', bmp_bytes())
    assert not screen.has_frame
    assert not screen._timer.isActive()
    assert screen.refresh_button.isEnabled()


def test_live_preview_waits_for_the_latest_control_command_then_resumes_polling(screen_page):
    app, controller, page, screen = screen_page
    controller.state_changed.emit("output_off")
    screen.live_switch.setChecked(True)
    page.set_live_control_enabled(True)
    controller.call("quick_setpoint", "first")
    controller.call("quick_setpoint", "second")
    controller.result.emit("quick_setpoint", .01)
    QTest.qWait(250)
    assert controller.calls == [("quick_setpoint", "first"), ("quick_setpoint", "second")]
    assert "waiting" in screen.status_label.text().lower()
    controller.result.emit("quick_setpoint", .02)
    controller.call("quick_readback")
    QTest.qWait(250)
    assert controller.calls[-1] == ("quick_readback", None)
    controller.result.emit("quick_readback", {"rigol.1.offset": .02})
    wait_until(app, lambda: controller.calls[-1] == ("capture_screen", None))
    assert [op for op, _ in controller.calls].count("capture_screen") == 1
    controller.result.emit("capture_screen", bmp_bytes())
    assert screen.has_frame and screen._timer.isActive()


def test_live_control_refreshes_after_an_applied_setting_with_periodic_polling_off(screen_page):
    app, controller, page, screen = screen_page
    controller.state_changed.emit("output_off")
    page.waveform.setCurrentText("DC")
    page.offset.setText("10 mV")
    page._cache_applied_carrier(page._visible_channel_config())
    page.set_live_control_enabled(True)
    page.quick_setpoint_requested.connect(
        lambda target, text: controller.call("quick_setpoint", (target, text)))
    page.offset.setText("12 mV")
    QTest.qWait(250)
    assert not controller.calls
    page.offset.apply.click()
    assert controller.calls == [("quick_setpoint", ("rigol.1.offset", "12 mV"))]
    controller.result.emit("quick_setpoint", .012)
    wait_until(app, lambda: controller.calls[-1] == ("capture_screen", None))
    controller.result.emit("capture_screen", bmp_bytes())
    assert screen.has_frame and not screen._timer.isActive()


def test_setting_applied_during_capture_requests_one_new_frame(screen_page):
    app, controller, page, screen = screen_page
    controller.state_changed.emit("output_off")
    page.set_live_control_enabled(True)
    screen.refresh_button.click()
    controller.call("quick_setpoint", "offset")
    controller.result.emit("quick_setpoint", .012)
    QTest.qWait(250)
    assert [op for op, _ in controller.calls].count("capture_screen") == 1
    controller.result.emit("capture_screen", bmp_bytes())
    wait_until(app, lambda: [op for op, _ in controller.calls].count("capture_screen") == 2)
    controller.result.emit("capture_screen", bmp_bytes())
    assert screen.has_frame and not screen._timer.isActive()


def test_periodic_preview_recovers_after_a_transient_capture_timeout(screen_page):
    app, controller, _page, screen = screen_page
    controller.state_changed.emit("output_off")
    screen.live_switch.setChecked(True)
    app.processEvents()
    controller.error.emit("capture_screen", "VISA query failed: VI_ERROR_TMO")
    assert screen.live_switch.isChecked()
    assert screen._timer.isActive()
    assert "timeout" in screen.status_label.text().lower() or "TMO" in screen.status_label.text()


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_screen_renders_in_page_and_floating_window(screen_page, theme, tmp_path):
    app, controller, page, screen = screen_page
    apply_application_theme(app, theme)
    controller.state_changed.emit('output_off')
    screen.refresh_button.click()
    controller.result.emit('capture_screen', bmp_bytes())
    for floating in (False, True):
        if floating:
            page.open_floating_button.click()
        host = page._control_window if floating else page
        for width in (1440, 900):
            host.resize(width, 960)
            app.processEvents()
            assert screen.isVisibleTo(host)
            assert screen.image_view.width() > 100 and screen.image_view.height() > 100
            for control in (screen.refresh_button, screen.live_switch, page.output_off):
                assert control.isVisibleTo(host)
                assert host.rect().contains(control.mapTo(host, control.rect().center()))
            assert host.grab().save(str(tmp_path / f'screen-{theme}-{width}-{floating}.png'))
    page._control_window.close()
    app.processEvents()


def test_invalid_image_stops_live_without_warning_modal(screen_page):
    app, controller, _page, screen = screen_page
    controller.state_changed.emit('output_off')
    screen.live_switch.setChecked(True)
    app.processEvents()
    controller.result.emit('capture_screen', b'bad image')
    assert not screen.has_frame
    assert not screen.live_switch.isChecked()
    assert 'image' in screen.status_label.text().lower()


def test_synchronous_admission_failure_does_not_leave_pending_capture(screen_page):
    app, controller, _page, screen = screen_page
    controller.state_changed.emit('output_off')

    def reject(operation, payload=None):
        controller.error.emit(operation, 'Instrument is reserved')

    controller.call = reject
    screen.live_switch.setChecked(True)
    app.processEvents()
    assert not screen._pending
    assert not screen._timer.isActive()
    assert not screen.live_switch.isChecked()
    assert 'reserved' in screen.status_label.text()
