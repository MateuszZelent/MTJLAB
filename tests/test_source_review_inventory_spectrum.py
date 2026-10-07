"""Spectrum-only checkpoints remain visible without accepting corrupt axes."""
import math

import h5py
import numpy as np
import pytest

from app.storage.hdf5_series_reader import Hdf5SeriesReader


def write_archive(path, frequencies, powers, *, complete=True):
    with h5py.File(path, "w") as file:
        point = file.create_group("points/0")
        point.attrs["complete"] = complete
        point.create_dataset("measurements_json", data="{}")
        spectrum = file.create_group("spectra/0")
        spectrum.create_dataset("frequency_hz", data=frequencies)
        spectrum.create_dataset("power_dbm", data=powers)


def test_spectrum_only_checkpoint_is_a_real_frequency_power_curve(tmp_path):
    path = tmp_path / "reference.h5"
    write_archive(path, [1e6, 2e6, 3e6], [-80., -40., -80.])
    series = Hdf5SeriesReader.read_series(path)
    assert series.curve_kind == "spectrum"
    assert (series.x_unit, series.y_unit) == ("Hz", "dBm")
    assert series.x_values == (1e6, 2e6, 3e6)
    assert series.y_values == (-80., -40., -80.)
    # An explicit scalar request must not silently become a different quantity.
    requested = Hdf5SeriesReader.read_series(path, preferred_y_channel="voltage_v")
    assert requested.curve_kind == "scalar" and requested.y_unit == "V"
    assert len(requested.y_values) == 1 and math.isnan(requested.y_values[0])


@pytest.mark.parametrize("damage", ["reversed", "duplicate", "rank", "complex", "single", "uncommitted"])
def test_inventory_never_displays_invalid_spectrum_as_valid_curve(tmp_path, damage):
    frequencies = np.array([1., 2., 3.])
    powers = np.array([-80., -40., -80.])
    if damage == "reversed":
        frequencies = frequencies[::-1]
    elif damage == "duplicate":
        frequencies[1] = frequencies[0]
    elif damage == "rank":
        frequencies = frequencies.reshape(3, 1)
    elif damage == "complex":
        powers = powers.astype(complex)
    elif damage == "single":
        frequencies, powers = frequencies[:1], powers[:1]
    path = tmp_path / "invalid.h5"
    write_archive(path, frequencies, powers, complete=damage != "uncommitted")
    assert Hdf5SeriesReader.read_series(path).is_empty
