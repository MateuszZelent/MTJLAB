"""Live edits stay responsive while the single transport owner confirms DAC."""

import math
import shutil
import time
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtTest import QTest

from app.devices.moke_box.protocol import MokeCommandType, MokeFrame, decode_voltage
from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
from app.domain.errors import DeviceError, RunInterrupted, SafetyViolation
from app.domain.models import DeviceState
from app.safety.moke_box import MokeLiveTargets
from app.ui.design_system import apply_application_theme
from app.ui.quick_controls import QuickControlsWindow
from tests.test_fluent_moke_field_workflow import application as application, workspace as workspace, wait_for
from tests.test_moke_quick_controls import coordinator_for
from tests.test_moke_voltage_control import controlled_adapter, plan_for, mutations


def test_mailbox_replaces_pending_target_and_rejects_changed_envelope():
    _, _, profile = controlled_adapter()
    plan = plan_for(profile, (.2,))
    mailbox = MokeLiveTargets(plan)
    mailbox.publish(replace(plan, targets_v=(.3,)))
    mailbox.publish(replace(plan, targets_v=(-.1,)))
    assert mailbox.take().targets_v == (-.1,)
    assert mailbox.take() is None
    for changed in (replace(plan, channel=0), replace(plan, maximum_v=.6),
                    replace(plan, settling_s=3), replace(plan, targets_v=(.2, .3)),
                    replace(plan, targets_v=(float("nan"),)), replace(plan, targets_v=(.51,))):
        with pytest.raises(SafetyViolation):
            mailbox.publish(changed)
    mailbox.publish(plan)
    mailbox.discard()
    assert mailbox.take() is None
    mailbox.close()
    assert not mailbox.publish(plan)


@pytest.mark.parametrize("during_settling", [False, True])
def test_live_retargets_from_confirmed_dac_and_settles_only_final_target(during_settling):
    adapter, transport, profile = controlled_adapter(simulation=False, minimum_settling_s=.15)
    plan = plan_for(profile, (.4,))
    mailbox = MokeLiveTargets(plan)
    samples, changed = [], False

    def observe(sample):
        nonlocal changed
        samples.append(sample)
        trigger = sample.phase == "settling" if during_settling else sample.actual_v > .05
        if trigger and not changed:
            changed = True
            mailbox.publish(replace(plan, targets_v=(.3,)))
            mailbox.publish(replace(plan, targets_v=(-.2,)))

    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    result = adapter.ramp_vout(2, .4, progress=observe, live_targets=mailbox)
    assert changed
    assert result.requested_v == -.2 and result.actual_v == pytest.approx(-.2, abs=.001)
    assert samples[-1].phase == "settling" and samples[-1].target_v < 0
    values = [0, *[decode_voltage(frame.msb, frame.lsb) for frame in mutations(transport)]]
    assert max(abs(b - a) for a, b in zip(values, values[1:])) <= profile.maximum_step_v
    if not during_settling:
        assert max(values) < .2  # Obsolete .4 V target was never reached.
    assert not mailbox.publish(plan)


def test_stop_precedes_pending_live_target_and_zero_is_confirmed():
    adapter, _, profile = controlled_adapter(simulation=False, minimum_settling_s=0)
    plan = plan_for(profile, (.4,))
    mailbox, cancel = MokeLiveTargets(plan), Event()
    samples = []

    def observe(sample):
        samples.append(sample)
        if sample.phase == "ramping" and sample.actual_v > .05:
            mailbox.publish(replace(plan, targets_v=(-.4,)))
            cancel.set()

    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    with pytest.raises(RunInterrupted):
        adapter.ramp_vout(2, .4, cancel=cancel, progress=observe, live_targets=mailbox)
    assert samples[-1].phase == "zeroing" and samples[-1].actual_v == 0
    assert all(sample.actual_v >= 0 for sample in samples)
    assert not mailbox.publish(plan)


def test_uncertain_live_write_closes_session_without_replaying_target():
    adapter, transport, profile = controlled_adapter(simulation=False, minimum_settling_s=0)
    plan = plan_for(profile, (.4,))
    mailbox = MokeLiveTargets(plan)
    samples = []

    def observe(sample):
        samples.append(sample)
        if sample.actual_v > .05:
            mailbox.publish(replace(plan, targets_v=(-.4,)))
            transport.fail_next_write = True

    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    with pytest.raises(DeviceError, match="lost write"):
        adapter.ramp_vout(2, .4, progress=observe, live_targets=mailbox)
    assert adapter.state == DeviceState.UNKNOWN and not adapter.connected
    assert not adapter.safe_target_confirmed
    assert samples[-1].actual_v > 0  # Never manufacture a zero or the latest draft.
    assert not mailbox.publish(plan)
    assert len(mutations(transport)) == 3  # Two confirmed steps, one lost SET, no replay.


def test_live_transaction_is_bounded_and_leaves_late_target_for_next_transaction(monkeypatch):
    from types import SimpleNamespace

    adapter, _, profile = controlled_adapter(minimum_settling_s=0)
    plan = plan_for(profile, (.2,))
    mailbox = MokeLiveTargets(plan)
    actual_clock = time.monotonic
    offset = [0]
    monkeypatch.setattr("app.devices.moke_box.adapter.time", SimpleNamespace(
        monotonic=lambda: actual_clock() + offset[0], sleep=time.sleep))

    def observe(sample):
        if sample.actual_v > .05:
            offset[0] = 6  # Continuous Live cannot keep one reservation forever.
            mailbox.publish(replace(plan, targets_v=(-.2,)))

    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    result = adapter.ramp_vout(2, .2, progress=observe, live_targets=mailbox)
    assert result.requested_v == .2 and result.actual_v == pytest.approx(.2, abs=.001)
    assert mailbox.take().targets_v == (-.2,)
    assert not mailbox.publish(plan)


@pytest.mark.parametrize("theme,width,operation", [
    ("light", 1360, "send"), ("dark", 1360, "readback"), ("dark", 960, "send")])
def test_live_ui_heartbeat_edits_and_two_curves_during_blocked_transport(
        workspace, application, monkeypatch, theme, width, operation):
    host, page, _, directory = workspace
    workflow = page.field_workflow
    coordinator_for(host, workflow, page._settings)
    apply_application_theme(application, theme)
    host.resize(width, 880)
    page.views.setCurrentIndex(2)
    QTest.qWait(200)
    entered, release = Event(), Event()
    original = SimulatedMokeBoxTransport.send
    original_read = SimulatedMokeBoxTransport.recv_exact
    blocked = [False]

    def hold_first_set(transport, raw):
        if MokeFrame.decode(raw).record_type == MokeCommandType.SET_VOUT:
            if operation == "send" and not blocked[0]:
                blocked[0] = True
                entered.set()
                release.wait(4)
            time.sleep(.05)
        original(transport, raw)

    def hold_first_readback(transport, count):
        if operation == "readback" and not blocked[0]:
            blocked[0] = True
            entered.set()
            release.wait(4)
        return original_read(transport, count)

    monkeypatch.setattr(SimulatedMokeBoxTransport, "send", hold_first_set)
    monkeypatch.setattr(SimulatedMokeBoxTransport, "recv_exact", hold_first_readback)
    ticks, bounds, confirmed = [], [], []
    heartbeat = QTimer(host)
    heartbeat.setInterval(10)
    heartbeat.timeout.connect(lambda: ticks.append(time.monotonic()))
    heartbeat.start()
    workflow.quick_bounds_changed.connect(lambda: bounds.append(True))
    workflow.voltage_confirmed.connect(lambda channel, voltage: confirmed.append(voltage))
    try:
        workflow.live_control_switch.setChecked(True)
        workflow.target.setText("400 mV")
        wait_for(application, entered.is_set)
        thread = workflow._thread
        ticks.clear()
        for text in ("300 mV", "-150 mV", "-200 mV"):
            workflow.target.setText(text)
            QTest.qWait(40)
        QTest.qWait(450)  # Live publishes latest target while transport is still blocked.
        assert len(ticks) >= 20, "The UI event loop stalled waiting for the device"
        assert workflow._thread is thread and workflow.busy
        assert workflow.target.isEnabled() and workflow.voltage_slider.isEnabled()
        assert workflow.target.text() == "-200 mV"
        assert not bounds, "Voltage edits must not reconstruct every QuickControls range"
        history = workflow.voltage_history
        assert history.requested_points[-1][1] == -.2
        assert history.requested_curve.yData[-1] == -.2
        assert not confirmed or confirmed[-1] != -.2
        assert all(point[1] != -.2 for point in history.points)
        assert history.requested_curve.isVisible() and history.voltage_curve.isVisible()
        assert history.width() > 240 and workflow.target.width() > 100
        screenshot = directory / f"live-wait-{theme}-{width}.png"
        assert host.grab().save(str(screenshot))
        artifacts = Path("artifacts/moke-live-responsive")
        artifacts.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(screenshot, artifacts / screenshot.name)
        release.set()
        wait_for(application, lambda: not workflow.busy)
        QTest.qWait(500)
        assert workflow._last_voltages[2] == pytest.approx(-.2, abs=.001)
        assert len(workflow._test_voltage_completions) == 1  # Retarget one transaction, no replay.
        assert max(confirmed) < .2
        assert history.points[-1][1] == pytest.approx(-.2, abs=.001)
        assert history.requested_points[-1][1] == -.2
        assert workflow.live_control_switch.isChecked()
        assert not workflow._live_pending
        assert max(b - a for a, b in zip(ticks, ticks[1:])) < .25
        assert host.grab().save(str(artifacts / f"live-confirmed-{theme}-{width}.png"))
    finally:
        release.set()
        heartbeat.stop()


def test_requested_history_invalid_gap_expiry_and_prediction_separation(application, monkeypatch):
    from app.devices.moke_box.ui.voltage_history import MokeVoltageHistory
    from types import SimpleNamespace

    clock = [1000.0]
    monkeypatch.setattr("app.devices.moke_box.ui.voltage_history.time",
                        SimpleNamespace(monotonic=lambda: clock[0]))
    history = MokeVoltageHistory()
    history.resize(900, 500)
    history.show()
    try:
        history.append_requested(.4)
        clock[0] += 1
        history.append_requested(None)
        clock[0] += 1
        history.append_requested(-.2)
        history.append(.1)
        QTest.qWait(80)
        assert list(history.requested_curve.yData)[0] == .4
        assert math.isnan(history.requested_curve.yData[1])
        assert history.requested_curve.yData[-1] == -.2
        assert list(history.voltage_curve.yData) == [.1]
        assert all(math.isnan(point[2]) for point in history.points)
        history.window_selector.setCurrentIndex(history.window_selector.findData(10.0))
        assert history.item.vb.viewRange()[0] == pytest.approx([-10, 0])
        assert len(history.requested_points) == 3  # Display zoom never discards history.
        clock[0] += 181
        QTest.qWait(80)
        assert not history.points and not history.requested_points
        history.clear()
        assert history.requested_curve.xData is None
    finally:
        history.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_quick_controls_keep_draft_and_confirmed_dac_separate_during_live(
        workspace, application, monkeypatch, theme):
    host, page, _, _ = workspace
    workflow = page.field_workflow
    coordinator = coordinator_for(host, workflow, page._settings)
    apply_application_theme(application, theme)
    page.views.setCurrentIndex(2)
    settings = QSettings("LabControl", "LabControl")
    keys = ("quick_controls/targets", "quick_controls/outputs", "quick_controls/geometry")
    saved = {key: (settings.contains(key), settings.value(key)) for key in keys}
    window = QuickControlsWindow(coordinator, host)
    target = "moke_box.vout2.voltage"
    entered, release = Event(), Event()
    original = SimulatedMokeBoxTransport.send

    def hold_first_set(transport, raw):
        if MokeFrame.decode(raw).record_type == MokeCommandType.SET_VOUT and not entered.is_set():
            entered.set()
            release.wait(4)
        original(transport, raw)

    monkeypatch.setattr(SimulatedMokeBoxTransport, "send", hold_first_set)
    try:
        window.set_output_targets(())
        window.set_targets((target,))
        window.resize(480, 650)
        window.show()
        QTest.qWait(150)
        row = window._rows[target]
        workflow.live_control_switch.setChecked(True)
        workflow.target.setText("400 mV")
        wait_for(application, entered.is_set)
        thread = workflow._thread
        assert row.value.text() == "400 mV"
        row.value.setText("-200 mV")
        row.value.setFocus()
        row.value.setSelection(0, 4)
        row.submit()  # A QuickControls commit updates the same live transaction.
        assert workflow.target.text() == "-200 mV"
        assert workflow._thread is thread
        assert "0.000000 V" in row.confirmed_dac.text()
        assert row.confirmed_dac.isVisibleTo(window)
        QTest.qWait(450)
        assert row.value.text() == "-200 mV" and row.value.selectedText() == "-200"
        release.set()
        wait_for(application, lambda: not workflow.busy)
        QTest.qWait(500)
        assert row.value.text() == "-200 mV", "Readback overwrote the operator draft"
        assert row.value.selectedText() == "-200"
        assert "-0.199890 V" in row.confirmed_dac.text()
        assert row.confirmed_dac.width() > 100 and row.height() > 120
        assert len(workflow._test_voltage_completions) == 1
        artifacts = Path("artifacts/moke-live-responsive")
        artifacts.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(artifacts / f"quick-live-{theme}.png"))
    finally:
        release.set()
        window.close()
        for key, (existed, value) in saved.items():
            settings.setValue(key, value) if existed else settings.remove(key)


def test_authorization_revoked_during_live_stops_pending_target_and_confirms_zero(
        workspace, application, monkeypatch):
    _, page, _, _ = workspace
    workflow = page.field_workflow
    original = SimulatedMokeBoxTransport.send
    entered, release = Event(), Event()

    def hold_first_set(transport, raw):
        if MokeFrame.decode(raw).record_type == MokeCommandType.SET_VOUT and not entered.is_set():
            entered.set()
            release.wait(4)
        original(transport, raw)

    monkeypatch.setattr(SimulatedMokeBoxTransport, "send", hold_first_set)
    readings = []
    workflow.voltage_confirmed.connect(lambda channel, voltage: readings.append(voltage))
    try:
        workflow.live_control_switch.setChecked(True)
        workflow.target.setText("400 mV")
        wait_for(application, entered.is_set)

        def deny(operation, request):
            raise SafetyViolation("Live permission revoked")

        workflow._authorize = deny
        workflow.target.setText("-200 mV")
        wait_for(application, lambda: not workflow.live_control_switch.isChecked())
        assert workflow._cancel.is_set() and not workflow.has_pending_live_target
        release.set()
        wait_for(application, lambda: not workflow.busy)
        assert workflow._last_voltages[2] == 0
        assert all(voltage >= 0 for voltage in readings)
        assert not workflow._live_timer.isActive()
    finally:
        release.set()


def test_invalid_draft_during_live_does_not_replay_old_pending_target_or_disable_editor(
        workspace, application, monkeypatch):
    _, page, _, _ = workspace
    workflow = page.field_workflow
    original = SimulatedMokeBoxTransport.recv_exact
    entered, release = Event(), Event()

    def hold_initial_readback(transport, count):
        if not entered.is_set():
            entered.set()
            release.wait(4)
        return original(transport, count)

    monkeypatch.setattr(SimulatedMokeBoxTransport, "recv_exact", hold_initial_readback)
    try:
        workflow.live_control_switch.setChecked(True)
        workflow.target.setText("400 mV")
        wait_for(application, entered.is_set)
        workflow.target.setText("-200 mV")
        QTest.qWait(450)
        workflow.target.setText("5 V")  # Finite but outside the operator envelope.
        assert not workflow.has_pending_live_target
        release.set()
        wait_for(application, lambda: not workflow.busy)
        QTest.qWait(500)
        assert workflow._last_voltages[2] == pytest.approx(.4, abs=.001)
        assert len(workflow._test_voltage_completions) == 1
        assert workflow.live_control_switch.isChecked()
        assert workflow.target.isEnabled() and workflow.target.text() == "5 V"
        assert not workflow.set_button.isEnabled()
        workflow.target.setText("-100 mV")
        wait_for(application, lambda: not workflow.busy and len(workflow._test_voltage_completions) == 2)
        assert workflow._last_voltages[2] == pytest.approx(-.1, abs=.001)
    finally:
        release.set()
