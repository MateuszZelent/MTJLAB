import json
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event

import h5py
import pytest

from app.devices.lakeshore_475.models import (
    FieldUnit,
    GaussmeterReading,
    GaussmeterSnapshot,
    MeasurementMode,
)
from app.devices.moke_box.calibration import CalibrationContext, CalibrationRequest
from app.devices.moke_box.calibration_runner import MokeCalibrationRunner
from app.domain.errors import ConfigurationError, DeviceError, RunInterrupted
from app.domain.models import DeviceIdentity
from app.storage.moke_calibration_store import MokeCalibrationRepository, MokeCalibrationRunStore
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_moke_voltage_control import controlled_adapter, plan_for


class SyntheticReference:
    def __init__(self, magnet):
        self.magnet = magnet
        self.identity = DeviceIdentity("SIM::LAKESHORE", "LSCI,MODEL475,SIM475,1")
        self.snapshot = GaussmeterSnapshot(
            "1", MeasurementMode.DC, "2", FieldUnit.TESLA, "0", True, "40", datetime.now(UTC),
            dc_resolution_code="3",
        )
        self.reads = 0
        self.fail_at = None
        self.change_unit_at = None

    def read_snapshot(self):
        return self.snapshot

    def read_measurement(self):
        self.reads += 1
        if self.reads == self.fail_at:
            raise DeviceError("injected reference failure")
        snapshot = self.snapshot
        if self.reads == self.change_unit_at:
            snapshot = replace(snapshot, unit_code="1", unit=FieldUnit.GAUSS)
        return GaussmeterReading.now(
            mode=MeasurementMode.DC, unit=snapshot.unit, snapshot=snapshot,
            field_t=self.magnet.field_t() + (self.reads % 3 - 1) * 1e-6,
        )


def calibration_fixture():
    adapter, transport, profile = controlled_adapter()
    reference = SyntheticReference(transport._context.magnet)
    context = CalibrationContext(
        profile.fingerprint, profile.binding_id, profile.channel, True,
        reference.identity.idn, "SIM-probe", "+z", "fixed simulated gap", 1e-5,
    )
    request = CalibrationRequest(
        plan_for(profile, (-0.5, 0.0, 0.5)), context, samples_per_point=2,
        settling_s=0, repetitions=2,
    )
    return adapter, reference, request


def test_complete_calibration_preserves_two_branches_raw_data_and_pythat(tmp_path):
    adapter, reference, request = calibration_fixture()
    result = MokeCalibrationRunner(adapter, reference, tmp_path).run(request, armed=True)
    assert result.dac_zero_confirmed
    assert len(result.points) == 12
    assert {point.direction for point in result.points} == {"ascending", "descending"}
    assert result.model.ascending.estimate(0) > result.model.descending.estimate(0)
    assert adapter.read_vouts()[2] == 0
    with h5py.File(result.path) as file:
        assert file["run"].attrs["status"] == "completed"
        assert len(file["points"]) == 12
        point = json.loads(file["points/0/metadata_json"].asstr()[()])["calibration_point"]
        assert len(point["reference_samples_t"]) == 2
        assert len(point["hall_raw_codes"]) == 2
        assert point["reference_snapshots"][0]["unit"] == "tesla"
        names = list(file["events/name"].asstr()[:])
        timing = json.loads(file["events/message"].asstr()[names.index("calibration_armed")])
        assert timing["effective_settling_s"] == 2
        assert timing["control_profile"]["minimum_settling_s"] == 2
        assert timing["hardware_waits_skipped"] is True
    report = ThatecCompatibilityValidator().validate(result.path, require_pythat=True)
    assert report.valid, report.errors
    repository = MokeCalibrationRepository(tmp_path)
    assert repository.load(result.model.calibration_id) == result.model
    assert repository.active(profile_fingerprint=request.context.profile_fingerprint, simulation=True) is None


def test_model_activation_is_explicit_and_cannot_cross_hardware_simulation(tmp_path):
    adapter, reference, request = calibration_fixture()
    result = MokeCalibrationRunner(adapter, reference, tmp_path).run(request, armed=True)
    repository = MokeCalibrationRepository(tmp_path)
    kwargs = dict(profile_fingerprint=request.context.profile_fingerprint, simulation=True)
    with pytest.raises(ConfigurationError, match="Review"):
        repository.activate(result.model.calibration_id, reviewed=False, **kwargs)
    with pytest.raises(ConfigurationError, match="different"):
        repository.activate(result.model.calibration_id, reviewed=True,
                            profile_fingerprint=request.context.profile_fingerprint, simulation=False)
    repository.activate(result.model.calibration_id, reviewed=True, **kwargs)
    assert repository.active(**kwargs) == result.model
    assert repository.active(profile_fingerprint="changed tor", simulation=True) is None
    assert repository.active(profile_fingerprint=request.context.profile_fingerprint, simulation=False) is None
    assert repository.save(result.model) == result.model.calibration_id


def test_estimate_requires_matching_profile_direction_history_and_calibrated_range(tmp_path):
    adapter, reference, request = calibration_fixture()
    model = MokeCalibrationRunner(adapter, reference, tmp_path).run(request, armed=True).model
    kwargs = dict(profile_fingerprint=request.context.profile_fingerprint, simulation=True, conditioned=True)
    assert model.estimate(0, "ascending", **kwargs) > 0
    with pytest.raises(ConfigurationError, match="extrapolation"):
        model.estimate(0.8, "ascending", **kwargs)
    with pytest.raises(ConfigurationError, match="conditioning"):
        model.estimate(0, "ascending", **{**kwargs, "conditioned": False})
    with pytest.raises(ConfigurationError, match="profile"):
        model.estimate(0, "ascending", **{**kwargs, "profile_fingerprint": "another channel"})
    with pytest.raises(ConfigurationError, match="branch"):
        model.estimate(0, "unknown", **kwargs)


@pytest.mark.parametrize("failure", ["reference", "units", "storage", "stop"])
def test_failure_preserves_partial_run_and_attempts_dac_zero(tmp_path, monkeypatch, failure):
    adapter, reference, request = calibration_fixture()
    cancel = Event()
    if failure == "reference":
        reference.fail_at = 3
    if failure == "units":
        reference.change_unit_at = 3
    if failure == "storage":
        original = MokeCalibrationRunStore.append

        def fail_after_one(self, point):
            if point.index == 1:
                raise OSError("injected storage failure")
            return original(self, point)
        monkeypatch.setattr(MokeCalibrationRunStore, "append", fail_after_one)
    progress = (lambda point: cancel.set()) if failure == "stop" else None
    runner = MokeCalibrationRunner(adapter, reference, tmp_path, cancel=cancel, progress=progress)
    with pytest.raises((DeviceError, OSError, RunInterrupted)):
        runner.run(request, armed=True)
    assert adapter.safe_target_confirmed
    assert adapter.read_vouts()[2] == 0
    paths = list((tmp_path / "runs").glob("*.h5"))
    assert len(paths) == 1
    with h5py.File(paths[0]) as file:
        assert file["run"].attrs["status"] == ("aborted" if failure == "stop" else "faulted")
        assert len(file["points"]) == 1
        assert int(file.attrs["measurement running"]) == 0
    assert not MokeCalibrationRepository(tmp_path).list_ids()


def test_unarmed_or_rms_calibration_creates_no_raw_run_and_no_output(tmp_path):
    adapter, reference, request = calibration_fixture()
    runner = MokeCalibrationRunner(adapter, reference, tmp_path)
    with pytest.raises(ConfigurationError, match="arm"):
        runner.run(request)
    reference.snapshot = replace(reference.snapshot, mode=MeasurementMode.RMS, mode_code="2")
    with pytest.raises(ConfigurationError, match="DC"):
        runner.run(request, armed=True)
    assert not (tmp_path / "runs").exists()
    assert adapter.read_vouts()[2] == 0


def test_profile_tampering_and_path_traversal_are_rejected(tmp_path):
    adapter, reference, request = calibration_fixture()
    result = MokeCalibrationRunner(adapter, reference, tmp_path).run(request, armed=True)
    repository = MokeCalibrationRepository(tmp_path)
    path = tmp_path / "profiles" / f"{result.model.calibration_id}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["ascending"]["field_t"][1] = 9
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="hash"):
        repository.load(result.model.calibration_id)
    with pytest.raises(ConfigurationError, match="identity"):
        repository.load("../settings")
