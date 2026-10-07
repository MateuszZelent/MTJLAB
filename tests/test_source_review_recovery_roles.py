"""Requested/applied/readback obey the same checkpoint boundary as measurements."""
import h5py
import numpy as np

from app.domain.models import MeasurementPoint
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.thatec_reader import ThatecRunReader
from app.storage.thatec_schema_mapper import SETPOINT_EVIDENCE_ROLES
from app.storage.thatec_validator import ThatecCompatibilityValidator


def point(index, value):
    return MeasurementPoint(index, {"keithley.A.current": value}, {}, metadata={
        "setpoint_evidence_v1": {"keithley.A.current": {
            "requested_si": value, "applied_si": value + 1e-6,
            "readback_si": value + 2e-6,
        }}
    })


def rows(file):
    result = {}
    for name in file["scan_definition"]:
        if name.startswith("row_"):
            pairs = dict(file[f"scan_definition/{name}"].asstr()[()])
            role = pairs.get("lab control role")
            if role in SETPOINT_EVIDENCE_ROLES:
                assert role not in result, "duplicate public evidence row"
                result[role] = name
    return result


def test_resume_truncates_and_reuses_all_evidence_rows(tmp_path):
    path = tmp_path / "evidence.h5"
    provenance = dict(recipe_source="", settings_source="", plan_hash="roles")
    writer = Hdf5RunWriter(path, device_idn={}, **provenance)
    writer.append(point(0, .001))
    writer.append(point(1, .002))
    writer.close("faulted")
    with h5py.File(path) as file:
        original = rows(file)
        assert set(original) == set(SETPOINT_EVIDENCE_ROLES)
        for name in original.values():
            assert ThatecRunReader._committed_row_limit(file, name, committed_count=1) == 1
    writer = Hdf5RunWriter.resume(path, checkpoint_count=1, **provenance)
    try:
        assert rows(writer._file) == original
        for role, name in original.items():
            assert writer._thatec._scalar_rows[(role, "keithley.A.current")] == name
            assert writer._file[f"measurement/{name}/data"].shape == (1,)
            assert writer._file[f"measurement/{name}/timestamp"].shape == (1,)
        writer.append(point(1, .003))
    finally:
        writer.close("completed")
    with h5py.File(path) as file:
        assert rows(file) == original
        for offset, role in enumerate(SETPOINT_EVIDENCE_ROLES):
            np.testing.assert_allclose(file[f"measurement/{original[role]}/data"][:],
                                       [.001 + offset * 1e-6, .003 + offset * 1e-6])
    result = ThatecCompatibilityValidator().validate(path, require_pythat=True)
    assert result.valid, result.errors
