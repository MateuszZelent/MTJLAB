"""Direct access cannot bypass the contiguous checkpoint boundary."""

import h5py
import pytest

from app.domain.errors import ExecutionError
from app.storage.hdf5_reader import Hdf5RunReader


READERS = [Hdf5RunReader.spectrum, Hdf5RunReader.spectrum_point_count,
           Hdf5RunReader.spectrum_acquisition, Hdf5RunReader.spectrum_correction]


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("gap", ["missing", "incomplete"])
def test_direct_spectrum_read_rejects_complete_point_after_gap(tmp_path, reader, gap):
    path = tmp_path / "interrupted.h5"
    with h5py.File(path, "w") as file:
        for index in (0, 2):
            file.create_group(f"points/{index}").attrs["complete"] = True
            spectrum = file.create_group(f"spectra/{index}")
            spectrum.create_dataset("frequency_hz", data=[1., 2.])
            spectrum.create_dataset("power_dbm", data=[-80., -79.])
        # A guard must reject the checkpoint before decoding its correction.
        file.create_group("spectra/2/correction_v1")
        if gap == "incomplete":
            file.create_group("points/1").attrs["complete"] = False
    with pytest.raises(ExecutionError, match="uncommitted checkpoint"):
        reader(path, 2)
    assert Hdf5RunReader.spectrum_point_count(path, 0) == 2
    assert Hdf5RunReader.spectrum(path, 0).powers_dbm == (-80., -79.)


@pytest.mark.parametrize("reader", READERS)
def test_negative_spectrum_index_is_rejected_before_open(tmp_path, reader):
    with pytest.raises(ExecutionError, match="negative"):
        reader(tmp_path / "absent.h5", -1)
