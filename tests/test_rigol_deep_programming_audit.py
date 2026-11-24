"""Regressions identified by the DC/SIN SCPI and quantity source audit."""

from types import SimpleNamespace

import pytest

from app.devices.rigol_dg1000z.adapter import RigolAdapter, RigolChannelConfig
from app.devices.simulators import RigolSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError, SafetyViolation
from app.domain.models import DeviceState
from app.engine.compiler import RecipeCompiler
from app.engine.runner import RecipeRunner
from app.recipes import parse_recipe_text
from tests.helpers import simulation_settings
from tests.test_adapters_and_runner import MemoryWriter
from tests.test_rigol_dc_firmware_offset import DcOffsetIgnoringFirmware


def settings_with_limit(limit="100 mV"):
    raw = simulation_settings().model_dump(mode="python")
    rigol = raw["devices"]["rigol"]["safety"]
    rigol["allow_output_enable"] = True
    rigol["channels"]["1"]["enabled"] = True
    rigol["channels"]["1"]["lab_limits"]["combined_voltage_limit"] = limit
    return type(simulation_settings()).model_validate(raw)


def connected(waveform="SIN", *, enabled=False, session=None):
    settings = settings_with_limit()
    session = session or RigolSimulator()
    adapter = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    adapter.configure_channel(RigolChannelConfig(1, waveform, 1000., .01, .01 if waveform == "DC" else -.01))
    if enabled:
        adapter.set_output(1, True)
    session.commands.clear()
    return adapter, session


def update(adapter, operation, value=.02):
    if operation == "pair":
        return adapter.update_levels(1, high_level_v=.03, low_level_v=-.01)
    return getattr(adapter, {
        "offset": "update_offset", "amplitude": "update_amplitude_vpp",
        "high": "update_high_level", "low": "update_low_level",
    }[operation])(1, value)


@pytest.mark.parametrize("operation,value", [
    ("amplitude", .10004), ("offset", .08004), ("high", .06334), ("low", -.06334),
])
def test_sin_raw_request_outside_limit_cannot_round_into_safety(operation, value):
    adapter, session = connected()
    with pytest.raises(SafetyViolation):
        update(adapter, operation, value)
    assert session.commands == []


@pytest.mark.parametrize("waveform,operation", [
    ("DC", "offset"), ("SIN", "offset"), ("SIN", "amplitude"),
    ("SIN", "high"), ("SIN", "low"), ("SIN", "pair"),
])
@pytest.mark.parametrize("initially_on", [False, True])
def test_external_output_transition_is_not_adopted_by_voltage_update(waveform, operation, initially_on):
    adapter, session = connected(waveform, enabled=initially_on)
    session.output[1] = not initially_on
    with pytest.raises((DeviceError, SafetyViolation)):
        update(adapter, operation, -.02 if operation == "low" else .02)
    assert not any(c.startswith(":SOUR1:") and "?" not in c for c in session.commands)
    assert not any(session.output.values())


@pytest.mark.parametrize("waveform", ["DC", "SIN"])
@pytest.mark.parametrize("field,value", [("polarity", "INV"), ("output_mode", "GAT"), ("sync", True)])
def test_live_voltage_update_detects_external_output_path_change(waveform, field, value):
    adapter, session = connected(waveform, enabled=True)
    getattr(session, field)[1] = value
    with pytest.raises((DeviceError, SafetyViolation)):
        adapter.update_offset(1, .02)
    assert not any(c.startswith(":SOUR1:") and "?" not in c for c in session.commands)
    assert not any(session.output.values())


def axis_recipe(waveform="SIN", target="amplitude", start="2 mV", stop="2.1 mV"):
    return parse_recipe_text(f"""schema_version: 1
name: voltage interpretation audit
root:
  id: root
  type: sequence
  children:
    - {{id: carrier, type: configure_rigol, channel: 1, waveform: {waveform}, frequency: '1 kHz', high_level: '{'10 mV' if waveform == 'DC' else '1 mV'}', low_level: '{'10 mV' if waveform == 'DC' else '-1 mV'}'}}
    - id: axis
      type: sweep
      target: rigol.1.{target}
      start: '{start}'
      stop: '{stop}'
      points: 2
      children: [{{id: point, type: checkpoint}}]
""")


def test_sine_amplitude_axis_keeps_half_step_endpoints():
    plan = RecipeCompiler(settings_with_limit()).compile(axis_recipe())
    updates = [a for a in plan.actions if a.kind == "update_rigol_levels"]
    assert [a.payload["high_level_v"] - a.payload["low_level_v"] for a in updates] == pytest.approx([.002, .0021])
    assert updates[-1].payload["high_level_v"] == pytest.approx(.00105)
    assert updates[-1].payload["low_level_v"] == pytest.approx(-.00105)
    assert updates[-1].payload["applied_si"] == pytest.approx(.0021)


def test_native_sine_roi_does_not_merge_distinct_amplitude_steps():
    recipe = parse_recipe_text("""schema_version: 1
name: native sine ROI
root:
  id: root
  type: sequence
  children:
    - {id: carrier, type: configure_rigol, channel: 1, waveform: SIN, frequency: '1 kHz', high_level: '1 mV', low_level: '-1 mV'}
    - id: selected
      type: sequence
      device_module: rigol
      operation: configure_selected_parameters
      configuration: {channel: 1}
      parameter_actions:
        - {parameter_id: carrier.amplitude, mode: sweep, segments: [{start: '2 mV', stop: '2.2 mV', step: '0.1 mV'}]}
      children: [{id: point, type: checkpoint}]
""")
    plan = RecipeCompiler(settings_with_limit()).compile(recipe)
    amplitudes = [a.payload["high_level_v"] - a.payload["low_level_v"]
                  for a in plan.actions if a.kind == "update_rigol_levels"]
    assert amplitudes == pytest.approx([.002, .0021, .0022])


@pytest.mark.parametrize("enabled", [False, True])
def test_sine_half_step_pair_programs_exact_vpp_at_runtime(enabled):
    adapter, session = connected(enabled=enabled)
    actual = adapter.update_levels(1, high_level_v=.00105, low_level_v=-.00105)
    assert actual == pytest.approx((.00105, -.00105))
    assert ":SOUR1:VOLT 0.0021" in session.commands
    assert ":SOUR1:VOLT 0.0022" not in session.commands
    assert session.output[1] is enabled


@pytest.mark.parametrize("persistent", [False, True])
def test_dc_firmware_command_acceptance_is_not_recorded_as_voltage_readback(tmp_path, persistent):
    settings = settings_with_limit()
    plan = RecipeCompiler(settings).compile(axis_recipe("DC", "offset", "10 mV", "20 mV"))
    session = DcOffsetIgnoringFirmware()
    adapter = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    if persistent:
        from app.storage.hdf5_writer import Hdf5RunWriter

        path = tmp_path / "dc-firmware-evidence.h5"
        writer = Hdf5RunWriter(path, recipe_source=plan.recipe_source,
                              settings_source=settings.model_dump_json(), plan_hash=plan.sha256,
                              expected_points=plan.total_points,
                              device_idn={"rigol": adapter.identity.idn},
                              simulation_metadata={"enabled": True})
    else:
        writer = MemoryWriter()
    unused = SimpleNamespace(state=DeviceState.OUTPUT_OFF)
    try:
        result = RecipeRunner(rigol=adapter, keithley=unused, anritsu=unused, writer=writer).run(plan)
        assert result.error is None, result.error
        if persistent:
            from app.storage.hdf5_reader import Hdf5RunReader
            from app.storage.thatec_validator import ThatecCompatibilityValidator

            points = Hdf5RunReader.points(path)
            assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
        else:
            points = [point for point, _trace in writer.points]
        assert len(points) == 2
        for point in points:
            evidence = point.metadata["setpoint_evidence_v1"]["rigol.1.offset"]
            assert evidence["readback_si"] is None
            assert evidence["verification"] == "command_accepted"
            assert "00.01.08" in evidence["limitation"]
            assert evidence["applied_si"] == pytest.approx(point.setpoints["rigol.1.offset"])
            assert point.metadata["readback_si"] is None
    finally:
        adapter.disconnect()
