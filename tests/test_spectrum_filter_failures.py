"""Filter failures stay local while invalid acquired data remains an error."""

import numpy as np
import pytest

from app.spectrum import SpectrumAnalysisParameters, clean_spectrum_pipeline


def test_unavailable_filter_preserves_raw_when_it_is_the_only_selection():
    values = tuple(-80 + np.random.default_rng(4).normal(0, .1, 501))
    result = clean_spectrum_pipeline(
        values, unit="dBm", modes=("narrow_reject",),
        frequencies_hz=tuple(np.linspace(200e6, 201e6, 501)),
    )
    assert result.values == values
    assert not result.modified_bin_indices and not result.removed_peak_indices
    assert result.method == "Raw (no processing)"
    assert "unavailable" in result.notes[0]


def test_invalid_stationary_history_does_not_erase_successful_narrow_filter():
    x = np.linspace(200e6, 1200e6, 2001)
    values = np.full(x.size, -80.)
    values[500] = -60.
    history = np.full((5, x.size), -80.)
    history[0, 0] = np.nan
    result = clean_spectrum_pipeline(
        values, unit="dBm", modes=("narrow_reject", "emi_reject", "denoise"),
        frequencies_hz=x, history=history,
        parameters=SpectrumAnalysisParameters(),
    )
    assert result.values[500] == pytest.approx(-80.)
    assert 500 in result.removed_peak_indices
    assert "Narrow-peak" in result.method and "denoise" in result.method
    assert any("Stationary-line rejection unavailable" in note for note in result.notes)
    assert values[500] == -60.


def test_invalid_source_is_rejected_before_any_filter_can_be_skipped():
    with pytest.raises(ValueError, match="finite"):
        clean_spectrum_pipeline(
            (-80., float("nan"), -80., -80., -80.),
            unit="dBm", modes=("narrow_reject", "denoise"),
            frequencies_hz=(1., 2., 3., 4., 5.),
        )


def test_unknown_filter_is_rejected_explicitly():
    with pytest.raises(ValueError, match="Unsupported spectrum filters"):
        clean_spectrum_pipeline((-80.,) * 5, unit="dBm", modes=("unknown",))
