"""Reference-only local SVD, immutable origin and signed-signal recovery."""

from dataclasses import replace
from datetime import datetime, timezone
import json

import h5py
import numpy as np
import pytest

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.models import MeasurementPoint
from app.domain.spectrum_correction import SpectrumAcquisitionContext, SpectrumFrameEnvelope, SpectrumFrameRole, SweepEvidence
from app.spectrum.background_profile import BackgroundProfileBuilder
from app.spectrum.interference_model import calibrated_interference_model
from app.spectrum.interference_training import InterferenceTrainingConfig, train_interference_basis
from app.spectrum.streaming_statistics import dbm_to_w
from app.storage.hdf5_reader import Hdf5RunReader
from app.storage.interference_training_store import train_interference_archive
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_spectrum_interference_store import writer


def data():
    x = np.linspace(-8, 8, 201)
    context = SpectrumAcquisitionContext(np.linspace(1e6, 2e6, x.size), "synthetic-local-lines")
    nuisance = np.abs(x) < 4
    line = np.where(nuisance, np.exp(-x**2 / 2) * 1e-9, 0)
    basis = np.column_stack((line, -x * line))
    builder = BackgroundProfileBuilder(context, reference_state="synthetic REF", minimum_sweeps=2)
    rows = []
    for index in range(40):
        coefficients = [.5 * np.sin(2 * np.pi * index / 40), .1 * np.cos(2 * np.pi * index / 40)]
        powers_dbm = 10 * np.log10(2e-9 + basis @ coefficients) + 30
        envelope = SpectrumFrameEnvelope(index, "reference", context.context_id, 0, index + 1,
                                         role=SpectrumFrameRole.REFERENCE, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
        watts = dbm_to_w(powers_dbm)
        builder.add(envelope, watts)
        rows.append((envelope, watts))
    return x, context, builder.finish(), rows, nuisance


def parameters(x, nuisance, **extra):
    return dict(model_id="learned-lines", nuisance_mask=nuisance,
                control_mask=np.ones(x.size, dtype=bool), protected_mask=np.abs(x) < .5,
                control_sigma_w=np.full(x.size, 1e-12),
                config=InterferenceTrainingConfig(components=2, maximum_training_frames=12), **extra)


def source_archive(path):
    x, context, profile, rows, nuisance = data()
    run = writer(path)
    try:
        run.store_background_profile(context, profile)
        for envelope, watts in rows:
            trace = SpectrumTrace(tuple(context.frequencies_hz), tuple(10 * np.log10(watts) + 30),
                                  datetime.fromtimestamp(envelope.acquired_at_s, timezone.utc), "TRAC1")
            run.append(MeasurementPoint(envelope.frame_id, {}, {}, metadata={"quantitative_accepted": True}),
                       trace, acquisition_envelope=envelope)
    finally:
        run.close("completed")
    return x, context, profile, rows, nuisance


def test_bounded_local_basis_preserves_overlapping_signed_signal_and_does_not_infer_qualification():
    x, context, profile, rows, nuisance = data()
    calibration = train_interference_basis(context, profile, lambda: iter(rows), **parameters(x, nuisance))
    assert not calibration.signal_control_regions_qualified
    report = json.loads(calibration.training_provenance_json)
    assert len(report["selected_reference_ordinals"]) == 12 and report["reference_sweeps"] == 40
    assert report["qualification_inferred"] is False
    assert np.count_nonzero(calibration.basis_w[~nuisance]) == 0
    qualified = replace(calibration, signal_control_regions_qualified=True, qualification_evidence="synthetic injection only")
    model = calibrated_interference_model(qualified)
    envelope, background = rows[9]
    signal = np.where(np.abs(x) < .3, np.where(x < 0, -1e-10, 2e-10), 0)
    fit = model.fit(replace(envelope, role=SpectrumFrameRole.SIGNAL), background + signal)
    np.testing.assert_allclose(background + signal - fit.background_w, signal, atol=1e-23)


def test_training_artifact_is_self_contained_source_unchanged_and_pythat_compatible(tmp_path):
    source, destination = tmp_path / "reference.h5", tmp_path / "model.h5"
    x, _context, _profile, _rows, nuisance = source_archive(source)
    before = source.read_bytes()
    calibration = train_interference_archive(source, destination, **parameters(x, nuisance))
    assert source.read_bytes() == before
    (restored,) = Hdf5RunReader.interference_calibrations(destination)
    assert restored.content_hash == calibration.content_hash
    assert "source_archive_sha256" in json.loads(restored.training_provenance_json)
    assert ThatecCompatibilityValidator().validate(destination, require_pythat=True).valid
    with pytest.raises(ExecutionError, match="new destination"):
        train_interference_archive(source, destination, **parameters(x, nuisance))


def test_signal_in_either_training_pass_is_rejected():
    x, context, profile, rows, nuisance = data()
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return iter(rows if calls == 1 else [(replace(row, role=SpectrumFrameRole.SIGNAL), watts) for row, watts in rows])

    with pytest.raises(ValueError, match="reference sweeps"):
        train_interference_basis(context, profile, factory, **parameters(x, nuisance))


def test_memory_limit_precedes_consumption_and_svd(monkeypatch):
    x, context, profile, rows, nuisance = data()
    args = parameters(x, nuisance)
    args["config"] = replace(args["config"], working_memory_limit_bytes=1024)
    with pytest.raises(ValueError, match="memory"):
        train_interference_basis(context, profile, lambda: pytest.fail("raw read before memory validation"), **args)


def test_wrong_profile_rank_and_full_axis_are_rejected():
    x, context, profile, rows, nuisance = data()
    with pytest.raises(ValueError, match="reproduce"):
        train_interference_basis(context, replace(profile, mean_w=profile.mean_w * 1.01), lambda: iter(rows),
                                 **parameters(x, nuisance))
    with pytest.raises(ValueError, match="entire frequency"):
        train_interference_basis(context, profile, lambda: iter(rows), **parameters(x, np.ones(x.size, dtype=bool)))
    args = parameters(x, nuisance)
    args["config"] = replace(args["config"], components=3)
    with pytest.raises(ValueError, match="component count"):
        train_interference_basis(context, profile, lambda: iter(rows), **args)


def test_cancellation_before_publication_creates_no_output(tmp_path):
    source, destination = tmp_path / "reference.h5", tmp_path / "model.h5"
    x, _context, _profile, _rows, nuisance = source_archive(source)

    def cancel():
        raise ProcessingCancelled("injected cancellation")

    with pytest.raises(ProcessingCancelled):
        train_interference_archive(source, destination, cancellation_check=cancel, **parameters(x, nuisance))
    assert not destination.exists()


def test_cancellation_after_creation_retains_aborted_artifact_that_cannot_be_imported(tmp_path):
    source, destination = tmp_path / "reference.h5", tmp_path / "model.h5"
    x, _context, _profile, _rows, nuisance = source_archive(source)

    def cancel():
        if destination.exists():
            raise ProcessingCancelled("injected cancellation after artifact creation")

    with pytest.raises(ProcessingCancelled):
        train_interference_archive(source, destination, cancellation_check=cancel, **parameters(x, nuisance))
    assert Hdf5RunReader.summary(destination).status == "aborted"
    with pytest.raises(ExecutionError, match="not completed"):
        Hdf5RunReader.interference_calibrations(destination)


def test_explicit_unit_specification_trains_without_qualifying_signal_controls(tmp_path):
    from tools.train_spectrum_interference import train_from_specification, frequency_region_mask

    source, destination = tmp_path / "reference.h5", tmp_path / "model.h5"
    source_archive(source)
    spec = {"model_id": "specified-lines", "nuisance_regions": [["1.26 MHz", "1.74 MHz"]],
            "control_regions": [["1 MHz", "2 MHz"]], "protected_regions": [["1.47 MHz", "1.53 MHz"]],
            "control_sigma": "1 pW", "components": 2, "maximum_training_frames": 12}
    model = train_from_specification(source, destination, spec)
    assert not model.signal_control_regions_qualified
    np.testing.assert_array_equal(model.control_sigma_w, np.full(201, 1e-12))
    with pytest.raises(ValueError):
        frequency_region_mask(model.context.frequencies_hz, [["1 s", "2 s"]])
    with pytest.raises(ValueError, match="within"):
        frequency_region_mask(model.context.frequencies_hz, [["1 MHz", "3 MHz"]])


def test_archive_signal_is_rejected_even_when_recorded_as_noncontributing(tmp_path):
    source, destination = tmp_path / "reference.h5", tmp_path / "model.h5"
    x, _context, _profile, _rows, nuisance = source_archive(source)
    with h5py.File(source, "r+") as file:
        raw = file["spectra/1"]
        envelope = json.loads(raw.attrs["acquisition_envelope_json"])
        envelope["role"] = "signal"
        raw.attrs["acquisition_envelope_json"] = json.dumps(envelope)
        file["points/1/metadata_json"][()] = json.dumps({"quantitative_accepted": False})
    with pytest.raises(ExecutionError, match="SIGNAL"):
        train_interference_archive(source, destination, **parameters(x, nuisance))
    assert not destination.exists()
