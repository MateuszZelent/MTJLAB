from __future__ import annotations

import unittest
from copy import deepcopy

from app.domain.errors import SafetyViolation
from app.engine.compiler import RecipeCompiler
from app.recipes import load_recipe, parse_recipe_text
from app.settings.models import StationSettings
from tests.helpers import ROOT, loaded_settings, simulation_settings


class RecipeCompilerTests(unittest.TestCase):
    def test_rf_interlock_blocks_unapproved_recipe(self) -> None:
        recipe = load_recipe(ROOT / "recipes" / "example_nested_sweep.yml")
        with self.assertRaises(SafetyViolation):
            RecipeCompiler(loaded_settings()).compile(recipe)

    def test_example_expands_to_2000_spectra(self) -> None:
        recipe = load_recipe(ROOT / "recipes" / "example_nested_sweep.yml")
        plan = RecipeCompiler(simulation_settings()).compile(recipe)
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

    def test_energized_template_requires_and_uses_explicit_arm_actions(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        recipe = load_recipe(ROOT / "recipes" / "example_energized_nested_sweep_template.yml")
        plan = RecipeCompiler(settings).compile(recipe)
        self.assertEqual(plan.total_points, 2000)
        self.assertIn("arm_rigol_output", tuple(action.kind for action in plan.actions))
        self.assertIn("arm_keithley_output", tuple(action.kind for action in plan.actions))

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
            ("anritsu.rf_off_and_abort", "storage.flush_checkpoint"),
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

    def test_recipe_dut_limits_are_parsed_and_embedded_in_keithley_request(self) -> None:
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
        envelope = plan.actions[0].payload["request"].dut_envelope
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope.current_max_a, 0.002)
        self.assertEqual(envelope.voltage_min_v, -0.07)
        self.assertAlmostEqual(envelope.max_abs_power_w, 100e-6)

    def test_recipe_dut_current_limit_intersects_station_profile(self) -> None:
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
        with self.assertRaisesRegex(SafetyViolation, "DUT limit"):
            RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))

    def test_recipe_dut_rigol_current_limit_is_applied_to_every_expanded_value(self) -> None:
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
        with self.assertRaisesRegex(SafetyViolation, "5e-06 A"):
            RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))

    def test_recipe_dut_anritsu_input_must_not_exceed_station_limit(self) -> None:
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
        with self.assertRaisesRegex(SafetyViolation, "exceeds the station limit"):
            RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))

    def test_output_arm_requires_complete_dut_limits(self) -> None:
        source = """\
schema_version: 1
name: missing-dut-envelope
root:
  id: arm
  type: arm_keithley_output
  channel: B
"""
        settings = simulation_settings(approved=True)
        raw = deepcopy(settings.model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        with self.assertRaisesRegex(SafetyViolation, "complete recipe.dut_limits"):
            RecipeCompiler(StationSettings.model_validate(raw)).compile(
                parse_recipe_text(source)
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


if __name__ == "__main__":
    unittest.main()
