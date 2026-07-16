from __future__ import annotations

import math
import unittest

from app.spectrum import apply_reference_operation, average_dbm_traces


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


if __name__ == "__main__":
    unittest.main()
