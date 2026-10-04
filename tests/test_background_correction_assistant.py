"""Guided setup through the real simulated adapter and durable correction worker."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from unittest.mock import MagicMock

import h5py
import pytest
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.anritsu_ms2830a.module import MODULE
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage, AnritsuPageState
from app.devices.simulators import SimulatedVisaFactory
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.test_spectrum_correction_controller import wait_until


class OutputProbe(QObject):
    result = Signal(str, object)
    error = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.is_connected = True
        self.states = {"A": False, "B": False}
        self.calls = []
        self.failure = None

    def call(self, operation, payload=None):
        self.calls.append(operation)
        states, failure = self.states.copy(), self.failure
        QTimer.singleShot(0, lambda: self.error.emit(operation, failure) if failure else self.result.emit(operation, states))


@pytest.fixture
def setup(tmp_path):
    app = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Segoe UI", 10))
    settings = simulation_settings()
    correction = settings.anritsu.spectrum_correction.model_copy(update={
        "calibration_duration": "1 ms", "calibration_min_sweeps": 3,
    })
    settings = settings.model_copy(update={
        "storage": {**settings.storage, "output_directory": str(tmp_path)},
        "devices": settings.devices.model_copy(update={
            "anritsu": settings.anritsu.model_copy(update={"spectrum_correction": correction}),
        }),
    })
    adapter = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
    adapter.connect()
    controller = MagicMock()
    controller.is_connected = True
    page = AnritsuPage(controller, settings, single_sweep_available=True)
    page.set_background_output_controller(OutputProbe(page))
    page.correction_workspace.set_simulation_mode(True)
    errors, requests = [], []

    def request(operation, payload=None):
        def deliver():
            try:
                requests.append(operation)
                result = MODULE.dispatch(adapter, operation, payload)
                page._result(operation, result)
            except (ValueError, RuntimeError) as exc:
                errors.append(str(exc))
                page._error(operation, str(exc))
        QTimer.singleShot(0, deliver)

    controller.call.side_effect = request
    page.correction_workspace._cpu.failed.connect(lambda *args: errors.append(args))
    page._set_page_state(AnritsuPageState.IDLE)
    yield app, page, requests, errors
    if page._background_assistant is not None:
        page._background_assistant.reject()
    if page.correction_workspace.running:
        page.correction_workspace.stop_acquisition()
    wait_until(app, lambda: not page.correction_workspace.running, timeout=15)
    page.close()
    page.deleteLater()
    app.processEvents()
    adapter.disconnect()


@pytest.mark.parametrize("start_live", [False, True])
def test_guided_background_then_corrected_live_is_automatic_and_archived(setup, start_live):
    app, page, requests, errors = setup
    page.resize(1500, 900)
    page.show()
    if start_live:
        page.live.click()
        wait_until(app, lambda: page._timer.isActive())
    page.auto_background_button.click()
    dialog = page._background_assistant
    assert dialog is not None and dialog.isVisible()
    dialog.next.click()
    assert dialog.phase == "setup"  # Missing physical-state evidence sends no new commands.
    wait_until(app, lambda: dialog.next.isEnabled())
    before = len(requests)
    dialog.next.click()
    wait_until(app, lambda: dialog.phase in {"restore", "failed"}, timeout=15)
    assert not errors, errors
    assert dialog.phase == "restore"
    assert not page.correction_workspace.running
    assert dialog.reference_path.exists()
    if start_live:
        assert requests[before] == "stop_live"
    assert "read_full_configuration" in requests[before:]
    assert "single_sweep" in requests[before:]
    count = len(requests)
    QTest.qWait(50)
    assert len(requests) == count  # Await explicit restoration; do not measure the background as SIGNAL.
    dialog.next.click()
    wait_until(app, lambda: page._background_assistant is None or bool(errors), timeout=15)
    assert not errors, errors
    assert page.correction_workspace.running
    assert page.current_spectrum_view.currentData() == "background"
    assert page._background_display is not None
    assert page.correction_workspace.mode.currentData() == "ema_preview"
    signal_path = page.correction_workspace._archive_path
    page.correction_workspace.stop_acquisition()
    wait_until(app, lambda: not page.correction_workspace.running, timeout=15)
    with h5py.File(signal_path, "r") as archive:
        assert len(archive["points"]) > 0
    assert not any(operation in {"configure", "set_signal_generator_output"} for operation in requests)


def test_cancelled_background_does_not_start_corrected_live(setup):
    app, page, _requests, _errors = setup
    page.auto_background_button.click()
    dialog = page._background_assistant
    wait_until(app, lambda: dialog.next.isEnabled())
    dialog.next.click()
    wait_until(app, lambda: dialog.phase == "collecting")
    dialog.reject()
    wait_until(app, lambda: not page.correction_workspace.running, timeout=15)
    assert page._background_assistant is None
    assert page.correction_workspace._kind == "reference"
    assert not page.correction_workspace.acquire_signal.isEnabled()


def test_failed_recollection_does_not_reuse_old_background(setup):
    app, page, requests, _errors = setup
    page.auto_background_button.click()
    first = page._background_assistant
    wait_until(app, lambda: first.next.isEnabled())
    first.next.click()
    wait_until(app, lambda: first.phase == "restore", timeout=15)
    previous_profile = page.correction_workspace._profile
    first.reject()
    app.processEvents()
    requests.clear()

    def fail_request(operation, _payload=None):
        requests.append(operation)
        QTimer.singleShot(0, lambda: page._error(operation, "Injected connection failure"))

    page._controller.call.side_effect = fail_request
    page.auto_background_button.click()
    dialog = page._background_assistant
    wait_until(app, lambda: dialog.next.isEnabled())
    dialog.next.click()
    wait_until(app, lambda: dialog.phase == "failed")
    assert page.correction_workspace._profile is previous_profile
    assert requests == ["read_full_configuration"]
    assert not dialog.next.isEnabled()
    assert not dialog.signal_path.exists()
    assert "Injected connection failure" in dialog.status.text()


def test_cancel_while_pausing_live_cannot_start_background_after_stop_confirmation(setup):
    app, page, requests, _errors = setup
    page.live.click()
    wait_until(app, lambda: page._timer.isActive())
    page.auto_background_button.click()
    dialog = page._background_assistant
    wait_until(app, lambda: dialog.next.isEnabled())
    before = len(requests)
    dialog.next.click()
    wait_until(app, lambda: dialog.phase == "preparing")
    dialog.reject()
    QTest.qWait(100)
    assert requests[before:] == ["stop_live"]
    assert not page.correction_workspace.running
    assert page._background_assistant is None


@pytest.mark.parametrize("theme,size", [("light", (620, 620)), ("dark", (440, 420))])
def test_assistant_renders_and_cancel_before_start_sends_no_commands(setup, theme, size):
    app, page, requests, _errors = setup
    apply_application_theme(app, theme)
    page.auto_background_button.click()
    dialog = page._background_assistant
    dialog.resize(*size)
    QTest.qWait(60)
    assert dialog.next.isVisible() and dialog.next.height() >= 30
    assert dialog.rect().contains(dialog.next.mapTo(dialog, dialog.next.rect().bottomRight()))
    artifacts = Path("artifacts/background-assistant")
    artifacts.mkdir(parents=True, exist_ok=True)
    assert dialog.grab().save(str(artifacts / f"{theme}-{size[0]}.png"))
    dialog.close()
    app.processEvents()
    assert not requests
    assert page._background_assistant is None
