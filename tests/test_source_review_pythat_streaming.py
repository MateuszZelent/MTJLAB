"""Final validation converts every spectrum without eager source arrays."""

from datetime import datetime, timezone

import dask.array as da
import h5py
import numpy as np
import pytest
import xarray as xr

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.domain.errors import ExecutionError
from app.storage import Hdf5RunWriter
from app.storage.pythat_bridge import inspect_measurement_tree, open_measurement_tree


def test_close_streams_public_spectra_and_converted_values_match(tmp_path, monkeypatch):
    from PyThat import MeasurementTree

    path = tmp_path / "streaming.h5"
    writer = Hdf5RunWriter(path, recipe_source="schema_version: 1\nname: streaming\n",
        settings_source="schema_version: 1\n", plan_hash="streaming", device_idn={}, expected_points=4)
    expected = []
    for index in range(4):
        values = tuple(-80 + index + point / 100 for point in range(101))
        expected.append(values)
        writer.append(MeasurementPoint(index=index, setpoints={}, measurements={}),
            SpectrumTrace(tuple(1e6 + point * 1e3 for point in range(101)), values,
                          datetime.now(timezone.utc), "TRAC1"))
    original_array = h5py.Dataset.__array__
    original_save = MeasurementTree.save_file_from_string
    converted = []

    def no_eager_spectrum(dataset, *args, **kwargs):
        assert not (dataset.name.startswith("/measurement/") and dataset.name.endswith("/data") and dataset.ndim > 1)
        return original_array(dataset, *args, **kwargs)

    def save_and_check(dataset, destination):
        assert isinstance(dataset["Spectrum"].data, da.Array)
        assert max(dataset["Spectrum"].data.chunks[0]) == 1
        original_save(dataset, destination)
        with xr.open_dataset(destination, engine="h5netcdf") as stored:
            converted.append(stored["Spectrum"].values.copy())

    with monkeypatch.context() as patch:
        patch.setattr(h5py.Dataset, "__array__", no_eager_spectrum)
        patch.setattr(MeasurementTree, "save_file_from_string", staticmethod(save_and_check))
        # Dataset.load was an additional full-array copy in the old finalizer.
        patch.setattr(xr.Dataset, "load", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unexpected eager load")))
        writer.close("completed")
    assert len(converted) == 1
    np.testing.assert_allclose(converted[0], expected)
    dimensions, variables = inspect_measurement_tree(path)
    eager = open_measurement_tree(path)
    assert dict(dimensions) == dict(eager.dataset.sizes)
    assert set(variables) == set(eager.dataset.data_vars)
    np.testing.assert_allclose(eager.dataset["Spectrum"].values, converted[0])
    assert not path.with_suffix(".nc").exists()
    from app.storage.pythat_reader import read_pythat_run_data
    with monkeypatch.context() as patch:
        patch.setattr(xr.Dataset, "load", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("metadata reader loaded samples")))
        summary = read_pythat_run_data(path)
    assert summary.dimensions == dict(dimensions)
    assert summary.variables == tuple(sorted(variables))
    from app.storage.validation_worker import validate_archive_isolated
    report = validate_archive_isolated(path)
    assert report.valid, report.errors
    assert dict(report.dimensions) == dict(dimensions)


@pytest.mark.parametrize("reader", [inspect_measurement_tree, open_measurement_tree])
@pytest.mark.parametrize("damage", ["length", "shape", "nan", "overflow"])
def test_public_import_rejects_corrupt_scale(tmp_path, reader, damage):
    path = tmp_path / "corrupt-scale.h5"
    writer = Hdf5RunWriter(path, recipe_source="schema_version: 1\nname: scale\n",
        settings_source="schema_version: 1\n", plan_hash="scale", device_idn={}, expected_points=1)
    writer.append(MeasurementPoint(index=0, setpoints={}, measurements={}),
        SpectrumTrace((1e6, 2e6, 3e6), (-80., -79., -78.),
                      datetime.now(timezone.utc), "TRAC1"))
    writer.close("completed")
    with h5py.File(path, "r+") as file:
        group = next(group for group in file["measurement"].values()
                     if isinstance(group, h5py.Group) and "data" in group and group["data"].ndim > 1)
        scale = group["scale"][:]
        if damage == "length":
            scale = scale[:-1]
        elif damage == "shape":
            scale = scale.reshape(1, -1)
        elif damage == "nan":
            scale[0] = np.nan
        else:
            scale[:2] = np.finfo(float).max
        del group["scale"]
        group.create_dataset("scale", data=scale)
    with pytest.raises(ExecutionError, match="Public scale"):
        reader(path)
    assert not path.with_suffix(".nc").exists()


@pytest.mark.parametrize("reader", [inspect_measurement_tree, open_measurement_tree])
@pytest.mark.parametrize("damage", ["offset", "increment", "nan"])
def test_later_trace_cannot_silently_inherit_first_axis(tmp_path, reader, damage):
    path = tmp_path / "later-scale.h5"
    writer = Hdf5RunWriter(path, recipe_source="schema_version: 1\nname: scale\n",
        settings_source="schema_version: 1\n", plan_hash="scale", device_idn={}, expected_points=2)
    for index in range(2):
        writer.append(MeasurementPoint(index=index, setpoints={}, measurements={}),
            SpectrumTrace((1e6, 2e6, 3e6), (-80., -79., -78.),
                          datetime.now(timezone.utc), "TRAC1"))
    writer.close("completed")
    with h5py.File(path, "r+") as file:
        group = next(group for group in file["measurement"].values()
                     if isinstance(group, h5py.Group) and "scale" in group)
        position = 5 if damage == "increment" else 4
        group["scale"][position] = np.nan if damage == "nan" else 4e6
    with pytest.raises(ExecutionError, match="Public scale"):
        reader(path)


def test_missing_spectrum_checkpoints_keep_frequency_axis_and_gaps(tmp_path):
    path = tmp_path / "gaps.h5"
    writer = Hdf5RunWriter(path, recipe_source="schema_version: 1\nname: gaps\n",
        settings_source="schema_version: 1\n", plan_hash="gaps", device_idn={}, expected_points=4)
    for index in range(4):
        trace = None if index in (0, 2) else SpectrumTrace(
            (1e6, 2e6, 3e6), (-80., -79., -78.), datetime.now(timezone.utc), "TRAC1")
        writer.append(MeasurementPoint(index=index, setpoints={}, measurements={}), trace)
    writer.close("completed")
    inspect_measurement_tree(path)
    result = open_measurement_tree(path).dataset["Spectrum"]
    np.testing.assert_array_equal(result.coords["Frequency"], (1e6, 2e6, 3e6))
    assert np.isnan(result.values[[0, 2]]).all()
    np.testing.assert_array_equal(result.values[[1, 3]], [(-80., -79., -78.)] * 2)
