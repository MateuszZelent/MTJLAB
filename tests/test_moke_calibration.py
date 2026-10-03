import json
from contextlib import nullcontext
import hashlib
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
from tests.test_moke_voltage_control import mutations


class SyntheticReference:
    def __init__(self, magnet):
        self.magnet = magnet
        self.identity = DeviceIdentity("SIM::LAKESHORE", "LSCI,MODEL475,SIM475,1")
        self.snapshot = GaussmeterSnapshot(
            "1", MeasurementMode.DC, "2", FieldUnit.TESLA, "0", True, "40", datetime.now(UTC),
            dc_resolution_code="3",
            operation_status_code=0,
        )
        self.reads = 0
        self.fail_at = None
        self.change_unit_at = None

    def io_timeout(self, timeout_s):
        return nullcontext()

    def read_snapshot(self, **kwargs):
        return self.snapshot

    def read_measurement(self, **kwargs):
        self.reads += 1
        if self.reads == self.fail_at:
            raise DeviceError("injected reference failure")
        snapshot = self.snapshot
        if self.reads == self.change_unit_at:
            snapshot = replace(snapshot, unit_code="1", unit=FieldUnit.GAUSS)
        return GaussmeterReading.now(
            mode=MeasurementMode.DC, unit=snapshot.unit, snapshot=snapshot,
            field_t=self.magnet.field_t() + (self.reads % 3 - 1) * 1e-6,
            operation_event_code=4,
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
        reference.fail_at = 4
    if failure == "units":
        reference.change_unit_at = 4
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


@pytest.mark.parametrize("unit", [FieldUnit.OERSTED, FieldUnit.AMPERE_PER_METER])
def test_incompatible_reference_units_are_rejected_before_any_dac_mutation(tmp_path, unit):
    adapter, reference, request = calibration_fixture()
    reference.snapshot = replace(reference.snapshot, unit=unit)
    with pytest.raises(ConfigurationError, match="G or T"):
        MokeCalibrationRunner(adapter, reference, tmp_path).run(request, armed=True)
    assert not mutations(adapter._transport)
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("failure", ["read", "no_probe", "no_fresh", "timeout"])
def test_reference_preflight_failure_never_changes_dac(tmp_path, failure, monkeypatch):
    adapter, reference, request = calibration_fixture()
    if failure == "read":
        reference.fail_at = 1
    elif failure == "no_probe":
        reference.snapshot = replace(reference.snapshot, operation_status_code=1)
    else:
        original = reference.read_measurement

        def invalid_reading(**kwargs):
            if failure == "timeout":
                raise TimeoutError("injected preflight deadline")
            return replace(original(**kwargs), operation_event_code=0)

        monkeypatch.setattr(reference, "read_measurement", invalid_reading)
    with pytest.raises((DeviceError, ValueError, TimeoutError)):
        MokeCalibrationRunner(adapter, reference, tmp_path).run(request, armed=True)
    assert not mutations(adapter._transport)
    assert not (tmp_path / "runs").exists()


def test_reference_acquisition_deadline_faults_raw_run_and_confirms_zero(tmp_path, monkeypatch):
    adapter, reference, request = calibration_fixture()
    original = reference.read_measurement
    calls = []

    def deadline_failure(**kwargs):
        calls.append(kwargs)
        if len(calls) == 4:  # preflight + one complete recorded point
            raise TimeoutError("injected reference acquisition deadline")
        return original(**kwargs)

    monkeypatch.setattr(reference, "read_measurement", deadline_failure)
    with pytest.raises(TimeoutError):
        MokeCalibrationRunner(adapter, reference, tmp_path).run(request, armed=True)
    assert all(call["require_fresh"] is True and call["deadline_s"] > 0 for call in calls)
    assert adapter.safe_target_confirmed
    assert not MokeCalibrationRepository(tmp_path).list_ids()
    with h5py.File(next((tmp_path / "runs").glob("*.h5"))) as raw:
        assert raw["run"].attrs["status"] == "faulted"
        assert len(raw["points"]) == 1


@pytest.mark.parametrize("damage", ["missing", "hash", "incomplete", "context", "zero"])
def test_damaged_raw_data_prevents_activation_and_restart_loading(tmp_path, damage):
    adapter, reference, request = calibration_fixture()
    result = MokeCalibrationRunner(adapter, reference, tmp_path).run(request, armed=True)
    repository = MokeCalibrationRepository(tmp_path)
    kwargs = dict(profile_fingerprint=request.context.profile_fingerprint, simulation=True)
    repository.activate(result.model.calibration_id, reviewed=True, **kwargs)
    model = result.model
    if damage == "missing":
        result.path.unlink()
    elif damage == "hash":
        with result.path.open("ab") as stream:
            stream.write(b"corrupted")
    else:
        with h5py.File(result.path, "r+") as raw:
            if damage == "incomplete":
                raw["run"].attrs["status"] = "faulted"
            elif damage == "context":
                document = json.loads(raw["run/settings_yaml"].asstr()[()])
                document["context"]["probe_id"] = "different probe"
                raw["run/settings_yaml"][()] = json.dumps(document)
            else:
                names = list(raw["events/name"].asstr()[:])
                index = names.index("calibration_finished")
                document = json.loads(raw["events/message"].asstr()[index])
                document["dac_zero_confirmed"] = False
                raw["events/message"][index] = json.dumps(document)
        model = replace(model, raw_sha256=hashlib.sha256(result.path.read_bytes()).hexdigest())
        repository.save(model)
    with pytest.raises(ConfigurationError):
        repository.activate(model.calibration_id, reviewed=True, **kwargs)
    with pytest.raises(ConfigurationError):
        MokeCalibrationRepository(tmp_path).active(**kwargs)


def test_new_repository_after_restart_restores_verified_active_model(tmp_path):
    adapter, reference, request = calibration_fixture()
    result = MokeCalibrationRunner(adapter, reference, tmp_path).run(request, armed=True)
    kwargs = dict(profile_fingerprint=request.context.profile_fingerprint, simulation=True)
    MokeCalibrationRepository(tmp_path).activate(result.model.calibration_id, reviewed=True, **kwargs)
    assert MokeCalibrationRepository(tmp_path).active(**kwargs) == result.model


def test_gui_worker_does_not_zero_an_existing_voltage_when_calibration_preflight_fails(tmp_path):
    from app.devices.moke_box.ui.field_control import MokeFieldWorker

    adapter, reference, request = calibration_fixture()
    plan = plan_for(adapter.get_control_profile(), (.2,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    adapter.ramp_vout(2, .2)
    adapter._transport.sent.clear()
    reference.snapshot = replace(reference.snapshot, unit=FieldUnit.OERSTED)
    for lease in (adapter, reference):
        lease.interruption_event = Event()
        lease.release = lambda: None
    worker = MokeFieldWorker("calibration", request,
                             {"moke_box": adapter, "lakeshore_gaussmeter": reference}, tmp_path, Event())
    failures = []
    worker.failed.connect(failures.append)
    worker.run()
    assert failures and "G or T" in failures[0]
    assert not mutations(adapter._transport)
    assert adapter.read_vouts()[2] == pytest.approx(.2, abs=.001)
