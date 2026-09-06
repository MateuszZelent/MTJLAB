"""Unit tests for physical parameter extraction and MTJ figures of merit."""

from __future__ import annotations

import math
import unittest

import numpy as np

from app.inventory.analysis import (
    calculate_mtj_metrics,
    parse_dimension_area,
)


class InventoryAnalyticsTests(unittest.TestCase):
    def test_parse_dimension_area(self) -> None:
        # Circular parsing: 200 nm -> radius 100 nm = 0.1 um -> area = pi * 0.1^2 um^2
        area_200nm = parse_dimension_area("200 nm")
        self.assertIsNotNone(area_200nm)
        assert area_200nm is not None
        expected_200nm = math.pi * (0.1**2)
        self.assertAlmostEqual(area_200nm, expected_200nm, places=5)

        # 1 um circular
        area_1um = parse_dimension_area("1 um")
        self.assertIsNotNone(area_1um)
        assert area_1um is not None
        expected_1um = math.pi * (0.5**2)
        self.assertAlmostEqual(area_1um, expected_1um, places=5)

        # Elliptical / rectangular parsing: 100x200 nm
        area_ellip = parse_dimension_area("100x200 nm")
        self.assertIsNotNone(area_ellip)
        assert area_ellip is not None
        expected_ellip = math.pi * 0.05 * 0.1
        self.assertAlmostEqual(area_ellip, expected_ellip, places=5)

        # Invalid or non-dimensional label
        self.assertIsNone(parse_dimension_area("Center Strip"))
        self.assertIsNone(parse_dimension_area(""))
        self.assertIsNone(parse_dimension_area("Pillar Alpha"))

    def test_calculate_mtj_metrics_standard_rh_loop(self) -> None:
        # Synthetic R-vs-H loop:
        # H goes from -200 to +200 Oe and back
        # Switch low at -50 Oe, switch high at +50 Oe
        # Rp = 1000 Ohm, Rap = 2000 Ohm -> TMR = 100%
        # Hc = 50 Oe, H_off = 0 Oe
        h_up = np.linspace(-200, 200, 100)
        r_up = np.where(h_up < 50, 1000.0, 2000.0)

        h_down = np.linspace(200, -200, 100)
        r_down = np.where(h_down > -50, 2000.0, 1000.0)

        h = np.concatenate([h_up, h_down])
        r = np.concatenate([r_up, r_down])

        metrics = calculate_mtj_metrics(
            h, r, x_name="B_Field", y_name="Resistance", dimension_label="200 nm"
        )

        self.assertIsNotNone(metrics.rp)
        self.assertIsNotNone(metrics.rap)
        assert metrics.rp is not None and metrics.rap is not None
        self.assertAlmostEqual(metrics.rp, 1000.0, places=1)
        self.assertAlmostEqual(metrics.rap, 2000.0, places=1)

        self.assertIsNotNone(metrics.tmr_percent)
        assert metrics.tmr_percent is not None
        # TMR = (2000 - 1000) / 1000 * 100% = 100%
        self.assertAlmostEqual(metrics.tmr_percent, 100.0, places=1)

        # RA product = Rp * Area = 1000 * (pi * 0.1^2)
        expected_area = math.pi * 0.01
        self.assertIsNotNone(metrics.ra_product)
        assert metrics.ra_product is not None
        self.assertAlmostEqual(metrics.ra_product, 1000.0 * expected_area, places=2)

        # Hc and H_dipolar
        self.assertIsNotNone(metrics.hc)
        self.assertIsNotNone(metrics.h_dipolar)
        assert metrics.hc is not None and metrics.h_dipolar is not None
        self.assertAlmostEqual(metrics.hc, 50.0, delta=5.0)
        self.assertAlmostEqual(metrics.h_dipolar, 0.0, delta=5.0)

    def test_calculate_mtj_metrics_empty_or_flat(self) -> None:
        metrics_empty = calculate_mtj_metrics(np.array([]), np.array([]))
        self.assertIsNone(metrics_empty.rp)
        self.assertIsNone(metrics_empty.rap)
        self.assertIsNone(metrics_empty.tmr_percent)

        # Flat line
        flat_x = np.linspace(0, 10, 20)
        flat_y = np.ones(20) * 50.0
        metrics_flat = calculate_mtj_metrics(flat_x, flat_y, y_name="R")
        self.assertIsNotNone(metrics_flat.rp)
        self.assertAlmostEqual(metrics_flat.rp or 0.0, 50.0)
        self.assertIsNone(metrics_flat.tmr_percent)  # No significant difference between min and max


if __name__ == "__main__":
    unittest.main()
