from __future__ import annotations

import unittest

from app.storage.thatec_schema_mapper import ThatecSchemaMapper


class ThatecSchemaMapperTests(unittest.TestCase):
    def test_nested_sweeps_become_ordered_si_axes(self) -> None:
        schema = ThatecSchemaMapper.from_recipe_source(
            """\
schema_version: 1
name: nested
root:
  id: outer
  type: sweep
  target: keithley.B.current
  start: 1 mA
  stop: 2 mA
  points: 2
  children:
    - id: inner
      type: sweep
      target: rigol.1.high_level
      start: 1 mV
      stop: 3 mV
      points: 3
      children:
        - id: spectrum
          type: acquire_spectrum
""",
            expected_points=6,
        )

        self.assertEqual(schema.mode, "recipe_sweeps")
        self.assertEqual(
            tuple(axis.target for axis in schema.axes),
            ("keithley.B.current", "rigol.1.high_level"),
        )
        self.assertEqual(schema.axes[0].values_si, (0.001, 0.002))
        self.assertEqual(schema.axes[1].values_si, (0.001, 0.002, 0.003))
        self.assertEqual(schema.point_count, 6)

    def test_log_sweep_preserves_actual_coordinate_values(self) -> None:
        schema = ThatecSchemaMapper.from_recipe_source(
            """\
schema_version: 1
name: logarithmic
root:
  id: sweep
  type: sweep
  target: keithley.B.current
  start: 1 uA
  stop: 1 mA
  points: 4
  spacing: log
  children:
    - id: spectrum
      type: acquire_spectrum
""",
            expected_points=4,
        )

        self.assertEqual(schema.mode, "recipe_sweeps")
        self.assertEqual(schema.axes[0].spacing, "log")
        for actual, expected in zip(
            schema.axes[0].values_si, (1e-6, 1e-5, 1e-4, 1e-3), strict=True
        ):
            self.assertAlmostEqual(actual, expected)

    def test_repeated_acquisitions_add_an_explicit_fast_axis(self) -> None:
        schema = ThatecSchemaMapper.from_recipe_source(
            """\
schema_version: 1
name: repeated
root:
  id: sweep
  type: sweep
  target: keithley.B.current
  start: 1 mA
  stop: 2 mA
  points: 2
  children:
    - id: first
      type: acquire_spectrum
    - id: second
      type: acquire_spectrum
""",
            expected_points=4,
        )

        self.assertEqual(
            tuple(axis.target for axis in schema.axes),
            ("keithley.B.current", "measurement.acquisition"),
        )
        self.assertEqual(schema.axes[-1].values_si, (0.0, 1.0))

    def test_repeat_node_becomes_an_explicit_recipe_axis(self) -> None:
        schema = ThatecSchemaMapper.from_recipe_source(
            """\
schema_version: 1
name: repeated-node
root:
  id: samples
  type: repeat
  count: 3
  children:
    - id: spectrum
      type: acquire_spectrum
""",
            expected_points=3,
        )

        self.assertEqual(schema.mode, "recipe_sweeps")
        self.assertEqual(schema.axes[0].target, "repeat.samples.index")
        self.assertEqual(schema.axes[0].values_si, (0.0, 1.0, 2.0))

    def test_ambiguous_topology_falls_back_without_losing_points(self) -> None:
        schema = ThatecSchemaMapper.from_recipe_source(
            """\
schema_version: 1
name: branches
root:
  id: root
  type: sequence
  children:
    - id: direct
      type: acquire_spectrum
    - id: sweep
      type: sweep
      target: keithley.B.current
      start: 1 mA
      stop: 2 mA
      points: 2
      children:
        - id: nested
          type: acquire_spectrum
""",
            expected_points=3,
        )

        self.assertEqual(schema.mode, "checkpoint_fallback")
        self.assertEqual(schema.point_count, 3)
        self.assertEqual(schema.axes[0].control_name, "Checkpoint")


if __name__ == "__main__":
    unittest.main()
