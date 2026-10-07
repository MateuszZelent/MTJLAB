"""Reject a run chosen as a reference before importing its checkpoint JSON."""
import h5py
import pytest

from app.domain.errors import ExecutionError
from app.storage import Hdf5RunReader, ReferenceHdf5Store


@pytest.mark.parametrize("count", [0, 2, 500])
def test_non_single_reference_checks_only_prefix_and_never_reads_metadata(tmp_path, monkeypatch, count):
    path = tmp_path / "run.h5"
    with h5py.File(path, "w") as file:
        for index in range(count):
            group = file.create_group(f"points/{index}")
            group.attrs["complete"] = True
    def forbidden(*args):
        raise AssertionError("Wrong-cardinality reference must not read checkpoint JSON")
    monkeypatch.setattr(Hdf5RunReader, "_dataset_json", forbidden)
    original = Hdf5RunReader._committed_point_names
    inspected = []
    def prefix(file, *, stop_after=None):
        inspected.append(stop_after)
        return original(file, stop_after=stop_after)
    monkeypatch.setattr(Hdf5RunReader, "_committed_point_names", prefix)
    with pytest.raises(ExecutionError, match="exactly one checkpoint"):
        ReferenceHdf5Store.load(path)
    assert inspected == [2]


def test_single_point_keeps_commit_boundary_and_full_provenance(tmp_path):
    path = tmp_path / "point.h5"
    with h5py.File(path, "w") as file:
        point = file.create_group("points/0")
        point.attrs["complete"] = True
        point.attrs["status"] = "ok"
        point.create_dataset("metadata_json", data='{"reference_schema":"example"}')
        point.create_dataset("device_states_json", data='{"anritsu":{"detector":"RMS"}}')
        point.create_dataset("setpoints_json", data='{"current_a":0.001}')
        file.create_group("spectra/0")
        file.create_group("points/1").attrs["complete"] = False
        file.create_group("points/2").attrs["complete"] = True
    point = Hdf5RunReader.single_point(path)
    assert point == Hdf5RunReader.points(path)[0]
    assert point.metadata == {"reference_schema": "example"}
    assert point.device_states == {"anritsu": {"detector": "RMS"}}
    assert point.setpoints == {"current_a": .001}
    assert point.has_spectrum
