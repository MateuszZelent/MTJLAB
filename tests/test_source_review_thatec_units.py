"""Public spectrum browsing must preserve case-sensitive SI prefixes."""

import h5py
import numpy as np
import pytest

from app.storage.thatec_reader import ThatecRunReader
from app.domain.errors import ExecutionError


@pytest.mark.parametrize("unit,factor,expected_unit", [
    ("mHz", 1e-3, "Hz"), ("MHz", 1e6, "Hz"), ("kHz", 1e3, "Hz"),
    ("Hz", 1., "Hz"), ("GHz", 1e9, "Hz"), ("s", 1., "s"),
])
def test_public_spectrum_axis_unit_conversion(tmp_path, unit, factor, expected_unit):
    path = tmp_path / "public.h5"
    _write_public(path, unit)
    result = ThatecRunReader.spectrum_slice(path, "row_01", 0)
    assert result.x_unit == expected_unit
    np.testing.assert_allclose(result.x_values, np.array([2., 2.5, 3.]) * factor)
    assert result.traces[0].values == (10., 20., 30.)
    assert result.y_unit == "dBm"


def _write_public(path, unit="Hz"):
    with h5py.File(path, "w") as file:
        text = h5py.string_dtype("utf-8")
        scan = file.create_group("scan_definition")
        scan.create_dataset("row_01", dtype=text, data=[
            ("control name", "Signal"), ("dimensions", "1"), ("function", "indicator"),
        ])
        row = file.create_group("measurement/row_01")
        row.create_dataset("data", data=[[10., 20., 30.]])
        row.create_dataset("timestamp", data=[1.])
        row.create_dataset("scale", data=[2., .5, 0., 1.])
        row.create_dataset("metadata", dtype=text, data=[
            ("name", "Frequency"), ("unit", unit),
            ("name", "Power"), ("unit", "dBm"),
        ])


@pytest.mark.parametrize("damage", ["short", "rank", "nan", "overflow", "metadata_text", "metadata_inf"])
def test_corrupt_public_axis_is_not_replaced_by_default_coordinates(tmp_path, damage):
    path = tmp_path / "damaged.h5"
    _write_public(path)
    with h5py.File(path, "r+") as file:
        row = file["measurement/row_01"]
        if damage.startswith("metadata"):
            del row["scale"]
            del row["metadata"]
            row.create_dataset("metadata", dtype=h5py.string_dtype("utf-8"), data=[
                ("name", "Frequency"), ("unit", "Hz"),
                ("multiplier", "invalid" if damage == "metadata_text" else "inf"),
            ])
        else:
            scale = row["scale"][:]
            del row["scale"]
            if damage == "short":
                scale = scale[:1]
            elif damage == "rank":
                scale = scale.reshape(1, -1)
            elif damage == "nan":
                scale[0] = np.nan
            else:
                scale[:2] = np.finfo(float).max
            row.create_dataset("scale", data=scale)
    with pytest.raises(ExecutionError, match="THATEC"):
        ThatecRunReader.spectrum_slice(path, "row_01", 0)


def test_absent_optional_scale_uses_valid_metadata(tmp_path):
    path = tmp_path / "metadata-axis.h5"
    _write_public(path)
    with h5py.File(path, "r+") as file:
        row = file["measurement/row_01"]
        del row["scale"]
        del row["metadata"]
        row.create_dataset("metadata", dtype=h5py.string_dtype("utf-8"), data=[
            ("name", "Frequency"), ("unit", "kHz"), ("offset", "2"),
            ("multiplier", "0.5"), ("name", "Power"), ("unit", "dBm"),
        ])
    result = ThatecRunReader.spectrum_slice(path, "row_01", 0)
    assert result.x_values == (2000., 2500., 3000.)
    assert result.traces[0].values == (10., 20., 30.)
