"""Hardware readback, authored scope and configuration-write minimisation."""
from dataclasses import replace

import pytest

from app.bootstrap import StationComposition
from app.devices.anritsu_ms2830a.adapter import AdvancedSpectrumConfig
from app.devices.rigol_dg1000z.adapter import RigolOutputConfig
from app.domain.errors import DeviceError, ExecutionError
from app.engine.recovery import RunRecoveryManager
from app.safety.keithley import KeithleySourceRequest
from app.settings.models import StationSettings
from tests.test_moke_voltage_control import controlled_adapter, mutations, plan_for
from tests.test_sweep_audit_contracts import audit_settings, compile_source


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("output_on", [False, True])
def test_one_difference_among_six_authored_targets_writes_only_current(tmp_path, channel, output_on):
    raw = audit_settings(tmp_path).model_dump(mode="python")
    # Synthetic envelope for the user's 670 mV example, never saved to the station.
    limits = raw["devices"]["keithley"]["safety"]["channels"][channel]["lab_limits"]
    limits["voltage_compliance"]["max"] = "670 mV"
    limits["measured_voltage_trip"].update(min="-675 mV", max="675 mV")
    adapter = StationComposition(StationSettings.model_validate(raw), simulation=True).create_adapter("keithley")
    adapter.connect()
    try:
        baseline = KeithleySourceRequest(channel, "current", .001, .67, source_range_si=.01,
                                         nplc=4, settle_time_s=.25)
        adapter.configure_source(baseline)
        if output_on:
            adapter.set_output(channel, True)
        session = adapter._require_session()
        session.commands.clear()
        target = replace(baseline, level_si=.0015, nplc=1, settle_time_s=0,
                         changed_fields=("mode", "level_si", "compliance_si", "sense_mode", "source_autorange", "source_range_si"))
        applied = adapter.configure_source(target)
        writes = [command for command in session.commands if " = " in command]
        smu = f"smu{channel.lower()}"
        expected = ([f"{smu}.source.output = {smu}.OUTPUT_OFF"] if output_on else [])
        assert writes == expected + [f"{smu}.source.leveli = 0.0015"]
        assert applied.nplc == 4 and applied.settle_time_s == .25
        session.commands.clear()
        adapter.configure_source(target)
        assert not any(" = " in command for command in session.commands)
        assert any(command.startswith("print(") for command in session.commands)
    finally:
        adapter.disconnect()


def test_equal_requested_measurement_ranges_mean_equal_physical_range(tmp_path):
    adapter = StationComposition(audit_settings(tmp_path), simulation=True).create_adapter("keithley")
    adapter.connect()
    try:
        baseline = KeithleySourceRequest("A", "current", .001, .02, source_range_si=.01,
                                         measure_voltage_autorange=False, measure_voltage_range_si=.08)
        adapter.configure_source(baseline)  # Selects the physical 100 mV range.
        session = adapter._require_session()
        session.commands.clear()
        adapter.configure_source(replace(baseline, measure_voltage_range_si=.1, changed_fields=("measure_voltage_range_si",)))
        assert not any(" = " in command for command in session.commands)
    finally:
        adapter.disconnect()


def test_stale_baseline_never_authorises_suppressing_a_write(tmp_path):
    adapter = StationComposition(audit_settings(tmp_path), simulation=True).create_adapter("keithley")
    adapter.connect()
    try:
        baseline = KeithleySourceRequest("A", "current", .001, .02, source_range_si=.01)
        adapter.configure_source(baseline)
        session = adapter._require_session()
        session.write("smua.source.leveli = 0.0012")  # Front-panel / other process.
        session.commands.clear()
        with pytest.raises(DeviceError, match="readback mismatch"):
            adapter.configure_source(replace(baseline, changed_fields=("level_si",)))
        assert not any(".source.leveli =" in command for command in session.commands)
    finally:
        adapter.disconnect()


def test_minimum_representable_current_step_is_not_suppressed(tmp_path):
    adapter = StationComposition(audit_settings(tmp_path), simulation=True).create_adapter("keithley")
    adapter.connect()
    try:
        baseline = KeithleySourceRequest("A", "current", 1e-12, .02, source_range_si=1e-7)
        adapter.configure_source(baseline)
        session = adapter._require_session()
        session.commands.clear()
        adapter.configure_source(replace(baseline, level_si=2e-12, changed_fields=("level_si",)))
        assert [cmd for cmd in session.commands if " = " in cmd] == ["smua.source.leveli = 2e-12"]
        session.write("smua.source.leveli = 3e-12")
        session.commands.clear()
        with pytest.raises(DeviceError, match="readback mismatch"):
            adapter.configure_source(replace(baseline, level_si=2e-12, changed_fields=("level_si",)))
    finally:
        adapter.disconnect()


def test_rigol_carrier_and_output_path_preserve_omitted_targets(tmp_path):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """    - {id: carrier, type: configure_rigol, channel: 1, waveform: SIN, frequency: '1 kHz', high_level: '1 mV', low_level: '-1 mV'}
    - {id: sync, type: configure_rigol_output, channel: 1, sync_enabled: true}
""")
    config, output = [action.payload["config"] for action in plan.actions]
    assert output.changed_fields == ("sync_enabled",)
    adapter = StationComposition(settings, simulation=True).create_adapter("rigol")
    adapter.connect()
    try:
        adapter.configure_channel(config)
        session = adapter._require_session()
        session.commands.clear()
        adapter.configure_channel(replace(config, frequency_hz=2000))
        assert [cmd for cmd in session.commands if not cmd.endswith("?")] == [":OUTP1 OFF", ":SOUR1:FREQ 2000"]
        session.commands.clear()
        adapter.configure_channel(replace(config, frequency_hz=2000))
        assert [cmd for cmd in session.commands if not cmd.endswith("?")] == [":OUTP1 OFF"]
        session.commands.clear()
        adapter.configure_channel(replace(config, frequency_hz=2000, high_level_v=.0015, low_level_v=-.0005))
        assert [cmd for cmd in session.commands if not cmd.endswith("?")] == [":OUTP1 OFF", ":SOUR1:VOLT:OFFS 0.0005"]
        session.write(":OUTP1:SYNC:DEL 0.01")
        session.commands.clear()
        applied = adapter.configure_output(output)
        assert applied.sync_delay_s == .01
        assert [cmd for cmd in session.commands if not cmd.endswith("?")] == [":OUTP1 OFF", ":OUTP1:SYNC ON"]
        session.commands.clear()
        adapter.configure_output(output)
        assert [cmd for cmd in session.commands if not cmd.endswith("?")] == [":OUTP1 OFF"]
    finally:
        adapter.disconnect()


def test_anritsu_literal_and_advanced_targets_skip_equal_settings(tmp_path):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, "    - {id: analyzer, type: configure_anritsu, start_frequency: '1 MHz', stop_frequency: '10 MHz', reference_level: '0 dBm', points: 101}\n")
    config = plan.actions[0].payload["config"]
    assert set(config.changed_fields) == {"start_hz", "stop_hz", "reference_level_dbm", "points"}
    adapter = StationComposition(settings, simulation=True).create_adapter("anritsu")
    adapter.connect()
    try:
        adapter.configure_spectrum(config)
        session = adapter._require_session()
        session.commands.clear()
        adapter.configure_spectrum(replace(config, reference_level_dbm=-5))
        assert [cmd for cmd in session.commands if not cmd.endswith("?")] == ["DISP:WIND:TRAC:Y:RLEV -5"]
        session.commands.clear()
        generation = adapter._configuration_generation
        adapter.configure_spectrum(replace(config, reference_level_dbm=-5))
        assert not any(not cmd.endswith("?") for cmd in session.commands)
        assert adapter._configuration_generation == generation
        before = adapter.read_advanced_spectrum_configuration()
        session.commands.clear()
        adapter.configure_advanced_spectrum(AdvancedSpectrumConfig(detector=before.detector, rbw_auto=before.rbw_auto))
        assert not any(not cmd.endswith("?") for cmd in session.commands)
        session.commands.clear()
        adapter.configure_spectrum(replace(config, start_hz=20e6, stop_hz=30e6, reference_level_dbm=-5))
        assert [cmd for cmd in session.commands if not cmd.endswith("?")] == ["FREQ:STOP 30000000HZ", "FREQ:STAR 20000000HZ"]
    finally:
        adapter.disconnect()


def test_moke_repeated_target_still_reads_hardware_but_does_not_set_dac():
    adapter, transport, profile = controlled_adapter(minimum_settling_s=0)
    try:
        plan = plan_for(profile, targets=(.01, .01))
        adapter.configure_voltage_plan(plan)
        adapter.arm_voltage_plan(plan)
        first = adapter.ramp_vout(2, .01)
        transport.sent.clear()
        second = adapter.ramp_vout(2, .01)
        assert first.actual_v == second.actual_v
        assert transport.sent and not mutations(transport)
    finally:
        adapter.disconnect()


def test_output_path_recovery_requires_actual_preserved_values(tmp_path):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, """    - {id: carrier, type: configure_rigol, channel: 1, waveform: SIN, frequency: '1 kHz', high_level: '1 mV', low_level: '-1 mV'}
    - {id: sync, type: configure_rigol_output, channel: 1, sync_enabled: true}
""")
    carrier = plan.actions[0].payload["config"]
    from dataclasses import asdict
    actual = asdict(carrier)
    with pytest.raises(ExecutionError, match="output-path snapshot"):
        RunRecoveryManager._configuration_prelude(plan, len(plan.actions), {"rigol": {"channel_1": {"actual": actual}}})
    actual["output_path"] = asdict(RigolOutputConfig(1, sync_enabled=True, sync_delay_s=.01))
    restored = RunRecoveryManager._configuration_prelude(plan, len(plan.actions), {"rigol": {"channel_1": {"actual": actual}}})
    assert restored[1].payload["config"].sync_delay_s == .01
