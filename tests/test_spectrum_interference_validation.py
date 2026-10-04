"""Unseen reference prediction, disjoint data, no conditional-error concealment."""

from dataclasses import replace
from datetime import datetime, timezone
import json

import numpy as np
import h5py
import pytest

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.models import MeasurementPoint
from app.spectrum.background_profile import BackgroundProfileBuilder
from app.spectrum.interference_training import train_interference_basis
from app.spectrum.interference_validation import validate_interference_references
from app.spectrum.streaming_statistics import dbm_to_w
from app.storage.interference_training_store import train_interference_archive
from app.storage.interference_validation_store import validate_interference_archive
from tests.test_spectrum_interference_training import data, parameters, source_archive
from tests.test_spectrum_interference_store import writer


def unseen_references(*, out_of_range=False):
    x, context, _profile, training_rows, nuisance = data()
    line = np.where(nuisance, np.exp(-x**2 / 2) * 1e-9, 0)
    basis = np.column_stack((line, -x * line))
    builder = BackgroundProfileBuilder(context, reference_state="unseen synthetic REF", minimum_sweeps=2)
    rows = []
    for index in range(12):
        amplitude = 2 if out_of_range else .2 * np.sin(index + .5)
        powers_dbm = 10 * np.log10(2e-9 + basis @ [amplitude, .02 * np.cos(index)]) + 30
        envelope = replace(training_rows[index][0], acquired_at_s=101 + index, segment_id="validation")
        watts = dbm_to_w(powers_dbm)
        builder.add(envelope, watts)
        rows.append((envelope, watts))
    return x, context, builder.finish(), rows, nuisance


def save_unseen(path, *, out_of_range=False):
    x, context, profile, rows, nuisance = unseen_references(out_of_range=out_of_range)
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


def trained_model():
    x, context, profile, rows, nuisance = data()
    return train_interference_basis(context, profile, lambda: iter(rows), **parameters(x, nuisance))


def test_unseen_reference_error_is_measured_on_bins_excluded_from_fitting():
    calibration = trained_model()
    _x, _context, profile, rows, _nuisance = unseen_references()
    report = validate_interference_references(calibration, profile, iter(rows), calibration.protected_mask)
    assert report["accepted_fits"] == 12 and report["rejected_fits"] == 0
    error = report["model_error_on_accepted_fits"]["unused_region_rms_w"]
    static_error = report["static_error_on_same_accepted_fits"]["unused_region_rms_w"]
    assert error < 1e-23 and static_error > 1e-11
    assert report["qualification_inferred"] is False
    assert report["units"]["unused_region_rms_w"] == "W"


def test_rejected_predictions_are_counted_and_cannot_appear_as_zero_error():
    calibration = trained_model()
    _x, _context, profile, rows, _nuisance = unseen_references(out_of_range=True)
    report = validate_interference_references(calibration, profile, iter(rows), calibration.protected_mask)
    assert report["accepted_fits"] == 0 and report["rejected_fits"] == 12
    assert report["model_error_on_accepted_fits"] is None
    assert report["static_error_on_same_accepted_fits"] is None
    assert sum(report["rejection_reasons"].values()) == 12


def test_overlapping_time_and_used_frequency_bins_are_rejected():
    calibration = trained_model()
    _x, _context, profile, rows, _nuisance = unseen_references()
    with pytest.raises(ValueError, match="overlaps"):
        validate_interference_references(calibration, replace(profile, started_at_s=30), iter(rows), calibration.protected_mask)
    with pytest.raises(ValueError, match="participate"):
        validate_interference_references(calibration, profile, iter(rows), calibration.control_mask)


def test_read_only_archive_validation_and_atomic_output(tmp_path):
    training, model, validation, destination = (tmp_path / name for name in ("train.h5", "model.h5", "ref.h5", "report.json"))
    x, _context, _profile, _rows, nuisance = source_archive(training)
    train_interference_archive(training, model, **parameters(x, nuisance))
    save_unseen(validation)
    before = {path: path.read_bytes() for path in (training, model, validation)}
    report = validate_interference_archive(model, validation, destination)
    assert report["accepted_fits"] == 12
    assert json.loads(destination.read_text(encoding="utf-8"))["qualification_inferred"] is False
    assert all(path.read_bytes() == content for path, content in before.items())
    with pytest.raises(ExecutionError, match="new report"):
        validate_interference_archive(model, validation, destination)
    with pytest.raises(ExecutionError, match="reuse"):
        validate_interference_archive(model, training, tmp_path / "bad.json")
    assert not (tmp_path / "bad.json").exists()


def test_cancelled_validation_never_publishes_report(tmp_path):
    training, model, validation = (tmp_path / name for name in ("train.h5", "model.h5", "ref.h5"))
    x, _context, _profile, _rows, nuisance = source_archive(training)
    train_interference_archive(training, model, **parameters(x, nuisance))
    save_unseen(validation)

    def cancel():
        raise ProcessingCancelled("injected cancellation")

    with pytest.raises(ProcessingCancelled):
        validate_interference_archive(model, validation, tmp_path / "report.json", cancellation_check=cancel)
    assert not (tmp_path / "report.json").exists()


@pytest.mark.parametrize("source_kind", ["model", "reference"])
def test_pending_source_is_rejected_even_with_completed_status(tmp_path, source_kind):
    training, model, validation = (tmp_path / name for name in ("train.h5", "model.h5", "ref.h5"))
    x, _context, _profile, _rows, nuisance = source_archive(training)
    train_interference_archive(training, model, **parameters(x, nuisance))
    save_unseen(validation)
    source = model if source_kind == "model" else validation
    with h5py.File(source, "r+") as archive:
        archive["_pending"].create_group("unfinished")
    before = source.read_bytes()
    with pytest.raises(ExecutionError, match="closed committed|completed raw"):
        validate_interference_archive(model, validation, tmp_path / "report.json")
    assert source.read_bytes() == before
    assert not (tmp_path / "report.json").exists()


def test_explicit_validation_regions_use_actual_grid_and_reject_fit_bins(tmp_path):
    training, model, validation = (tmp_path / name for name in ("train.h5", "model.h5", "ref.h5"))
    x, _context, _profile, _rows, nuisance = source_archive(training)
    train_interference_archive(training, model, **parameters(x, nuisance))
    save_unseen(validation)
    report = validate_interference_archive(model, validation, tmp_path / "report.json",
        validation_regions=[["1,48 MHz", "1,52 MHz"]])
    assert report["validation_bin_indices"] == list(range(96, 105))
    with pytest.raises(ValueError, match="participate"):
        validate_interference_archive(model, validation, tmp_path / "used.json",
            validation_regions=[["1 MHz", "1.1 MHz"]])
    with pytest.raises(ValueError, match="within"):
        validate_interference_archive(model, validation, tmp_path / "outside.json",
            validation_regions=[["3 MHz", "4 MHz"]])
    assert not (tmp_path / "used.json").exists() and not (tmp_path / "outside.json").exists()


def test_cli_repeated_regions_publish_only_a_diagnostic(tmp_path, monkeypatch, capsys):
    from tools.validate_spectrum_interference import main

    training, model, validation = (tmp_path / name for name in ("train.h5", "model.h5", "ref.h5"))
    x, _context, _profile, _rows, nuisance = source_archive(training)
    train_interference_archive(training, model, **parameters(x, nuisance))
    save_unseen(validation)
    destination = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["validate_spectrum_interference", str(model), str(validation),
        "--output", str(destination), "--region", "1.48 MHz", "1.49 MHz",
        "--region", "1.51 MHz", "1.52 MHz"])
    main()
    report = json.loads(destination.read_text(encoding="utf-8"))
    assert report["validation_bin_indices"] == [96, 97, 98, 102, 103, 104]
    assert not report["qualification_inferred"]
    assert "Diagnostics only" in capsys.readouterr().out


def test_reference_statistics_use_heldout_context_without_inferred_model_qualification():
    calibration = trained_model()
    _x, context, profile, rows, _nuisance = unseen_references()
    context = replace(context, independent_sweeps_qualified=True)
    builder = BackgroundProfileBuilder(context, reference_state=profile.reference_state, minimum_sweeps=2)
    for envelope, watts in rows:
        builder.add(envelope, watts)
    profile = builder.finish()
    assert profile.mean_variance_w2 is not None
    report = validate_interference_references(calibration, profile, iter(rows), calibration.protected_mask,
                                              reference_context=context)
    assert report["accepted_fits"] == 12
    assert not report["qualification_inferred"]


@pytest.mark.parametrize("failure", ["cancel", "destination_race"])
def test_atomic_publication_cleans_pending_without_overwriting_report(tmp_path, monkeypatch, failure):
    from app.storage import interference_validation_store as module

    training, model, validation = (tmp_path / name for name in ("train.h5", "model.h5", "ref.h5"))
    x, _context, _profile, _rows, nuisance = source_archive(training)
    train_interference_archive(training, model, **parameters(x, nuisance))
    save_unseen(validation)
    destination = tmp_path / "report.json"
    if failure == "cancel":
        def cancel():
            if list(tmp_path.glob(".report.json.*.pending")):
                raise ProcessingCancelled("cancel publication after fsync")

        with pytest.raises(ProcessingCancelled):
            validate_interference_archive(model, validation, destination, cancellation_check=cancel)
        assert not destination.exists()
    else:
        original = module.os.link

        def racing_link(source, target):
            target.write_bytes(b"another process created this report")
            return original(source, target)

        monkeypatch.setattr(module.os, "link", racing_link)
        with pytest.raises(FileExistsError):
            validate_interference_archive(model, validation, destination)
        assert destination.read_bytes() == b"another process created this report"
    assert not list(tmp_path.glob("*.pending"))
