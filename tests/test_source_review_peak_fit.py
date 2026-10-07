"""Batched fit retains the same model search and physical result."""

import numpy as np
import pytest

from app.spectrum.analysis import _fit_peak_shape, detect_spectrum_peaks
from tests.peak_fit_reference import reference_fit


@pytest.mark.parametrize("model", ["Gaussian", "Lorentzian"])
@pytest.mark.parametrize("width", [2., 9., 28.])
@pytest.mark.parametrize("noise", [0., .3])
def test_batched_fit_matches_original_lstsq_search(model, width, noise):
    x = np.arange(201, dtype=float) * 1e3 + 1e9
    normalized = (np.arange(201) - 100.2) / width
    shape = np.exp(-4 * np.log(2) * normalized**2) if model == "Gaussian" else 1 / (1 + 4 * normalized**2)
    y = 10 * np.log10(1e-9 + 1e-4 * shape)
    y += np.random.default_rng(27).normal(0, noise, y.size)
    expected = reference_fit(x, y, 100, width * 1e3)
    actual = _fit_peak_shape(x, y, 100, width * 1e3)
    assert actual[0] == expected[0]
    assert actual[1:] == pytest.approx(expected[1:], rel=1e-9, abs=1e-9)


def test_large_input_has_bounded_fit_matrices(monkeypatch):
    original = np.linalg.pinv
    shapes = []
    def checked(matrix, **kwargs):
        shapes.append(matrix.shape)
        assert matrix.shape[0] == 420 and matrix.shape[1] <= 101 and matrix.shape[2] == 2
        return original(matrix, **kwargs)
    monkeypatch.setattr(np.linalg, "pinv", checked)
    x = np.arange(10001, dtype=float)
    y = 10 * np.log10(1e-9 + 1e-4 * np.exp(-4 * np.log(2) * ((x - 5000) / 10)**2))
    _fit_peak_shape(x, y, 5000, 10)
    assert len(shapes) == 2


def test_result_limit_stops_fitting_lower_ranked_candidates(monkeypatch):
    from app.spectrum import analysis
    x = np.arange(4001, dtype=float)
    power = np.full(x.size, 1e-10)
    for ordinal, center in enumerate(range(100, 3901, 100)):
        power += 10**((-25 - ordinal * .3) / 10) * np.exp(-4 * np.log(2) * ((x-center)/8)**2)
    y = 10 * np.log10(power)
    calls = []
    def fit(frequencies, _values, index, width):
        calls.append(index)
        return "Gaussian", frequencies[index], width, .01
    monkeypatch.setattr(analysis, "_fit_peak_shape", fit)
    peaks = detect_spectrum_peaks(x, y, max_peaks=3)
    assert len(peaks) == len(calls) == 3
    assert [p.index for p in peaks] == calls


def test_equal_snr_and_prominence_preserve_rmse_tie_break(monkeypatch):
    from app.spectrum import analysis
    x = np.arange(201, dtype=float)
    detection = np.zeros(x.size)
    detection[[20, 60, 100]] = 20
    monkeypatch.setattr(analysis, "_gaussian_detection_trace", lambda _: detection)
    monkeypatch.setattr(analysis, "rolling_noise_floor_dbm", lambda values, **_: np.zeros(len(values)))
    calls = []
    def fit(frequencies, _values, index, width):
        calls.append(index)
        return "Gaussian", frequencies[index], width, {20: 3., 60: 1., 100: 2.}[index]
    monkeypatch.setattr(analysis, "_fit_peak_shape", fit)
    peaks = detect_spectrum_peaks(x, np.full(x.size, 20.), max_peaks=1)
    assert calls == [20, 60, 100]
    assert [peak.index for peak in peaks] == [60]
