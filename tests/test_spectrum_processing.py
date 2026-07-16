from __future__ import annotations

import math
import unittest

from app.spectrum import (
    LinearPowerAverager,
    apply_reference_operation,
    average_dbm_traces,
    frequency_grids_match,
    peak_preserving_indices,
)


class SpectrumProcessingTests(unittest.TestCase):
    def test_dbm_averaging_is_done_in_linear_power(self) -> None:
        averaged = average_dbm_traces(((-10.0, -20.0), (0.0, -20.0)))
        self.assertAlmostEqual(averaged[0], 10 * math.log10(0.55), places=10)
        self.assertAlmostEqual(averaged[1], -20.0)
        self.assertNotAlmostEqual(averaged[0], -5.0)

    def test_reference_operations_have_explicit_units(self) -> None:
        difference, unit = apply_reference_operation((-10.0, -20.0), (-20.0, -20.0), "difference_db")
        self.assertEqual(difference, (10.0, 0.0))
        self.assertEqual(unit, "dB")
        ratio, unit = apply_reference_operation((-10.0, -20.0), (-20.0, -20.0), "ratio_linear")
        self.assertEqual(ratio, (10.0, 1.0))
        self.assertEqual(unit, "ratio")

    def test_reference_requires_matching_point_counts(self) -> None:
        with self.assertRaises(ValueError):
            apply_reference_operation((-10.0, -20.0), (-20.0,), "difference_db")

    def test_streaming_average_matches_batch_average(self) -> None:
        traces = ((-10.0, -20.0), (0.0, -30.0), (-3.0, -40.0))
        averager = LinearPowerAverager()
        for trace in traces:
            averager.add(trace)
        self.assertEqual(averager.count, 3)
        for streamed, batch in zip(averager.result(), average_dbm_traces(traces), strict=True):
            self.assertAlmostEqual(streamed, batch)

    def test_subtract_power_marks_non_positive_residual_as_invalid(self) -> None:
        values, unit = apply_reference_operation((-20.0, -10.0), (-10.0, -20.0), "subtract_power")
        self.assertTrue(math.isnan(values[0]))
        self.assertTrue(math.isfinite(values[1]))
        self.assertEqual(unit, "dBm")

    def test_frequency_grid_comparison_absorbs_float_rounding_only(self) -> None:
        self.assertTrue(frequency_grids_match((1e9, 2e9), (1e9 + 0.1, 2e9 - 0.1)))
        self.assertFalse(frequency_grids_match((1e9, 2e9), (1e9, 2.1e9)))

    def test_peak_preserving_downsampling_keeps_narrow_peak(self) -> None:
        values = [-100.0] * 10_001
        values[5_123] = 0.0
        indices = peak_preserving_indices(values, 200)
        self.assertLessEqual(len(indices), 200)
        self.assertIn(5_123, indices)


if __name__ == "__main__":
    unittest.main()
