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
        with self.assertRaisesRegex(Exception, "nie jest unikalny"):
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
        with self.assertRaisesRegex(SafetyViolation, "Sekcja finally"):
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


if __name__ == "__main__":
    unittest.main()
