"""Exercise the real dual-channel worker and store without laboratory output."""

import json
import threading
from dataclasses import replace

import pytest

from app.devices.keithley_2600.characterization.field_worker import (
    FieldSeriesWorker,
    restore_field_policies,
)
from app.domain.errors import DeviceError
from tests.test_keithley_field_series import make_runner


@pytest.mark.parametrize("interrupt", [False, "stop", "emergency_off", "field_read_failure"])
def test_field_worker_uses_reserved_controller_thread(tmp_path, interrupt):
    import time
    from PySide6.QtWidgets import QApplication
    from app.ui.workers import DeviceController
    from app.domain.models import DeviceState
    app = QApplication.instance() or QApplication([])
    template, device = make_worker(tmp_path)
    device.state = DeviceState.VERIFIED
    device.disconnect = lambda: None
    def emergency_off():
        device.set_output("A", False)
        device.set_output("B", False)
    device.emergency_off = emergency_off
    thread_ids = []
    interruption_indices = []
    measure = device.measure
    def traced_measure(channel):
        thread_ids.append(threading.get_ident())
        if interrupt == "field_read_failure" and channel == "B" and any(call[0] == "measure" and call[1] == "A" for call in device.calls):
            raise DeviceError("injected B read timeout after acquired A")
        result = measure(channel)
        if interrupt in ("stop", "emergency_off") and channel == "A" and not interruption_indices:
            interruption_indices.append(len(device.calls))
            if interrupt == "emergency_off":
                controller.call("emergency_off")
            else:
                worker.request_stop()
        return result
    device.measure = traced_measure
    controller = DeviceController(device)
    lease = controller.acquire_run_lease()
    worker = FieldSeriesWorker(lease, template.settings, template.config, tmp_path / "queued_series",
                               template.expected_policies, template.restore_policies)
    try:
        worker.start()
        deadline = time.monotonic() + 20
        while worker.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert not worker.isRunning()
        expected = "fault" if interrupt == "field_read_failure" else "cancelled" if interrupt else "completed_with_skips"
        assert worker.outcome.status == expected, worker.outcome.errors
        assert worker.outcome.outputs_off and worker.outcome.policies_restored
        assert thread_ids and len(set(thread_ids)) == 1
        assert thread_ids[0] != threading.get_ident()
        assert device.policies == template.restore_policies
        if interrupt == "field_read_failure":
            from app.devices.keithley_2600.characterization.field_reader import load_field_series
            from app.devices.keithley_2600.characterization.field_journal import export_field_journal
            journal = (worker.directory / "events.jsonl").read_text(encoding="utf-8")
            assert '"kind": "sample_raw"' in journal
            assert '"kind": "sample_point"' not in journal
            assert not any(device.outputs.values())
            recovered = export_field_journal(load_field_series(worker.directory))
            assert recovered["raw_readings"] == 1
            assert "raw_only_field_after_unconfirmed" in (worker.directory / recovered["csv"]).read_text(encoding="utf-8")
        if interruption_indices:
            assert not any(call[0] == "output" and call[2] for call in device.calls[interruption_indices[0]:])
    finally:
        worker.request_stop()
        worker.wait(5000)
        lease.release()
        controller.close()
        app.processEvents()


def make_worker(tmp_path):
    runner, device, _, _ = make_runner()
    device.identity = "TEST ONLY: independent fake"
    device.interruption_event = threading.Event()
    device.policies = {"A": "warn_clamp", "B": "skip"}

    def set_policy(channel, policy):
        assert not any(device.outputs.values()), "Policy mutation requires both outputs OFF"
        device.calls.append(("policy", channel, policy))
        device.policies[channel] = policy
        return policy

    device.set_compliance_policy = set_policy
    worker = FieldSeriesWorker(device, runner.settings, runner.config, tmp_path / "series",
                               device.policies, device.policies)
    return worker, device


def test_both_stop_before_enable_and_restore_after_both_off(tmp_path):
    worker, device = make_worker(tmp_path)
    worker.run()
    outcome = worker.outcome
    assert outcome.status == "completed_with_skips"
    assert outcome.outputs_off and outcome.policies_restored
    assert not outcome.errors
    assert device.policies == {"A": "warn_clamp", "B": "skip"}
    first_enable = next(i for i, call in enumerate(device.calls) if call[:1] == ("output",) and call[2])
    assert device.calls.index(("policy", "A", "stop")) < first_enable
    assert device.calls.index(("policy", "B", "stop")) < first_enable
    restore_index = device.calls.index(("policy", "A", "warn_clamp"))
    assert device.calls[restore_index - 2:restore_index] == [("output", "A", False), ("output", "B", False)]
    manifest = json.loads((outcome.directory / "series.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed_with_skips"
    assert manifest["entries"][1]["status"] == "skipped_field_compliance"
    assert manifest["entries"][2]["status"] == "completed"
    assert manifest["provenance"]["restore_policies"] == device.policies


@pytest.mark.parametrize("a_policy", ["stop", "warn_clamp"])
@pytest.mark.parametrize("b_policy", ["stop", "warn_clamp"])
@pytest.mark.parametrize("cancel_during_measurement", [False, True])
def test_each_channel_restores_its_own_original_mode(tmp_path, a_policy, b_policy, cancel_during_measurement):
    worker, device = make_worker(tmp_path)
    original = {"A": a_policy, "B": b_policy}
    device.policies = dict(original)
    worker.expected_policies = dict(original)
    worker.restore_policies = dict(original)
    measure = device.measure
    def checked_measure(channel):
        assert device.policies == {"A": "stop", "B": "stop"}
        result = measure(channel)
        if channel == "A" and cancel_during_measurement:
            worker.request_stop()
        return result
    device.measure = checked_measure
    worker.run()
    assert worker.outcome.outputs_off and worker.outcome.policies_restored
    assert device.policies == original
    assert not any(device.outputs.values())
    assert worker.outcome.status == ("cancelled" if cancel_during_measurement else "completed_with_skips")


@pytest.mark.parametrize("channel", ["A", "B"])
def test_setter_mutates_then_raises_rolls_back_both(tmp_path, channel):
    worker, device = make_worker(tmp_path)
    original = device.set_compliance_policy

    def fail_after_mutation(ch, policy):
        result = original(ch, policy)
        if ch == channel and policy == "stop":
            raise DeviceError("injected policy failure after mutation")
        return result

    device.set_compliance_policy = fail_after_mutation
    worker.run()
    assert worker.outcome.status == "fault"
    assert worker.outcome.outputs_off and worker.outcome.policies_restored
    assert device.policies == {"A": "warn_clamp", "B": "skip"}
    assert not any(call[0] == "output" and call[2] for call in device.calls)


def test_stale_confirmation_does_not_overwrite_new_policy(tmp_path):
    worker, device = make_worker(tmp_path)
    device.policies["B"] = "warn_clamp"
    worker.run()
    assert worker.outcome.status == "fault"
    assert device.policies == {"A": "warn_clamp", "B": "warn_clamp"}
    assert device.calls == []
    assert not worker.directory.exists()


def test_cancel_before_start_does_not_mutate_policy_or_enable(tmp_path):
    worker, device = make_worker(tmp_path)
    worker.request_stop()
    worker.run()
    assert worker.outcome.status == "cancelled"
    assert worker.outcome.outputs_off and worker.outcome.policies_restored
    assert device.calls == []


def test_restore_attempts_b_off_after_a_failure_and_keeps_stop(tmp_path):
    worker, device = make_worker(tmp_path)
    device.policies = {"A": "stop", "B": "stop"}
    device.fail_off = "A"
    off, restored, errors = restore_field_policies(device, worker.restore_policies)
    assert not off and not restored and errors
    assert ("output", "B", False) in device.calls
    assert device.policies == {"A": "stop", "B": "stop"}


def test_restore_failure_attempts_other_channel_and_reports_fault(tmp_path):
    worker, device = make_worker(tmp_path)
    original = device.set_compliance_policy

    def fail_restore(channel, policy):
        if channel == "A" and policy == "warn_clamp":
            raise DeviceError("injected restore failure")
        return original(channel, policy)

    device.set_compliance_policy = fail_restore
    worker.run()
    assert worker.outcome.status == "fault"
    assert worker.outcome.outputs_off and not worker.outcome.policies_restored
    assert device.policies == {"A": "stop", "B": "skip"}
    assert any("A policy restore" in error for error in worker.outcome.errors)


def test_invalid_restore_snapshot_is_rejected_without_mutation(tmp_path):
    worker, device = make_worker(tmp_path)
    worker.restore_policies["B"] = "unknown"
    worker.run()
    assert worker.outcome.status == "fault"
    assert device.calls == []
    assert not worker.directory.exists()


def test_last_target_sample_compliance_is_not_reported_as_success(tmp_path):
    worker, device = make_worker(tmp_path)
    worker.config = replace(
        worker.config, currents_a=(0,), continue_after_sample_compliance=False,
    )
    original_measure = device.measure

    def sample_compliance(channel):
        measurement = original_measure(channel)
        if channel == "A":
            device.outputs[channel] = False
            return replace(measurement, output_enabled=False, compliance_detected=True,
                           compliance_stop_required=True)
        return measurement

    device.measure = sample_compliance
    worker.run()
    assert worker.outcome.status == "stopped_on_compliance"
    assert worker.outcome.outputs_off and worker.outcome.policies_restored
    manifest = json.loads((worker.directory / "series.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "stopped_on_compliance"
    assert manifest["entries"][0]["status"] == "sample_compliance"


@pytest.mark.parametrize("interrupt", [None, "stop", "emergency_off"])
def test_field_series_with_real_adapter_and_visa_simulator(tmp_path, interrupt):
    import time
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from app.ui.workers import DeviceController
    from app.devices.keithley_2600 import KeithleyAdapter
    from app.devices.simulators import SimulatedVisaFactory
    from app.devices.keithley_2600.characterization.field_reader import load_field_series
    template, _ = make_worker(tmp_path)
    raw = template.settings.model_dump(mode="python")
    raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True  # Simulator only.
    template.settings = type(template.settings).model_validate(raw)
    adapter = KeithleyAdapter(template.settings, session_factory=SimulatedVisaFactory(
        "keithley", keithley_resistance_ohm=5000))
    adapter.connect()
    controller = None
    lease = None
    worker = None
    app = QApplication.instance() or QApplication([])
    try:
        from app.domain.errors import SafetyViolation
        for channel in ("A", "B"):
            with pytest.raises(SafetyViolation):
                adapter.last_source_request(channel)
        policies = {channel: adapter.compliance_policy(channel) for channel in ("A", "B")}
        controller = DeviceController(adapter)
        lease = controller.acquire_run_lease()
        worker = FieldSeriesWorker(lease, template.settings, template.config,
            tmp_path / "simulated_series", policies, policies)
        interrupted = []
        def on_event(kind, data):
            if interrupt and kind == "sample_point" and not interrupted:
                interrupted.append(data)
                if interrupt == "stop":
                    worker.request_stop()
                else:
                    controller.call("emergency_off")
        worker.event.connect(on_event, Qt.ConnectionType.DirectConnection)
        worker.start()
        deadline = time.monotonic() + 20
        while worker.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        assert not worker.isRunning()
        assert worker.outcome.status == ("cancelled" if interrupt else "completed_with_skips"), worker.outcome.errors
        assert worker.outcome.outputs_off and worker.outcome.policies_restored
        for channel in ("A", "B"):
            lease.confirm_output_off(channel)
            assert lease.compliance_policy(channel) == policies[channel]
        series = load_field_series(worker.directory)
        if interrupt:
            assert len(interrupted) == 1
            assert series.curves[0].dataset is not None, worker.outcome.errors
            assert len(series.curves[0].dataset.points) == 1
            assert all(curve.dataset is None for curve in series.curves[1:])
            return
        assert [curve.status for curve in series.curves] == [
            "completed", "skipped_field_compliance", "completed", "completed"]
        assert series.curves[1].dataset is None
        for curve in (series.curves[0], series.curves[2], series.curves[3]):
            assert len(curve.dataset.points) == 3
            assert all(point.true_resistance_ohm == pytest.approx(5000) for point in curve.dataset.points)
    finally:
        if worker is not None:
            worker.request_stop()
            worker.wait(5000)
        if lease is not None:
            lease.release()
        if controller is not None:
            controller.close()
        else:
            adapter.disconnect()
        app.processEvents()
