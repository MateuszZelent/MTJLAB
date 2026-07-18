from __future__ import annotations

import unittest

from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_CURRENT
from app.recipes import (
    estimate_sweep_point_count,
    generate_sweep_points,
    generate_sweep_stage_points,
    parse_recipe_text,
)
from app.engine.compiler import RecipeCompiler
from tests.helpers import simulation_settings


class SweepPointGeneratorTests(unittest.TestCase):
    def test_count_estimate_matches_generation_without_allocating_axis(self) -> None:
        segments = [
            {"start": "0 A", "stop": "1 A", "points": 11},
            {"start": "1 A", "stop": "2 A", "step": "0.25 A"},
            {"value": "3 A"},
        ]
        self.assertEqual(
            estimate_sweep_point_count(segments, DIMENSION_CURRENT),
            len(generate_sweep_points(segments, DIMENSION_CURRENT)),
        )

    def test_count_estimate_handles_a_large_step_axis(self) -> None:
        self.assertEqual(
            estimate_sweep_point_count(
                [{"start": "0 A", "stop": "1000 A", "step": "1 mA"}],
                DIMENSION_CURRENT,
            ),
            1_000_001,
        )

    def test_descending_second_stage_preserves_full_round_trip_trajectory(self) -> None:
        segments = [
            {"start": "0 A", "stop": "1 A", "points": 3, "spacing": "linear"},
            {"start": "1 A", "stop": "0 A", "points": 3, "spacing": "linear"},
        ]
        stages = generate_sweep_stage_points(segments, DIMENSION_CURRENT)
        self.assertEqual(
            tuple(tuple(point.si_value for point in stage) for stage in stages),
            ((0.0, 0.5, 1.0), (0.5, 0.0)),
        )
        self.assertEqual(
            tuple(point.si_value for point in generate_sweep_points(segments, DIMENSION_CURRENT)),
            (0.0, 0.5, 1.0, 0.5, 0.0),
        )

    def test_single_value_stage_is_an_explicit_measurement_and_is_not_deduplicated(self) -> None:
        segments = [
            {"start": "0 A", "stop": "1 A", "points": 3, "spacing": "linear"},
            {"value": "1 A"},
            {"value": "2 A"},
            {"value": "0 A"},
        ]
        stages = generate_sweep_stage_points(segments, DIMENSION_CURRENT)
        self.assertEqual(tuple(len(stage) for stage in stages), (3, 1, 1, 1))
        self.assertEqual(
            tuple(point.si_value for point in generate_sweep_points(segments, DIMENSION_CURRENT)),
            (0.0, 0.5, 1.0, 1.0, 2.0, 0.0),
        )

    def test_axis_may_consist_of_one_single_value_measurement(self) -> None:
        points = generate_sweep_points([{"value": "0 A"}], DIMENSION_CURRENT)
        self.assertEqual(tuple(point.si_value for point in points), (0.0,))

    def test_single_value_stage_round_trips_through_recipe_and_compiler(self) -> None:
        recipe = parse_recipe_text(
            """\
schema_version: 1
name: single-point-stage
root:
  id: axis
  type: sweep
  target: keithley.B.current
  segments:
    - {value: 0 A}
  children:
    - {id: checkpoint, type: checkpoint, label: zero}
"""
        )
        plan = RecipeCompiler(simulation_settings()).compile(recipe)
        self.assertEqual(plan.total_points, 1)
        self.assertEqual(plan.actions[0].setpoints_si["keithley.B.current"], 0.0)

    def test_multiple_intervals_join_into_one_deduplicated_axis(self) -> None:
        points = generate_sweep_points(
            [
                {"start": "0 A", "stop": "10 mA", "step": "2 mA"},
                {"start": "10 mA", "stop": "20 mA", "points": 3},
            ],
            DIMENSION_CURRENT,
        )
        self.assertEqual(tuple(round(point.si_value, 12) for point in points), (0, .002, .004, .006, .008, .01, .015, .02))

    def test_stage_points_preserve_visual_provenance_without_changing_axis(self) -> None:
        segments = [
            {"start": "10 mA", "stop": "100 mA", "points": 100},
            {"start": "100 mA", "stop": "150 mA", "points": 20},
        ]
        stages = generate_sweep_stage_points(segments, DIMENSION_CURRENT)
        self.assertEqual(tuple(len(stage) for stage in stages), (100, 19))
        self.assertEqual(len(tuple(point for stage in stages for point in stage)), 119)
        self.assertEqual(
            tuple(point.si_value for stage in stages for point in stage),
            tuple(point.si_value for point in generate_sweep_points(segments, DIMENSION_CURRENT)),
        )

    def test_step_keeps_stop_when_interval_is_not_evenly_divisible(self) -> None:
        points = generate_sweep_points(
            [{"start": "0 A", "stop": "1 A", "step": "0.3 A"}], DIMENSION_CURRENT
        )
        self.assertEqual(tuple(point.si_value for point in points), (0.0, 0.3, 0.6, 0.8999999999999999, 1.0))

    def test_multi_segment_recipe_expands_and_compiles(self) -> None:
        recipe = parse_recipe_text(
            """\
schema_version: 1
name: generated-points
root:
  id: root
  type: sweep
  target: keithley.B.current
  segments:
    - {start: 0 A, stop: 1 mA, step: 500 uA}
    - {start: 1 mA, stop: 2 mA, points: 3}
  children:
    - id: configure
      type: configure_keithley
      channel: B
      mode: current
      level: "${keithley.B.current}"
      compliance: 1 mV
    - id: checkpoint
      type: checkpoint
      label: generated
"""
        )
        plan = RecipeCompiler(simulation_settings()).compile(recipe)
        self.assertEqual(plan.total_points, 5)
        checkpoints = [action for action in plan.actions if action.kind == "checkpoint"]
        self.assertEqual(
            [round(action.setpoints_si["keithley.B.current"], 12) for action in checkpoints],
            [0, .0005, .001, .0015, .002],
        )

    def test_segment_rejects_points_and_step_together(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "exactly one"):
            parse_recipe_text(
                """\
schema_version: 1
name: invalid-generator
root:
  id: root
  type: sweep
  target: keithley.B.current
  segments:
    - {start: 0 A, stop: 1 mA, points: 2, step: 1 mA}
  children:
    - {id: checkpoint, type: checkpoint, label: generated}
"""
            )


if __name__ == "__main__":
    unittest.main()
