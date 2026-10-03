"""Ramp feedback reports confirmed DAC samples without introducing output commands."""

import time
from threading import Event

import pytest
from PySide6.QtCore import QThread

from app.devices.moke_box.protocol import MokeFrame, MokeCommandType, decode_voltage
from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
from app.domain.errors import RunInterrupted
from app.ui.design_system import apply_application_theme
from tests.test_fluent_moke_field_workflow import application as application, workspace as workspace, wait_for
from tests.test_moke_quick_controls import coordinator_for
from tests.test_moke_voltage_control import controlled_adapter, plan_for


def test_feedback_uses_confirmed_samples_and_retains_command_sequence():
    sequences = []
    for feedback in (False, True):
        adapter, transport, profile = controlled_adapter()
        samples = []
        plan = plan_for(profile, (.2,))
        adapter.configure_voltage_plan(plan)
        adapter.arm_voltage_plan(plan)
        adapter.ramp_vout(2, .2, progress=samples.append if feedback else None)
        adapter.stop_vout(progress=samples.append if feedback else None)
        sequences.append(tuple(transport.sent))
        if feedback:
            assert samples[0].actual_v == 0
            assert samples[-1].actual_v == 0 and samples[-1].phase == "zeroing"
            assert all(sample.channel == 2 and 0 <= sample.fraction <= 1 for sample in samples)
            assert any(sample.actual_v > .1 for sample in samples)
    assert sequences[0] == sequences[1]


def test_physical_ramp_reports_intermediate_readback_and_settling():
    adapter, transport, profile = controlled_adapter(simulation=False, minimum_settling_s=.05)
    samples = []
    plan = plan_for(profile, (.2,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    adapter.ramp_vout(2, .2, progress=samples.append)
    assert len(samples) >= 4
    assert any(0 < sample.actual_v < .19 for sample in samples)
    assert samples[-1].phase == "settling"
    assert [sample.elapsed_s for sample in samples] == sorted(sample.elapsed_s for sample in samples)
    assert [sample.fraction for sample in samples] == sorted(sample.fraction for sample in samples)
    assert samples[-1].actual_v == adapter.read_vouts()[2]


def test_cancel_reports_confirmed_zero_cleanup_and_observer_failure_cannot_block_shutdown(caplog):
    adapter, _, profile = controlled_adapter(simulation=False, minimum_settling_s=0)
    cancel = Event()
    samples = []

    def observe(sample):
        samples.append(sample)
        if sample.phase == "ramping" and sample.actual_v > .05:
            cancel.set()

    plan = plan_for(profile, (.3,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    with pytest.raises(RunInterrupted):
        adapter.ramp_vout(2, .3, cancel=cancel, progress=observe)
    assert samples[-1].phase == "zeroing" and samples[-1].actual_v == 0
    assert adapter.safe_target_confirmed

    def broken_observer(sample):
        raise RuntimeError("Display unavailable")

    assert adapter.stop_vout(progress=broken_observer).safe_target_confirmed
    assert "observer failed" in caplog.text


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_turn_off_field_updates_readout_history_and_quick_controls_during_ramp(
        workspace, application, theme, monkeypatch):
    host, page, controllers, directory = workspace
    workflow = page.field_workflow
    coordinator = coordinator_for(host, workflow, page._settings)
    send = SimulatedMokeBoxTransport.send

    def slow_send(transport, raw):
        if MokeFrame.decode(raw).record_type == MokeCommandType.SET_VOUT:
            time.sleep(.08)
        send(transport, raw)

    monkeypatch.setattr(SimulatedMokeBoxTransport, "send", slow_send)
    apply_application_theme(application, theme)
    host.resize(1360, 880)
    page.views.setCurrentIndex(2)
    wait_for(application, lambda: workflow.control_page.isVisible())
    readings, threads = [], []
    workflow.voltage_confirmed.connect(lambda channel, voltage: (
        readings.append((channel, voltage)), threads.append(QThread.currentThread())))
    assert workflow.zero_button.text() == "Turn off field"
    workflow.target.setText("400 mV")
    workflow.set_button.click()
    wait_for(application, lambda: workflow.busy and any(0 < voltage < .3 for _, voltage in readings))
    assert workflow.ramp_progress_bar.isVisibleTo(host)
    assert "Changing voltage" in workflow.ramp_status.text()
    target = f"moke_box.vout{workflow._profile.channel}.voltage"
    assert 0 < coordinator._confirmed_values[target] < .3
    assert workflow.target.text() == "400 mV"  # Feedback does not alter the armed/draft target.
    assert host.grab().save(str(directory / f"ramp-{theme}.png"))
    wait_for(application, lambda: not workflow.busy)
    assert not workflow.ramp_progress_bar.isVisible()
    readings.clear()
    workflow.zero_button.click()
    wait_for(application, lambda: workflow.busy and any(.1 < voltage < .35 for _, voltage in readings))
    assert "Turning off" in workflow.ramp_status.text()
    assert workflow.target.text() == "0 mV"
    assert not workflow.zero_button.isEnabled()
    assert workflow.ramp_progress_bar.isVisibleTo(host)
    assert len(workflow.voltage_history.points) > 4
    assert host.grab().save(str(directory / f"off-{theme}.png"))
    wait_for(application, lambda: not workflow.busy)
    assert coordinator._confirmed_values[target] == 0
    assert "zero confirmed" in workflow.ramp_status.text()
    assert "not verified" in workflow.ramp_status.text()
    assert "0.000000 V" in workflow.voltage_readout.text()
    assert not workflow.ramp_progress_bar.isVisible()
    assert workflow.zero_button.isEnabled()
    assert all(thread == application.thread() for thread in threads)


def test_turn_off_with_invalid_draft_and_failed_write_keeps_last_readback(workspace, application, monkeypatch):
    host, page, controllers, _ = workspace
    workflow = page.field_workflow
    coordinator = coordinator_for(host, workflow, page._settings)
    states = []
    coordinator.state_changed.connect(lambda *args: states.append(args))
    workflow.target.setText("200 mV")
    workflow.set_button.click()
    wait_for(application, lambda: not workflow.busy)
    workflow.target.setText("invalid voltage")
    assert workflow.zero_button.isEnabled()
    readings = []
    workflow.voltage_confirmed.connect(lambda channel, voltage: readings.append(voltage))
    send = SimulatedMokeBoxTransport.send

    def fail_zero(transport, raw):
        frame = MokeFrame.decode(raw)
        if frame.record_type == MokeCommandType.SET_VOUT and decode_voltage(frame.msb, frame.lsb) == 0:
            raise TimeoutError("Injected shutdown write failure")
        send(transport, raw)

    monkeypatch.setattr(SimulatedMokeBoxTransport, "send", fail_zero)
    coordinator.stop_moke_voltage()
    assert workflow.busy
    wait_for(application, lambda: not workflow.busy)
    assert readings and readings[-1] > 0
    assert "zero confirmed" not in workflow.ramp_status.text()
    assert "failed" in workflow.ramp_status.text()
    assert "Injected shutdown" in workflow.ramp_status.text()
    assert states[-1][1] == "rejected"
    assert not workflow.ramp_progress_bar.isVisible()
