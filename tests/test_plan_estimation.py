from __future__ import annotations

from copy import deepcopy
import unittest

from app.engine import ExecutionPlan, PlanAction, PlanEstimator, RecipeCompiler
from app.engine.policy import ExecutionPolicy
from app.recipes import load_recipe
from app.settings.models import StationSettings
from tests.helpers import ROOT, simulation_settings


class PlanEstimationTests(unittest.TestCase):
    def test_nested_2000_spectrum_plan_has_time_and_uncompressed_size_model(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        plan = RecipeCompiler(settings).compile(
            load_recipe(
                ROOT / "recipes" / "example_energized_nested_sweep_template.yml"
            )
        )
        estimate = PlanEstimator(settings).estimate(plan)

        self.assertEqual(estimate.checkpoints, 2000)
        self.assertEqual(estimate.spectra, 2000)
        self.assertGreater(estimate.spectrum_values, 2_000_000)
        self.assertGreater(estimate.nominal_duration_s, 0)
        self.assertGreaterEqual(estimate.retry_upper_duration_s, estimate.nominal_duration_s)
        self.assertGreater(estimate.uncompressed_hdf5_bytes, estimate.spectrum_values * 8)
        self.assertIn("Large run", " ".join(estimate.warnings))

    def test_energized_plan_carries_an_explicit_static_warning(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        plan = RecipeCompiler(settings).compile(
            load_recipe(ROOT / "recipes" / "example_energized_template.yml")
        )
        estimate = PlanEstimator(settings).estimate(plan)

        self.assertIn("OUTPUT ON", " ".join(estimate.warnings))

    def test_no_checkpoint_plan_is_reported_without_claiming_data(self) -> None:
        plan = ExecutionPlan(
            recipe_name="wait-only",
            actions=(PlanAction("wait", "wait", {"duration_s": 2.0}, {}),),
            total_points=0,
            sha256="wait-only",
            recipe_source="schema_version: 1\n",
        )
        estimate = PlanEstimator(simulation_settings()).estimate(plan)

        self.assertGreaterEqual(estimate.nominal_duration_s, 2.0)
        self.assertIn("no checkpoints", " ".join(estimate.warnings))

    def test_spectrum_average_count_multiplies_physical_acquisition_time(self) -> None:
        single = ExecutionPlan(
            recipe_name="single",
            actions=(
                PlanAction(
                    "spectrum",
                    "acquire_spectrum",
                    {"trace": "TRAC1", "average_count": 1},
                    {},
                ),
            ),
            total_points=1,
            sha256="single",
            recipe_source="schema_version: 1\n",
            total_spectra=1,
        )
        averaged = ExecutionPlan(
            recipe_name="averaged",
            actions=(
                PlanAction(
                    "spectrum",
                    "acquire_spectrum",
                    {"trace": "TRAC1", "average_count": 4},
                    {},
                ),
            ),
            total_points=1,
            sha256="averaged",
            recipe_source="schema_version: 1\n",
            total_spectra=1,
        )
        estimator = PlanEstimator(simulation_settings())
        single_estimate = estimator.estimate(single)
        averaged_estimate = estimator.estimate(averaged)

        self.assertGreater(
            averaged_estimate.nominal_duration_s,
            single_estimate.nominal_duration_s * 3,
        )
        self.assertIn("averages 4 complete spectra", " ".join(averaged_estimate.warnings))
        policy = ExecutionPolicy(
            command_timeout_s=5,
            acquisition_timeout_s=30,
            watchdog_grace_s=0.5,
        )
        self.assertEqual(policy.deadline_for(averaged.actions[0]), 125.5)


if __name__ == "__main__":
    unittest.main()
