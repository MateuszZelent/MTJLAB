"""Continue keeps OUTPUT on while applying all explicitly selected values."""

import pytest

from tests.test_sweep_audit_contracts import audit_settings, compile_source


@pytest.mark.parametrize("sweep", [False, True])
def test_continue_applies_static_set_and_roi_without_output_transitions(tmp_path, sweep):
    settings = audit_settings(tmp_path)
    raw = settings.model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    settings = type(settings).model_validate(raw)
    frequency_action = (
        "{parameter_id: carrier.frequency, mode: sweep, segments: [{start: '2 MHz', stop: '3 MHz', points: 2}]}"
        if sweep else "{parameter_id: carrier.frequency, mode: set, value: '2 MHz'}"
    )
    plan = compile_source(settings, f"""    - {{id: carrier, type: configure_rigol, channel: 1, waveform: SIN, frequency: '1 MHz', high_level: '10 mV', low_level: '-10 mV'}}
    - {{id: on, type: set_rigol_output, channel: 1, enabled: true}}
    - id: selected
      type: sequence
      device_module: rigol
      operation: configure_selected_parameters
      configuration: {{channel: 1}}
      output_policy: continue
      parameter_actions:
        - {frequency_action}
        - {{parameter_id: carrier.offset, mode: set, value: '2 mV'}}
      children: [{{id: settle, type: wait, duration: '1 ms'}}]
""")
    kinds = [a.kind for a in plan.actions]
    start = kinds.index("set_rigol_output") + 1
    following = plan.actions[start:]
    assert following[0].kind == "assert_output_on"
    assert not any(a.kind in {"configure_rigol", "set_rigol_output"} for a in following)
    frequencies = [a.payload["frequency_hz"] for a in following if a.kind == "update_rigol_frequency"]
    assert frequencies == ([2e6, 3e6] if sweep else [2e6])
    levels = [a.payload for a in following if a.kind == "update_rigol_levels"]
    assert len(levels) == 1
    assert levels[0]["high_level_v"] == pytest.approx(.012)
    assert levels[0]["low_level_v"] == pytest.approx(-.008)
    if sweep:
        roi = [a for a in following if a.kind == "update_rigol_frequency"]
        assert [a.payload["applied_si"] for a in roi] == [2e6, 3e6]
        assert all(a.payload["target"] == "rigol.1.frequency" for a in roi)
        static = next(a for a in following if a.kind == "update_rigol_levels")
        assert static.semantic_id != roi[0].semantic_id
        assert "target" not in static.payload


@pytest.mark.parametrize("parameter,start,stop,expected", [
    ("amplitude", "20 mV", "30 mV", [.02, .03]),
    ("offset", "1 mV", "2 mV", [.001, .002]),
    ("high_level", "10 mV", "20 mV", [.01, .02]),
    ("low_level", "-10 mV", "-20 mV", [-.01, -.02]),
])
def test_first_and_later_level_roi_report_the_selected_quantity(tmp_path, parameter, start, stop, expected):
    plan = compile_source(audit_settings(tmp_path), f"""    - {{id: carrier, type: configure_rigol, channel: 1, waveform: SIN, frequency: '1 MHz', high_level: '10 mV', low_level: '-10 mV'}}
    - id: selected
      type: sequence
      device_module: rigol
      operation: configure_selected_parameters
      configuration: {{channel: 1}}
      parameter_actions:
        - {{parameter_id: carrier.{parameter}, mode: sweep, segments: [{{start: '{start}', stop: '{stop}', points: 2}}]}}
      children: [{{id: settle, type: wait, duration: '1 ms'}}]
""")
    updates = [a for a in plan.actions if a.kind == "update_rigol_levels"]
    assert len(updates) == 2
    assert [a.payload["applied_si"] for a in updates] == pytest.approx(expected)
    assert [a.payload["requested_si"] for a in updates] == pytest.approx(expected)
    assert all(a.payload["target"] == f"rigol.1.{parameter}" for a in updates)
