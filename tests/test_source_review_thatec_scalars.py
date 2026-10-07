"""Scalar browser preserves complete sample/time pairs and checkpoint scope."""

import h5py
import pytest

from app.domain.errors import ExecutionError
from app.storage.thatec_reader import ThatecRunReader


def write_scalar(path, values, times, *, committed=None):
    with h5py.File(path, "w") as file:
        scan = file.create_group("scan_definition")
        scan.create_dataset("row_01", dtype=h5py.string_dtype("utf-8"), data=[
            ("dimensions", "0"), ("lab control role", "measurement"),
        ])
        row = file.create_group("measurement/row_01")
        row.create_dataset("data", data=values)
        if times is not None:
            row.create_dataset("timestamp", data=times)
        if committed is not None:
            file.create_group("run")
            for index in range(committed + 1):
                file.create_group(f"points/{index}").attrs["complete"] = index < committed


@pytest.mark.parametrize("times", [[10.], [10., 20., 30.], [[10., 20.]]])
def test_external_scalar_rejects_present_but_malformed_timestamps(tmp_path, times):
    path = tmp_path / "external.h5"
    write_scalar(path, [1., 2.], times)
    with pytest.raises(ExecutionError, match="THATEC"):
        ThatecRunReader.scalar_series(path, "row_01")


@pytest.mark.parametrize("values,times", [([1.], [10., 20.]), ([1., 2.], [10.]), ([1., 2.], None)])
def test_missing_committed_scalar_evidence_is_reported(tmp_path, values, times):
    path = tmp_path / "incomplete.h5"
    write_scalar(path, values, times, committed=2)
    with pytest.raises(ExecutionError, match="THATEC"):
        ThatecRunReader.scalar_series(path, "row_01")


@pytest.mark.parametrize("values,times", [([1., 2., 999.], [10., 20.]), ([1., 2.], [10., 20., 999.])])
def test_uncommitted_tail_does_not_hide_valid_pairs(tmp_path, values, times):
    path = tmp_path / "interrupted.h5"
    write_scalar(path, values, times, committed=2)
    result, timestamps = ThatecRunReader.scalar_series(path, "row_01")
    assert tuple(result) == (1., 2.)
    assert tuple(timestamps) == (10., 20.)


def test_external_scalar_without_optional_timestamps_remains_readable(tmp_path):
    path = tmp_path / "no-times.h5"
    write_scalar(path, [1., 2.], None)
    result, timestamps = ThatecRunReader.scalar_series(path, "row_01")
    assert tuple(result) == (1., 2.)
    assert timestamps == ()
