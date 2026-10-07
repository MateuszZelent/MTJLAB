"""Raw and reference readers reject corrupted scientific arrays alike."""

import h5py
import numpy as np
import pytest

from app.domain.errors import ExecutionError
from app.storage.hdf5_reader import Hdf5RunReader


@pytest.mark.parametrize("reader", ["spectrum", "reference", "references"])
@pytest.mark.parametrize("damage", ["rank", "mismatch", "nan", "infinity", "reversed", "duplicate", "complex", "missing"])
def test_corrupt_axes_cannot_be_read_as_spectrum(tmp_path, reader, damage):
    path = tmp_path / "axes.h5"
    frequency, power = np.array([1., 2., 3.]), np.array([-80., -79., -78.])
    if damage == "rank":
        frequency = frequency.reshape(1, -1)
    elif damage == "mismatch":
        power = power[:2]
    elif damage == "nan":
        frequency[1] = np.nan
    elif damage == "infinity":
        power[1] = np.inf
    elif damage == "reversed":
        frequency = frequency[::-1]
    elif damage == "duplicate":
        frequency[1] = frequency[0]
    elif damage == "complex":
        power = power + 1j
    with h5py.File(path, "w") as file:
        file.create_group("points/0").attrs["complete"] = True
        group = file.create_group("spectra/0" if reader == "spectrum" else "references/0")
        group.create_dataset("frequency_hz", data=frequency)
        if damage != "missing":
            group.create_dataset("power_dbm", data=power)
    method = getattr(Hdf5RunReader, reader)
    with pytest.raises(ExecutionError):
        method(path) if reader == "references" else method(path, 0)
