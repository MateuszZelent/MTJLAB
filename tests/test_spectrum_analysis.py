from __future__ import annotations

import math
import unittest

import numpy as np

from app.spectrum import (
    SpectrumAnalysisParameters,
    bilateral_denoise_dbm,
    clean_spectrum_dbm,
    clean_spectrum_values,
    detect_spectrum_peaks,
    detect_stationary_interference,
    rolling_noise_floor_dbm,
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

    def test_clean_spectrum_values_with_custom_parameters(self) -> None:
        _frequencies, powers = self._gaussian_trace(noise_seed=7)
        params_default = SpectrumAnalysisParameters(denoise_window=9)
        params_wide = SpectrumAnalysisParameters(denoise_window=25)

        res_default = clean_spectrum_values(powers, unit="dBm", mode="denoise", parameters=params_default)
        res_wide = clean_spectrum_values(powers, unit="dBm", mode="denoise", parameters=params_wide)

        self.assertEqual(len(res_default.values), len(powers))
        self.assertEqual(len(res_wide.values), len(powers))
        # Wider window causes different smoothing behavior
        self.assertNotEqual(res_default.values, res_wide.values)

    def test_detect_stationary_interference_with_custom_parameters(self) -> None:
        _frequencies = tuple(np.linspace(1e9, 1.01e9, 101))
        current = np.full(101, -100.0)
        current[50] = -30.0  # Stable carrier 70 dB above noise
        history = [current.copy() for _ in range(4)]  # Only 4 history frames

        # With default emi_min_frames=5, 4 history frames are insufficient
        params_strict = SpectrumAnalysisParameters(emi_min_frames=5)
        res_strict = clean_spectrum_values(
            tuple(current), unit="dBm", mode="emi_reject", history_dbm=history, parameters=params_strict
        )
        self.assertEqual(res_strict.stationary_interference_indices, ())

        # With emi_min_frames=3, 4 frames are sufficient to detect the stable line
        params_permissive = SpectrumAnalysisParameters(emi_min_frames=3)
        res_permissive = clean_spectrum_values(
            tuple(current), unit="dBm", mode="emi_reject", history_dbm=history, parameters=params_permissive
        )
        self.assertIn(50, res_permissive.stationary_interference_indices)

    def test_detect_spectrum_peaks_with_custom_parameters(self) -> None:
        frequencies, powers = self._gaussian_trace()
        # With default parameters, Gaussian peak is detected
        default_peaks = detect_spectrum_peaks(frequencies, powers)
        self.assertEqual(len(default_peaks), 1)

        # With very high min SNR, peak is rejected
        high_snr_params = SpectrumAnalysisParameters(peak_min_snr_db=120.0)
        no_peaks = detect_spectrum_peaks(frequencies, powers, parameters=high_snr_params)
        self.assertEqual(len(no_peaks), 0)

        # Max peaks constraint
        single_peak_params = SpectrumAnalysisParameters(peak_max_count=1)
        limited_peaks = detect_spectrum_peaks(frequencies, powers, parameters=single_peak_params)
        self.assertLessEqual(len(limited_peaks), 1)

    def test_rolling_noise_floor_matches_np_percentile_for_exact_and_fractional_ranks(self) -> None:
        rng = np.random.default_rng(123)
        values = -90.0 + rng.normal(0.0, 2.5, 401)
        for window in (51, 201, 203):
            radius = window // 2
            frames = np.lib.stride_tricks.sliding_window_view(
                np.pad(values, radius, mode="edge"), window
            )
            expected = np.percentile(frames, 30.0, axis=1)
            actual = rolling_noise_floor_dbm(values, window=window)
            np.testing.assert_allclose(actual, expected, rtol=0.0, atol=1e-13)

    def test_auto_clean_in_linear_watts_preserves_narrow_peak(self) -> None:
        rng = np.random.default_rng(99)
        values_w = rng.normal(0.0, 1e-14, 201)
        values_w[100] = 1e-11  # 1000x noise scale in watts, far smaller than 0.35 dBm
        cleaned = clean_spectrum_values(values_w, unit="W", mode="auto_clean")
        self.assertEqual(cleaned.unit, "W")
        # Edge-preserving bilateral denoise in linear W must keep the peak height intact.
        self.assertGreater(cleaned.values[100], 0.9 * values_w[100])

    def test_detect_spectrum_peaks_remaps_index_for_descending_frequency_grid(self) -> None:
        frequencies_hz, values_dbm = self._gaussian_trace(noise_seed=7)
        desc_freqs = frequencies_hz[::-1]
        desc_vals = values_dbm[::-1]
        for fit in (False, True):
            asc_peaks = detect_spectrum_peaks(frequencies_hz, values_dbm, fit=fit, max_peaks=3)
            desc_peaks = detect_spectrum_peaks(desc_freqs, desc_vals, fit=fit, max_peaks=3)
            self.assertEqual(len(asc_peaks), len(desc_peaks))
            self.assertGreaterEqual(len(desc_peaks), 1)
            expected_index = len(frequencies_hz) - 1 - asc_peaks[0].index
            self.assertEqual(desc_peaks[0].index, expected_index)
            self.assertAlmostEqual(desc_freqs[desc_peaks[0].index], frequencies_hz[asc_peaks[0].index], places=6)
            self.assertAlmostEqual(desc_vals[desc_peaks[0].index], desc_peaks[0].amplitude_dbm, places=12)
            self.assertAlmostEqual(desc_peaks[0].frequency_hz, asc_peaks[0].frequency_hz, delta=1.0)
            self.assertEqual(desc_peaks[0].fit_model, asc_peaks[0].fit_model)

    def test_peak_fit_handles_extreme_dbm_without_overflow(self) -> None:
        frequencies_hz, base_dbm = self._gaussian_trace(noise_seed=7)
        for offset in (4000.0, -4000.0):
            shifted = base_dbm + offset
            peaks = detect_spectrum_peaks(frequencies_hz, shifted, fit=True, max_peaks=2)
            self.assertGreaterEqual(len(peaks), 1)
            self.assertEqual(peaks[0].fit_model, "Gaussian")
            self.assertIsNotNone(peaks[0].fit_rmse_db)
            self.assertTrue(math.isfinite(peaks[0].fit_rmse_db))

    def test_analysis_parameters_validate_all_dsp_fields(self) -> None:
        invalid_kwargs = [
            {"denoise_window": 2},
            {"denoise_window": True},
            {"emi_threshold_db": 0.0},
            {"emi_threshold_db": float("nan")},
            {"emi_max_std_db": -0.1},
            {"emi_min_frames": 1},
            {"emi_min_frames": False},
            {"peak_min_snr_db": -1.0},
            {"peak_min_snr_db": float("inf")},
            {"peak_min_prominence_db": -0.5},
            {"peak_max_count": 0},
            {"peak_max_count": True},
            {"narrow_max_width_hz": 0.0},
            {"narrow_threshold_sigma": 2.5},
            {"narrow_threshold_sigma": 31.0},
        ]
        for kwargs in invalid_kwargs:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SpectrumAnalysisParameters(**kwargs)


if __name__ == "__main__":
    unittest.main()
