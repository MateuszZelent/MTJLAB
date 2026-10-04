"""Bracketed final blocks preserve signal and common reference uncertainty."""

from dataclasses import replace

import numpy as np
import pytest

from app.domain.spectrum_correction import (
    BackgroundProfile, CorrectionQuality, SpectrumAcquisitionContext,
    SpectrumFrameEnvelope, SpectrumFrameRole, SweepEvidence,
)
from app.spectrum.finalize import finalize_bracketed_block
from app.spectrum.reference_estimator import bracketed_reference


def fixture():
    context = SpectrumAcquisitionContext([1e6, 2e6, 3e6], "rms-fixture",
                                         settings_verified=True, independent_sweeps_qualified=True)
    before = BackgroundProfile("before", context.context_id, [1e-9, 2e-9, 3e-9],
                               [1e-20] * 3, [1e-22] * 3, 100, 0, 10, "off resonance", True)
    after = replace(before, profile_id="after", mean_w=before.mean_w * 1.4,
                    started_at_s=30, completed_at_s=40)
    signal = np.array([1e-10, -1e-10, 2e-10])
    frames = []
    for index, at_s in enumerate([15, 20, 25]):
        envelope = SpectrumFrameEnvelope(index, "signal", context.context_id, 0, at_s,
                                         evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
        raw_w = bracketed_reference(before, after, at_s).mean_w + signal
        frames.append((envelope, 10 * np.log10(raw_w) + 30))
    return context, before, after, signal, frames


def test_bracketed_linear_drift_preserves_signed_signal_and_reference_floor():
    context, before, after, signal, frames = fixture()
    block = finalize_bracketed_block(context, before, after, iter(frames),
                                    independent_blocks_qualified=True,
                                    interpolation_error_variance_w2=np.zeros(3))
    result = block.result
    assert result.final and result.quality == CorrectionQuality.READY
    assert result.profile_weights == (("before", 0.5), ("after", 0.5))
    assert block.source_frame_ids == (0, 1, 2)
    np.testing.assert_allclose(result.values_w, signal, rtol=1e-12)
    np.testing.assert_allclose(result.standard_uncertainty_w ** 2, [0.5e-22] * 3, rtol=1e-12)
    assert result.values_w[1] < 0
    assert not result.values_w.flags.writeable and not block.raw_mean_w.flags.writeable


def test_finalization_checks_cancellation_between_signal_frames():
    from app.domain.errors import ProcessingCancelled

    context, before, after, _, frames = fixture()
    checks = []

    def cancel():
        checks.append(True)
        if len(checks) == 2:
            raise ProcessingCancelled("cancelled between frames")

    with pytest.raises(ProcessingCancelled, match="between frames"):
        finalize_bracketed_block(context, before, after, frames, cancellation_check=cancel)
    assert len(checks) == 2


@pytest.mark.parametrize("counter", ["40", "39", "41"])
def test_finalization_counter_checks_survive_intervening_host_evidence(counter):
    context, before, after, signal, frames = fixture()
    frames[0] = replace(frames[0][0], evidence=SweepEvidence.INSTRUMENT_COUNTER, sweep_id="40"), frames[0][1]
    frames[1] = replace(frames[1][0], sweep_id="host:opaque"), frames[1][1]
    frames[2] = replace(frames[2][0], evidence=SweepEvidence.INSTRUMENT_COUNTER, sweep_id=counter), frames[2][1]
    if counter == "41":
        result = finalize_bracketed_block(context, before, after, frames).result
        np.testing.assert_allclose(result.values_w, signal, rtol=1e-12)
    else:
        with pytest.raises(ValueError, match="counter must increase"):
            finalize_bracketed_block(context, before, after, frames)


@pytest.mark.parametrize("missing", ["blocks", "drift", "signal-free", "sweep-independence", "variance", "readback"])
def test_final_does_not_imply_qualified_uncertainty(missing):
    context, before, after, _signal, frames = fixture()
    kwargs = dict(independent_blocks_qualified=True, interpolation_error_variance_w2=np.zeros(3))
    if missing == "blocks":
        kwargs["independent_blocks_qualified"] = False
    elif missing == "drift":
        kwargs.pop("interpolation_error_variance_w2")
    elif missing == "signal-free":
        after = replace(after, signal_free_qualified=False)
    elif missing == "sweep-independence":
        context = replace(context, independent_sweeps_qualified=False)
    elif missing == "readback":
        context = replace(context, settings_verified=False)
    else:
        before = replace(before, mean_variance_w2=None)
    result = finalize_bracketed_block(context, before, after, frames, **kwargs).result
    assert result.final and result.quality == CorrectionQuality.UNQUALIFIED
    assert result.standard_uncertainty_w is None


@pytest.mark.parametrize("invalid", ["unknown", "transition", "gap", "segment", "generation", "order", "outside"])
def test_invalid_raw_block_cannot_be_finalized(invalid):
    context, before, after, _signal, frames = fixture()
    envelope, raw = frames[1]
    changes = {
        "unknown": {"evidence": SweepEvidence.UNKNOWN},
        "transition": {"role": SpectrumFrameRole.TRANSITION},
        "gap": {"acquired_at_s": 22},
        "segment": {"segment_id": "different"},
        "generation": {"configuration_generation": 1},
        "order": {"frame_id": 0},
        "outside": {"acquired_at_s": 31},
    }
    frames[1] = replace(envelope, **changes[invalid]), raw
    with pytest.raises(ValueError):
        finalize_bracketed_block(context, before, after, frames)
