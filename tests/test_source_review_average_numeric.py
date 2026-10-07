"""Malformed frames cannot corrupt a previously valid power average."""

import math

import pytest

from app.spectrum.processing import LinearPowerAverager


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
