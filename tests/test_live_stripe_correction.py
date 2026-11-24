import numpy as np
import pytest

from app.spectrum.streaming_statistics import RollingPowerAverage, dbm_to_w
from app.spectrum.stripe_correction import (
    StripeBands,
    build_automatic_stripe_template,
    build_stripe_offsets,
    parse_frequency_bands,
    reconstruct_stripe_bands,
)


def test_stripe_template_uses_local_background_and_protects_bins():
    frequencies = np.arange(790, 831, dtype=float) * 1e6
    bands = parse_frequency_bands("800 MHz .. 805 MHz;\n 804 MHz .. 810 MHz")
    protected = parse_frequency_bands("803 MHz .. 804 MHz\n813 MHz .. 813.5 MHz")
    config = StripeBands(bands, protected, shoulder_hz=5e6)
    baseline = 2e-9 + np.arange(frequencies.size) * 1e-11
    background = baseline.copy()
    background[11] += 6e-10
    background[15] -= 4e-10
    background[[13, 14, 23]] += 1e-7
    original = background.copy()

    offsets = build_stripe_offsets(frequencies, background, config)

    np.testing.assert_allclose(offsets[[11, 15]], [6e-10, -4e-10], atol=1e-24)
    np.testing.assert_array_equal(offsets[np.r_[0:10, 13:15, 21:41]], 0.0)
    np.testing.assert_array_equal(background, original)


@pytest.mark.parametrize("text", ["800 .. 805 MHz", "800 mV .. 805 MHz", "805 MHz .. 800 MHz"])
def test_stripe_bands_require_ordered_frequencies_with_explicit_units(text):
    with pytest.raises(ValueError):
        parse_frequency_bands(text)


def test_stripe_template_reports_unsupported_grid():
    frequencies = np.arange(800, 821, dtype=float) * 1e6
    background = np.full(frequencies.size, 2e-9)
    with pytest.raises(ValueError, match="three"):
        build_stripe_offsets(frequencies, background, StripeBands(((801e6, 803e6),)))
    with pytest.raises(ValueError, match="No stripe bins"):
        build_stripe_offsets(frequencies, background, StripeBands(((900e6, 902e6),)))


def test_reconstruction_brackets_known_stripes_and_excludes_protected_samples():
    frequencies = np.arange(790, 811, dtype=float) * 1e6
    bands = ((790e6, 791e6), (795e6, 801e6))
    protected = ((797.5e6, 798.5e6),)
    broad_db = np.linspace(.2, .7, frequencies.size)
    input_db = broad_db.copy()
    input_db[:2] -= 3
    input_db[5:12] -= 3
    input_db[8] += 7
    input_db[16] = np.nan
    original = input_db.copy()

    result = reconstruct_stripe_bands(frequencies, input_db, bands, protected)

    estimated = [5, 6, 7, 9, 10, 11]
    np.testing.assert_allclose(result.values[estimated], broad_db[estimated], atol=1e-12)
    other = np.setdiff1d(np.arange(frequencies.size), estimated)
    np.testing.assert_array_equal(result.values[other], original[other])
    np.testing.assert_array_equal(input_db, original)
    assert result.estimated_bands_hz == ((794.5e6, 797.5e6), (798.5e6, 801.5e6))
    assert (result.estimated_count, result.skipped_count) == (6, 2)


def test_automatic_stripes_use_only_stable_narrow_excess_in_the_background():
    frequencies = np.arange(600, 1001, dtype=float) * 1e6
    baseline = np.full(frequencies.size, 2e-9)
    background = baseline.copy()
    background[96:99] += [0.9e-9, 3e-9, 0.9e-9]
    background[135] -= 1.4e-9   # A dip would add power after subtraction.
    background[165] += 4e-9     # A line with large temporal scatter.
    background[199:202] += [1e-9, 4e-9, 1e-9]  # Protected.
    background[240:270] += 4e-9  # Broad feature.
    variance = np.full(frequencies.size, (0.04e-9) ** 2)
    variance[165] = (3e-9) ** 2
    original = background.copy()

    template = build_automatic_stripe_template(
        frequencies, background, variance, rbw_hz=1e6, protected_hz=((798e6, 802e6),),
    )

    assert len(template.bands_hz) == 1
    low, high = template.bands_hz[0]
    assert low <= frequencies[97] <= high
    assert any(low <= frequencies[165] <= high for low, high in template.reconstruction_bands_hz)
    assert not any(low <= frequencies[200] <= high for low, high in template.reconstruction_bands_hz)
    signal = baseline.copy()
    signal[97] += 0.15e-9  # A genuine additional line in the same resolution cell.
    signal[104] += 0.08e-9
    interference = np.zeros_like(signal)
    interference[96:99] = [0.9e-9, 3e-9, 0.9e-9]
    live = signal + interference
    removed, skipped = template.offsets_for(live)
    assert not skipped
    np.testing.assert_allclose(live - removed, signal, atol=1e-24, rtol=0)
    np.testing.assert_array_equal(template.offsets_w[135:], 0)
    np.testing.assert_array_equal(background, original)


def test_automatic_stripes_skip_a_mismatched_band_without_reducing_a_neighbour():
    frequencies = np.arange(700, 1001, dtype=float) * 1e6
    background = np.full(frequencies.size, 2e-9)
    background[50] += 10e-9
    background[200] += 5e-9
    template = build_automatic_stripe_template(frequencies, background, np.zeros_like(background), rbw_hz=1e6)
    signal = np.full(frequencies.size, 2e-9)
    signal[50] += 0.7e-9
    signal[51] += 0.4e-9
    live = signal.copy()
    live[200] += 5e-9

    removed, skipped = template.offsets_for(live)

    assert skipped == (template.bands_hz[0],)
    assert removed[50] == removed[51] == 0
    assert removed[200] == pytest.approx(5e-9, abs=1e-24)
    np.testing.assert_allclose(live - removed, signal, atol=1e-24, rtol=0)


def _resolved_background_stripe():
    frequencies = 600e6 + np.arange(401) * .5e6
    floor = np.full(frequencies.size, 5e-12)
    background = floor + 3e-9 * np.exp(-.5 * ((frequencies - 660e6) / 1.8e6) ** 2)
    template = build_automatic_stripe_template(
        frequencies, background, np.zeros_like(background), rbw_hz=3e6,
    )
    assert len(template.bands_hz) == 1
    return floor, template


@pytest.mark.parametrize("signal_gain", [.25, 2.0])
def test_relative_stripes_balance_the_change_in_a_shared_carrier(signal_gain):
    floor, template = _resolved_background_stripe()
    observed = floor + signal_gain * template.offsets_w
    reference = floor + template.offsets_w
    peak = int(np.argmax(template.offsets_w))
    observed[peak] += floor[peak] * .6

    result = template.balance_reference(observed, reference)

    assert not result.unresolved_bands_hz
    common = floor + min(signal_gain, 1) * template.offsets_w
    expected_signal = common.copy()
    expected_signal[peak] += floor[peak] * .6
    np.testing.assert_allclose(result.signal_w, expected_signal, atol=1e-21)
    np.testing.assert_allclose(result.reference_w, common, atol=1e-21)
    assert observed[peak] > floor[peak] + signal_gain * template.offsets_w[peak]


def test_relative_stripes_do_not_create_a_peak_when_both_traces_contain_the_line():
    floor, template = _resolved_background_stripe()
    reference = floor + template.offsets_w
    observed = reference.copy()
    peak = int(np.argmax(template.offsets_w))
    observed[peak + 1] -= 2 * floor[peak]
    assert template.offsets_for(observed)[1] == template.bands_hz
    assert not template.offsets_for(reference)[1]

    result = template.balance_reference(observed, reference)

    relative_db = 10 * np.log10(result.signal_w / result.reference_w)
    assert np.max(np.abs(relative_db)) < .05
    assert relative_db[peak + 1] < 0


def test_relative_stripes_leave_a_mismatched_line_for_inspection():
    floor, template = _resolved_background_stripe()
    reference = floor + template.offsets_w
    observed = floor + 2 * np.roll(template.offsets_w, 4)

    result = template.balance_reference(observed, reference)

    assert result.unresolved_bands_hz == template.bands_hz
    np.testing.assert_array_equal(result.signal_w, observed)
    np.testing.assert_array_equal(result.reference_w, reference)


def test_rolling_reference_follows_changes_in_background_line_amplitude():
    floor, template = _resolved_background_stripe()
    reference_w = floor + template.offsets_w
    stripe_bins = template.offsets_w > 0
    quiet = 10
    assert not stripe_bins[quiet]
    signal_floor = floor.copy()
    signal_floor[quiet] *= 10 ** (.2 / 10)
    rolling = RollingPowerAverage(frames=1, seconds=30)
    for index, gain in enumerate((.2, 1.5, 4.0, .5, .25, 3.0, 5.0)):
        measured_w = signal_floor + gain * template.offsets_w
        mean_dbm = rolling.add(10 * np.log10(measured_w) + 30, index * 10.)
        pair = template.balance_reference(dbm_to_w(mean_dbm), reference_w)
        difference_db = 10 * np.log10(pair.signal_w / pair.reference_w)
        assert not pair.unresolved_bands_hz
        np.testing.assert_allclose(difference_db[stripe_bins], 0, atol=1e-10)
        assert difference_db[quiet] == pytest.approx(.2, abs=1e-10)


def test_automatic_stripe_detection_returns_empty_template_for_flat_background():
    frequencies = np.arange(800, 901, dtype=float) * 1e6
    background = np.full(frequencies.size, 2e-9)
    template = build_automatic_stripe_template(frequencies, background, np.zeros_like(background), rbw_hz=1e6)
    assert template.bands_hz == ()
    assert not template.offsets_w.any()
