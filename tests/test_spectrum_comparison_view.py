from datetime import UTC, datetime

import numpy as np
import pytest

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.spectrum.comparison_view import compare_power, convert_power_view


def frame(values):
    return SpectrumTrace((1e6, 2e6, 3e6), tuple(values), datetime.now(UTC), "TRAC1")


def test_three_curves_share_frame_and_signed_power_units():
    raw = frame((0, -10, -20))
    state = compare_power(raw, frame_id=7, show_raw=True,
                          background_w=(.002, .0001, .000001), reference=frame((-10, -10, -10)))
    assert len(state.traces) == 3
    assert all(trace.unit == "W" and trace.frame_id == 7 for trace in state.traces)
    assert state.by_key["raw"].values == pytest.approx((.001, .0001, .00001))
    assert state.by_key["background_difference"].values == pytest.approx((-.001, 0, .000009))
    assert state.by_key["reference_difference"].values == pytest.approx((.0009, 0, -.00009))
    assert raw.powers_dbm == (0, -10, -20)
    with pytest.raises(TypeError):
        state.by_key["other"] = state.traces[0]


def test_log_omits_nonpositive_bins_without_modifying_source():
    state = compare_power(frame((0, -10, -20)), frame_id=1, background_w=(.002, .0001, .000001))
    log_state, note = convert_power_view(state, "dBm")
    assert "2 non-positive" in note
    assert np.isnan(log_state.selected.values[:2]).all()
    assert log_state.selected.values[2] == pytest.approx(10 * np.log10(.000009 / .001))
    assert state.selected.values[0] < 0
    restored, _ = convert_power_view(log_state, "W")
    assert np.isnan(restored.selected.values[:2]).all()
    assert restored.selected.values[2] == pytest.approx(.000009)


def test_mismatching_grid_and_bad_background_are_rejected():
    raw = frame((0, 0, 0))
    with pytest.raises(ValueError, match="Background"):
        compare_power(raw, frame_id=1, background_w=(1, 2))
    reference = SpectrumTrace((2e6, 3e6, 4e6), (0, 0, 0), datetime.now(UTC), "TRAC1")
    with pytest.raises(ValueError, match="frequency grid"):
        compare_power(raw, frame_id=1, reference=reference)
