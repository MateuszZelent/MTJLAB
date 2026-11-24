"""Malformed frames cannot corrupt a previously valid power average."""

import math

import pytest

from app.spectrum.processing import LinearPowerAverager
from app.spectrum.streaming_statistics import RollingPowerAverage


@pytest.mark.parametrize(
    "invalid",
    [
        [[-10.0, -20.0]],
        [[-10.0], [-20.0]],
        [-10.0],
        [math.nan, -20.0],
        [math.inf, -20.0],
        [4000.0, -20.0],
        [-4000.0, -20.0],
    ],
)
def test_rejected_frame_preserves_average_and_allows_next_frame(invalid):
    average = LinearPowerAverager()
    average.add([-10.0, -20.0])
    before = average.result()
    with pytest.raises(ValueError):
        average.add(invalid)
    assert average.count == 1
    assert average.result() == before
    average.add([0.0, -20.0])
    assert average.count == 2
    assert average.result() == pytest.approx([10 * math.log10(0.55), -20.0])


def test_accumulator_overflow_is_atomic():
    average = LinearPowerAverager()
    average.add([3080.0, 0.0])
    with pytest.raises(ValueError, match="Accumulated"):
        average.add([3080.0, 0.0])
    assert average.count == 1
    assert average.result() == pytest.approx([3080.0, 0.0])
    average.add([0.0, 0.0])
    assert average.count == 2
    assert average.result() == pytest.approx([3080 - 10 * math.log10(2), 0.0])


def test_representable_small_power_is_not_clamped():
    average = LinearPowerAverager()
    average.add([-3100.0, -3100.0])
    average.add([-3100.0, -3100.0])
    assert average.result() == pytest.approx([-3100.0, -3100.0])


def test_rolling_eviction_recovers_quiet_power_after_a_loud_read():
    rolling = RollingPowerAverage(frames=2, seconds=None)
    rolling.add([10., 10.], 0.)
    rolling.add([-180., -95.], 1.)
    result = rolling.add([-180., -95.], 2.)
    assert rolling.count == 2
    assert result == pytest.approx([-180., -95.])


def test_rolling_reports_capacity_instead_of_silently_shortening_window(monkeypatch):
    monkeypatch.setattr(RollingPowerAverage, "MEMORY_LIMIT_BYTES", 48)
    rolling = RollingPowerAverage(frames=0, seconds=30.)
    rolling.add([-90., -90.], 0.)
    rolling.add([-60., -60.], 1.)
    with pytest.raises(ValueError, match="Choose a shorter duration"):
        rolling.add([-50., -50.], 2.)
    assert rolling.count == 2
    result = rolling.add([-70., -70.], 31.)
    assert result == pytest.approx([-70., -70.])
