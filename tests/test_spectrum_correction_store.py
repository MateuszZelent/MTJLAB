"""Durability, unit metadata and compatibility of quantitative spectrum data."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import h5py
import numpy as np
import pytest

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.domain.quantities import (
    DIMENSION_POWER,
    DIMENSION_POWER_DENSITY,
    DIMENSION_SPECTRAL_AREA,
    QuantityError,
    format_quantity_auto,
    parse_quantity,
)
from app.domain.spectrum_correction import (
    BackgroundProfile,
    CorrectionConfig,
    SpectrumAcquisitionContext,
    SpectrumFrameEnvelope,
    SweepEvidence,
    TemporalAverageMode,
)
from app.spectrum.realtime_processor import RealtimeSpectrumProcessor
from app.storage.background_profile_store import BackgroundProfileHdf5Store
from app.storage.hdf5_reader import Hdf5RunReader
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.pythat_bridge import open_measurement_tree
from app.storage.thatec_validator import ThatecCompatibilityValidator


def fixture_profile(known_variance=True):
    ctx = SpectrumAcquisitionContext(
        [1e6, 2e6, 3e6], "RMS;RBW=1000;VBW=1000;PREAMP=0;ATT=10",
        settings_verified=True,
    )
    bg = BackgroundProfile(
        "fixture-profile", ctx.context_id, [1e-9, 2e-9, 3e-9], [1e-20, 2e-20, 3e-20],
        [1e-22, 2e-22, 3e-22] if known_variance else None,
        100, 100, 110, "off resonance", signal_free_qualified=True,
    )
    return ctx, bg


def run_writer(path):
    return Hdf5RunWriter(
        path, recipe_source="schema_version: 1\nname: Correction fixture\nsteps: []\n",
        settings_source="fixture: true\n", plan_hash="correction-fixture", device_idn={},
    )


def signal_fixture(ctx, bg):
    watts = np.array([1.1e-9, 1.9e-9, 3.2e-9])
    dbm = 10 * np.log10(watts) + 30
    raw = SpectrumTrace(tuple(ctx.frequencies_hz), tuple(dbm), datetime.now(timezone.utc), "TRAC1")
    envelope = SpectrumFrameEnvelope(
        7, "signal-1", ctx.context_id, 0, 111, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP,
    )
    processor = RealtimeSpectrumProcessor(ctx, CorrectionConfig(
        average_mode=TemporalAverageMode.BLOCK, minimum_reference_sweeps=3,
    ))
    processor.set_background_profile(bg)
    processor.ingest(envelope, dbm)
    return raw, envelope, processor.snapshot()


@pytest.mark.parametrize("known_variance", [True, False])
def test_profile_round_trip_remains_readable_by_pythat(tmp_path, known_variance):
    ctx, bg = fixture_profile(known_variance)
    path = tmp_path / "background.h5"
    BackgroundProfileHdf5Store.save(path, ctx, bg)
    read_context, read_profile = BackgroundProfileHdf5Store.load(path)
    assert read_context.context_id == ctx.context_id
    assert Hdf5RunReader.detail(path).simulation_metadata == {
        "enabled": None, "mode": "unknown", "mode_source": "profile_export_without_source_archive",
    }
    assert read_profile.content_hash == bg.content_hash
    np.testing.assert_array_equal(read_profile.mean_w, bg.mean_w)
    assert (read_profile.mean_variance_w2 is not None) == known_variance
    assert not read_profile.mean_w.flags.writeable
    report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
    assert report.valid, report.errors
    tree = open_measurement_tree(path)
    assert tree.dataset.sizes["Frequency"] == 3
    assert tree.dataset.sizes["Checkpoint"] == 1
    with h5py.File(path, "r") as file:
        group = file["spectrum_processing_v1/profiles/fixture-profile"]
        assert group["mean_w"].attrs["unit"] == "W"
        assert group["sample_variance_w2"].attrs["unit"] == "W^2"


@pytest.mark.parametrize("array_input", [False, True])
def test_signed_result_and_envelope_commit_with_raw(tmp_path, array_input):
    ctx, bg = fixture_profile()
    raw, envelope, result = signal_fixture(ctx, bg)
    path = tmp_path / "measurement.h5"
    writer = run_writer(path)
    writer.store_background_profile(ctx, bg)
    writer.append(
        MeasurementPoint(0, {}, {}), raw,
        processed_values=result.values_w if array_input else tuple(result.values_w), processed_unit="W",
        processing_operation="signed_reference",
        acquisition_envelope=envelope, corrected_frame=result,
    )
    writer.close("completed")
    restored = Hdf5RunReader.spectrum_correction(path, 0)
    assert Hdf5RunReader.spectrum_acquisition(path, 0) == envelope
    assert restored.values_w[1] < 0
    assert restored.profile_weights == result.profile_weights
    np.testing.assert_array_equal(restored.values_w, result.values_w)
    np.testing.assert_array_equal(Hdf5RunReader.spectrum(path, 0).powers_dbm, raw.powers_dbm)
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid


def test_processed_array_snapshot_survives_caller_mutation_between_private_and_public_writes(tmp_path, monkeypatch):
    ctx, bg = fixture_profile()
    raw, _envelope, result = signal_fixture(ctx, bg)
    original = result.values_w.copy()
    supplied = original.copy()
    path = tmp_path / "mutable-processed.h5"
    writer = run_writer(path)
    append_public = writer._thatec.append

    def mutate_caller_then_append(*args, **kwargs):
        supplied[:] = 42.0
        return append_public(*args, **kwargs)

    monkeypatch.setattr(writer._thatec, "append", mutate_caller_then_append)
    try:
        writer.append(MeasurementPoint(0, {}, {}), raw, processed_values=supplied,
                      processed_unit="W", processing_operation="signed_reference")
    finally:
        writer.close("completed")
    assert np.all(supplied == 42.0)
    with h5py.File(path, "r") as file:
        np.testing.assert_array_equal(file["spectra/0/processed_values"][()], original)
        rows = [file[f"measurement/{name}"] for name, definition in file["scan_definition"].items()
                if isinstance(definition, h5py.Dataset)
                and definition.ndim == 2 and definition.shape[1] == 2
                and dict(definition.asstr()[()]).get("lab control role") == "spectrum_processed"]
        assert len(rows) == 1
        np.testing.assert_array_equal(rows[0]["data"][0], original)
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid


def test_uncommitted_profile_dependency_rejected_before_checkpoint_mutation(tmp_path):
    ctx, bg = fixture_profile()
    raw, envelope, result = signal_fixture(ctx, bg)
    writer = run_writer(tmp_path / "missing-profile.h5")
    with pytest.raises(ExecutionError, match="uncommitted"):
        writer.append(MeasurementPoint(0, {}, {}), raw,
                      acquisition_envelope=envelope, corrected_frame=result)
    assert writer.point_count == 0
    assert not len(writer._pending)
    writer.append(MeasurementPoint(0, {}, {}), raw)
    writer.close("completed")


def test_existing_profile_is_immutable_and_idempotently_reusable(tmp_path):
    ctx, bg = fixture_profile()
    writer = run_writer(tmp_path / "immutable-profile.h5")
    assert writer.store_background_profile(ctx, bg) == "fixture-profile"
    assert writer.store_background_profile(ctx, bg) == "fixture-profile"
    with pytest.raises(ExecutionError, match="overwrite"):
        writer.store_background_profile(ctx, replace(bg, mean_w=bg.mean_w * 2))
    raw, _envelope, _result = signal_fixture(ctx, bg)
    writer.append(MeasurementPoint(0, {}, {}), raw)
    writer.close("completed")


def test_cached_axis_does_not_accept_a_profile_from_other_instrument_settings(tmp_path):
    context, profile = fixture_profile()
    other_context = replace(context, configuration_fingerprint="different detector or attenuation")
    other_profile = replace(profile, profile_id="other-profile", context_id=other_context.context_id)
    raw, envelope, result = signal_fixture(context, profile)
    writer = run_writer(tmp_path / "cross-context.h5")
    writer.store_background_profile(context, profile)
    writer.store_background_profile(other_context, other_profile)
    try:
        with pytest.raises(ExecutionError, match="different acquisition context"):
            writer.append(MeasurementPoint(0, {}, {}), raw, acquisition_envelope=envelope,
                          corrected_frame=replace(result, profile_weights=(("other-profile", 1.0),)))
        assert writer.point_count == 0 and len(writer._pending) == 0
        writer.append(MeasurementPoint(0, {}, {}), raw)
    finally:
        writer.close("completed")


def test_failure_at_public_append_rolls_back_raw_envelope_and_correction_together(tmp_path, monkeypatch):
    context, profile = fixture_profile()
    raw, envelope, result = signal_fixture(context, profile)
    path = tmp_path / "failed-checkpoint.h5"
    writer = run_writer(path)
    writer.store_background_profile(context, profile)
    original_append = writer._thatec.append

    def fail_after_public_write(*args, **kwargs):
        original_append(*args, **kwargs)
        raise OSError("injected storage failure")

    monkeypatch.setattr(writer._thatec, "append", fail_after_public_write)
    with pytest.raises(ExecutionError, match="injected storage failure"):
        writer.append(MeasurementPoint(0, {}, {}), raw, acquisition_envelope=envelope,
                      corrected_frame=result, processed_values=tuple(result.values_w),
                      processed_unit="W", processing_operation="signed_reference")
    assert writer.point_count == 0
    assert len(writer._pending) == len(writer._points) == len(writer._spectra) == 0
    monkeypatch.setattr(writer._thatec, "append", original_append)
    writer.append(MeasurementPoint(0, {}, {}), raw, acquisition_envelope=envelope,
                  corrected_frame=result, processed_values=tuple(result.values_w),
                  processed_unit="W", processing_operation="signed_reference")
    writer.close("faulted")
    assert Hdf5RunReader.spectrum_correction(path, 0).frame_id == envelope.frame_id
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid


@pytest.mark.parametrize("corruption", ["unit", "checksum", "complete", "schema", "axis"])
def test_corrupt_profile_cannot_be_used(tmp_path, corruption):
    ctx, bg = fixture_profile()
    path = tmp_path / "corrupt.h5"
    BackgroundProfileHdf5Store.save(path, ctx, bg)
    with h5py.File(path, "r+") as file:
        group = file["spectrum_processing_v1/profiles/fixture-profile"]
        if corruption == "unit":
            group["mean_w"].attrs["unit"] = "mW"
        elif corruption == "checksum":
            group["mean_w"][1] *= 2
        elif corruption == "complete":
            group.attrs["complete"] = False
        elif corruption == "schema":
            group.attrs["schema"] = "spectrum-correction-v999"
        else:
            group["frequency_hz"][1] += 1e4
    with pytest.raises(ExecutionError):
        BackgroundProfileHdf5Store.load(path)


def test_uncommitted_result_is_never_exposed_by_reader(tmp_path):
    ctx, bg = fixture_profile()
    raw, envelope, result = signal_fixture(ctx, bg)
    path = tmp_path / "incomplete.h5"
    writer = run_writer(path)
    writer.store_background_profile(ctx, bg)
    writer.append(MeasurementPoint(0, {}, {}), raw,
                  acquisition_envelope=envelope, corrected_frame=result)
    writer.close("aborted")
    with h5py.File(path, "r+") as file:
        file["points/0"].attrs["complete"] = False
    with pytest.raises(ExecutionError, match="uncommitted"):
        Hdf5RunReader.spectrum_correction(path, 0)


@pytest.mark.parametrize("field, values", [
    ("powers_dbm", (1.0, np.nan, 2.0)),
    ("powers_dbm", (1.0, np.inf, 2.0)),
    ("powers_dbm", (1.0, 1j, 2.0)),
    ("powers_dbm", ("1", "2", "3")),
    ("powers_dbm", ((1.0,), (2.0,), (3.0,))),
    ("frequencies_hz", (1.0, 1.0, 2.0)),
    ("frequencies_hz", (3.0, 2.0, 1.0)),
    ("frequencies_hz", (2 ** 60, 2 ** 60 + 1, 2 ** 60 + 2)),
    ("processed", (1.0, -np.inf, 2.0)),
    ("processed", (1.0, 1j, 2.0)),
    ("processed", ((1.0,), (2.0,), (3.0,))),
    ("processed", np.array([1.0, np.nan, 2.0])),
    ("processed", np.array([1.0, 1j, 2.0])),
    ("processed", np.ones((3, 1))),
    ("processed", np.array([1.0, 2.0])),
])
def test_vector_validation_rejects_bad_storage_values_before_checkpoint_mutation(tmp_path, field, values):
    ctx, bg = fixture_profile()
    raw, _envelope, _result = signal_fixture(ctx, bg)
    invalid = replace(raw, **{field: values}) if field != "processed" else raw
    writer = run_writer(tmp_path / "vector-validation.h5")
    try:
        kwargs = {"processed_values": values, "processed_unit": "W", "processing_operation": "signed_reference"} if field == "processed" else {}
        with pytest.raises(ExecutionError):
            writer.append(MeasurementPoint(0, {}, {}), invalid, **kwargs)
        assert writer.point_count == 0
        assert not len(writer._points) and not len(writer._pending) and not len(writer._spectra)
        writer.append(MeasurementPoint(0, {}, {}), raw)
    finally:
        writer.close("aborted")


def test_profile_export_is_exclusive(tmp_path):
    ctx, bg = fixture_profile()
    path = tmp_path / "exclusive.h5"
    BackgroundProfileHdf5Store.save(path, ctx, bg)
    with pytest.raises(ExecutionError):
        BackgroundProfileHdf5Store.save(path, ctx, bg)
    assert BackgroundProfileHdf5Store.load(path)[1].content_hash == bg.content_hash


def test_tiny_power_units_and_spectral_dimensions_are_distinct():
    assert parse_quantity("-23 pW", DIMENSION_POWER).si_value == pytest.approx(-23e-12, rel=1e-12, abs=0)
    assert parse_quantity("2 fW/Hz", DIMENSION_POWER_DENSITY).si_value == pytest.approx(2e-15, rel=1e-12, abs=0)
    assert parse_quantity("3 nW*Hz", DIMENSION_SPECTRAL_AREA).si_value == pytest.approx(3e-9, rel=1e-12, abs=0)
    assert format_quantity_auto(-23e-12, DIMENSION_POWER) == "-23 pW"
    with pytest.raises(QuantityError):
        parse_quantity("3 nW*Hz", DIMENSION_POWER)
    with pytest.raises(QuantityError):
        parse_quantity("3 nW", DIMENSION_POWER_DENSITY)
