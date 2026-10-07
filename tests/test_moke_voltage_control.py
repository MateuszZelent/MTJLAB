from dataclasses import replace
from threading import Event
from threading import Timer
import time

import pytest

from app.devices.moke_box.adapter import MokeBoxAdapter
from app.devices.moke_box.models import MokeBoxConfig
from app.devices.moke_box.protocol import MokeCommandType, MokeFrame, decode_voltage
from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
from app.devices.simulation import SimulationContext
from app.domain.errors import DeviceError, RunInterrupted, SafetyViolation
from app.domain.models import DeviceState
from app.domain.quantities import DIMENSION_VOLTAGE_SLEW, parse_quantity
from app.safety.moke_box import MokeControlProfile, MokeVoltagePlan
from app.settings.models import StationSettings
from tests.helpers import loaded_settings


class RecordingTransport(SimulatedMokeBoxTransport):
    def __init__(self, context):
        super().__init__(context)
        self.sent = []
        self.fail_next_write = False

    def send(self, frame):
        self.sent.append(frame)
        if self.fail_next_write and MokeFrame.decode(frame).record_type == MokeCommandType.SET_VOUT:
            raise TimeoutError("injected lost write acknowledgement")
        super().send(frame)


def controlled_adapter(*, minimum=-1.0, maximum=1.0, simulation=True, minimum_settling_s=2.0):
    context = SimulationContext(seed=17)
    profile = MokeControlProfile(2, "SIM::COIL", minimum, maximum, 0, 0.05, 1, 0.05, 30, "simulation", simulation,
                                 minimum_settling_s=minimum_settling_s)
    transport = RecordingTransport(context)
    adapter = MokeBoxAdapter(MokeBoxConfig(
        "SIM::MOKE::INSTR", allow_vout_control=True, allowed_vout_channels=(2,), control_profile=profile,
    ), transport)
    adapter.connect()
    return adapter, transport, profile


def plan_for(profile, targets=(0.2,), minimum=-0.5, maximum=0.5):
    return MokeVoltagePlan(profile.fingerprint, profile.channel, minimum, maximum, targets)


def mutations(transport):
    return [MokeFrame.decode(raw) for raw in transport.sent
            if MokeFrame.decode(raw).record_type == MokeCommandType.SET_VOUT]


def test_initial_voltage_outside_profile_has_exact_diagnostics_and_no_writes():
    from app.devices.moke_box.protocol import set_vout, encode_voltage
    adapter, transport, profile = controlled_adapter(minimum=-0.1, maximum=0.1, minimum_settling_s=0)
    transport.send(set_vout(2, 0.2))
    transport.sent.clear()
    plan = plan_for(profile, targets=(0,), minimum=-0.1, maximum=0.1)
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    with pytest.raises(DeviceError) as error:
        adapter.ramp_vout(2, 0)
    message = str(error.value)
    expected = decode_voltage(*encode_voltage(0.2))
    assert f"VOUT 2 readback {expected:+.9g} V" in message
    assert "approved range [-0.1, +0.1] V" in message
    assert "requested target +0 V" in message
    assert "Ramp was not started" in message
    assert not mutations(transport)


def test_repeated_stop_confirms_fresh_zero_without_rewriting_it():
    adapter, transport, profile = controlled_adapter(minimum_settling_s=0)
    plan = plan_for(profile, targets=(0.2,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    adapter.ramp_vout(2, 0.2)
    assert adapter.stop_vout(2).safe_target_confirmed
    transport.sent.clear()
    result = adapter.stop_vout(2)
    assert result.safe_target_confirmed and result.actual_v == 0
    assert transport.sent  # Fresh readback; cached zero is never sufficient.
    assert not mutations(transport)
    assert not adapter._armed


def test_repeated_stop_corrects_external_change_after_confirmed_zero():
    from app.devices.moke_box.protocol import set_vout
    adapter, transport, _ = controlled_adapter(minimum_settling_s=0)
    assert adapter.stop_vout(2).safe_target_confirmed
    transport.send(set_vout(2, 0.2))  # Another actor changes the DAC after shutdown.
    transport.sent.clear()
    result = adapter.stop_vout(2)
    assert result.safe_target_confirmed and result.actual_v == 0
    assert len(mutations(transport)) > 1
    assert all(frame.channel == 2 for frame in mutations(transport))


def test_repeated_stop_readback_failure_does_not_reuse_cached_zero(monkeypatch):
    adapter, _, _ = controlled_adapter(minimum_settling_s=0)
    assert adapter.stop_vout(2).safe_target_confirmed
    def failed_readback():
        raise TimeoutError("injected shutdown readback failure")
    monkeypatch.setattr(adapter, "_read_vouts_from_transport", failed_readback)
    with pytest.raises(DeviceError):
        adapter.stop_vout(2)
    assert not adapter._safe_target_confirmed


@pytest.mark.parametrize("initial", [0.4, -0.4])
def test_dac_zero_ramps_monotonically_with_physical_step_and_slew_limits(initial):
    adapter, transport, profile = controlled_adapter(simulation=False, minimum_settling_s=0)
    plan = plan_for(profile, targets=(initial,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    start = adapter.ramp_vout(2, initial).actual_v
    transport.sent.clear()
    began = time.monotonic()
    result = adapter.stop_vout()
    elapsed = time.monotonic() - began
    voltages = [decode_voltage(frame.msb, frame.lsb) for frame in mutations(transport)]
    assert len(voltages) > 1  # No direct jump to zero.
    assert voltages[-1] == 0
    previous = start
    for voltage in voltages:
        assert abs(voltage) < abs(previous)
        assert abs(voltage - previous) <= profile.maximum_step_v
        previous = voltage
    assert elapsed >= abs(start) / profile.maximum_slew_v_s
    assert all(frame.channel == 2 for frame in mutations(transport))
    assert result.safe_target_confirmed and result.actual_v == 0
    assert adapter.read_vouts()[2] == 0


def test_dac_zero_write_failure_never_reports_zero_or_retries_the_write():
    adapter, transport, profile = controlled_adapter()
    plan = plan_for(profile, targets=(0.4,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    adapter.ramp_vout(2, 0.4)
    transport.sent.clear()
    transport.fail_next_write = True
    with pytest.raises(DeviceError):
        adapter.stop_vout()
    assert len(mutations(transport)) == 1
    assert not adapter.safe_target_confirmed
    assert adapter.state is DeviceState.UNKNOWN


class LaggedReadbackTransport(RecordingTransport):
    """A SET is processed only after a few complete, stale readback responses."""

    def __init__(self, context, lag_reads=2):
        super().__init__(context)
        self.lag_reads = lag_reads
        self.pending_write = None
        self.queries_after_write = 0

    def send(self, raw):
        frame = MokeFrame.decode(raw)
        if frame.record_type == MokeCommandType.SET_VOUT:
            if decode_voltage(frame.msb, frame.lsb) != 0:
                self.sent.append(raw)
                self.pending_write = raw
                self.queries_after_write = 0
                return
            self.pending_write = None  # Approved shutdown is processed normally.
        if frame.record_type == MokeCommandType.READBACK_VOUT and self.pending_write:
            self.queries_after_write += 1
            if self.lag_reads is not None and self.queries_after_write > self.lag_reads:
                SimulatedMokeBoxTransport.send(self, self.pending_write)
                self.pending_write = None
        super().send(raw)


def lagged_adapter(*, lag_reads=2, simulation=False):
    _, _, profile = controlled_adapter(simulation=simulation, minimum_settling_s=0)
    transport = LaggedReadbackTransport(SimulationContext(seed=17), lag_reads)
    adapter = MokeBoxAdapter(MokeBoxConfig(
        "SIM::MOKE::INSTR", allow_vout_control=True, allowed_vout_channels=(2,),
        control_profile=profile,
    ), transport)
    adapter.connect()
    plan = plan_for(profile, (0.01,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    return adapter, transport


def test_delayed_dac_readback_is_polled_without_resending_setpoint():
    adapter, transport = lagged_adapter()
    result = adapter.ramp_vout(2, 0.01)
    assert result.actual_v == pytest.approx(0.01, abs=0.001)
    assert transport.queries_after_write == 3
    assert len(mutations(transport)) == 1
    assert not adapter.safe_target_confirmed


def test_ignored_write_has_bounded_confirmation_diagnostics_and_confirmed_zero():
    adapter, transport = lagged_adapter(lag_reads=None)
    started = time.monotonic()
    with pytest.raises(DeviceError, match="VOUT2 readback differs") as error:
        adapter.ramp_vout(2, 0.01)
    assert time.monotonic() - started < 1
    assert "requested +0.010" in str(error.value)
    assert "received +0.000000 V" in str(error.value)
    assert "VOUT7=" in str(error.value)
    assert "approved DAC zero was confirmed" in str(error.value)
    assert len(mutations(transport)) == 2  # One SET, then one approved zero.
    assert adapter.safe_target_confirmed
    assert adapter.read_vouts()[2] == 0
    with pytest.raises(SafetyViolation, match="not armed"):
        adapter.ramp_vout(2, 0.01)


@pytest.mark.parametrize("fault", ["checksum", "timeout"])
def test_invalid_confirmation_closes_without_polling_or_repeating_write(fault):
    adapter, transport = lagged_adapter()
    receive = transport.recv_exact

    def bad_confirmation(count):
        raw = receive(count)
        if transport.pending_write is not None:
            if fault == "timeout":
                raise TimeoutError("injected confirmation transport timeout")
            return raw[:-1] + bytes([raw[-1] ^ 1])
        return raw

    transport.recv_exact = bad_confirmation
    with pytest.raises(DeviceError, match="checksum|transport timeout"):
        adapter.ramp_vout(2, 0.01)
    assert transport.queries_after_write == 1
    assert len(mutations(transport)) == 1
    assert not adapter.connected
    assert not adapter.safe_target_confirmed
    assert adapter.state is DeviceState.UNKNOWN


def test_cancel_interrupts_delayed_confirmation_and_confirms_zero():
    adapter, transport = lagged_adapter(lag_reads=None)
    cancel = Event()
    send = transport.send

    def cancel_after_write(frame):
        send(frame)
        if transport.pending_write is not None:
            cancel.set()

    transport.send = cancel_after_write
    with pytest.raises(RunInterrupted, match="confirmation"):
        adapter.ramp_vout(2, 0.01, cancel=cancel)
    assert len(mutations(transport)) == 2
    assert adapter.safe_target_confirmed
    assert adapter.read_vouts()[2] == 0


def test_prepare_and_arm_do_not_mutate_and_prearm_write_is_rejected():
    adapter, transport, profile = controlled_adapter()
    plan = plan_for(profile)
    with pytest.raises(SafetyViolation, match="not armed"):
        adapter.ramp_vout(2, 0.2)
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    assert not mutations(transport)


@pytest.mark.parametrize("targets", [(float("nan"),), (float("inf"),), (True,), (0.501,), (-0.501,)])
def test_invalid_plan_sends_no_mutation(targets):
    adapter, transport, profile = controlled_adapter()
    with pytest.raises(SafetyViolation):
        adapter.configure_voltage_plan(plan_for(profile, targets))
    assert not mutations(transport)


def test_ramp_checks_every_readback_and_keeps_steps_and_other_channels_bounded():
    adapter, transport, profile = controlled_adapter()
    plan = plan_for(profile, (0.5, -0.5))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    positive = adapter.ramp_vout(2, 0.5)
    negative = adapter.ramp_vout(2, -0.5)
    assert 0.499 < positive.actual_v <= 0.5
    assert -0.5 <= negative.actual_v < -0.499
    previous = 0
    for frame in mutations(transport):
        actual = decode_voltage(frame.msb, frame.lsb)
        assert frame.channel == 2
        assert abs(actual - previous) <= profile.maximum_step_v
        previous = actual
    assert all(value == 0 for ch, value in adapter.read_vouts().items() if ch != 2)
    assert len(transport.sent) >= 2 * len(mutations(transport))
    with pytest.raises(SafetyViolation, match="not armed"):
        adapter.ramp_vout(2, -0.5)


def test_zero_shutdown_is_permitted_outside_positive_working_range():
    adapter, transport, profile = controlled_adapter()
    plan = plan_for(profile, (0.4,), minimum=0.1, maximum=0.5)
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    adapter.ramp_vout(2, 0.4)
    stop = adapter.stop_vout()
    assert stop.safe_target_confirmed
    assert adapter.read_vouts()[2] == 0
    assert adapter.state is DeviceState.UNKNOWN  # no power/current measurement


def test_preexisting_cancel_still_returns_dac_to_zero_and_revokes_arm():
    adapter, transport, profile = controlled_adapter()
    plan = plan_for(profile, (0.2, 0.4))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    adapter.ramp_vout(2, 0.2)
    cancel = Event()
    cancel.set()
    with pytest.raises(RunInterrupted):
        adapter.ramp_vout(2, 0.4, cancel=cancel)
    assert adapter.safe_target_confirmed
    assert adapter.read_vouts()[2] == 0
    with pytest.raises(SafetyViolation):
        adapter.ramp_vout(2, 0.4)


def test_write_timeout_is_not_retried_and_state_is_unknown():
    adapter, transport, profile = controlled_adapter()
    plan = plan_for(profile)
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    transport.fail_next_write = True
    with pytest.raises(DeviceError, match="injected"):
        adapter.ramp_vout(2, 0.2)
    assert len(mutations(transport)) == 1
    assert not adapter.connected
    assert adapter.state is DeviceState.UNKNOWN
    assert not adapter.safe_target_confirmed


def test_reconnect_does_not_retain_arm_and_wrong_profile_cannot_arm():
    adapter, transport, profile = controlled_adapter()
    plan = plan_for(profile)
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    adapter.disconnect()
    adapter.connect()
    with pytest.raises(SafetyViolation):
        adapter.ramp_vout(2, 0.2)
    with pytest.raises(SafetyViolation):
        adapter.configure_voltage_plan(replace(plan, profile_fingerprint="another tor"))


def test_hardware_profile_requires_separate_output_approval_and_units():
    raw = loaded_settings().model_dump(mode="python")
    raw["devices"]["moke_box"].update(enabled=True, protocol_qualified=True, endpoint="127.0.0.1:10001",
                                         allow_vout_control=True, allowed_vout_channels=(2,))
    with pytest.raises(ValueError, match="read-only"):
        StationSettings.model_validate(raw)
    raw["devices"]["moke_box"]["voltage_control"].update(
        approved=True, binding_id="BLS2-coil-longitudinal", qualification_reference="qualification/BLS2-v1",
    )
    settings = StationSettings.model_validate(raw)
    assert settings.moke_box.allow_vout_control
    raw["devices"]["moke_box"]["voltage_control"]["maximum_slew"] = "3 A"
    with pytest.raises(ValueError):
        StationSettings.model_validate(raw)
    assert parse_quantity("250 mV/s", DIMENSION_VOLTAGE_SLEW).si_value == 0.25


def test_quantization_never_expands_operator_bounds():
    _, _, profile = controlled_adapter()
    plan = plan_for(profile, (0.5,), minimum=-0.5, maximum=0.5)
    assert 0.499 < plan.applied_voltage(0.5) <= 0.5
    assert -0.5 <= plan.applied_voltage(-0.5) < -0.499


def test_real_clock_hold_enforces_station_floor_and_longer_operator_time_on_fake_transport():
    adapter, transport, profile = controlled_adapter(simulation=False, minimum_settling_s=0.08)
    written_at = []
    send = transport.send

    def record_send(frame):
        if MokeFrame.decode(frame).record_type == MokeCommandType.SET_VOUT:
            written_at.append(time.monotonic())
        send(frame)

    transport.send = record_send
    for voltage, requested_hold, expected_hold in ((0.01, 0, 0.08), (0.02, 0.15, 0.15)):
        plan = replace(plan_for(profile, (voltage,)), settling_s=requested_hold)
        adapter.configure_voltage_plan(plan)
        adapter.arm_voltage_plan(plan)
        result = adapter.ramp_vout(2, voltage)
        assert time.monotonic() - written_at[-1] >= expected_hold
        assert abs(result.actual_v - voltage) < 0.001
    assert len(mutations(transport)) == 2


def test_stop_interrupts_settling_without_waiting_for_full_hold_on_fake_transport():
    adapter, transport, profile = controlled_adapter(simulation=False, minimum_settling_s=2)
    plan = plan_for(profile, (0.01,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    cancel = Event()
    timer = Timer(0.1, cancel.set)
    started = time.monotonic()
    timer.start()
    try:
        with pytest.raises(RunInterrupted, match="settling"):
            adapter.ramp_vout(2, 0.01, cancel=cancel)
    finally:
        timer.cancel()
        timer.join()
    assert time.monotonic() - started < 1
    assert adapter.safe_target_confirmed
    assert adapter.read_vouts()[2] == 0
    assert len(mutations(transport)) == 2


def test_settling_deadline_failure_returns_dac_to_zero_on_fake_transport():
    adapter, _, profile = controlled_adapter(simulation=False, minimum_settling_s=0.2)
    plan = plan_for(profile, (0.01,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    with pytest.raises(DeviceError, match="settling time"):
        adapter.ramp_vout(2, 0.01, deadline_s=0.1)
    assert adapter.safe_target_confirmed
    assert adapter.read_vouts()[2] == 0


@pytest.mark.parametrize("value", ["-1 s", "nan s", "1 V", "30 s"])
def test_invalid_station_settling_settings_are_rejected(value):
    raw = loaded_settings().model_dump(mode="python")
    raw["devices"]["moke_box"]["voltage_control"]["minimum_settling_time"] = value
    with pytest.raises(ValueError):
        StationSettings.model_validate(raw)
