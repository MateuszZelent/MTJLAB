"""Reference selection must not materialize all stored spectrum arrays."""
import h5py
import numpy as np
import pytest

from app.domain.errors import ExecutionError
from app.storage.hdf5_reader import Hdf5RunReader, StoredReferenceSummary


def test_catalogue_reads_metadata_only_and_full_read_still_validates(tmp_path, monkeypatch):
    path = tmp_path / "references.h5"
    with h5py.File(path, "w") as file:
        for index in range(3):
            group = file.create_group(f"references/{index}")
            group.attrs["purpose"] = "background" if index == 1 else "reference"
            group.create_dataset("frequency_hz", data=np.arange(1., 10002.))
            values = np.full(10001, -80.)
            if index == 2:
                values[5000] = np.nan
            group.create_dataset("power_dbm", data=values)
    original = h5py.Dataset.__getitem__

    def read(dataset, selection):
        assert not dataset.name.endswith(("frequency_hz", "power_dbm")), "Catalogue read spectrum samples"
        return original(dataset, selection)

    with monkeypatch.context() as patch:
        patch.setattr(h5py.Dataset, "__getitem__", read)
        catalogue = Hdf5RunReader.references(path, metadata_only=True)
    assert len(catalogue) == 3
    assert all(isinstance(entry, StoredReferenceSummary) for entry in catalogue)
    assert catalogue[1].source_point_count == 10001
    assert catalogue[1].purpose == "background"
    assert not hasattr(catalogue[0], "powers_dbm")
    assert len(Hdf5RunReader.reference(path, 0).powers_dbm) == 10001
    with pytest.raises(ExecutionError):
        Hdf5RunReader.reference(path, 2)
