"""Faults inside resize/create must preserve the preceding public checkpoint."""
from datetime import datetime, timezone

import h5py
import numpy as np
import pytest

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.storage.hdf5_writer import Hdf5RunWriter


def trace():
    return SpectrumTrace((1., 2., 3.), (-60., -61., -62.), datetime.now(timezone.utc), "TRAC1")


def writer_for(path):
    return Hdf5RunWriter(path, recipe_source="", settings_source="", plan_hash="partial", device_idn={})


@pytest.mark.parametrize("role, dataset", [
    ("axis", "data"), ("axis", "timestamp"),
    ("measurement", "data"), ("measurement", "timestamp"),
    ("spectrum", "data"), ("spectrum", "timestamp"), ("spectrum", "scale"),
    ("spectrum_processed", "data"), ("spectrum_processed", "timestamp"), ("spectrum_processed", "scale"),
])
def test_exception_after_resize_restores_exact_previous_shapes(tmp_path, monkeypatch, role, dataset):
    writer = writer_for(tmp_path / "partial.h5")
    kwargs = dict(trace=trace(), processed_values=(1., 2., 3.), processed_unit="W", processing_operation="background_subtraction")
    writer.append(MeasurementPoint(0, {}, {"keithley.A.current_a": .001}), **kwargs)
    public = writer._thatec
    row = "row_00" if role == "axis" else next(
        name for name in writer._file["scan_definition"] if name.startswith("row_") and
        dict(writer._file[f"scan_definition/{name}"].asstr()[()]).get("lab control role") == role)
    target = f"/measurement/{row}/{dataset}"
    before = {}
    for name, group in writer._file["measurement"].items():
        if isinstance(group, h5py.Group):
            for key in ("data", "timestamp", "scale"):
                if key in group:
                    before[f"measurement/{name}/{key}"] = group[key][:]
    fired = False
    original = h5py.Dataset.resize

    def fail_after_resize(data, *args, **kw):
        nonlocal fired
        result = original(data, *args, **kw)
        if data.name == target and not fired:
            fired = True
            raise OSError("partial resize failure")
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(h5py.Dataset, "resize", fail_after_resize)
            with pytest.raises(ExecutionError, match="partial resize failure"):
                writer.append(MeasurementPoint(1, {}, {"keithley.A.current_a": .002}), **kwargs)
        assert fired
        assert writer.point_count == public._checkpoint_count == 1
        for name, values in before.items():
            np.testing.assert_array_equal(writer._file[name][:], values)
        writer.append(MeasurementPoint(1, {}, {"keithley.A.current_a": .003}), **kwargs)
    finally:
        writer.close("faulted")


@pytest.mark.parametrize("role", ["measurement", "spectrum", "spectrum_processed"])
def test_exception_during_new_row_creation_removes_partial_row(tmp_path, monkeypatch, role):
    writer = writer_for(tmp_path / "create.h5")
    writer.append(MeasurementPoint(0, {}, {"keithley.A.current_a": .001}),
                  trace=trace() if role == "spectrum_processed" else None)
    initial_rows = set(writer._file["scan_definition"])
    next_row = writer._thatec._next_row
    name = f"row_{next_row:02d}"
    original = h5py.Group.create_dataset
    fired = False

    def fail_after_create(group, child, *args, **kwargs):
        nonlocal fired
        result = original(group, child, *args, **kwargs)
        if group.name == f"/measurement/{name}" and child == "timestamp" and not fired:
            fired = True
            raise OSError("partial create failure")
        return result

    measurements = {"keithley.A.current_a": .002}
    if role == "measurement":
        measurements["keithley.A.voltage_v"] = .1
    kwargs = dict(trace=trace() if role != "measurement" else None)
    if role == "spectrum_processed":
        kwargs.update(processed_values=(1., 2., 3.), processed_unit="W", processing_operation="background_subtraction")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(h5py.Group, "create_dataset", fail_after_create)
            with pytest.raises(ExecutionError, match="partial create failure"):
                writer.append(MeasurementPoint(1, {}, measurements), **kwargs)
        assert fired
        assert set(writer._file["scan_definition"]) == initial_rows
        assert name not in writer._file["measurement"]
        assert writer._thatec._next_row == next_row
        writer.append(MeasurementPoint(1, {}, measurements), **kwargs)
    finally:
        writer.close("faulted")


def test_mutated_grid_is_revalidated_instead_of_trusting_object_id(tmp_path):
    writer = writer_for(tmp_path / "grid.h5")
    grid = [1., 2., 3.]
    spectrum = SpectrumTrace(grid, (-60., -61., -62.), datetime.now(timezone.utc), "TRAC1")
    try:
        writer._thatec._validate_uniform_grid(spectrum)
        grid[1] = 1.1
        with pytest.raises(ValueError, match="uniform frequency grid"):
            writer._thatec._validate_uniform_grid(spectrum)
        writer.append(MeasurementPoint(0, {}, {"keithley.A.current_a": .001}))
    finally:
        writer.close("faulted")


def test_changed_spectrum_size_fails_without_touching_public_checkpoint(tmp_path):
    writer = writer_for(tmp_path / "size.h5")
    writer.append(MeasurementPoint(0, {}, {}), trace=trace())
    public = writer._thatec
    next_row, count = public._next_row, public._checkpoint_count
    changed = SpectrumTrace((1., 2.), (-60., -61.), datetime.now(timezone.utc), "TRAC1")
    try:
        with pytest.raises(ExecutionError, match="point count changed"):
            writer.append(MeasurementPoint(1, {}, {}), trace=changed)
        assert public._next_row == next_row and public._checkpoint_count == count
        assert writer._file[f"measurement/{public._spectrum_row}/data"].shape == (1, 3)
        writer.append(MeasurementPoint(1, {}, {}), trace=trace())
    finally:
        writer.close("faulted")
