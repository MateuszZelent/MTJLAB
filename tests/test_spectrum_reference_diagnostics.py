"""Independent numerical oracles and raw-archive diagnostic boundaries."""

from dataclasses import replace
from datetime import datetime, timezone
import json

import h5py
import numpy as np
import pytest

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.domain.spectrum_correction import (
    SpectrumAcquisitionContext, SpectrumFrameEnvelope, SpectrumFrameRole, SweepEvidence,
)
from app.spectrum.background_profile import BackgroundProfileBuilder
from app.spectrum.reference_diagnostics import (
    ReferenceDiagnosticConfig, diagnose_reference, overlapping_allan_variance,
)
from app.storage.background_profile_store import BackgroundProfileHdf5Store
from app.storage.finalized_spectrum_store import file_sha256
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.reference_diagnostic_store import diagnose_reference_archive


def reference_frames(blocks=32):
    context = SpectrumAcquisitionContext([1e6, 2e6, 3e6], "diagnostic-fixture")
    rng = np.random.default_rng(801)
    powers = np.column_stack((np.full(blocks + 1, 1e-9),
                              2e-9 + np.arange(blocks + 1) * 1e-12,
                              3e-9 + rng.normal(0, 1e-11, blocks + 1)))
    frames = []
    for block in range(blocks + 1):
        for offset in (0., .25, .5, .75):
            envelope = SpectrumFrameEnvelope(
                len(frames), "reference", context.context_id, 0, 1_700_000_000 + block + offset,
                role=SpectrumFrameRole.REFERENCE, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP,
            )
            frames.append((envelope, 10 * np.log10(powers[block]) + 30))
    return context, frames, powers


def test_allan_matches_direct_adjacent_average_oracle_and_linear_drift():
    rng = np.random.default_rng(203)
    values = rng.normal(size=(37, 3)) * 1e-12 + 1e-9
    factors = (1, 2, 4, 8)
    actual, counts = overlapping_allan_variance(values, factors)
    oracle = []
    for m in factors:
        differences = [values[i + m:i + 2 * m].mean(axis=0) - values[i:i + m].mean(axis=0)
                       for i in range(len(values) - 2 * m + 1)]
        oracle.append(.5 * np.mean(np.square(differences), axis=0))
    np.testing.assert_allclose(actual, oracle, rtol=2e-12, atol=0)
    np.testing.assert_array_equal(counts, [37 - 2 * m + 1 for m in factors])
    drift = (1e-9 + np.arange(64) * 1e-12)[:, None]
    actual, _ = overlapping_allan_variance(drift, factors)
    np.testing.assert_allclose(actual[:, 0], .5 * (np.array(factors) * 1e-12) ** 2, rtol=1e-12)


def test_white_power_noise_allan_decreases_with_average_duration():
    rng = np.random.default_rng(440)
    variance = 1e-24
    values = 1e-9 + rng.normal(0, np.sqrt(variance), (100_000, 2))
    factors = (1, 2, 4, 8, 16)
    actual, _ = overlapping_allan_variance(values, factors)
    np.testing.assert_allclose(actual, variance / np.array(factors)[:, None] * np.ones((1, 2)), rtol=.05)


def test_regular_history_keeps_units_tail_counts_and_constant_bin_mask():
    context, frames, powers = reference_frames()
    result = diagnose_reference(context, iter(frames), (0, 1, 2))
    assert result.cadence_valid and not result.issues
    assert result.total_sweeps == 132 and result.discarded_tail_sweeps == 4
    assert result.block_counts.sum() + result.discarded_tail_sweeps == result.total_sweeps
    np.testing.assert_allclose(result.block_mean_w, powers[:-1], rtol=1e-14)
    np.testing.assert_array_equal(result.source_frame_ranges, np.arange(128).reshape(32, 4)[:, [0, 3]])
    np.testing.assert_allclose(result.block_times_s, 1_700_000_000 + np.arange(32) + .375)
    assert not result.correlation_valid_bins[0]
    np.testing.assert_array_equal(result.autocorrelation[:, 0], 0)
    np.testing.assert_allclose(result.autocorrelation[0, 1:], 1)
    np.testing.assert_allclose(result.allan_variance_w2[:, 1], .5 * (result.allan_tau_s * 1e-12) ** 2,
                               rtol=1e-11)
    for array in (result.block_mean_w, result.autocorrelation, result.block_counts):
        with pytest.raises(ValueError):
            array.setflags(write=True)


def test_fractional_buckets_at_epoch_boundaries_do_not_lose_frames():
    context, frames, _ = reference_frames()
    frames = [(replace(env, acquired_at_s=1_700_000_000 + index * .025), raw)
              for index, (env, raw) in enumerate(frames)]
    result = diagnose_reference(context, frames, (1,), ReferenceDiagnosticConfig(block_duration_s=.1))
    assert result.cadence_valid and not result.issues
    np.testing.assert_array_equal(result.block_counts, np.full(32, 4))
    assert result.discarded_tail_sweeps == 4
    with pytest.raises(ValueError, match="timestamp resolution"):
        diagnose_reference(context, frames, (1,), ReferenceDiagnosticConfig(block_duration_s=1e-8))


@pytest.mark.parametrize("case", ["gap", "irregular", "short"])
def test_inadequate_time_history_suppresses_allan_and_correlation(case):
    context, frames, _ = reference_frames(8 if case == "short" else 32)
    if case == "gap":
        frames = [(env, raw) for env, raw in frames if not 8 <= env.acquired_at_s - 1_700_000_000 < 9]
    elif case == "irregular":
        frames = [(env, raw) for env, raw in frames
                  if not 8.01 <= env.acquired_at_s - 1_700_000_000 < 9]
    result = diagnose_reference(context, frames, (1,))
    assert result.allan_variance_w2 is None and result.autocorrelation is None
    expected = {"gap": "missing_time_blocks", "irregular": "irregular_effective_cadence",
                "short": "insufficient_blocks"}[case]
    assert expected in result.issues


@pytest.mark.parametrize("case", ["role", "unknown", "generation", "segment", "order", "counter", "axis"])
def test_invalid_sweep_history_is_rejected(case):
    context, frames, _ = reference_frames()
    env, raw = frames[1]
    changes = {"role": {"role": SpectrumFrameRole.SIGNAL}, "unknown": {"evidence": SweepEvidence.UNKNOWN},
               "generation": {"configuration_generation": 1}, "segment": {"segment_id": "other"},
               "order": {"frame_id": 0}}
    if case == "counter":
        frames[0] = replace(frames[0][0], evidence=SweepEvidence.INSTRUMENT_COUNTER, sweep_id="9"), frames[0][1]
        env = replace(env, evidence=SweepEvidence.INSTRUMENT_COUNTER, sweep_id="9")
    elif case == "axis":
        raw = raw[:2]
    else:
        env = replace(env, **changes[case])
    frames[1] = env, raw
    with pytest.raises(ValueError):
        diagnose_reference(context, frames, (1,))


def test_resource_limits_and_cancellation_are_enforced():
    context, frames, _ = reference_frames()
    with pytest.raises(ValueError, match="block limit"):
        diagnose_reference(context, frames, (1,), ReferenceDiagnosticConfig(maximum_blocks=16))
    with pytest.raises(ValueError, match="unique"):
        diagnose_reference(context, frames, (1, 1))
    calls = []

    def cancel():
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        overlapping_allan_variance(np.ones((32, 1)), (1, 2, 4), cancellation_check=cancel)
    assert len(calls) == 2


@pytest.mark.parametrize("counter", ["40", "39", "41"])
def test_diagnostics_counter_checks_survive_intervening_host_evidence(counter):
    context, frames, _ = reference_frames()
    for index, evidence, sweep_id in ((0, SweepEvidence.INSTRUMENT_COUNTER, "40"),
                                      (1, SweepEvidence.QUALIFIED_SINGLE_SWEEP, "host:opaque"),
                                      (2, SweepEvidence.INSTRUMENT_COUNTER, counter)):
        frames[index] = replace(frames[index][0], evidence=evidence, sweep_id=sweep_id), frames[index][1]
    if counter == "41":
        assert diagnose_reference(context, frames, (1,)).total_sweeps == len(frames)
    else:
        with pytest.raises(ValueError, match="counter must increase"):
            diagnose_reference(context, frames, (1,))


def archive(tmp_path):
    context, frames, _ = reference_frames()
    builder = BackgroundProfileBuilder(context, reference_state="control, signal absence unknown")
    for env, dbm in frames:
        builder.add(env, 10 ** ((dbm - 30) / 10))
    profile = builder.finish()
    path = tmp_path / "reference.h5"
    writer = Hdf5RunWriter(path, recipe_source="schema_version: 1\nname: diagnostic\nsteps: []\n",
                           settings_source="fixture: true\n", plan_hash="fixture", device_idn={},
                           simulation_metadata={"enabled": True})
    writer.store_background_profile(context, profile)
    for index, (env, dbm) in enumerate(frames):
        trace = SpectrumTrace(tuple(context.frequencies_hz), tuple(dbm),
                              datetime.fromtimestamp(env.acquired_at_s, timezone.utc), "TRAC1")
        writer.append(MeasurementPoint(index, {}, {}), trace, acquisition_envelope=env)
    writer.close("completed")
    return path, context, profile


def test_archive_report_is_separate_explicit_units_and_diagnostic_only(tmp_path):
    source, _, _ = archive(tmp_path)
    original_hash = file_sha256(source)
    output = tmp_path / "diagnostic.json"
    diagnostics, report = diagnose_reference_archive(source, destination=output)
    assert report["qualification"] == "diagnostic_only" and report["qualified_ttl_s"] is None
    assert not report["sweep_independence_inferred"]
    assert report["raw_profile_verified"]
    assert report["units"]["allan_variance_w2"] == "W^2"
    assert report["source"]["sha256"] == original_hash == file_sha256(source)
    assert json.loads(output.read_text(encoding="utf-8"))["total_sweeps"] == diagnostics.total_sweeps
    with pytest.raises(ExecutionError, match="new destination"):
        diagnose_reference_archive(source, destination=output)
    with pytest.raises(ExecutionError, match="new destination"):
        diagnose_reference_archive(source, destination=source)


@pytest.mark.parametrize("corruption", ["incomplete", "axis", "profile", "envelope", "status", "raw", "pending", "shape"])
def test_corrupt_archive_does_not_create_report(tmp_path, corruption):
    source, _, _ = archive(tmp_path)
    with h5py.File(source, "r+") as file:
        if corruption == "incomplete":
            file["points/2"].attrs["complete"] = False
        elif corruption == "axis":
            file["spectra/2/frequency_hz"][0] += 1
        elif corruption == "profile":
            next(iter(file["spectrum_processing_v1/profiles"].values()))["mean_w"][0] *= 2
        elif corruption == "status":
            file["run"].attrs["status"] = "running"
        elif corruption == "raw":
            file["spectra/2/power_dbm"][0] += 1
        elif corruption == "pending":
            file["_pending"].create_group("uncommitted")
        elif corruption == "shape":
            del file["spectra/2/power_dbm"]
            file["spectra/2"].create_dataset("power_dbm", data=[-60., -60.])
        else:
            del file["spectra/2"].attrs["acquisition_envelope_json"]
    output = tmp_path / "never.json"
    with pytest.raises(ExecutionError):
        diagnose_reference_archive(source, destination=output)
    assert not output.exists()


def test_portable_mean_profile_is_not_raw_history(tmp_path):
    _, context, profile = archive(tmp_path)
    source = tmp_path / "portable.h5"
    BackgroundProfileHdf5Store.save(source, context, profile)
    with pytest.raises(ExecutionError, match="raw sweep history"):
        diagnose_reference_archive(source)


def test_report_publication_failure_leaves_no_partial_destination(tmp_path, monkeypatch):
    source, _, _ = archive(tmp_path)
    output = tmp_path / "failed.json"

    def fail_link(*args):
        raise OSError("publication failed")

    monkeypatch.setattr("app.storage.reference_diagnostic_store.os.link", fail_link)
    with pytest.raises(ExecutionError, match="publication failed"):
        diagnose_reference_archive(source, destination=output)
    assert not output.exists() and not list(tmp_path.glob("*.pending"))


def test_destination_created_during_analysis_is_never_replaced(tmp_path):
    source, _, _ = archive(tmp_path)
    output = tmp_path / "race.json"

    def create_destination():
        if not output.exists():
            output.write_text("other process", encoding="utf-8")

    with pytest.raises(ExecutionError):
        diagnose_reference_archive(source, destination=output, cancellation_check=create_destination)
    assert output.read_text(encoding="utf-8") == "other process"
    assert not list(tmp_path.glob("*.pending"))


def test_archive_cancellation_creates_no_report(tmp_path):
    source, _, _ = archive(tmp_path)
    output = tmp_path / "cancelled.json"
    checks = []

    def cancel():
        checks.append(True)
        if len(checks) == 8:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        diagnose_reference_archive(source, destination=output, cancellation_check=cancel)
    assert not output.exists() and not list(tmp_path.glob("*.pending"))


def test_published_diagnostics_reject_inconsistent_shapes_and_counts():
    context, frames, _ = reference_frames()
    result = diagnose_reference(context, frames, (1, 2))
    with pytest.raises(ValueError, match="shapes"):
        replace(result, block_mean_w=np.ones((2, 2)))
    with pytest.raises(ValueError, match="counts"):
        replace(result, total_sweeps=result.total_sweeps + 1)
    with pytest.raises(ValueError, match="shapes"):
        replace(result, allan_variance_w2=None)


def test_cancellation_after_report_flush_prevents_publication(tmp_path):
    source, _, _ = archive(tmp_path)
    output = tmp_path / "late-cancel.json"

    def cancel_after_write():
        if list(tmp_path.glob("*.pending")):
            raise RuntimeError("cancelled after flush")

    with pytest.raises(RuntimeError, match="after flush"):
        diagnose_reference_archive(source, destination=output, cancellation_check=cancel_after_write)
    assert not output.exists() and not list(tmp_path.glob("*.pending"))


@pytest.mark.parametrize("duration", ["1 s", "1 Hz"])
def test_cli_explicit_units_and_report_export(tmp_path, monkeypatch, capsys, duration):
    from tools.diagnose_spectrum_reference import main

    source, _, _ = archive(tmp_path)
    output = tmp_path / "cli.json"
    monkeypatch.setattr("sys.argv", ["diagnose_spectrum_reference", str(source), "--output", str(output),
                                    "--block-duration", duration, "--bins", "1", "2"])
    if duration == "1 Hz":
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 1 and not output.exists()
    else:
        main()
        assert "32 blocks; 132 raw sweeps" in capsys.readouterr().out
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["bin_indices"] == [1, 2] and report["config"]["block_duration_s"] == 1
