from __future__ import annotations

import unittest
from copy import deepcopy

from app.domain.errors import SafetyViolation
from app.engine.compiler import RecipeCompiler
from app.recipes import load_recipe, parse_recipe_text
from app.settings.models import StationSettings
from tests.helpers import ROOT, loaded_settings, simulation_settings


class RecipeCompilerTests(unittest.TestCase):
    def test_keithley_a_anritsu_recipe_stores_ten_raw_and_processed_spectra(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            load_recipe(
                ROOT / "recipes" / "keithley_a_100ua_anritsu_raw_processed_10.yml"
            )
        )

        self.assertEqual(plan.total_points, 10)
        self.assertEqual(plan.total_spectra, 10)
        self.assertEqual(plan.required_devices, frozenset({"keithley", "anritsu"}))
        kinds = [action.kind for action in plan.actions]
        self.assertLess(kinds.index("configure_keithley"), kinds.index("set_keithley_output", 1))
        self.assertLess(kinds.index("set_keithley_output", 1), kinds.index("acquire_reference"))
        spectra = [action for action in plan.actions if action.kind == "acquire_spectrum"]
        self.assertEqual(len(spectra), 10)
        self.assertTrue(all(action.payload["store_raw"] for action in spectra))
        self.assertTrue(all(action.payload["store_processed"] for action in spectra))
        self.assertTrue(
            all(action.payload["reference_operation"] == "difference_db" for action in spectra)
        )
        self.assertNotIn("ramp_keithley_to_zero", kinds)
        self.assertEqual(plan.actions[-1].kind, "set_keithley_output")
        self.assertFalse(plan.actions[-1].payload["enabled"])

    def test_thatec_fmr_analog_recipe_preserves_two_branches_in_24_combined_points(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        raw["devices"]["lakeshore_gaussmeter"].update(
            {"enabled": True, "resource": "SIM::LAKESHORE::INSTR"}
        )

        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            load_recipe(ROOT / "recipes" / "fmr_thatec_tree_analog_keithley_a_lakeshore_anritsu.yml")
        )

        self.assertEqual(plan.total_points, 24)
        self.assertEqual(plan.total_spectra, 24)
        self.assertEqual(sum(action.kind == "acquire_reference" for action in plan.actions), 2)
        field_actions = [
            action for action in plan.actions if action.kind == "measure_lakeshore_field"
        ]
        self.assertEqual(len(field_actions), 24)
        self.assertTrue(all(action.payload["checkpoint"] is False for action in field_actions))
        self.assertEqual(
            plan.required_devices,
            frozenset({"keithley", "lakeshore_gaussmeter", "anritsu"}),
        )

    def test_lakeshore_measurement_compiles_as_one_read_only_checkpoint(self) -> None:
        raw = deepcopy(simulation_settings().model_dump(mode="python"))
        raw["devices"]["lakeshore_gaussmeter"].update(
            {"enabled": True, "resource": "SIM::LAKESHORE::INSTR"}
        )
        source = """\
schema_version: 1
name: lakeshore-checkpoint
root:
  id: field
  type: measure_lakeshore_field
"""

        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            parse_recipe_text(source)
        )

        self.assertEqual([action.kind for action in plan.actions], ["measure_lakeshore_field"])
        self.assertEqual(plan.total_points, 1)
        self.assertEqual(plan.required_devices, frozenset({"lakeshore_gaussmeter"}))

    def test_lakeshore_measurement_can_be_attached_to_following_spectrum_checkpoint(self) -> None:
        raw = deepcopy(simulation_settings().model_dump(mode="python"))
        raw["devices"]["lakeshore_gaussmeter"].update(
            {"enabled": True, "resource": "SIM::LAKESHORE::INSTR"}
        )
        source = """\
schema_version: 1
name: field-and-spectrum-one-point
dut_limits:
  anritsu: {max_expected_input: "-10 dBm"}
root:
  id: root
  type: sequence
  children:
    - {id: configure, type: configure_anritsu, start_frequency: "10 MHz", stop_frequency: "4 GHz", reference_level: "0 dBm", points: 5001, trace: TRAC1}
    - {id: field, type: measure_lakeshore_field, checkpoint: false}
    - {id: spectrum, type: acquire_spectrum, trace: TRAC1}
"""

        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            parse_recipe_text(source)
        )

        self.assertFalse(plan.actions[1].payload["checkpoint"])
        self.assertEqual(plan.total_points, 1)
        self.assertEqual(plan.total_spectra, 1)

    def test_hall_measurement_inside_sweep_expands_to_one_stored_point_per_step(self) -> None:
        raw = deepcopy(simulation_settings().model_dump(mode="python"))
        raw["devices"]["moke_box"].update(
            {
                "enabled": True,
                "protocol_qualified": True,
                "endpoint": "127.0.0.1:10001",
            }
        )
        source = """\
schema_version: 1
name: hall-at-each-sweep-point
root:
  id: source-level
  type: sweep
  target: keithley.B.current
  start: "0 A"
  stop: "1 mA"
  points: 3
  children:
    - {id: hall, type: measure_moke_hall}
"""

        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            parse_recipe_text(source)
        )

        self.assertEqual([action.kind for action in plan.actions], ["measure_moke_hall"] * 3)
        self.assertEqual(plan.total_points, 3)
        self.assertEqual(plan.required_devices, frozenset({"moke_box"}))

    def test_every_output_sweep_revalidates_generated_points_against_limits(self) -> None:
        sources = {
            "keithley": """\
schema_version: 1
name: unsafe-keithley-sweep
root:
  id: root
  type: sequence
  children:
    - {id: configure, type: configure_keithley, channel: B, mode: current, level: "1 mA", compliance: "67 mV"}
    - id: axis
      type: sweep
      target: keithley.B.current
      start: "1 mA"
      stop: "10.001 mA"
      points: 3
      children:
        - {id: point, type: update_keithley_level, channel: B, mode: current, level: "${keithley.B.current}"}
""",
            "rigol": """\
schema_version: 1
name: unsafe-rigol-sweep
root:
  id: root
  type: sequence
  children:
    - {id: configure, type: configure_rigol, channel: 1, waveform: SIN, frequency: "1 kHz", high_level: "1 mV", low_level: "-1 mV", output_load: HIGHZ, dut_min_impedance: "50 ohm"}
    - id: axis
      type: sweep
      target: rigol.1.frequency
      start: "1 kHz"
      stop: "1000 GHz"
      points: 3
      children:
        - {id: point, type: update_rigol_frequency, channel: 1, frequency: "${rigol.1.frequency}"}
""",
            "anritsu": """\
schema_version: 1
name: unsafe-anritsu-sweep
root:
  id: axis
  type: sweep
  target: anritsu.spectrum.stop_frequency
  start: "10 MHz"
  stop: "101 GHz"
  points: 3
  children:
    - {id: point, type: configure_anritsu, start_frequency: "1 MHz", stop_frequency: "${anritsu.spectrum.stop_frequency}", reference_level: "0 dBm", points: 101}
""",
        }
        for device, source in sources.items():
            with self.subTest(device=device), self.assertRaises(Exception):
                RecipeCompiler(simulation_settings()).compile(
                    parse_recipe_text(source)
                )

        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["anritsu"]["signal_generator"].update(
            {
                "control_protocol": "basic_scpi",
                "frequency": {"min": "100 MHz", "max": "6 GHz"},
                "power": {"min": "-100 dBm", "max": "0 dBm"},
            }
        )
        sg_source = """\
schema_version: 1
name: unsafe-anritsu-sg-sweep
root:
  id: sg
  type: sequence
  device_module: anritsu_sg
  operation: configure_selected_parameters
  configuration: {frequency: "1 GHz", power: "-30 dBm"}
  parameter_actions:
    - parameter_id: sg.frequency
      mode: sweep
      segments:
        - {start: "1 GHz", stop: "6.001 GHz", points: 3}
  children:
    - {id: wait, type: wait, duration: "1 ms"}
"""
        with self.assertRaises(SafetyViolation):
            RecipeCompiler(StationSettings.model_validate(raw)).compile(
                parse_recipe_text(sg_source)
            )

    def test_compilation_can_be_cancelled_before_expansion(self) -> None:
        source = """\
schema_version: 1
name: cancellable
root:
  id: root
  type: sequence
  children:
    - {id: wait, type: wait, duration: 1 ms}
"""
        with self.assertRaisesRegex(Exception, "compilation cancelled"):
            RecipeCompiler(
                simulation_settings(), cancellation_requested=lambda: True
            ).compile(parse_recipe_text(source))

    def test_disabled_subtree_is_skipped_with_all_children(self) -> None:
        source = """\
schema_version: 1
name: disabled-subtree
root:
  id: root
  type: sequence
  children:
    - id: disabled-group
      type: sequence
      disabled: true
      children:
        - {id: hidden-wait, type: wait, duration: 1 s}
    - {id: active-wait, type: wait, duration: 1 ms}
"""
        plan = RecipeCompiler(simulation_settings()).compile(
            parse_recipe_text(source)
        )
        self.assertEqual([action.node_id for action in plan.actions], ["active-wait"])

    def test_finally_safety_action_cannot_be_disabled(self) -> None:
        source = """\
schema_version: 1
name: disabled-finally
root:
  id: active-wait
  type: wait
  duration: 1 ms
finally:
  - {id: unsafe-disabled-off, type: set_rigol_output, channel: 1, enabled: false, disabled: true}
"""
        with self.assertRaisesRegex(Exception, "finally safety actions cannot be disabled"):
            RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))

    def test_compiles_reference_processed_nested_setpoint_updates(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        raw["devices"]["rigol"]["safety"]["channels"]["1"]["lab_limits"]["frequency"]["max"] = "30 MHz"
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        channel = raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]
        channel["source_current"] = {"min": "0 A", "max": "150 mA", "max_abs": "150 mA"}
        channel["measured_current_trip"] = {"min": "-1 mA", "max": "151 mA"}
        channel["max_abs_power"] = "10.05 mW"
        settings = StationSettings.model_validate(raw)
        source = """\
schema_version: 1
name: requested-reference-cartesian-sweep
dut_limits:
  keithley:
    B:
      current: {min: "0 A", max: "150 mA"}
      voltage: {min: "-67 mV", max: "67 mV"}
      max_abs_power: "10.05 mW"
  rigol:
    1:
      minimum_impedance: "50 ohm"
      max_abs_current: "20 uA"
      max_abs_power: "20 nW"
  anritsu:
    max_expected_input: "-10 dBm"
root:
  id: root
  type: sequence
  children:
    - {id: rigol-off-initial, type: set_rigol_output, channel: 1, enabled: false}
    - {id: keithley-off-initial, type: set_keithley_output, channel: B, enabled: false}
    - id: anritsu-config
      type: configure_anritsu
      start_frequency: "1 MHz"
      stop_frequency: "10 MHz"
      reference_level: "0 dBm"
      points: 101
    - {id: reference, type: acquire_reference, trace: TRAC1}
    - id: keithley-config
      type: configure_keithley
      channel: B
      mode: current
      level: "0 A"
      compliance: "67 mV"
    - id: rigol-config
      type: configure_rigol
      channel: 1
      waveform: SIN
      frequency: "100 kHz"
      high_level: "1 mV"
      low_level: "-1 mV"
      output_load: HIGHZ
      dut_min_impedance: "50 ohm"
    - {id: keithley-on, type: set_keithley_output, channel: B, enabled: true}
    - {id: rigol-on, type: set_rigol_output, channel: 1, enabled: true}
    - id: keithley-current
      type: sweep
      target: keithley.B.current
      start: "0 A"
      stop: "150 mA"
      points: 10
      children:
        - id: keithley-point
          type: update_keithley_level
          channel: B
          mode: current
          level: "${keithley.B.current}"
        - id: rigol-frequency
          type: sweep
          target: rigol.1.frequency
          start: "100 kHz"
          stop: "30 MHz"
          points: 100
          children:
            - id: rigol-point
              type: update_rigol_frequency
              channel: 1
              frequency: "${rigol.1.frequency}"
            - id: spectrum
              type: acquire_spectrum
              trace: TRAC1
              reference_operation: difference_db
              store_raw: true
              store_processed: true
finally:
  - {id: rigol-off-finally, type: set_rigol_output, channel: 1, enabled: false}
  - {id: keithley-zero-finally, type: ramp_keithley_to_zero, channel: B, deadline: "30 s"}
  - {id: keithley-off-finally, type: set_keithley_output, channel: B, enabled: false}
"""
        plan = RecipeCompiler(settings).compile(parse_recipe_text(source))

        self.assertEqual(plan.total_points, 1000)
        self.assertEqual(plan.total_spectra, 1000)
        self.assertEqual(
            sum(action.kind == "acquire_reference" for action in plan.actions), 1
        )
        self.assertEqual(
            sum(action.kind == "update_keithley_level" for action in plan.actions), 10
        )
        self.assertEqual(
            sum(action.kind == "update_rigol_frequency" for action in plan.actions), 1000
        )
        acquisitions = [
            action for action in plan.actions if action.kind == "acquire_spectrum"
        ]
        self.assertEqual(acquisitions[0].payload["reference_operation"], "difference_db")
        self.assertTrue(acquisitions[0].payload["store_raw"])
        self.assertTrue(acquisitions[0].payload["store_processed"])

    def test_keithley_device_provider_expands_roi_and_runs_children_per_point(self) -> None:
        source = """\
schema_version: 1
name: keithley-provider
root:
  id: root
  type: sequence
  children:
    - id: keithley-axis
      type: sequence
      device_module: keithley
      operation: configure_selected_parameters
      channel: B
      source_mode: current
      configuration:
        channel: B
        source_mode: current
        source_level: 1 mA
        compliance: 67 mV
        nplc: 1
        settling_time: 100 ms
        sense_mode: 2wire
        source_autorange: true
        source_range: AUTO
        measure_voltage_autorange: true
        measure_voltage_range: AUTO
        measure_current_autorange: true
        measure_current_range: AUTO
      parameter_actions:
        - parameter_id: source.level
          mode: sweep
          value: 1 mA
          segments:
            - start: 0 A
              stop: 1 mA
              points: 3
              spacing: linear
      children:
        - id: spectrum
          type: acquire_spectrum
          trace: TRAC1
"""
        plan = RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))

        self.assertEqual(plan.total_spectra, 3)
        self.assertEqual(
            [action.kind for action in plan.actions],
            [
                "configure_keithley",
                "update_keithley_level",
                "wait",
                "acquire_spectrum",
                "update_keithley_level",
                "wait",
                "acquire_spectrum",
                "update_keithley_level",
                "wait",
                "acquire_spectrum",
            ],
        )
        acquisitions = [
            action for action in plan.actions if action.kind == "acquire_spectrum"
        ]
        self.assertEqual(
            [action.setpoints_si["keithley.B.current"] for action in acquisitions],
            [0.0, 0.0005, 0.001],
        )

    def test_keithley_device_provider_requires_full_configuration_snapshot(self) -> None:
        source = """\
schema_version: 1
name: missing-provider-snapshot
root:
  id: keithley-axis
  type: sequence
  device_module: keithley
  operation: configure_selected_parameters
  channel: B
  source_mode: current
  parameter_actions:
    - parameter_id: source.level
      mode: set
      value: 1 mA
  children:
    - id: spectrum
      type: acquire_spectrum
      trace: TRAC1
"""
        with self.assertRaisesRegex(Exception, "complete configuration snapshot"):
            RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))

    def test_keithley_provider_compiles_compliance_axis_without_output_cycle(self) -> None:
        source = """\
schema_version: 1
name: compliance-provider
root:
  id: keithley-axis
  type: sequence
  device_module: keithley
  operation: configure_selected_parameters
  channel: B
  source_mode: current
  output_policy: unchanged
  configuration:
    channel: B
    source_mode: current
    source_level: 1 mA
    compliance: 50 mV
    nplc: 1
    settling_time: 0 s
    sense_mode: 2wire
    source_autorange: true
    source_range: AUTO
    measure_voltage_autorange: true
    measure_voltage_range: AUTO
    measure_current_autorange: true
    measure_current_range: AUTO
  parameter_actions:
    - parameter_id: source.compliance
      mode: sweep
      value: 50 mV
      segments:
        - {start: 40 mV, stop: 60 mV, points: 3}
  children:
    - id: spectrum
      type: acquire_spectrum
      trace: TRAC1
"""
        plan = RecipeCompiler(simulation_settings()).compile(
            parse_recipe_text(source)
        )

        self.assertEqual(
            sum(
                action.kind == "update_keithley_compliance"
                for action in plan.actions
            ),
            3,
        )
        self.assertFalse(
            any(
                action.kind == "set_keithley_output"
                for action in plan.actions
            )
        )
        acquisitions = [
            action for action in plan.actions if action.kind == "acquire_spectrum"
        ]
        self.assertEqual(
            [
                action.setpoints_si["keithley.B.compliance_voltage"]
                for action in acquisitions
            ],
            [0.04, 0.05, 0.06],
        )

    def test_rigol_device_provider_expands_frequency_axis_per_child(self) -> None:
        source = """\
schema_version: 1
name: rigol-provider
root:
  id: rigol-axis
  type: sequence
  device_module: rigol
  operation: configure_selected_parameters
  channel: 1
  output_policy: unchanged
  configuration:
    channel: 1
    waveform: SQU
    frequency: 1 kHz
    high_level: 1 mV
    low_level: -1 mV
    output_load: HIGHZ
    phase_deg: "0"
    square_duty_percent: "50"
    ramp_symmetry_percent: "50"
    pulse_width: 100 us
    pulse_leading: 10 ns
    pulse_trailing: 10 ns
    dut_min_impedance: 50 ohm
  parameter_actions:
    - parameter_id: carrier.frequency
      mode: sweep
      value: 1 kHz
      segments:
        - {start: 1 kHz, stop: 3 kHz, points: 3}
    - parameter_id: carrier.high_level
      mode: set
      value: 1 mV
    - parameter_id: carrier.low_level
      mode: set
      value: -1 mV
  children:
    - id: spectrum
      type: acquire_spectrum
      trace: TRAC1
"""
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["anritsu"]["advanced_spectrum"] = {
            "control_protocol": "standard_scpi",
            "qualified_firmware": ["7.03.00"],
        }
        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            parse_recipe_text(source)
        )

        self.assertEqual(
            [action.kind for action in plan.actions],
                [
                    "configure_rigol",
                    "configure_rigol_output",
                    "update_rigol_frequency",
                "acquire_spectrum",
                "update_rigol_frequency",
                "acquire_spectrum",
                "update_rigol_frequency",
                "acquire_spectrum",
            ],
        )
        output_path = plan.actions[1].payload["config"]
        self.assertEqual(output_path.polarity, "NORM")
        self.assertFalse(output_path.sync_enabled)
        acquisitions = [
            action for action in plan.actions if action.kind == "acquire_spectrum"
        ]
        self.assertEqual(
            [action.setpoints_si["rigol.1.frequency"] for action in acquisitions],
            [1000.0, 2000.0, 3000.0],
        )

    def test_rigol_high_level_axis_uses_readback_update_action(self) -> None:
        source = """\
schema_version: 1
name: rigol-level-provider
root:
  id: rigol-axis
  type: sequence
  device_module: rigol
  operation: configure_selected_parameters
  channel: 1
  configuration:
    channel: 1
    waveform: SIN
    frequency: 1 kHz
    high_level: 1 mV
    low_level: -1 mV
    output_load: HIGHZ
    phase_deg: "0"
    dut_min_impedance: 50 ohm
  parameter_actions:
    - parameter_id: carrier.high_level
      mode: sweep
      value: 1 mV
      segments:
        - {start: 1 mV, stop: 3 mV, points: 3}
  children:
    - id: spectrum
      type: acquire_spectrum
"""
        plan = RecipeCompiler(simulation_settings()).compile(
            parse_recipe_text(source)
        )

        self.assertEqual(
            sum(action.kind == "update_rigol_levels" for action in plan.actions),
            3,
        )
        self.assertFalse(
            any(
                action.kind == "set_rigol_output"
                for action in plan.actions
            )
        )

    def test_rigol_amplitude_axis_preserves_offset_at_every_point(self) -> None:
        source = """\
schema_version: 1
name: rigol-amplitude-provider
root:
  id: rigol-axis
  type: sequence
  device_module: rigol
  operation: configure_selected_parameters
  channel: 1
  configuration:
    channel: 1
    waveform: SIN
    frequency: 1 kHz
    high_level: 2 mV
    low_level: 0 mV
    output_load: HIGHZ
    phase_deg: "0"
    dut_min_impedance: 50 ohm
  parameter_actions:
    - parameter_id: carrier.amplitude
      mode: sweep
      value: 2 mV
      segments:
        - {start: 2 mV, stop: 4 mV, points: 3}
  children: []
"""
        plan = RecipeCompiler(simulation_settings()).compile(
            parse_recipe_text(source)
        )
        updates = [
            action.payload
            for action in plan.actions
            if action.kind == "update_rigol_levels"
        ]
        self.assertEqual(len(updates), 3)
        self.assertEqual(
            [round(item["high_level_v"] - item["low_level_v"], 9) for item in updates],
            [0.002, 0.003, 0.004],
        )
        self.assertEqual(
            [round((item["high_level_v"] + item["low_level_v"]) / 2, 9) for item in updates],
            [0.001, 0.001, 0.001],
        )

    def test_anritsu_device_provider_expands_axis_and_preserves_acquisition_child(self) -> None:
        source = """\
schema_version: 1
name: anritsu-provider
root:
  id: anritsu-axis
  type: sequence
  device_module: anritsu
  operation: configure_selected_parameters
  configuration:
    start_frequency: 1 MHz
    stop_frequency: 10 MHz
    reference_level: 0 dBm
    points: 101
  parameter_actions:
    - parameter_id: spectrum.start_frequency
      mode: sweep
      value: 1 MHz
      segments:
        - {start: 1 MHz, stop: 3 MHz, points: 3}
    - parameter_id: advanced.rbw_mode
      mode: set
      value: manual
    - parameter_id: advanced.rbw
      mode: set
      value: 10 kHz
  children:
    - id: spectrum
      type: acquire_spectrum
      trace: TRAC1
"""
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["anritsu"]["advanced_spectrum"] = {
            "control_protocol": "standard_scpi",
            "qualified_firmware": ["7.03.00"],
        }
        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            parse_recipe_text(source)
        )

        self.assertEqual(
            [action.kind for action in plan.actions],
            [
                "configure_anritsu",
                "configure_anritsu_advanced",
                "acquire_spectrum",
                "configure_anritsu",
                "configure_anritsu_advanced",
                "acquire_spectrum",
                "configure_anritsu",
                "configure_anritsu_advanced",
                "acquire_spectrum",
            ],
        )
        acquisitions = [
            action for action in plan.actions if action.kind == "acquire_spectrum"
        ]
        self.assertEqual(
            [
                action.setpoints_si["anritsu.spectrum.start_frequency"]
                for action in acquisitions
            ],
            [1e6, 2e6, 3e6],
        )

    def test_anritsu_provider_rejects_manual_mode_without_paired_value(self) -> None:
        source = """\
schema_version: 1
name: invalid-anritsu-advanced-pair
root:
  id: anritsu
  type: sequence
  device_module: anritsu
  operation: configure_selected_parameters
  configuration:
    start_frequency: 1 MHz
    stop_frequency: 10 MHz
    reference_level: 0 dBm
    points: 101
  parameter_actions:
    - {parameter_id: advanced.rbw_mode, mode: set, value: manual}
  children:
    - {id: wait, type: wait, duration: 1 ms}
"""
        with self.assertRaisesRegex(Exception, "must be selected together"):
            RecipeCompiler(simulation_settings()).compile(
                parse_recipe_text(source)
            )

    def test_anritsu_sg_device_provider_configures_off_then_uses_live_updates(self) -> None:
        source = """\
schema_version: 1
name: anritsu-sg-provider
root:
  id: sg
  type: sequence
  device_module: anritsu_sg
  operation: configure_selected_parameters
  configuration:
    frequency: 1 GHz
    power: -30 dBm
  parameter_actions:
    - parameter_id: sg.frequency
      mode: sweep
      value: 1 GHz
      segments:
        - {start: 1 GHz, stop: 1.2 GHz, points: 3}
    - {parameter_id: sg.power, mode: set, value: -40 dBm}
  children:
    - {id: spectrum, type: acquire_spectrum, trace: TRAC1}
"""
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["anritsu"]["signal_generator"].update(
            {
                "control_protocol": "basic_scpi",
                "frequency": {"min": "100 MHz", "max": "6 GHz"},
                "power": {"min": "-100 dBm", "max": "0 dBm"},
            }
        )
        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            parse_recipe_text(source)
        )

        self.assertEqual(
            [action.kind for action in plan.actions],
                [
                    "configure_anritsu_sg",
                    "acquire_spectrum",
                    "update_anritsu_sg",
                    "acquire_spectrum",
                    "update_anritsu_sg",
                    "acquire_spectrum",
                ],
        )
        acquisitions = [
            action for action in plan.actions if action.kind == "acquire_spectrum"
        ]
        self.assertEqual(
            [
                action.setpoints_si["anritsu.sg.frequency"]
                for action in acquisitions
            ],
            [1e9, 1.1e9, 1.2e9],
        )
        self.assertTrue(
            all(
                action.setpoints_si["anritsu.sg.power"] == -40.0
                for action in acquisitions
            )
        )

    def test_anritsu_sg_axis_preserves_explicit_per_point_rf_transitions(self) -> None:
        source = """\
schema_version: 1
name: anritsu-sg-energized-provider
dut_limits:
  anritsu:
    max_signal_generator_output: -10 dBm
root:
  id: sg
  type: sequence
  device_module: anritsu_sg
  operation: configure_selected_parameters
  configuration: {frequency: 1 GHz, power: -30 dBm}
  parameter_actions:
    - parameter_id: sg.frequency
      mode: sweep
      value: 1 GHz
      segments:
        - {start: 1 GHz, stop: 1.1 GHz, points: 2}
  children:
    - {id: rf-on, type: set_anritsu_sg_output, enabled: true}
    - {id: spectrum, type: acquire_spectrum, trace: TRAC1}
    - {id: rf-off, type: set_anritsu_sg_output, enabled: false}
"""
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["anritsu"]["safety"][
            "signal_generator_output_allowed"
        ] = True
        raw["devices"]["anritsu"]["signal_generator"].update(
            {
                "control_protocol": "basic_scpi",
                "frequency": {"min": "100 MHz", "max": "6 GHz"},
                "power": {"min": "-100 dBm", "max": "0 dBm"},
            }
        )
        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            parse_recipe_text(source)
        )

        self.assertEqual(
            [action.kind for action in plan.actions],
            [
                "configure_anritsu_sg",
                    "set_anritsu_sg_output",
                    "acquire_spectrum",
                    "set_anritsu_sg_output",
                    "update_anritsu_sg",
                    "set_anritsu_sg_output",
                "acquire_spectrum",
                "set_anritsu_sg_output",
            ],
        )

    def test_device_without_configuration_snapshot_blocks_children(self) -> None:
        source = """\
schema_version: 1
name: incomplete-placeholder
root:
  id: root
  type: sequence
  children:
    - id: keithley-placeholder
      type: sequence
      device_module: keithley
      children:
        - id: spectrum-must-not-run
          type: acquire_spectrum
          trace: TRAC1
"""
        with self.assertRaisesRegex(Exception, "configuration is incomplete"):
            RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))

    def test_preflight_rejects_update_without_prior_device_configuration(self) -> None:
        source = """\
schema_version: 1
name: invalid-update-order
root:
  id: update
  type: update_rigol_frequency
  channel: 1
  frequency: 1 kHz
"""
        with self.assertRaisesRegex(Exception, "requires an earlier configuration"):
            RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))

    def test_preflight_accepts_output_on_after_configuration(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        source = """\
schema_version: 1
name: invalid-output-order
dut_limits:
  rigol:
    1:
      minimum_impedance: 50 ohm
      max_abs_current: 1 mA
      max_abs_power: 1 mW
root:
  id: root
  type: sequence
  children:
    - id: configure
      type: configure_rigol
      channel: 1
      waveform: SIN
      frequency: 1 kHz
      high_level: 1 mV
      low_level: -1 mV
      dut_min_impedance: 50 ohm
    - {id: output-on, type: set_rigol_output, channel: 1, enabled: true}
"""
        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            parse_recipe_text(source)
        )
        self.assertEqual(plan.actions[-1].kind, "set_rigol_output")
        self.assertTrue(plan.actions[-1].payload["enabled"])

    def test_anritsu_complete_snapshot_ignores_stale_required_marker(self) -> None:
        source = """\
schema_version: 1
name: anritsu-settings-only
root:
  id: anritsu-settings
  type: sequence
  device_module: anritsu
  operation: configure_selected_parameters
  configuration_required: true
  parameter_actions: []
  post_configuration_operation: configure
  configuration:
    start_frequency: 200 MHz
    stop_frequency: 6 GHz
    reference_level: -10 dBm
    points: 10001
  children: []
"""
        plan = RecipeCompiler(simulation_settings()).compile(
            parse_recipe_text(source)
        )
        self.assertEqual(
            [action.kind for action in plan.actions],
            ["configure_anritsu"],
        )

    def test_anritsu_sg_settings_only_provider_compiles_with_rf_off(self) -> None:
        source = """\
schema_version: 1
name: anritsu-sg-settings-only
root:
  id: anritsu-sg-settings
  type: sequence
  device_module: anritsu_sg
  operation: configure_selected_parameters
  configuration_required: false
  parameter_actions: []
  configuration:
    frequency: 1 GHz
    power: -30 dBm
  children: []
"""
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["anritsu"]["signal_generator"].update(
            {
                "control_protocol": "basic_scpi",
                "frequency": {"min": "100 MHz", "max": "6 GHz"},
                "power": {"min": "-100 dBm", "max": "0 dBm"},
            }
        )
        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            parse_recipe_text(source)
        )
        self.assertEqual(
            [action.kind for action in plan.actions],
            ["configure_anritsu_sg"],
        )

    def test_output_on_authoring_action_expands_to_direct_rigol_output(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        source = """\
schema_version: 1
name: consolidated-rigol-output-on
dut_limits:
  rigol:
    1:
      minimum_impedance: 50 ohm
      max_abs_current: 1 mA
      max_abs_power: 1 mW
root:
  id: root
  type: sequence
  children:
    - id: configure
      type: configure_rigol
      channel: 1
      waveform: SIN
      frequency: 1 kHz
      high_level: 1 mV
      low_level: -1 mV
      dut_min_impedance: 50 ohm
    - {id: output-on, type: enable_rigol_output, channel: 1}
"""
        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            parse_recipe_text(source)
        )
        self.assertEqual(plan.actions[-1].kind, "set_rigol_output")
        self.assertEqual(plan.actions[-1].node_id, "output-on.rigol.output-on")
        self.assertTrue(plan.actions[-1].payload["enabled"])

    def test_consolidated_output_on_is_rejected_in_finally(self) -> None:
        source = """\
schema_version: 1
name: unsafe-finally-output-on
root: {id: wait, type: wait, duration: 1 ms}
finally:
  - {id: output-on, type: enable_rigol_output, channel: 1}
"""
        with self.assertRaisesRegex(SafetyViolation, "not allowed in finally"):
            RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))

    def test_rf_interlock_blocks_unapproved_recipe(self) -> None:
        recipe = load_recipe(
            ROOT / "recipes" / "example_energized_nested_sweep_template.yml"
        )
        with self.assertRaises(SafetyViolation):
            RecipeCompiler(loaded_settings()).compile(recipe)

    def test_example_expands_to_2000_spectra(self) -> None:
        recipe = load_recipe(
            ROOT / "recipes" / "example_energized_nested_sweep_template.yml"
        )
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(recipe)
        self.assertEqual(plan.total_points, 2000)
        self.assertEqual(plan.required_devices, {"rigol", "keithley", "anritsu"})
        self.assertEqual(
            plan.safe_shutdown_actions,
            (
                "keithley.outputs_off",
                "rigol.outputs_off",
                "anritsu.rf_off_and_abort",
                "storage.flush_checkpoint",
            ),
        )
        self.assertEqual(len(plan.sha256), 64)
        self.assertIn("ramp_keithley_to_zero", tuple(action.kind for action in plan.actions))
        self.assertEqual(plan.actions[-1].kind, "set_keithley_output")
        self.assertFalse(plan.actions[-1].payload["enabled"])
        self.assertTrue(plan.actions[-1].is_finally)

    def test_energized_template_uses_direct_output_actions(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        recipe = load_recipe(ROOT / "recipes" / "example_energized_nested_sweep_template.yml")
        plan = RecipeCompiler(settings).compile(recipe)
        self.assertEqual(plan.total_points, 2000)
        self.assertIn("set_rigol_output", tuple(action.kind for action in plan.actions))
        self.assertIn("set_keithley_output", tuple(action.kind for action in plan.actions))

    def test_recipe_rejects_duplicate_node_id(self) -> None:
        source = """\
schema_version: 1
name: duplicate-id
root:
  id: root
  type: sequence
  children:
    - id: duplicate
      type: comment
    - id: duplicate
      type: comment
"""
        with self.assertRaisesRegex(Exception, "not unique"):
            parse_recipe_text(source)

    def test_repeat_expands_finitely_and_preserves_iteration_setpoint(self) -> None:
        source = """\
schema_version: 1
name: repeat-checkpoints
root:
  id: repeated
  type: repeat
  count: 3
  children:
    - id: checkpoint
      type: checkpoint
      label: repeated scalar point
"""
        plan = RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))
        self.assertEqual(plan.total_points, 3)
        self.assertEqual(plan.total_spectra, 0)
        self.assertEqual([action.kind for action in plan.actions], ["checkpoint"] * 3)
        self.assertEqual(
            [action.setpoints_si["repeat.repeated.index"] for action in plan.actions],
            [0.0, 1.0, 2.0],
        )

    def test_fixed_keithley_a_is_executed_once_and_kept_in_sweep_checkpoints(self) -> None:
        source = """\
schema_version: 1
name: fixed-a-provenance
root:
  id: root
  type: sequence
  children:
    - id: fixed-a
      type: configure_keithley
      channel: A
      mode: current
      level: 0.5 mA
      compliance: 67 mV
    - id: current-sweep
      type: sweep
      target: keithley.B.current
      start: 1 mA
      stop: 2 mA
      points: 2
      children:
        - id: configure-b
          type: configure_keithley
          channel: B
          mode: current
          level: "${keithley.B.current}"
          compliance: 67 mV
        - id: checkpoint
          type: checkpoint
"""
        raw = deepcopy(simulation_settings().model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        settings = StationSettings.model_validate(raw)
        plan = RecipeCompiler(settings).compile(parse_recipe_text(source))
        fixed_actions = [action for action in plan.actions if action.node_id == "fixed-a"]
        checkpoints = [action for action in plan.actions if action.kind == "checkpoint"]
        self.assertEqual(len(fixed_actions), 1)
        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(
            [action.setpoints_si["keithley.A.current"] for action in checkpoints],
            [0.0005, 0.0005],
        )
        self.assertEqual(
            [action.setpoints_si["keithley.B.current"] for action in checkpoints],
            [0.001, 0.002],
        )

    def test_conditional_branch_is_resolved_for_each_sweep_value(self) -> None:
        source = """\
schema_version: 1
name: conditional
root:
  id: current
  type: sweep
  target: keithley.B.current
  start: 1 mA
  stop: 3 mA
  points: 3
  children:
    - id: threshold
      type: if
      left: "${keithley.B.current}"
      operator: ">="
      right: 2 mA
      children:
        - id: spectrum
          type: acquire_spectrum
      else:
        - id: below-threshold
          type: checkpoint
"""
        plan = RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))
        self.assertEqual(plan.total_points, 3)
        self.assertEqual(plan.total_spectra, 2)
        self.assertEqual(
            [action.kind for action in plan.actions],
            ["checkpoint", "acquire_spectrum", "acquire_spectrum"],
        )

    def test_connect_node_compiles_to_explicit_verified_session_check(self) -> None:
        source = """\
schema_version: 1
name: explicit-connection
root:
  id: connect-anritsu
  type: connect
  device: anritsu
"""
        plan = RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))
        self.assertEqual(plan.actions[0].kind, "verify_connection")
        self.assertEqual(plan.actions[0].payload, {"device": "anritsu"})
        self.assertEqual(plan.required_devices, {"anritsu"})
        self.assertEqual(
            plan.safe_shutdown_actions,
            (
                "anritsu.rf_off_and_abort",
                "keithley.outputs_off",
                "rigol.outputs_off",
                "storage.flush_checkpoint",
            ),
        )

    def test_repeat_rejects_unbounded_or_excessive_count(self) -> None:
        for count in (0, 100_001, "forever"):
            source = f"""\
schema_version: 1
name: invalid-repeat
root:
  id: repeated
  type: repeat
  count: {count}
  children:
    - id: checkpoint
      type: checkpoint
"""
            with self.subTest(count=count), self.assertRaisesRegex(Exception, "count"):
                parse_recipe_text(source)

    def test_finally_rejects_non_cleanup_action(self) -> None:
        source = """\
schema_version: 1
name: invalid-finally
root:
  id: root
  type: sequence
  children:
    - id: wait-main
      type: wait
      duration: "0 s"
finally:
  - id: wait-finally
    type: wait
    duration: "0 s"
"""
        with self.assertRaisesRegex(SafetyViolation, "finally section"):
            RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))

    def test_recipe_rejects_non_qualified_anritsu_trace_name(self) -> None:
        source = """\
schema_version: 1
name: invalid-trace
root:
  id: root
  type: sequence
  children:
    - id: acquire
      type: acquire_spectrum
      trace: "TRAC1;*RST"
"""
        with self.assertRaisesRegex(SafetyViolation, "TRAC1"):
            RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))

    def test_legacy_recipe_dut_limits_are_ignored(self) -> None:
        source = """\
schema_version: 1
name: dut-envelope
dut_limits:
  keithley:
    B:
      current: {min: "-2 mA", max: "2 mA"}
      voltage: {min: "-70 mV", max: "70 mV"}
      max_abs_power: "100 uW"
root:
  id: configure
  type: configure_keithley
  channel: B
  mode: current
  level: "1 mA"
  compliance: "67 mV"
"""
        plan = RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))
        request = plan.actions[0].payload["request"]
        self.assertFalse(hasattr(request, "dut_envelope"))

    def test_legacy_recipe_dut_current_limit_does_not_override_station_profile(self) -> None:
        source = """\
schema_version: 1
name: unsafe-dut-current
dut_limits:
  keithley:
    B:
      current: {min: "0 A", max: "500 uA"}
      voltage: {min: "-70 mV", max: "70 mV"}
root:
  id: configure
  type: configure_keithley
  channel: B
  mode: current
  level: "1 mA"
  compliance: "67 mV"
"""
        plan = RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))
        self.assertEqual(plan.actions[0].payload["request"].level_si, 0.001)

    def test_legacy_recipe_dut_rigol_limit_is_ignored(self) -> None:
        source = """\
schema_version: 1
name: unsafe-rigol-dut
dut_limits:
  rigol:
    1:
      minimum_impedance: "50 ohm"
      max_abs_current: "5 uA"
      max_abs_power: "10 uW"
root:
  id: configure
  type: configure_rigol
  channel: 1
  waveform: SQU
  frequency: "1 kHz"
  high_level: "1 mV"
  low_level: "-1 mV"
  output_load: HIGHZ
  dut_min_impedance: "50 ohm"
"""
        plan = RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))
        self.assertEqual(plan.actions[0].kind, "configure_rigol")

    def test_legacy_recipe_dut_anritsu_input_is_ignored(self) -> None:
        source = """\
schema_version: 1
name: unsafe-rf-input
dut_limits:
  anritsu:
    max_expected_input: "0 dBm"
root:
  id: configure
  type: configure_anritsu
  start_frequency: "1 MHz"
  stop_frequency: "2 MHz"
  reference_level: "0 dBm"
  points: 101
"""
        plan = RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))
        self.assertEqual(plan.actions[0].kind, "configure_anritsu")

    def test_keithley_output_on_requires_permission_but_demo_does_not(self) -> None:
        source = """\
schema_version: 1
name: missing-dut-envelope
root:
  id: sequence
  type: sequence
  children:
    - id: configure
      type: configure_keithley
      channel: B
      mode: current
      level: 1 mA
      compliance: 67 mV
    - id: output-on
      type: set_keithley_output
      channel: B
      enabled: true
"""
        settings = simulation_settings(approved=False)
        with self.assertRaisesRegex(SafetyViolation, "output permission is disabled"):
            RecipeCompiler(settings).compile(parse_recipe_text(source))

        raw = settings.model_dump(mode="python")
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        permitted = StationSettings.model_validate(raw)
        permitted_plan = RecipeCompiler(permitted).compile(parse_recipe_text(source))
        self.assertEqual(permitted_plan.actions[-1].kind, "set_keithley_output")

        demo = RecipeCompiler(settings, outputs_forced_off=True).compile(
            parse_recipe_text(source)
        )
        self.assertEqual(
            [action.kind for action in demo.actions],
            ["configure_keithley", "set_keithley_output"],
        )

    def test_qualified_anritsu_advanced_action_compiles_documented_values(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["anritsu"]["advanced_spectrum"] = {
            "control_protocol": "standard_scpi",
            "qualified_firmware": ["7.03.00"],
        }
        raw["devices"]["anritsu"]["identity"]["required_options"] = ["008"]
        rf_input = raw["devices"]["anritsu"]["safety"]["rf_input"]
        rf_input["minimum_internal_attenuation"] = "20 dB"
        rf_input["preamplifier_allowed"] = True
        source = """\
schema_version: 1
name: qualified-advanced-spectrum
root:
  id: advanced
  type: configure_anritsu_advanced
  rbw_mode: manual
  rbw: "3 kHz"
  vbw_mode: off
  detector: POS
  attenuation_mode: manual
  attenuation: "20 dB"
  preamplifier_enabled: true
  sweep_time_mode: manual
  sweep_time: "200 ms"
"""

        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            parse_recipe_text(source)
        )
        config = plan.actions[0].payload["config"]
        self.assertEqual(plan.actions[0].kind, "configure_anritsu_advanced")
        self.assertEqual(config.rbw_hz, 3e3)
        self.assertEqual(config.vbw_mode, "off")
        self.assertEqual(config.attenuation_db, 20)
        self.assertTrue(config.preamplifier_enabled)
        self.assertEqual(config.sweep_time_s, 0.2)

    def test_keithley_keep_on_then_continue_sweep_never_reconfigures_or_turns_off(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        source = """\
schema_version: 1
name: continuous-keithley
dut_limits:
  keithley:
    A:
      current: {min: 0 A, max: 3 mA}
      voltage: {min: -1 V, max: 1 V}
      max_abs_power: 3 mW
root:
  id: root
  type: sequence
  children:
    - id: establish
      type: sequence
      device_module: keithley
      operation: configure_selected_parameters
      output_policy: on_keep
      configuration:
            {channel: A, source_mode: current, source_level: 500 uA,
         compliance: 100 mV, nplc: 1, settling_time: 0 s, sense_mode: 2wire,
         source_autorange: true, source_range: AUTO,
         measure_voltage_autorange: true, measure_voltage_range: AUTO,
         measure_current_autorange: true, measure_current_range: AUTO}
      parameter_actions:
            - {parameter_id: source.level, mode: set, value: 500 uA}
    - id: live-sweep
      type: sequence
      device_module: keithley
      operation: configure_selected_parameters
      output_policy: continue
      configuration:
            {channel: A, source_mode: current, source_level: 500 uA,
         compliance: 100 mV, nplc: 1, settling_time: 0 s, sense_mode: 2wire,
         source_autorange: true, source_range: AUTO,
         measure_voltage_autorange: true, measure_voltage_range: AUTO,
         measure_current_autorange: true, measure_current_range: AUTO}
      parameter_actions:
        - parameter_id: source.level
          mode: sweep
          value: 500 uA
          segments:
                - {start: 500 uA, stop: 1 mA, points: 2}
"""
        plan = RecipeCompiler(StationSettings.model_validate(raw)).compile(
            parse_recipe_text(source)
        )
        kinds = [action.kind for action in plan.actions]
        self.assertEqual(kinds.count("configure_keithley"), 1)
        self.assertEqual(kinds.count("set_keithley_output"), 1)
        self.assertTrue(plan.actions[kinds.index("set_keithley_output")].payload["enabled"])
        self.assertEqual(kinds.count("assert_output_on"), 3)
        self.assertEqual(kinds.count("update_keithley_level"), 2)

    def test_continuous_output_without_plan_owned_on_transition_is_rejected(self) -> None:
        source = """\
schema_version: 1
name: invalid-continuity
root:
  id: live
  type: sequence
  device_module: keithley
  operation: configure_selected_parameters
  output_policy: continue
  configuration:
    {channel: A, source_mode: current, source_level: 500 uA,
     compliance: 100 mV, nplc: 1, settling_time: 0 s, sense_mode: 2wire,
     source_autorange: true, source_range: AUTO,
     measure_voltage_autorange: true, measure_voltage_range: AUTO,
     measure_current_autorange: true, measure_current_range: AUTO}
  parameter_actions:
    - parameter_id: source.level
      mode: sweep
      value: 500 uA
      segments:
        - {start: 500 uA, stop: 1 mA, points: 2}
"""
        raw = deepcopy(simulation_settings().model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        with self.assertRaisesRegex(
            Exception, "continuous OUTPUT requires an earlier configuration"
        ):
            RecipeCompiler(StationSettings.model_validate(raw)).compile(
                parse_recipe_text(source)
            )


if __name__ == "__main__":
    unittest.main()
