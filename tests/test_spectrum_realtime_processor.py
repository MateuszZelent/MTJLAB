"""Independent scientific and streaming checks for signed reference correction."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from app.domain.spectrum_correction import (
    BackgroundProfile,
    CorrectionConfig,
    CorrectionQuality,
    SpectrumAcquisitionContext,
    SpectrumFrameEnvelope,
    SpectrumFrameRole,
    SweepEvidence,
    TemporalAverageMode,
)
from app.spectrum.background_profile import BackgroundProfileBuilder
from app.spectrum.realtime_processor import RealtimeSpectrumProcessor
from app.spectrum.reference_estimator import bracketed_reference, causal_reference
from app.spectrum.streaming_statistics import TemporalPowerAverage, VectorWelford, dbm_to_w


def context(size=101, *, independent=True):
    return SpectrumAcquisitionContext(
        np.linspace(1e6, 10e6, size), "RMS;RBW=1000;VBW=1000;ATT=10;PREAMP=0",
        settings_verified=True, independent_sweeps_qualified=independent,
    )


def envelope(ctx, number, at, *, role=SpectrumFrameRole.SIGNAL, segment="sample"):
    return SpectrumFrameEnvelope(
        number, segment, ctx.context_id, ctx.configuration_generation, at,
        role=role, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP,
    )


def profile(ctx, *, mean=None, mean_variance=None, start=0.0, end=1.0, name="reference"):
    size = ctx.frequencies_hz.size
    return BackgroundProfile(
        name, ctx.context_id,
        np.full(size, 1e-9) if mean is None else mean,
        np.full(size, 4e-20),
        np.full(size, 4e-22) if mean_variance is None else mean_variance,
        100, start, end, "off-resonance, same input path", signal_free_qualified=True,
    )


def as_dbm(watts):
    return 10 * np.log10(watts) + 30


def processor(ctx, mode=TemporalAverageMode.BLOCK, **kwargs):
    config = CorrectionConfig(
        average_mode=mode, minimum_reference_sweeps=3, maximum_reference_age_s=120,
        **kwargs,
    )
    result = RealtimeSpectrumProcessor(ctx, config)
    result.set_background_profile(profile(ctx), drift_variance_rate_w2_s=np.zeros(ctx.frequencies_hz.size))
    return result


def test_dbm_to_w_has_correct_absolute_si_scale():
    np.testing.assert_allclose(dbm_to_w([0, -30, -60]), [1e-3, 1e-6, 1e-9], rtol=1e-14)


def test_monte_carlo_coverage_in_explicitly_ideal_independent_power_model():
    # Bins represent 2,000 independent synthetic repetitions, not a claim of
    # independence between frequencies of a real analyzer. No hardware
    # qualification or confidence policy is inferred from this unit test.
    ctx = context(2000)
    random = np.random.default_rng(317)
    reference_rows = 1e-9 + random.normal(0, 2e-11, (40, 2000))
    builder = BackgroundProfileBuilder(ctx, reference_state="synthetic signal-free power model",
                                      minimum_sweeps=40, signal_free_qualified=True)
    for index, row in enumerate(reference_rows):
        builder.add(envelope(ctx, index, index * .05, role=SpectrumFrameRole.REFERENCE,
                             segment="reference"), row)
    proc = RealtimeSpectrumProcessor(ctx, CorrectionConfig(
        average_mode=TemporalAverageMode.BLOCK, maximum_reference_age_s=120,
    ))
    proc.set_background_profile(builder.finish(), drift_variance_rate_w2_s=np.zeros(2000))
    for index in range(100):
        raw = 1e-9 + random.normal(0, 2e-11, 2000)
        proc.ingest(envelope(ctx, 40 + index, 2 + index * .05), as_dbm(raw))
    result = proc.snapshot()
    assert result.quality == CorrectionQuality.READY
    assert result.standard_uncertainty_w is not None
    coverage = np.mean(np.abs(result.values_w / result.standard_uncertainty_w) <= 1.96)
    assert 0.925 < coverage < 0.975
    assert 0.45 < np.mean(result.values_w < 0) < 0.55
    theoretical_variance = (2e-11) ** 2 * (1 / 40 + 1 / 100)
    np.testing.assert_allclose(np.mean(result.standard_uncertainty_w ** 2),
                               theoretical_variance, rtol=.04)


@pytest.mark.parametrize("bad", [[float("nan"), 0], [0, float("inf")], [4000, 0], [-4000, 0], [[0, 1]]])
def test_unrepresentable_or_malformed_power_rejected(bad):
    with pytest.raises(ValueError):
        dbm_to_w(bad)


def test_welford_matches_independent_batch_moments():
    rows = np.random.default_rng(102).gamma(5, 2e-12, size=(200, 101))
    stats = VectorWelford(101)
    for row in rows:
        stats.add(row)
    np.testing.assert_allclose(stats.mean, rows.mean(axis=0), rtol=1e-14)
    np.testing.assert_allclose(stats.variance(), rows.var(axis=0, ddof=1), rtol=1e-14)


def test_snapshots_cannot_be_made_writable_or_changed_by_source():
    ctx = context()
    source = np.ones(101)
    bg = profile(ctx, mean=source)
    source.fill(2)
    assert np.all(bg.mean_w == 1)
    for vector in (bg.mean_w, ctx.frequencies_hz):
        with pytest.raises(ValueError):
            vector.setflags(write=True)


def test_profile_requires_matching_grid_and_physical_variance():
    ctx = context()
    with pytest.raises(ValueError):
        profile(ctx, mean_variance=np.full(101, -1e-22))
    with pytest.raises(ValueError):
        profile(ctx, mean=np.zeros(2))


def test_background_only_trains_on_completed_reference_frames():
    ctx = context()
    builder = BackgroundProfileBuilder(ctx, reference_state="off resonance", minimum_sweeps=3)
    with pytest.raises(ValueError):
        builder.add(envelope(ctx, 0, 0), np.ones(101))
    with pytest.raises(ValueError):
        builder.add(replace(envelope(ctx, 0, 0, role=SpectrumFrameRole.REFERENCE),
                            evidence=SweepEvidence.UNKNOWN), np.ones(101))
    with pytest.raises(ValueError):
        builder.finish()


def test_reference_variance_does_not_assume_independence_from_completion():
    for independent in (False, True):
        ctx = context(independent=independent)
        builder = BackgroundProfileBuilder(ctx, reference_state="off resonance", minimum_sweeps=3)
        rows = np.array([np.full(101, x * 1e-9) for x in (1, 2, 3)])
        for i, row in enumerate(rows):
            builder.add(envelope(ctx, i, i, role=SpectrumFrameRole.REFERENCE), row)
        result = builder.finish()
        np.testing.assert_allclose(result.mean_w, rows.mean(axis=0))
        assert (result.mean_variance_w2 is not None) == independent
        assert result.profile_id == result.content_hash


def test_signed_residual_keeps_negative_signal_and_preserves_narrow_peak():
    ctx = context()
    engine = processor(ctx)
    signal = np.zeros(101)
    signal[20] = -2e-10
    signal[55] = 3e-10
    engine.ingest(envelope(ctx, 0, 2), as_dbm(1e-9 + signal))
    result = engine.snapshot()
    np.testing.assert_allclose(result.values_w, signal, atol=2e-24)
    assert result.values_w[20] < 0
    assert result.values_w[55] > 0


def test_common_reference_uncertainty_does_not_shrink_with_signal_count():
    ctx = context()
    engine = processor(ctx)
    for i in range(80):
        engine.ingest(envelope(ctx, i, 2 + i * 0.1), as_dbm(np.full(101, 1.3e-9)))
    result = engine.snapshot()
    np.testing.assert_allclose(result.standard_uncertainty_w, np.full(101, 2e-11))
    assert result.profile_weights == (("reference", 1.0),)


def test_iid_block_uncertainty_matches_batch_signal_plus_common_reference():
    ctx = context()
    engine = processor(ctx)
    rows = 1e-9 + np.random.default_rng(24).gamma(4, 2e-11, size=(50, 101))
    for i, row in enumerate(rows):
        engine.ingest(envelope(ctx, i, 2 + i * .1), as_dbm(row))
    expected = np.sqrt(rows.var(axis=0, ddof=1) / 50 + 4e-22)
    np.testing.assert_allclose(engine.snapshot().standard_uncertainty_w, expected, rtol=1e-12)


@pytest.mark.parametrize("mode", [TemporalAverageMode.WINDOW, TemporalAverageMode.EMA_PREVIEW])
def test_preview_has_no_fabricated_confidence_interval(mode):
    ctx = context()
    engine = processor(ctx, mode)
    for i in range(4):
        engine.ingest(envelope(ctx, i, 2 + i), as_dbm(np.full(101, 1.2e-9)))
    assert engine.snapshot().standard_uncertainty_w is None


def test_unknown_correlation_and_drift_remain_unknown():
    ctx = context(independent=False)
    engine = processor(ctx)
    for i in range(4):
        engine.ingest(envelope(ctx, i, 2 + i), as_dbm(np.full(101, 1.2e-9)))
    assert engine.snapshot().standard_uncertainty_w is None
    engine.set_background_profile(profile(ctx))
    engine.ingest(envelope(ctx, 4, 6), as_dbm(np.full(101, 1.2e-9)))
    assert engine.snapshot().quality == CorrectionQuality.UNQUALIFIED
    assert engine.snapshot().standard_uncertainty_w is None


def test_ema_irregular_timing_and_first_sample_initialization():
    average = TemporalPowerAverage(2, TemporalAverageMode.EMA_PREVIEW, tau_s=2, window_frames=3)
    average.add([1e-9, -1e-9], 10, "ref")
    np.testing.assert_allclose(average.mean, [1e-9, -1e-9])
    average.add([3e-9, 1e-9], 10.5, "ref")
    expected = np.array([1e-9, -1e-9]) + (1 - np.exp(-.5 / 2)) * 2e-9
    np.testing.assert_allclose(average.mean, expected)
    assert average.weights() == (("ref", 1.0),)


def test_window_matches_batch_even_after_periodic_rebuild():
    average = TemporalPowerAverage(2, TemporalAverageMode.WINDOW, tau_s=1, window_frames=9)
    rows = np.random.default_rng(5).normal(0, 1e-9, size=(1100, 2))
    for i, row in enumerate(rows):
        average.add(row, i, "ref")
    np.testing.assert_allclose(average.mean, rows[-9:].mean(axis=0), atol=1e-23)
    assert average.count == 9
    assert len(average._ring) == 9
    assert average.first_time_s == 1091


def test_refresh_new_state_and_gap_reset_temporal_average():
    ctx = context()
    engine = processor(ctx)
    engine.ingest(envelope(ctx, 0, 2), as_dbm(np.full(101, 2e-9)))
    old = engine.snapshot()
    engine.ingest(envelope(ctx, 1, 3, segment="new-bias"), as_dbm(np.full(101, 3e-9)))
    assert engine.snapshot().count == 1
    engine.ingest(envelope(ctx, 2, 20, segment="new-bias"), as_dbm(np.full(101, 4e-9)))
    assert engine.snapshot().count == 1
    engine.set_background_profile(profile(ctx, name="new-reference"))
    assert engine.snapshot() is None
    assert old.count == 1
    np.testing.assert_allclose(old.values_w, 1e-9)


def test_stale_reference_ages_even_without_new_acquisition():
    ctx = context()
    engine = processor(ctx)
    engine.ingest(envelope(ctx, 0, 2), as_dbm(np.full(101, 1.2e-9)))
    assert engine.status_at(200) == CorrectionQuality.STALE
    assert engine.snapshot().quality == CorrectionQuality.STALE
    assert not engine.ingest(envelope(ctx, 1, 201), as_dbm(np.full(101, 1.2e-9)))
    assert engine.snapshot() is None


def test_unknown_repeat_counter_and_out_of_order_do_not_increase_count():
    ctx = context()
    engine = processor(ctx)
    row = as_dbm(np.full(101, 1.2e-9))
    original = replace(envelope(ctx, 0, 2), evidence=SweepEvidence.INSTRUMENT_COUNTER, sweep_id="12")
    assert engine.ingest(original, row)
    assert not engine.ingest(replace(original, frame_id=1, acquired_at_s=3), row)
    assert not engine.ingest(replace(envelope(ctx, 2, 4), evidence=SweepEvidence.UNKNOWN), row)
    assert not engine.ingest(original, row)
    assert engine.snapshot().count == 1
    assert engine.rejected_frames == 3


@pytest.mark.parametrize("host_id", ["host:opaque-token", "999", None])
@pytest.mark.parametrize("final_counter", ["40", "39", "41"])
def test_reference_mixed_completion_evidence_remembers_last_device_counter(host_id, final_counter):
    ctx = context()
    builder = BackgroundProfileBuilder(ctx, reference_state="control", minimum_sweeps=3)
    row = np.full(101, 1e-9)
    first = replace(envelope(ctx, 0, 0, role=SpectrumFrameRole.REFERENCE),
                    evidence=SweepEvidence.INSTRUMENT_COUNTER, sweep_id="40")
    host = replace(envelope(ctx, 1, 1, role=SpectrumFrameRole.REFERENCE), sweep_id=host_id)
    third = replace(envelope(ctx, 2, 2, role=SpectrumFrameRole.REFERENCE),
                    evidence=SweepEvidence.INSTRUMENT_COUNTER, sweep_id=final_counter)
    builder.add(first, row)
    builder.add(host, row)
    if final_counter != "41":
        with pytest.raises(ValueError, match="counter must increase"):
            builder.add(third, row * 100)
        assert builder.statistics.count == 2 and builder.last is host
        third = replace(third, sweep_id="41")
    builder.add(third, row)
    assert builder.finish().sweep_count == 3
    np.testing.assert_allclose(builder.finish().mean_w, row)


def test_realtime_counter_repetition_after_host_sweep_cannot_change_average():
    ctx = context()
    engine = processor(ctx)
    first = replace(envelope(ctx, 0, 2), evidence=SweepEvidence.INSTRUMENT_COUNTER, sweep_id="40")
    assert engine.ingest(first, as_dbm(np.full(101, 1.2e-9)))
    assert engine.ingest(replace(envelope(ctx, 1, 3), sweep_id="host:uuid"), as_dbm(np.full(101, 1.2e-9)))
    repeated = replace(envelope(ctx, 2, 4), evidence=SweepEvidence.INSTRUMENT_COUNTER, sweep_id="40")
    assert not engine.ingest(repeated, as_dbm(np.full(101, 100e-9)))
    assert engine.snapshot().count == 2
    assert engine.ingest(replace(repeated, sweep_id="41"), as_dbm(np.full(101, 1.2e-9)))
    assert engine.snapshot().count == 3
    np.testing.assert_allclose(engine.snapshot().values_w, 2e-10, rtol=1e-12)
    # Explicit new segment permits a reset of the device counter.
    assert engine.ingest(replace(repeated, frame_id=3, acquired_at_s=5, segment_id="new", sweep_id="0"),
                         as_dbm(np.full(101, 1.3e-9)))
    assert engine.snapshot().count == 1


def test_rejected_reference_values_do_not_consume_an_instrument_counter():
    ctx = context()
    builder = BackgroundProfileBuilder(ctx, reference_state="control", minimum_sweeps=2)
    first = replace(envelope(ctx, 0, 0, role=SpectrumFrameRole.REFERENCE),
                    evidence=SweepEvidence.INSTRUMENT_COUNTER, sweep_id="40")
    second = replace(first, frame_id=1, acquired_at_s=1, sweep_id="41")
    builder.add(first, np.full(101, 1e-9))
    with pytest.raises(ValueError, match="positive watts"):
        builder.add(second, np.full(101, -1e-9))
    builder.add(second, np.full(101, 1e-9))
    assert builder.finish().sweep_count == 2


def test_identical_values_from_proven_new_sweeps_are_not_duplicates():
    ctx = context()
    engine = processor(ctx)
    row = as_dbm(np.full(101, 1.2e-9))
    for i in range(4):
        assert engine.ingest(envelope(ctx, i, i + 2), row)
    assert engine.snapshot().count == 4


def test_wrong_context_and_generation_invalidate_without_using_old_profile():
    ctx = context()
    engine = processor(ctx)
    row = as_dbm(np.full(101, 1.2e-9))
    engine.ingest(envelope(ctx, 0, 2), row)
    assert not engine.ingest(replace(envelope(ctx, 1, 3), configuration_generation=1), row)
    assert engine.quality == CorrectionQuality.INCOMPATIBLE
    assert engine.snapshot() is None
    with pytest.raises(ValueError):
        engine.set_background_profile(profile(replace(ctx, configuration_fingerprint="different")))


def test_reference_workflow_and_transition_cannot_train_from_signal():
    ctx = context()
    engine = RealtimeSpectrumProcessor(ctx, CorrectionConfig(minimum_reference_sweeps=3))
    engine.begin_reference("off resonance", signal_free_qualified=True)
    assert not engine.ingest(envelope(ctx, 0, 0), as_dbm(np.full(101, 2e-9)))
    assert engine.calibration_count == 0
    for i in range(1, 4):
        assert engine.ingest(envelope(ctx, i, i, role=SpectrumFrameRole.REFERENCE),
                             as_dbm(np.full(101, 1e-9)))
    engine.finish_reference()
    assert engine.ingest(envelope(ctx, 4, 4), as_dbm(np.full(101, 1.1e-9)))
    assert not engine.ingest(envelope(ctx, 5, 5, role=SpectrumFrameRole.TRANSITION),
                             as_dbm(np.full(101, 1.1e-9)))
    assert engine.snapshot() is None


def test_memory_limit_rejects_excessive_window():
    with pytest.raises(ValueError, match="memory"):
        RealtimeSpectrumProcessor(context(10001), CorrectionConfig(
            average_mode=TemporalAverageMode.WINDOW, window_frames=1000,
            working_memory_limit_bytes=1024 * 1024,
        ))


def test_future_reference_cannot_be_used_causally():
    ctx = context()
    with pytest.raises(ValueError):
        causal_reference(profile(ctx, end=10), 5)


def test_final_interpolation_follows_bracket_times_and_uncertainty():
    ctx = context()
    first = profile(ctx, start=0, end=2, mean=np.full(101, 1e-9), name="first")
    second = profile(ctx, start=8, end=10, mean=np.full(101, 3e-9), name="second")
    result = bracketed_reference(first, second, 5)
    assert result.final
    np.testing.assert_allclose(result.mean_w, 2e-9)
    assert result.variance_w2 is None
    qualified = bracketed_reference(first, second, 5, independent_blocks_qualified=True,
                                    drift_variance_w2=np.full(101, 1e-22))
    np.testing.assert_allclose(qualified.variance_w2, 3e-22)
    assert qualified.profile_weights == (("first", .5), ("second", .5))


def test_overlapping_reference_blocks_and_extrapolation_rejected():
    ctx = context()
    first = profile(ctx, start=0, end=4, name="first")
    second = profile(ctx, start=3, end=6, name="second")
    with pytest.raises(ValueError):
        bracketed_reference(first, second, 3.5)
    with pytest.raises(ValueError):
        bracketed_reference(first, profile(ctx, start=8, end=10, name="second"), 20)


def test_injection_recovery_of_lorentzian_directly_under_emi():
    ctx = context(1001)
    x = np.linspace(-10, 10, 1001)
    emi = 1e-9 + 5e-9 * np.exp(-x**2 / .02)
    signal = 2e-10 / (1 + (x / .7)**2)
    engine = processor(ctx)
    engine.set_background_profile(profile(ctx, mean=emi),
                                  drift_variance_rate_w2_s=np.zeros(1001))
    for i in range(10):
        engine.ingest(envelope(ctx, i, 2 + .1 * i), as_dbm(emi + signal))
    result = engine.snapshot()
    np.testing.assert_allclose(result.values_w, signal, rtol=3e-13, atol=3e-24)
    assert np.argmax(result.values_w) == np.argmax(signal)
    np.testing.assert_allclose(np.trapezoid(result.values_w, x), np.trapezoid(signal, x), rtol=1e-13)
