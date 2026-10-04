"""Model identity, reference dependency and preservation of overlapping signals."""

from dataclasses import replace
from datetime import datetime, timezone

import h5py
import numpy as np
import pytest

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.domain.spectrum_correction import (
    BackgroundProfile, SpectrumAcquisitionContext, SpectrumFrameEnvelope,
    SpectrumFrameRole, SweepEvidence,
)
from app.domain.spectrum_interference import SpectrumInterferenceCalibration
from app.spectrum.interference_model import calibrated_interference_model
from app.storage.hdf5_reader import Hdf5RunReader
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.thatec_validator import ThatecCompatibilityValidator


def fixture():
    x = np.linspace(-10, 10, 1001)
    context = SpectrumAcquisitionContext(np.linspace(1e6, 2e6, x.size), "synthetic-model-calibration")
    baseline = np.full(x.size, 2e-9)
    profile = BackgroundProfile("reference", context.context_id, baseline, np.zeros(x.size),
                                None, 100, 1, 2, "synthetic")
    line = np.exp(-x**2 / 2) * 1e-9
    calibration = SpectrumInterferenceCalibration(
        "line-model", context, baseline, np.column_stack((line, -x * line)),
        np.ones(x.size, dtype=bool), np.abs(x) < 0.5, np.full(x.size, 1e-12),
        ((-0.5, 2), (-0.25, 0.25)), ((profile.profile_id, profile.content_hash),),
        signal_control_regions_qualified=True, qualification_evidence="synthetic injection; not laboratory evidence",
    )
    return x, profile, calibration


def writer(path):
    return Hdf5RunWriter(path, recipe_source="schema_version: 1\nname: Model\nsteps: []\n",
                         settings_source="synthetic: true\n", plan_hash="test", device_idn={},
                         simulation_metadata={"enabled": True, "mode": "simulation"})


def save(path):
    _x, profile, calibration = fixture()
    run = writer(path)
    try:
        run.store_background_profile(calibration.context, profile)
        run.store_interference_calibration(calibration)
    finally:
        run.close("aborted")
    return calibration


def test_committed_model_round_trip_preserves_protected_signal_and_identity(tmp_path):
    path = tmp_path / "model.h5"
    original = save(path)
    (restored,) = Hdf5RunReader.interference_calibrations(path)
    assert restored.content_hash == original.content_hash
    assert restored.source_profiles == original.source_profiles
    x, _profile, _ = fixture()
    background = restored.baseline_w + restored.basis_w @ [0.75, 0.1]
    signal = np.where(np.abs(x) < .3, 3e-10, 0)
    frame = SpectrumFrameEnvelope(1, "signal", restored.context.context_id, 0, 3,
                                 role=SpectrumFrameRole.SIGNAL, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
    fit = calibrated_interference_model(restored).fit(frame, background + signal)
    np.testing.assert_allclose(background + signal - fit.background_w, signal, atol=2e-24)
    for values in (restored.basis_w, restored.control_mask, restored.protected_mask, restored.control_sigma_w):
        with pytest.raises(ValueError):
            values.setflags(write=True)
    with h5py.File(path, "r") as file:
        assert len(file["_pending"]) == 0
        assert file["spectrum_processing_v1/interference_models/line-model/basis_w"].attrs["unit"] == "W"


def test_dependencies_and_immutable_model_id_are_enforced_before_commit(tmp_path):
    _x, profile, calibration = fixture()
    run = writer(tmp_path / "dependencies.h5")
    try:
        with pytest.raises(ExecutionError, match="committed source"):
            run.store_interference_calibration(calibration)
        run.store_background_profile(calibration.context, profile)
        wrong = replace(calibration, source_profiles=((profile.profile_id, "0" * 64),))
        with pytest.raises(ExecutionError, match="identity differs"):
            run.store_interference_calibration(wrong)
        run.store_interference_calibration(calibration)
        assert run.store_interference_calibration(calibration) == calibration.model_id
        with pytest.raises(ExecutionError, match="overwrite"):
            run.store_interference_calibration(replace(calibration, baseline_w=calibration.baseline_w * 1.01))
    finally:
        run.close("aborted")
    (restored,) = Hdf5RunReader.interference_calibrations(tmp_path / "dependencies.h5")
    assert restored.content_hash == calibration.content_hash
    with pytest.raises(ExecutionError, match="closed"):
        run.store_interference_calibration(calibration)


@pytest.mark.parametrize("damage", ["basis", "mask", "unit", "schema", "complete", "dependency"])
def test_reader_rejects_modified_or_uncommitted_calibration(tmp_path, damage):
    path = tmp_path / "bad.h5"
    save(path)
    with h5py.File(path, "r+") as file:
        group = file["spectrum_processing_v1/interference_models/line-model"]
        if damage == "basis":
            group["basis_w"][500, 0] += 1e-10
        elif damage == "mask":
            group["protected_mask"][500] = False
        elif damage == "unit":
            group["baseline_w"].attrs["unit"] = "dBm"
        elif damage == "schema":
            group.attrs["schema"] = "future-v2"
        elif damage == "complete":
            group.attrs["complete"] = False
        else:
            del file["spectrum_processing_v1/profiles/reference"]
    before = path.read_bytes()
    with pytest.raises(ExecutionError):
        Hdf5RunReader.interference_calibrations(path)
    assert path.read_bytes() == before


def test_qualification_requires_evidence_and_unqualified_model_cannot_fit_signal(tmp_path):
    _x, _profile, calibration = fixture()
    with pytest.raises(ValueError, match="evidence"):
        replace(calibration, qualification_evidence="")
    unqualified = replace(calibration, signal_control_regions_qualified=False, qualification_evidence="")
    assert unqualified.content_hash != calibration.content_hash
    frame = SpectrumFrameEnvelope(1, "signal", unqualified.context.context_id, 0, 3,
                                 role=SpectrumFrameRole.SIGNAL, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
    with pytest.raises(ValueError, match="independently qualified"):
        calibrated_interference_model(unqualified).fit(frame, unqualified.baseline_w)


def test_rank_failure_creates_no_model_or_pending_record(tmp_path):
    _x, profile, calibration = fixture()
    bad = replace(calibration, basis_w=np.column_stack((calibration.basis_w[:, 0], calibration.basis_w[:, 0])))
    run = writer(tmp_path / "rank.h5")
    try:
        run.store_background_profile(calibration.context, profile)
        with pytest.raises(ValueError, match="conditioned"):
            run.store_interference_calibration(bad)
    finally:
        run.close("aborted")
    assert Hdf5RunReader.interference_calibrations(tmp_path / "rank.h5") == ()
    with h5py.File(tmp_path / "rank.h5", "r") as file:
        assert len(file["_pending"]) == 0


@pytest.mark.parametrize("phase", ["write", "flush"])
def test_partial_model_commit_is_removed_and_can_be_retried(tmp_path, monkeypatch, phase):
    from app.storage import spectrum_interference_codec as codec

    path = tmp_path / "failure.h5"
    _x, profile, calibration = fixture()
    run = writer(path)
    try:
        run.store_background_profile(calibration.context, profile)
        with monkeypatch.context() as patch:
            if phase == "write":
                def fail(group, _calibration):
                    group.create_dataset("partial", data=[1])
                    raise OSError("injected model write failure")
                patch.setattr(codec, "write_interference_calibration", fail)
            else:
                original = h5py.File.flush
                failed = False

                def fail_once(file):
                    nonlocal failed
                    if not failed:
                        failed = True
                        raise OSError("injected model flush failure")
                    return original(file)

                patch.setattr(h5py.File, "flush", fail_once)
            with pytest.raises(ExecutionError, match="Could not commit"):
                run.store_interference_calibration(calibration)
        assert run.store_interference_calibration(calibration) == calibration.model_id
    finally:
        run.close("aborted")
    assert len(Hdf5RunReader.interference_calibrations(path)) == 1
    with h5py.File(path, "r") as file:
        assert len(file["_pending"]) == 0


def test_private_model_keeps_public_pythat_round_trip(tmp_path):
    path = tmp_path / "public.h5"
    _x, profile, calibration = fixture()
    run = writer(path)
    try:
        run.store_background_profile(calibration.context, profile)
        run.store_interference_calibration(calibration)
        trace = SpectrumTrace(tuple(calibration.context.frequencies_hz),
                              tuple(10 * np.log10(calibration.baseline_w) + 30),
                              datetime.now(timezone.utc), "TRAC1")
        run.append(MeasurementPoint(0, {}, {}), trace)
    finally:
        run.close("aborted")
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
    assert Hdf5RunReader.spectrum(path, 0) is not None
    assert len(Hdf5RunReader.interference_calibrations(path)) == 1


@pytest.mark.parametrize("name", ["baseline_w", "basis_w", "control_sigma_w"])
@pytest.mark.parametrize("kind", ["complex", "string"])
def test_power_contract_rejects_complex_and_text_calibration(name, kind):
    _x, _profile, calibration = fixture()
    values = getattr(calibration, name).astype(complex if kind == "complex" else str)
    with pytest.raises(ValueError, match="real numeric"):
        replace(calibration, **{name: values})
