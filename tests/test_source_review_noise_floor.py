"""Order-statistic optimization preserves the existing percentile definition."""
import numpy as np
import pytest

from app.spectrum.analysis import _odd_window, rolling_noise_floor_dbm


@pytest.mark.parametrize("size,window", [(5, 101), (100, 63), (100, 65), (101, 101), (1001, 201)])
@pytest.mark.parametrize("kind", ["random", "constant", "duplicates", "monotonic"])
def test_noise_floor_matches_padded_linear_percentile(size, window, kind):
    rng = np.random.default_rng(192)
    values = rng.normal(-80, 7, size)
    if kind == "constant":
        values[:] = -73
    elif kind == "duplicates":
        values = np.round(values)
    elif kind == "monotonic":
        values.sort()
    width = _odd_window(size, window)
    expected = np.percentile(np.lib.stride_tricks.sliding_window_view(
        np.pad(values, width // 2, mode="edge"), width), 30., axis=1)
    np.testing.assert_allclose(rolling_noise_floor_dbm(values, window=window), expected, rtol=0, atol=1e-12)


def test_large_noise_floor_does_not_materialize_window_matrix(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Quadratic percentile window was used")

    monkeypatch.setattr(np.lib.stride_tricks, "sliding_window_view", forbidden)
    values = np.linspace(-90, -50, 10001)
    actual = rolling_noise_floor_dbm(values, window=2001)
    assert actual.shape == values.shape
    assert np.all(np.isfinite(actual))
    assert np.all(np.diff(actual) >= 0)
