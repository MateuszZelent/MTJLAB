import numpy as np
import pytest

from app.spectrum.analysis import SpectrumAnalysisParameters, clean_spectrum_values
from app.spectrum.narrow_spikes import filter_narrow_spikes


def trace():
    x = np.linspace(200e6, 1200e6, 2001)
    rng = np.random.default_rng(710)
    background = .05 * rng.normal(size=x.size) + .1 * (x - x[0]) / np.ptp(x)
    return x, background


def gaussian(x, center, width, amplitude):
    return amplitude * np.exp(-4 * np.log(2) * ((x - center) / width)**2)


@pytest.mark.parametrize("amplitude", [2, 5, -2, -8])
def test_fluctuating_both_sign_spikes_removed_without_changing_other_bins(amplitude):
    x, background = trace()
    y = background + gaussian(x, 500e6, 3e6, amplitude)
    original = y.copy()
    result = filter_narrow_spikes(x, y)
    center = int(np.argmin(abs(x - 500e6)))
    assert center in result.peak_indices
    assert abs(result.values[center] - background[center]) < .2
    untouched = np.ones(x.size, dtype=bool)
    untouched[list(result.modified_indices)] = False
    np.testing.assert_array_equal(np.asarray(result.values)[untouched], y[untouched])
    np.testing.assert_array_equal(y, original)


@pytest.mark.parametrize("sign", [-1, 1])
def test_broad_resonance_unchanged_and_narrow_spike_on_top_removed(sign):
    x, noise = trace()
    broad = gaussian(x, 650e6, 90e6, sign * 3)
    y = noise + broad + gaussian(x, 650e6, 3e6, 6)
    result = filter_narrow_spikes(x, y)
    center = int(np.argmin(abs(x - 650e6)))
    assert center in result.peak_indices
    assert abs(result.values[center] - (noise + broad)[center]) < .25
    shoulders = (abs(x - 650e6) > 12e6) & (abs(x - 650e6) < 100e6)
    np.testing.assert_array_equal(np.asarray(result.values)[shoulders], y[shoulders])
    without_spike = filter_narrow_spikes(x, noise + broad)
    assert not without_spike.modified_indices


def test_protected_spike_and_touching_tails_are_preserved_as_whole_features():
    x, background = trace()
    y = background + gaussian(x, 500e6, 3e6, 5) + gaussian(x, 800e6, 3e6, -4)
    result = filter_narrow_spikes(x, y, protected_regions_hz=((501e6, 530e6),))
    assert 600 not in result.peak_indices  # 500 MHz, tails cross protection.
    assert 1200 in result.peak_indices
    region = (x >= 480e6) & (x <= 540e6)
    np.testing.assert_array_equal(np.asarray(result.values)[region], y[region])


def test_width_gate_preserves_significant_peak_above_cutoff():
    x, background = trace()
    y = background + gaussian(x, 500e6, 12e6, 5)
    result = filter_narrow_spikes(x, y, maximum_width_hz=6e6)
    assert not result.modified_indices


def test_wide_shoulders_prevent_replacing_narrow_core():
    x, noise = trace()
    y = noise + gaussian(x, 600e6, 3e6, 3) + gaussian(x, 600e6, 30e6, 3)
    result = filter_narrow_spikes(x, y)
    assert not result.modified_indices


def test_edge_spikes_are_not_extrapolated():
    x, noise = trace()
    y = noise + gaussian(x, x[1], 3e6, 6)
    result = filter_narrow_spikes(x, y)
    assert not result.modified_indices


def test_scale_equivariance_for_signed_watts_and_descending_grid():
    x, background = trace()
    y = background + gaussian(x, 500e6, 3e6, -5)
    regular = filter_narrow_spikes(x, y)
    watts = filter_narrow_spikes(x[::-1], y[::-1] * 1e-12)
    np.testing.assert_allclose(watts.values, np.asarray(regular.values)[::-1] * 1e-12,
                               rtol=1e-10, atol=1e-26)
    assert watts.modified_indices == tuple(sorted(x.size - 1 - i for i in regular.modified_indices))
    assert watts.noise_scale == pytest.approx(regular.noise_scale * 1e-12)


def test_noise_only_not_erased_or_flattened():
    x, y = trace()
    result = filter_narrow_spikes(x, y)
    assert not result.modified_indices
    np.testing.assert_array_equal(result.values, y)


def test_cleanup_requires_frequency_and_keeps_input_units():
    x, background = trace()
    y = background + gaussian(x, 500e6, 3e6, -5)
    result = clean_spectrum_values(y, frequencies_hz=x, unit="dB", mode="narrow_reject",
                                  parameters=SpectrumAnalysisParameters())
    assert result.unit == "dB" and result.removed_peak_indices
    assert not result.stationary_interference_indices
    with pytest.raises(ValueError, match="frequency axis"):
        clean_spectrum_values(y, unit="dB", mode="narrow_reject")


@pytest.mark.parametrize("kwargs", [dict(maximum_width_hz=0), dict(maximum_width_hz=float("nan")),
    dict(threshold_sigma=2), dict(protected_regions_hz=((5e8, 4e8),)),
    dict(maximum_width_hz=100e6)])
def test_invalid_settings_and_memory_budget_fail_explicitly(kwargs):
    x, y = trace()
    with pytest.raises(ValueError):
        filter_narrow_spikes(x, y, **kwargs)


def test_nonuniform_and_nonfinite_inputs_rejected():
    x, y = trace()
    irregular = x.copy()
    irregular[10] += 100
    with pytest.raises(ValueError, match="uniform"):
        filter_narrow_spikes(irregular, y)
    y[10] = np.nan
    with pytest.raises(ValueError, match="finite"):
        filter_narrow_spikes(x, y)


def test_crowded_candidate_count_and_replacement_fraction_leave_data_unchanged():
    x = np.arange(12001, dtype=float) * 1e6
    y = np.zeros(x.size)
    y[5::10] = 5
    result = filter_narrow_spikes(x, y)
    assert not result.modified_indices and "Too many" in result.notes[0]
    np.testing.assert_array_equal(result.values, y)
    x, y = trace()
    for center in np.arange(250e6, 1150e6, 20e6):
        y += gaussian(x, center, 3e6, 4)
    result = filter_narrow_spikes(x, y)
    assert not result.modified_indices and "10%" in result.notes[0]
    np.testing.assert_array_equal(result.values, y)


def test_unresolved_width_and_short_trace_are_explicit_noops():
    x, y = trace()
    for xx, yy, cutoff in ((x, y, 1.), (x[:10], y[:10], 6e6)):
        result = filter_narrow_spikes(xx, yy, maximum_width_hz=cutoff)
        assert result.notes and not result.modified_indices
        np.testing.assert_array_equal(result.values, yy)
