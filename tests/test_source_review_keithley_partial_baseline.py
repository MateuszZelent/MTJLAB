"""Omitted configuration fields survive planning and live continuation."""
import pytest

from tests.test_sweep_audit_contracts import audit_settings, compile_source, execute


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("manual_ranges", [False, True])
def test_partial_literal_configuration_preserves_baseline_for_live_sweep(
    tmp_path, monkeypatch, channel, manual_ranges
):
    settings = audit_settings(tmp_path)
    ranges = (
        ", measure_voltage_autorange: false, measure_voltage_range: '100 mV', "
        "measure_current_autorange: false, measure_current_range: '1 uA'"
        if manual_ranges else ""
    )
    plan = compile_source(settings, f"""    - {{id: baseline, type: configure_keithley, channel: {channel}, mode: current, level: '0.1 mA', compliance: '20 mV', source_range: '10 mA', nplc: 4, settle_time: '250 ms'{ranges}}}
    - {{id: partial, type: configure_keithley, channel: {channel}, mode: current, level: '0.2 mA', compliance: '20 mV', source_range: '10 mA'}}
    - {{id: enable, type: set_keithley_output, channel: {channel}, enabled: true}}
    - id: live
      type: sequence
      device_module: keithley
      operation: configure_selected_parameters
      channel: {channel}
      source_mode: current
      configuration: {{channel: {channel}, source_mode: current}}
      output_policy: continue
      parameter_actions:
        - parameter_id: source.level
          mode: sweep
          segments: [{{start: '0.3 mA', stop: '0.4 mA', points: 2}}]
      children:
        - {{id: checkpoint, type: checkpoint}}
""")
    result, _waits = execute(settings, plan, tmp_path / "partial-baseline.h5", monkeypatch)
    assert result.error is None, result.error
    assertion = next(action for action in plan.actions if action.kind == "assert_output_on")
    assert assertion.payload["expected_state"]["nplc"] == 4
    assert assertion.payload["expected_state"]["settle_time_s"] == .25
    assert assertion.payload["expected_state"]["measure_voltage_autorange"] is not manual_ranges
    assert assertion.payload["expected_state"]["measure_current_autorange"] is not manual_ranges
    if manual_ranges:
        assert assertion.payload["expected_state"]["measure_voltage_range_si"] == .1
        assert assertion.payload["expected_state"]["measure_current_range_si"] == 1e-6
    assert result.stored_points == 2
