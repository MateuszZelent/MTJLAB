"""Guided setup through the real simulated adapter and durable correction worker."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from unittest.mock import MagicMock

import h5py
import pytest
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.anritsu_ms2830a.module import MODULE
from app.devices.anritsu_ms2830a.ui.page import AnritsuPage, AnritsuPageState
from app.devices.simulators import SimulatedVisaFactory
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.fixture
def setup(tmp_path, shell_qt_application):
    app = shell_qt_application
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
    wait_until(app, page.close, timeout=15)
    page.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    adapter.disconnect()


@pytest.mark.parametrize("start_live", [False, True])
def test_background_setup_returns_to_main_and_live_starts_only_on_request(setup, start_live):
    app, page, requests, errors = setup
    page.resize(1500, 900)
    page.show()
    if start_live:
        page.live.click()
        wait_until(app, lambda: page._timer.isActive())
    page.correction_controls.configure_background.click()
    dialog = page._background_assistant
    assert dialog is not None and dialog.isVisible()
    assert dialog.next.isEnabled()
    path = dialog.reference_path
    before = len(requests)
    dialog.next.click()
    wait_until(app, lambda: page._background_assistant is None or bool(errors), timeout=15)
    assert not errors, errors
    assert not page.correction_workspace.running
    assert not page._timer.isActive()
    assert page.cleanup_filters["background"].isChecked()
    assert page.analysis_tabs.currentIndex() == 0
    assert page.live.text() == "Start Live" and page.live.isEnabled()
    assert path.exists()
    if start_live:
        assert requests[before] == "stop_live"
    assert "read_acquisition_configuration" in requests[before:]
    assert "single_sweep" in requests[before:]
    assert "start_live" not in requests[before:]
    with h5py.File(path, "r") as archive:
        assert len(archive["points"]) >= 3
    count = len(requests)
    QTest.qWait(80)
    assert not any(operation in {"single_sweep", "start_live"} for operation in requests[count:])
    assert not any(operation in {"configure", "set_signal_generator_output"} for operation in requests)
    assert "source output states not checked" in page.correction_workspace._profile.reference_state
    page.live.click()
    wait_until(app, lambda: page._timer.isActive())
    assert requests[-1] == "start_live"
    assert not page.correction_workspace.running
    page.live.click()
    wait_until(app, lambda: not page._timer.isActive() and not page._live_transition_pending)


def test_cancelled_background_does_not_start_corrected_live(setup):
    app, page, _requests, _errors = setup
    page.correction_controls.configure_background.click()
    dialog = page._background_assistant
    wait_until(app, lambda: dialog.next.isEnabled())
    dialog.next.click()
    wait_until(app, lambda: dialog.phase == "collecting")
    dialog.reject()
    wait_until(app, lambda: not page.correction_workspace.running, timeout=15)
    assert page._background_assistant is None
    assert page.correction_workspace._kind == "reference"
    assert not page.correction_workspace.acquire_signal.isEnabled()


def test_fault_holds_workflow_until_archive_close_and_does_not_retry_failed_close(setup, monkeypatch):
    _app, page, _requests, _errors = setup
    workspace = page.correction_workspace
    stop = MagicMock()
    monkeypatch.setattr(workspace._cpu, "stop_session", stop)
    workspace._failed("single_sweep", "Injected transport failure")
    assert workspace.running and workspace._stopping
    assert not workspace.acquire_reference.isEnabled()
    stop.assert_called_once_with("faulted")
    workspace._processed("reference", object())
    stop.assert_called_once_with("faulted")
    workspace._processed("stop", None)
    assert not workspace.running
    assert workspace._recording_failed
    workspace._failed("stop", "Injected close failure")
    stop.assert_called_once_with("faulted")
    assert not workspace.running


@pytest.mark.parametrize("source_state", ["disconnected", "on", "off"])
def test_background_is_independent_of_keithley_and_sends_no_source_commands(setup, source_state):
    from app.devices.keithley_2600 import KeithleyAdapter
    from app.devices.keithley_2600.module import MODULE as KEITHLEY_MODULE
    from app.ui.workers import DeviceController

    app, page, _requests, errors = setup
    adapter = KeithleyAdapter(simulation_settings(), session_factory=SimulatedVisaFactory("keithley"))
    controller = DeviceController(adapter, dispatcher=KEITHLEY_MODULE.dispatch)
    completed, failed = [], []
    controller.result.connect(lambda operation, result: completed.append(operation))
    controller.error.connect(lambda operation, message: failed.append((operation, message)))
    session = None
    try:
        if source_state != "disconnected":
            controller.call("connect")
            wait_until(app, lambda: "connect" in completed)
            session = adapter._require_session()
            if source_state == "on":
                session.write("smua.source.leveli = 1e-9")
                session.write("smua.source.output = 1")
                session.write("smub.source.output = 1")
            before = session.commands.copy()
        page.correction_controls.configure_background.click()
        dialog = page._background_assistant
        assert dialog.next.isEnabled()
        assert not hasattr(dialog, "output_status") and not hasattr(dialog, "recheck")
        assert not hasattr(page, "_background_output_controller")
        dialog.next.click()
        wait_until(app, lambda: dialog.phase in {"ready", "failed"}, timeout=15)
        assert dialog.phase == "ready"
        assert not errors and not failed
        assert completed == ([] if source_state == "disconnected" else ["connect"])
        if session is not None:
            assert session.commands == before  # No queries, output changes or setpoint writes.
        assert page.correction_workspace._profile is not None
        assert "source output states not checked" in page.correction_workspace._profile.reference_state
    finally:
        controller.close()


def test_background_does_not_add_a_keithley_off_gate_to_normal_permission_checks():
    from types import SimpleNamespace

    from app.security import Permission
    from app.ui.shell.main_window import MainWindow

    station = SimpleNamespace(
        _leased_run_devices=set(), _audit_healthy=True, _require_permission=MagicMock(),
        _refresh_audit_health=MagicMock(),
        anritsu_page=SimpleNamespace(_background_assistant=SimpleNamespace(phase="collecting")),
    )
    for operation, payload in (("set_output", ("A", True)), ("set_output_group", (("A", "B"), True)), ("quick_setpoint", None)):
        MainWindow._guard_manual_operation(station, "keithley", operation, payload)
        station._require_permission.assert_called_with(
            Permission.OPERATE_OUTPUT, f"manual instrument operation {operation}", audit=True)
    MainWindow._guard_manual_operation(station, "keithley", "set_output", ("A", False))


def test_failed_recollection_does_not_reuse_old_background(setup):
    app, page, requests, _errors = setup
    page.correction_controls.configure_background.click()
    first = page._background_assistant
    wait_until(app, lambda: first.next.isEnabled())
    first.next.click()
    wait_until(app, lambda: first.phase == "ready", timeout=15)
    previous_profile = page.correction_workspace._profile
    app.processEvents()
    requests.clear()

    def fail_request(operation, _payload=None):
        requests.append(operation)
        QTimer.singleShot(0, lambda: page._error(operation, "Injected connection failure"))

    page._controller.call.side_effect = fail_request
    page.correction_controls.configure_background.click()
    dialog = page._background_assistant
    wait_until(app, lambda: dialog.next.isEnabled())
    dialog.next.click()
    wait_until(app, lambda: dialog.phase == "failed")
    assert page.correction_workspace._profile is previous_profile
    assert requests == ["read_acquisition_configuration"]
    assert not dialog.next.isEnabled()
    assert not list(dialog.directory.glob("corrected_live_*.h5"))
    assert "Injected connection failure" in dialog.status.text()


def test_cancel_while_pausing_live_cannot_start_background_after_stop_confirmation(setup):
    app, page, requests, _errors = setup
    page.live.click()
    wait_until(app, lambda: page._timer.isActive())
    page.correction_controls.configure_background.click()
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
    page.correction_controls.configure_background.click()
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


@pytest.mark.parametrize("source_kind", ["recording", "export"])
def test_modal_loads_saved_background_without_new_sweeps_and_applies_shared_filter(setup, source_kind):
    from app.storage.background_profile_store import BackgroundProfileHdf5Store
    app, page, requests, errors = setup
    page.correction_controls.configure_background.click()
    original = page._background_assistant
    original.next.click()
    wait_until(app, lambda: original.phase == "ready", timeout=15)
    workspace = page.correction_workspace
    expected_profile = workspace._profile
    path = original.reference_path
    if source_kind == "export":
        path = path.with_name("exported-background.h5")
        BackgroundProfileHdf5Store.save(path, workspace._context, expected_profile)
    app.processEvents()
    workspace._profile = None
    workspace._context = None
    requests.clear()
    page._open_background_setup()
    dialog = page._background_assistant
    dialog.background_source.setCurrentIndex(dialog.background_source.findData("load"))
    assert not dialog.next.isEnabled()
    dialog.profile_path.setText(str(path))
    assert dialog.next.isEnabled()
    dialog.next.click()
    wait_until(app, lambda: dialog.phase == "ready", timeout=15)
    assert not errors
    # Enabling the imported filter may verify settings through read-only queries.
    assert all(operation.startswith("read_background_filter_configuration:") for operation in requests)
    assert workspace._profile.content_hash == expected_profile.content_hash
    assert not workspace.running
    assert not list(dialog.directory.glob("corrected_live_*.h5"))
    wait_until(app, lambda: page._background_assistant is None)
    assert page.cleanup_filters["background"].isChecked()
    assert page.analysis_tabs.currentIndex() == 0
    assert not page._timer.isActive()
    assert not workspace.running
    assert not any(operation == "single_sweep" for operation in requests)


def test_modal_failed_import_is_retryable_and_never_starts_acquisition(setup, tmp_path):
    app, page, requests, errors = setup
    page.correction_controls.configure_background.click()
    dialog = page._background_assistant
    dialog.background_source.setCurrentIndex(dialog.background_source.findData("load"))
    path = tmp_path / "invalid-background.h5"
    path.write_bytes(b"not an HDF5 archive")
    dialog.profile_path.setText(str(path))
    dialog.next.click()
    wait_until(app, lambda: dialog.phase == "setup" and bool(errors))
    assert "Could not load" in dialog.status.text()
    assert dialog.next.isEnabled() and dialog.form.isEnabled()
    assert not page.cleanup_filters["background"].isChecked()
    assert not requests and not page.correction_workspace.running
    assert page.correction_workspace._profile is None
    dialog.background_source.setCurrentIndex(dialog.background_source.findData("record"))
    dialog.next.click()
    wait_until(app, lambda: dialog.phase == "ready", timeout=15)
    assert page.correction_workspace._profile is not None


def test_cancelled_import_cannot_start_live_or_apply_filter(setup):
    app, page, requests, _ = setup
    page.correction_controls.configure_background.click()
    dialog = page._background_assistant
    dialog.background_source.setCurrentIndex(dialog.background_source.findData("load"))
    dialog.profile_path.setText("a-background.h5")
    page.correction_workspace.load_background_profile = MagicMock()
    dialog.next.click()
    assert dialog.phase == "loading"
    dialog.reject()
    page.correction_workspace.profile_load_finished.emit(True, "Late completion")
    app.processEvents()
    assert not requests and not page.correction_workspace.running
    assert not page.cleanup_filters["background"].isChecked()
    assert page._background_assistant is None


@pytest.mark.parametrize("theme,size", [("light", (620, 620)), ("dark", (440, 420))])
def test_load_choice_renders_without_overlap_at_normal_and_narrow_size(setup, theme, size):
    app, page, requests, _ = setup
    apply_application_theme(app, theme)
    page.correction_controls.configure_background.click()
    dialog = page._background_assistant
    dialog.background_source.setCurrentIndex(dialog.background_source.findData("load"))
    dialog.resize(*size)
    QTest.qWait(80)
    app.processEvents()
    assert dialog.load_fields.isVisible() and not dialog.record_fields.isVisible()
    assert dialog.profile_path.isVisible() and dialog.choose_profile.isVisible()
    assert not dialog.profile_path.geometry().intersects(dialog.choose_profile.geometry())
    for control in (dialog.cancel, dialog.next, dialog.background_source):
        assert dialog.rect().contains(control.mapTo(dialog, control.rect().bottomRight()))
    artifacts = Path("artifacts/background-assistant")
    artifacts.mkdir(parents=True, exist_ok=True)
    assert dialog.grab().save(str(artifacts / f"load-{theme}-{size[0]}.png"))
    dialog.reject()
    assert not requests
