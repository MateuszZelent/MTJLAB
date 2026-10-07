"""Structural corruption returns an invalid report without invoking PyThat."""

import h5py
import pytest

from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests import test_thatec_validator as fixtures


@pytest.mark.parametrize("corruption", [
    "root_string", "root_array", "devices_dataset", "scan_dataset",
    "tree_group", "row_group", "row_numeric", "data_group", "run_status_array",
])
def test_malformed_file_is_reported_and_never_passed_to_pythat(tmp_path, monkeypatch, corruption):
    path = tmp_path / "bad.h5"
    fixtures.ThatecCompatibilityValidatorTests._write_run(path, "completed")
    with h5py.File(path, "r+") as file:
        if corruption.startswith("root_"):
            file.attrs["measurement running"] = "invalid" if corruption == "root_string" else [0, 1]
        elif corruption == "run_status_array":
            file["run"].attrs["status"] = [1, 2]
        else:
            row = next(name for name in file["scan_definition"] if name.startswith("row_"))
            target = {
                "devices_dataset": "devices", "scan_dataset": "scan_definition",
                "tree_group": "scan_definition/tree_view", "row_group": f"scan_definition/{row}",
                "row_numeric": f"scan_definition/{row}", "data_group": f"measurement/{row}/data",
            }[corruption]
            del file[target]
            if corruption.endswith("group"):
                file.create_group(target)
            else:
                file.create_dataset(target, data=[[1, 2]])
    def forbidden(_path):
        pytest.fail("Invalid structure reached PyThat")
    monkeypatch.setattr("app.storage.thatec_validator.inspect_measurement_tree", forbidden)
    report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
    assert not report.valid and report.errors
    with pytest.raises(ValueError, match="Incompatible"):
        report.require_valid()
