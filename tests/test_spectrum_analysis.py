from __future__ import annotations

import math
import unittest

import numpy as np

from app.spectrum import (
    bilateral_denoise_dbm,
    clean_spectrum_dbm,
    detect_spectrum_peaks,
    detect_stationary_interference,
)


class SpectrumAnalysisTests(unittest.TestCase):
    @staticmethod
    def _gaussian_trace(*, noise_seed: int | None = None) -> tuple[np.ndarray, np.ndarray]:
        frequencies_hz = np.linspace(995e6, 1005e6, 2001)
        baseline_mw = 10.0 ** (-100.0 / 10.0)
        peak_mw = 10.0 ** (-30.0 / 10.0)
        fwhm_hz = 400e3
        shape = np.exp(
            -4.0
            * math.log(2.0)
            * ((frequencies_hz - 1e9) / fwhm_hz) ** 2
        )
        values_dbm = 10.0 * np.log10(baseline_mw + peak_mw * shape)
        if noise_seed is not None:
            values_dbm += np.random.default_rng(noise_seed).normal(
                0.0, 0.6, values_dbm.size
            )
        return frequencies_hz, values_dbm

    def test_peak_detector_measures_frequency_width_q_and_selects_gaussian_fit(self) -> None:
        frequencies_hz, values_dbm = self._gaussian_trace(noise_seed=7)
        peaks = detect_spectrum_peaks(
            frequencies_hz,
            values_dbm,
            min_snr_db=12.0,
            min_prominence_db=6.0,
            max_peaks=4,
        )

        self.assertGreaterEqual(len(peaks), 1)
        peak = peaks[0]
        self.assertAlmostEqual(peak.frequency_hz, 1e9, delta=15e3)
        self.assertIsNotNone(peak.fwhm_hz)
        self.assertAlmostEqual(peak.fwhm_hz, 400e3, delta=80e3)
        self.assertEqual(peak.fit_model, "Gaussian")
        self.assertIsNotNone(peak.fit_rmse_db)
        self.assertGreater(peak.snr_db, 20.0)
        self.assertAlmostEqual(peak.q_factor, 2500.0, delta=600.0)

    def test_bilateral_denoise_reduces_floor_noise_without_clipping_narrow_peak(self) -> None:
        _frequencies_hz, noisy = self._gaussian_trace(noise_seed=17)
        clean = np.asarray(bilateral_denoise_dbm(noisy))
        peak_index = int(np.argmax(noisy))
        floor = np.r_[0:700, 1300:2001]

        self.assertLess(float(np.std(np.diff(clean[floor]))), float(np.std(np.diff(noisy[floor]))))
        self.assertAlmostEqual(float(clean[peak_index]), float(noisy[peak_index]), delta=0.8)

    def test_stationary_line_detection_requires_temporal_stability(self) -> None:
        rng = np.random.default_rng(22)
        history = rng.normal(-100.0, 0.2, (12, 101))
        history[:, 25] = -70.0
        history[:, 75] = np.linspace(-90.0, -50.0, history.shape[0])

        candidates = detect_stationary_interference(history)

        self.assertIn(25, candidates)
        self.assertNotIn(75, candidates)

    def test_cleanup_modes_preserve_raw_and_label_derived_emi_result(self) -> None:
        raw = tuple(-100.0 for _ in range(101))
        history = [list(raw) for _ in range(6)]
        for frame in history:
            frame[50] = -70.0
        current = list(raw)
        current[50] = -70.0

        untouched = clean_spectrum_dbm(current, mode="raw", history_dbm=history)
        rejected = clean_spectrum_dbm(
            current, mode="emi_reject", history_dbm=history
        )

        self.assertEqual(untouched.values_dbm, tuple(current))
        self.assertEqual(untouched.method, "Raw (no processing)")
        self.assertIn(50, rejected.stationary_interference_indices)
        self.assertAlmostEqual(rejected.values_dbm[50], -100.0)
        self.assertIn("display only", rejected.method)

    def test_analysis_rejects_nonfinite_and_nonmonotonic_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            detect_spectrum_peaks((1.0, 2.0, 3.0, 4.0, 5.0), (-10.0, -11.0, math.nan, -12.0, -13.0))
        with self.assertRaisesRegex(ValueError, "monotonic"):
            detect_spectrum_peaks((1.0, 3.0, 2.0, 4.0, 5.0), (-10.0, -11.0, -9.0, -12.0, -13.0))


if __name__ == "__main__":
    unittest.main()
