"""Dual-channel field sweep fault and independent-limit tests (no hardware)."""

from dataclasses import replace
from copy import deepcopy
import threading

import pytest

from app.devices.keithley_2600 import KeithleySourceRequest
from app.devices.keithley_2600.adapter import KeithleyMeasurement
from app.devices.keithley_2600.characterization.field_series import FieldSeriesConfig, FieldSeriesRunner
from app.devices.keithley_2600.characterization.models import CharacterizationSweepConfig
from app.devices.keithley_2600.characterization.runner import KeithleyCharacterizationRunner
from app.devices.simulators import simulated_station_settings
from app.domain.errors import DeviceError, SafetyViolation
from app.domain.quantities import parse_quantity
from app.settings.models import StationSettings
from tests.helpers import loaded_settings


def config():
    return FieldSeriesConfig(
        sweep=CharacterizationSweepConfig(start_level_si=1e-6, stop_level_si=3e-6,
                                         points_count=3, compliance_si=0.05, dwell_time_s=0.001, source_range_si=0.01),
        field_source=KeithleySourceRequest(channel="B", mode="current", level_si=0,
                                           compliance_si=0.05, nplc=1, settle_time_s=0.001, source_range_si=100e-6),
        currents_a=(0, 10e-6, 0, 5e-6), ramp_step_a=5e-6, ramp_settle_s=0.001,
        stabilization_s=0, stable_readings=2, current_tolerance_a=1e-8,
        current_tolerance_relative=0.001, max_field_hold_s=10,
    )


class Device:
    def __init__(self, cfg):
        self.requests = {"A": KeithleyCharacterizationRunner.source_request_for_level(cfg.sweep, 0),
                         "B": cfg.field_source}
        self.levels = {"A": 0, "B": 0}
        self.outputs = {"A": False, "B": False}
        self.policies = {"A": "stop", "B": "stop"}
        self.calls = []
        self.fail_off = None

    def compliance_policy(self, channel):
        return self.policies[channel]

    def last_source_request(self, channel):
        return self.requests[channel]

    def configure_source(self, request):
        assert not self.outputs[request.channel]
        self.requests[request.channel] = request
        self.levels[request.channel] = request.level_si
        self.calls.append(("configure", request.channel))
        return request

    def set_output(self, channel, enabled):
        self.calls.append(("output", channel, enabled))
        if not enabled and self.fail_off == channel:
            raise DeviceError("injected shutdown fault")
        self.outputs[channel] = enabled

    def confirm_output_off(self, channel):
        if self.outputs[channel]:
            raise DeviceError(f"{channel} still ON")

    def assert_output_state(self, channel, *, expected_enabled):
        assert self.outputs[channel] == expected_enabled

    def update_source_level(self, channel, *, mode, level_si):
        if channel == "B":
            assert not self.outputs["A"]
        self.levels[channel] = level_si
        self.calls.append(("level", channel, level_si))

    def measure(self, channel):
        current = self.levels[channel]
        resistance = 5000 if channel == "B" else 1000
        voltage = current * resistance
        compliance = abs(voltage) >= self.requests[channel].compliance_si
        if compliance:
            self.outputs[channel] = False
        self.calls.append(("measure", channel, current))
        return KeithleyMeasurement(channel=channel, voltage_v=voltage, current_a=current,
                                    power_w=voltage*current, output_enabled=self.outputs[channel],
                                    compliance_detected=compliance, compliance_stop_required=compliance)

    def recover_from_compliance(self, channel, choice):
        assert choice == "keep_off"
        assert not self.outputs[channel]
        self.calls.append(("recover", channel))
        return {"outputs_confirmed_off": True}

    def ramp_to_zero(self, channel):
        self.levels[channel] = 0


def make_runner(cfg=None, **kwargs):
    cfg = cfg or config()
    device = Device(cfg)
    events, curves = [], []
    raw = simulated_station_settings(loaded_settings()).model_dump(mode="python")
    channels = raw["devices"]["keithley"]["safety"]["channels"]
    channels["A"] = deepcopy(channels["B"])
    runner = FieldSeriesRunner(device, StationSettings.model_validate(raw), cfg,
                               kwargs.pop("write_event", lambda kind, data: events.append((kind, data))),
                               curves.append, **kwargs)
    return runner, device, events, curves


@pytest.mark.parametrize("targets", [(0.0,), (0.0, 5e-6)])
def test_slow_curve_save_cannot_reset_expired_field_deadline(monkeypatch, targets):
    from app.devices.keithley_2600.characterization import field_series
    now = [100.0]
    monkeypatch.setattr(field_series.time, "monotonic", lambda: now[0])
    runner, device, events, curves = make_runner(replace(config(), currents_a=targets))
    def slow_save(entry):
        curves.append(entry)
        now[0] += 11.0
    runner.save_curve = slow_save
    with pytest.raises(DeviceError, match="deadline"):
        runner.run()
    assert len(curves) == 1
    assert len([kind for kind, data in events if kind == "field_start"]) == 1
    assert not any(device.outputs.values())
    assert runner.output_off_confirmed


def test_wait_is_bounded_by_remaining_exposure(monkeypatch):
    from app.devices.keithley_2600.characterization import field_series
    now = [100.0]
    monkeypatch.setattr(field_series.time, "monotonic", lambda: now[0])
    runner, _, _, _ = make_runner()
    runner._hold_started = 91.0
    waits = []
    def wait(seconds):
        waits.append(seconds)
        now[0] += seconds
        return False
    monkeypatch.setattr(runner.cancel, "wait", wait)
    with pytest.raises(DeviceError, match="deadline"):
        runner._wait(50.0)
    assert waits == [1.0]


def test_interval_minus_100_to_100_ma_accepts_exact_100_ua_ramp_boundary():
    start_a = parse_quantity("-100 mA", "current").si_value
    stop_a = parse_quantity("100 mA", "current").si_value
    targets = tuple(start_a + (stop_a - start_a) * index / 9 for index in range(10))
    cfg = replace(
        config(),
        currents_a=targets,
        field_source=replace(config().field_source, source_range_si=1.0),
        ramp_step_a=parse_quantity("100 uA", "current").si_value,
    )
    runner, device, _, _ = make_runner(cfg)
    raw = runner.settings.model_dump(mode="python")
    limits = raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]
    limits["source_current"].update(
        min="-150 mA", max="150 mA", max_abs="150 mA"
    )
    limits["measured_current_trip"].update(min="-160 mA", max="160 mA")
    limits["max_abs_power"] = "10 mW"
    runner.settings = StationSettings.model_validate(raw)

    FieldSeriesRunner.validate(cfg, runner.settings)

    assert device.calls == []


def test_field_compliance_skips_target_and_keeps_zero_and_repeats():
    runner, device, events, curves = make_runner()
    entries = runner.run()
    assert [entry.demanded_current_a for entry in entries] == [0, 10e-6, 0, 5e-6]
    assert [entry.status for entry in entries] == ["completed", "skipped_field_compliance",
                                                  "completed", "completed"]
    assert entries[1].dataset is None
    assert [entry.history_segment for entry in entries] == [0, 0, 1, 1]
    assert ("recover", "B") in device.calls
    assert all(not value for value in device.outputs.values())
    assert runner.output_off_confirmed
    point = entries[-1].dataset.points[0]
    assert point.field_before.measured_current_a == pytest.approx(5e-6)
    assert point.field_after.measured_voltage_v == pytest.approx(0.025)
    assert any(kind == "field_skipped" for kind, _ in events)
    assert len(curves) == 4


@pytest.mark.parametrize("continue_series", [True, False])
def test_real_adapter_sample_compliance_between_field_targets(continue_series):
    from app.devices.keithley_2600.adapter import KeithleyAdapter
    from app.devices.simulators import SimulatedVisaFactory

    cfg = replace(config(), currents_a=(0, 2e-6, 4e-6),
                  continue_after_sample_compliance=continue_series,
                  max_field_hold_s=20,
                  sweep=replace(config().sweep, stop_level_si=20e-6, points_count=20))
    runner, _, events, curves = make_runner(cfg)
    raw = runner.settings.model_dump(mode="python")
    raw["devices"]["keithley"]["safety"].update(
        allow_output_enable=True, compliance_policy="stop", stop_on_compliance=True)
    adapter = KeithleyAdapter(StationSettings.model_validate(raw),
        session_factory=SimulatedVisaFactory("keithley", keithley_resistance_ohm=5000))
    runner.device = adapter
    adapter.connect()
    try:
        entries = runner.run()
        assert len(entries) == (3 if continue_series else 1)
        assert len(curves) == len(entries)
        assert [entry.demanded_current_a for entry in entries] == list(cfg.currents_a[:len(entries)])
        for entry in entries:
            assert entry.status == "sample_compliance"
            assert entry.dataset.completion_status == "stopped_on_compliance"
            assert entry.dataset.points[0].demanded_si == pytest.approx(1e-6)
            assert len(entry.dataset.points) < cfg.sweep.points_count
            assert entry.dataset.points[-1].compliance_active
        assert runner.output_off_confirmed
        for channel in ("A", "B"):
            adapter.confirm_output_off(channel)
    finally:
        adapter.disconnect()


def test_simple_wait_keeps_compliance_skip_and_actual_readback():
    cfg = replace(config(), verify_current_stability=False, stable_readings=1,
                  current_tolerance_a=0, current_tolerance_relative=0)
    runner, device, _, _ = make_runner(cfg)
    measure = device.measure
    def shifted_readback(channel):
        result = measure(channel)
        return replace(result, current_a=result.current_a + 1e-9) if channel == 'B' else result
    device.measure = shifted_readback
    entries = runner.run()
    assert [entry.status for entry in entries] == [
        'completed', 'skipped_field_compliance', 'completed', 'completed']
    assert entries[0].dataset.points[0].field_before.measured_current_a == 1e-9
    assert runner.output_off_confirmed


def test_invalid_last_field_target_rejected_before_any_mutation():
    runner, device, _, _ = make_runner(replace(config(), currents_a=(0, 100.0)))
    with pytest.raises(SafetyViolation):
        runner.run()
    assert device.calls == []


def test_b_must_confirm_stop_even_when_a_is_stop():
    runner, device, _, _ = make_runner()
    device.policies["B"] = "warn_clamp"
    with pytest.raises(SafetyViolation, match="Channel B"):
        runner.run()
    assert device.calls == []


def test_asymmetric_a_b_limits_and_wire_commands_never_cross_channels():
    from app.devices.keithley_2600.adapter import KeithleyAdapter
    from app.devices.simulators import KeithleySimulator

    base_runner, _, _, _ = make_runner()
    raw = base_runner.settings.model_dump(mode="python")
    safety = raw["devices"]["keithley"]["safety"]
    safety.update(allow_output_enable=True, compliance_policy="stop", stop_on_compliance=True)
    safety["channels"]["A"]["lab_limits"]["source_current"].update(
        min="0 A", max="100 uA")
    safety["channels"]["B"]["lab_limits"]["source_current"].update(
        min="0 A", max="10 mA")
    safety["channels"]["B"]["lab_limits"]["ramp_current_step_max"] = "100 uA"
    settings = StationSettings.model_validate(raw)
    cfg = replace(
        config(),
        sweep=replace(config().sweep, start_level_si=1e-6, stop_level_si=3e-6,
                      points_count=3),
        field_source=replace(config().field_source, source_range_si=0.01),
        currents_a=(5e-3, 10e-3), ramp_step_a=99e-6,
        stabilization_s=0, max_field_hold_s=10,
    )

    # 10 mA is above A's 100 uA limit but must be accepted for B only.
    FieldSeriesRunner.validate(cfg, settings)
    with pytest.raises(SafetyViolation):
        FieldSeriesRunner.validate(
            replace(cfg, sweep=replace(cfg.sweep, stop_level_si=150e-6)), settings)

    class Factory:
        def __init__(self):
            self.session = KeithleySimulator()
            self.session.resistance_ohm = {"smua": 1.0, "smub": 1.0}

        def open(self, resource, backend, timeout_ms):
            del resource, backend
            self.session.timeout = timeout_ms
            return self.session

    factory = Factory()
    adapter = KeithleyAdapter(settings, session_factory=factory)
    adapter.connect()
    events, curves = [], []
    try:
        entries = FieldSeriesRunner(
            adapter, settings, cfg, lambda kind, data: events.append((kind, data)),
            curves.append).run()
        assert [entry.status for entry in entries] == ["completed", "completed"]
        writes = factory.session.commands
        assert "smub.source.leveli = 0.005" in writes
        assert "smub.source.leveli = 0.01" in writes
        assert "smua.source.leveli = 3e-06" in writes
        assert "smua.source.leveli = 0.005" not in writes
        assert "smua.source.leveli = 0.01" not in writes
        adapter.confirm_output_off("A")
        adapter.confirm_output_off("B")
    finally:
        adapter.disconnect()


def test_journal_fault_attempts_shutdown_of_both_channels():
    def fail(kind, data):
        if kind == "field_observation":
            raise OSError("disk full")
    runner, device, _, _ = make_runner(write_event=fail)
    with pytest.raises(OSError, match="disk full"):
        runner.run()
    assert device.outputs == {"A": False, "B": False}


def test_failed_off_a_does_not_prevent_attempt_on_b():
    runner, device, _, _ = make_runner()
    device.fail_off = "A"
    with pytest.raises(DeviceError, match="OUTPUT OFF"):
        runner.run()
    assert ("output", "B", False) in device.calls
    assert not runner.output_off_confirmed


def test_cancel_does_not_enable_either_channel():
    cancel = threading.Event()
    cancel.set()
    runner, device, _, _ = make_runner(cancel_event=cancel)
    assert runner.run() == ()
    assert not any(call[0] == "output" and call[2] for call in device.calls)


def test_applied_b_parameters_must_match_reviewed_request():
    runner, device, _, _ = make_runner()
    configure = device.configure_source
    def mismatch(request):
        applied = configure(request)
        return replace(applied, sense_mode="4wire") if request.channel == "B" else applied
    device.configure_source = mismatch
    with pytest.raises(SafetyViolation, match="sense_mode"):
        runner.run()
    assert not any(call[0] == 'output' and call[2] for call in device.calls)


def test_field_trip_after_sample_measurement_preserves_invalid_point():
    runner, device, events, _ = make_runner(replace(config(), currents_a=(5e-6, 0)))
    original_measure = device.measure
    sample_seen = False
    tripped = False

    def measure(channel):
        nonlocal sample_seen, tripped
        value = original_measure(channel)
        if channel == "A":
            sample_seen = True
        elif sample_seen and not tripped:
            tripped = True
            device.outputs["B"] = False
            return replace(value, compliance_detected=True, compliance_stop_required=True,
                           output_enabled=False)
        return value

    device.measure = measure
    entries = runner.run()
    assert entries[0].status == "skipped_field_compliance"
    point = entries[0].dataset.points[0]
    assert not point.valid
    assert point.field_after.compliance_active
    assert entries[1].status == "completed"
    assert any(kind == "sample_point" and not data["valid"] for kind, data in events)


def test_loss_of_b_policy_mid_sweep_stops_both_channels():
    runner, device, _, _ = make_runner(replace(config(), currents_a=(0,)))
    original_measure = device.measure

    def measure(channel):
        result = original_measure(channel)
        if channel == "A":
            device.policies["B"] = "warn_clamp"
        return result

    device.measure = measure
    with pytest.raises(SafetyViolation, match="lost its STOP"):
        runner.run()
    assert device.outputs == {"A": False, "B": False}


def test_series_store_keeps_skips_partial_data_and_raw_telemetry(tmp_path):
    import json
    from app.devices.keithley_2600.characterization.field_storage import FieldSeriesStore

    runner, _, _, _ = make_runner()
    store = FieldSeriesStore(tmp_path / "series", runner.config, {"simulation": True})
    runner.write_event = store.write_event
    runner.save_curve = store.save_curve
    runner.run()
    store.close("completed_with_skips", outputs_confirmed_off=runner.output_off_confirmed)
    manifest = json.loads((store.directory / "series.json").read_text())
    assert manifest["status"] == "completed_with_skips"
    assert manifest["entries"][1]["status"] == "skipped_field_compliance"
    assert manifest["entries"][1]["point_count"] == 0
    assert manifest["entries"][2]["history_segment"] == 1
    curve = store.directory / manifest["entries"][3]["directory"]
    content = (curve / "characterization.csv").read_text()
    assert "Field_Before_Current_A" in content
    snapshot = json.loads((curve / "dataset.json").read_text())
    assert snapshot["points"][0]["field_before"]["measured_current_a"] == pytest.approx(5e-6)
    events = [json.loads(line) for line in (store.directory / "events.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "sample_raw" for event in events)
    assert any(event["kind"] == "field_skipped" for event in events)
    assert events[-1]["kind"] == "series_closed"
