"""Opening an early spectrum must not enumerate/read later HDF5 points."""
from types import SimpleNamespace

import pytest

from app.domain.errors import ExecutionError
from app.storage.hdf5_reader import Hdf5RunReader


class Points:
    def __init__(self, *, gap=None):
        self.read = []
        self.gap = gap

    def get(self, name):
        index = int(name)
        self.read.append(index)
        assert index < 4, "Reader inspected an unrelated later checkpoint"
        return None if index == self.gap else SimpleNamespace(attrs={"complete": True})

    def __iter__(self):
        raise AssertionError("Reader enumerated the entire run")


def test_single_spectrum_validates_only_prefix_through_requested_point():
    points = Points()
    Hdf5RunReader._require_committed_spectrum({"points": points}, 2)
    assert points.read == [0, 1, 2]


@pytest.mark.parametrize("gap", [0, 1, 2])
def test_bounded_read_still_rejects_earlier_commit_gap(gap):
    points = Points(gap=gap)
    with pytest.raises(ExecutionError, match="uncommitted"):
        Hdf5RunReader._require_committed_spectrum({"points": points}, 2)
    assert points.read == list(range(gap + 1))
