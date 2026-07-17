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

    def test_piecewise_axis_keeps_exact_119_points_and_ignores_fixed_control(self) -> None:
        schema = ThatecSchemaMapper.from_recipe_source(
            """\
schema_version: 1
name: fixed-plus-piecewise
root:
  id: root
  type: sequence
  children:
    - id: fixed-a
      type: configure_keithley
      channel: A
      mode: current
      level: 0.5 mA
      compliance: 1 V
    - id: current-sweep
      type: sweep
      target: keithley.B.current
      segments:
        - {start: 10 mA, stop: 100 mA, points: 100}
        - {start: 100 mA, stop: 150 mA, points: 20}
      children:
        - id: spectrum
          type: acquire_spectrum
""",
            expected_points=119,
        )

        self.assertEqual(schema.mode, "recipe_sweeps")
        self.assertEqual(tuple(axis.target for axis in schema.axes), ("keithley.B.current",))
        self.assertEqual(schema.axes[0].spacing, "piecewise")
        self.assertEqual(schema.axes[0].points, 119)
        self.assertEqual(schema.axes[0].values_si[0], 0.01)
        self.assertEqual(schema.axes[0].values_si[99], 0.1)
        self.assertEqual(schema.axes[0].values_si[-1], 0.15)

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

    def test_anritsu_signal_generator_sweep_is_a_public_rf_axis(self) -> None:
        schema = ThatecSchemaMapper.from_recipe_source(
            """\
schema_version: 1
name: sg-axis
root:
  id: sg-sweep
  type: sweep
  target: anritsu.sg.frequency
  segments:
    - {start: 1 GHz, stop: 2 GHz, points: 3}
  children:
    - id: spectrum
      type: acquire_spectrum
""",
            expected_points=3,
        )
        self.assertEqual(schema.mode, "recipe_sweeps")
        self.assertEqual(schema.axes[0].target, "anritsu.sg.frequency")
        self.assertEqual(schema.axes[0].values_si, (1e9, 1.5e9, 2e9))
        self.assertEqual(schema.axes[0].unit, "Hz")

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
