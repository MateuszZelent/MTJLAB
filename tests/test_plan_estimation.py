from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import replace

import pytest
import yaml

from app.domain.errors import ConfigurationError
from app.engine import ExecutionPlan, PlanAction, PlanEstimator, RecipeCompiler
from app.engine.policy import ExecutionPolicy
from app.recipes import load_recipe, parse_recipe_text
from app.settings.models import StationSettings
from tests.helpers import ROOT, simulation_settings
from tests.test_sweep_audit_contracts import audit_settings, compile_source


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


def test_instrument_time_estimate_uses_whole_sweeps_and_one_transfer(tmp_path):
    settings = audit_settings(tmp_path)
    source = """
    - {id: analyzer, type: configure_anritsu, start_frequency: '1 MHz', stop_frequency: '2 MHz', reference_level: '0 dBm', points: 101}
    - {id: timing, type: configure_anritsu_advanced, sweep_time_mode: manual, sweep_time: '50 ms'}
    - {id: measurement, type: acquire_spectrum, instrument_average_duration: '101 ms'}
"""
    plan = compile_source(settings, source)
    # The pre-estimate compiler at ca7e116 produced this resume identity for the same recipe.
    assert plan.sha256 == "93b5589e56d3acf9f27b9cd62d8df861044380dec1ba710693be2389837ce217"
    payload = plan.actions[-1].payload
    assert payload["instrument_average_duration_s"] == pytest.approx(0.101)
    assert payload["instrument_average_nominal_sweep_count"] == 3
    assert payload["instrument_average_nominal_duration_s"] == pytest.approx(0.15)
    estimator = PlanEstimator(settings)
    timed = estimator.estimate(plan)
    exact = estimator.estimate(compile_source(
        settings, source.replace("instrument_average_duration: '101 ms'", "instrument_average_count: 3"),
    ))
    assert timed.nominal_duration_s == exact.nominal_duration_s
    assert timed.retry_upper_duration_s == exact.retry_upper_duration_s
    assert timed.spectrum_values == exact.spectrum_values == 101
    assert any("3 sweeps" in warning and "0.15 s" in warning for warning in timed.warnings)


@pytest.mark.parametrize(("sweep_time", "target", "message"), [
    ("20 s", "100 ms", "2 instrument sweeps.*acquisition timeout"),
    ("5.1 s", "29 s", "6 instrument sweeps.*acquisition timeout"),
    ("1 ms", "10 s", "limit of 9999 sweeps"),
])
def test_preflight_rejects_timed_instrument_targets_that_cannot_complete(tmp_path, sweep_time, target, message):
    with pytest.raises(ConfigurationError, match=message):
        compile_source(audit_settings(tmp_path), f"""
    - {{id: timing, type: configure_anritsu_advanced, sweep_time_mode: manual, sweep_time: '{sweep_time}'}}
    - {{id: measurement, type: acquire_spectrum, instrument_average_duration: '{target}'}}
""")


def test_preflight_rejects_imported_anritsu_reference_for_pylab_correction(tmp_path):
    from tests.test_sweep_spectrum_processing import saved_reference, source_with_file

    path = saved_reference(tmp_path, "reference", instrument_average=True)
    recipe = parse_recipe_text(source_with_file(path, "reference"))
    with pytest.raises(ConfigurationError, match="matching acquisition modes"):
        RecipeCompiler(simulation_settings()).compile(recipe)


def test_preflight_keeps_legacy_anritsu_reference_backend_without_inventing_timing(tmp_path):
    from app.storage.reference_store import ReferenceHdf5Store
    from tests.test_sweep_spectrum_processing import saved_reference, source_with_file

    saved = ReferenceHdf5Store.load(saved_reference(tmp_path, "reference", instrument_average=True))
    legacy = replace(saved, trace=replace(saved.trace, trace_name="TRAC1_ANRITSU_LAV8", instrument_average=None))
    path = tmp_path / "legacy-reference.h5"
    ReferenceHdf5Store.save(path, legacy)
    source = yaml.safe_load(source_with_file(path, "reference"))
    _analyzer, _reference, signal = source["root"]["children"]
    signal.pop("average_count")
    signal["instrument_average_count"] = 8
    compiler = RecipeCompiler(simulation_settings())
    plan = compiler.compile(parse_recipe_text(yaml.safe_dump(source)))
    payload = plan.actions[1].payload
    assert payload["reference_averaging_source"] == "instrument"
    assert payload["average_count"] == 8
    assert "reference_instrument_average" not in payload


def test_preflight_requires_spectrum_analyzer_mode_to_verify_a_reference_file(tmp_path):
    from tests.test_sweep_spectrum_processing import saved_reference, source_with_file

    path = saved_reference(tmp_path, "reference")
    source = yaml.safe_load(source_with_file(path, "reference"))
    analyzer, reference, _signal = source["root"]["children"]
    source["root"]["children"] = [analyzer, {
        "id": "sg", "type": "configure_anritsu_sg", "frequency": "1 GHz", "power": "-30 dBm",
    }, reference]
    raw = simulation_settings().model_dump(mode="python")
    raw["devices"]["anritsu"]["signal_generator"].update(
        control_protocol="basic_scpi", frequency={"min": "100 MHz", "max": "6 GHz"},
        power={"min": "-100 dBm", "max": "0 dBm"},
    )
    compiler = RecipeCompiler(StationSettings.model_validate(raw))
    with pytest.raises(ConfigurationError, match="Spectrum Analyzer configuration after SG mode"):
        compiler.compile(parse_recipe_text(yaml.safe_dump(source)))


if __name__ == "__main__":
    unittest.main()
