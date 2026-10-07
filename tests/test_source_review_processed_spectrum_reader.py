"""Corrupted private processed spectra cannot masquerade as usable data."""
import h5py
import numpy as np
import pytest

from app.domain.errors import ExecutionError
from app.storage.hdf5_reader import Hdf5RunReader


@pytest.mark.parametrize("damage", ["rank", "count", "complex", "text", "nan", "inf", "group",
                                   "unit", "operation", "none"])
def test_rejects_processed_corruption_before_decimation(tmp_path, damage):
    path = tmp_path / "processed.h5"
    values = np.array([-1., 0., 1.])
    if damage == "rank":
        values = values.reshape(3, 1)
    elif damage == "count":
        values = values[:2]
    elif damage == "complex":
        values = values + 1j
    elif damage == "text":
        values = np.array([b"1", b"2", b"3"])
    elif damage in {"nan", "inf"}:
        values[1] = float(damage)
    with h5py.File(path, "w") as file:
        file.create_group("points/0").attrs["complete"] = True
        group = file.create_group("spectra/0")
        group.create_dataset("frequency_hz", data=[1., 2., 3.])
        group.create_dataset("power_dbm", data=[-80., -79., -78.])
        if damage == "group":
            group.create_group("processed_values")
        else:
            group.create_dataset("processed_values", data=values)
        if damage != "unit":
            group.attrs["processed_unit"] = "W"
        if damage != "operation":
            group.attrs["processing_operation"] = "none" if damage == "none" else "subtract_power_signed"
    with pytest.raises(ExecutionError):
        Hdf5RunReader.spectrum(path, 0, max_points=2)


def test_signed_power_residual_is_preserved(tmp_path):
    path = tmp_path / "signed.h5"
    with h5py.File(path, "w") as file:
        file.create_group("points/0").attrs["complete"] = True
        group = file.create_group("spectra/0")
        group.create_dataset("frequency_hz", data=[1., 2., 3.])
        group.create_dataset("power_dbm", data=[-80., -79., -78.])
        group.create_dataset("processed_values", data=[-1e-12, 0., 2e-12])
        group.attrs["processed_unit"] = "W"
        group.attrs["processing_operation"] = "subtract_power_signed"
    result = Hdf5RunReader.spectrum(path, 0)
    assert result.processed_values == (-1e-12, 0., 2e-12)
    assert result.processed_unit == "W"
    assert result.processing_operation == "subtract_power_signed"


@pytest.mark.parametrize("budget", [1, 2, 3, 6, 7, 32, 33])
def test_shared_preview_axis_preserves_both_curves_within_budget(tmp_path, budget):
    path = tmp_path / "peaks.h5"
    frequencies = np.arange(1., 1002.)
    raw = np.full(1001, -80.)
    processed = np.zeros(1001)
    raw[123], raw[456] = -20., -100.
    processed[321], processed[654] = 1e-12, -2e-12
    with h5py.File(path, "w") as file:
        file.create_group("points/0").attrs["complete"] = True
        group = file.create_group("spectra/0")
        group.create_dataset("frequency_hz", data=frequencies)
        group.create_dataset("power_dbm", data=raw)
        group.create_dataset("processed_values", data=processed)
        group.attrs["processed_unit"] = "W"
        group.attrs["processing_operation"] = "subtract_power_signed"
    result = Hdf5RunReader.spectrum(path, 0, max_points=budget)
    assert len(result.frequencies_hz) <= budget
    assert result.source_point_count == 1001
    indices = np.asarray(result.frequencies_hz, dtype=int) - 1
    np.testing.assert_array_equal(result.powers_dbm, raw[indices])
    np.testing.assert_array_equal(result.processed_values, processed[indices])
    if budget >= 6:
        assert {123, 456, 321, 654} <= set(indices)
