from __future__ import annotations

import unittest

from app.domain.errors import SafetyViolation
from app.engine.compiler import RecipeCompiler
from app.recipes import load_recipe
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
        self.assertEqual(plan.actions[-1].kind, "set_keithley_output")
        self.assertFalse(plan.actions[-1].payload["enabled"])


if __name__ == "__main__":
    unittest.main()

