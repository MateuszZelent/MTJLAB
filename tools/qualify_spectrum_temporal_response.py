"""Measure signed amplitude-step response through the real spectrum correction core."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path

import numpy as np

from app.domain.spectrum_correction import (
    CorrectionConfig, SpectrumAcquisitionContext, SpectrumFrameEnvelope, SpectrumFrameRole,
    SweepEvidence, TemporalAverageMode,
)
from app.spectrum.background_profile import BackgroundProfileBuilder
from app.spectrum.realtime_processor import RealtimeSpectrumProcessor
from app.spectrum.resonance_metrics import resonance_values
from app.spectrum.streaming_statistics import dbm_to_w


def expected_step_response(times_s, step_index, mode, *, tau_s, window_frames):
    """Closed-form filter responses, measured from the last pre-step sample."""
    times = np.asarray(times_s, dtype=float)
    if (times.ndim != 1 or not np.all(np.isfinite(times)) or np.any(np.diff(times) <= 0)
            or type(step_index) is not int or not 1 <= step_index < times.size
            or not np.isfinite(tau_s) or tau_s <= 0 or type(window_frames) is not int or window_frames < 1):
        raise ValueError("Response requires ordered finite times, an internal step and positive filter parameters.")
    mode = TemporalAverageMode(mode)
    index = np.arange(times.size)
    post = np.maximum(0, index - step_index + 1)
    if mode == TemporalAverageMode.BLOCK:
        return post / (index + 1)
    if mode == TemporalAverageMode.WINDOW:
        return np.minimum(post, np.minimum(index + 1, window_frames)) / np.minimum(index + 1, window_frames)
    return np.where(index < step_index, 0., -np.expm1(-(times - times[step_index - 1]) / tau_s))


def first_crossing_delay(times, fraction, step_index, level=1 - np.exp(-1)):
    crossed = np.flatnonzero(np.asarray(fraction)[step_index:] >= level)
    return None if not crossed.size else float(times[step_index + crossed[0]] - times[step_index - 1])


def temporal_response_report(*, repetitions=100, points=101, seed=20261013, noise=True):
    for name, value, low, high in (("repetitions", repetitions, 1, 1000), ("points", points, 101, 10001),
                                  ("seed", seed, 0, 2**64 - 1)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Temporal {name} must be an integer in {low}..{high}.")
    if type(noise) is not bool:
        raise ValueError("Noise must be an explicit boolean.")
    frequencies = np.linspace(1e6, 2e6, points)
    shape = resonance_values(frequencies, 1, 1.500123e6, 100000.)
    projection = shape / (shape @ shape)
    context = SpectrumAcquisitionContext(frequencies, "synthetic-temporal-response")
    step_index, frames = 40, 240
    scenarios = {}
    for scenario_index, (name, amplitude, jitter) in enumerate((
        ("positive_regular", 1e-10, False), ("negative_regular", -1e-10, False),
        ("positive_irregular", 1e-10, True),
    )):
        rows = {mode.value: [] for mode in TemporalAverageMode}
        example = None
        rejected = 0
        for trial in range(repetitions):
            random = np.random.default_rng(np.random.SeedSequence([seed, scenario_index, trial]))

            def background():
                if not noise:
                    return np.full(points, 1e-9)
                power = random.gamma(16, 2e-13 / 16, points)
                return 1e-9 + .25 * np.roll(power, 1) + .5 * power + .25 * np.roll(power, -1)

            builder = BackgroundProfileBuilder(context, reference_state="synthetic signal-free REF", minimum_sweeps=2)
            for index in range(40):
                envelope = SpectrumFrameEnvelope(index, "ref", context.context_id, 0, 1 + index * .05,
                    role=SpectrumFrameRole.REFERENCE, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
                builder.add(envelope, dbm_to_w(10 * np.log10(background()) + 30))
            profile = builder.finish()
            configs = {mode: CorrectionConfig(average_mode=mode, time_constant_s=1., window_frames=32)
                       for mode in TemporalAverageMode}
            processors = {mode: RealtimeSpectrumProcessor(context, config) for mode, config in configs.items()}
            for processor in processors.values():
                processor.set_background_profile(profile)
            cadence = random.uniform(.03, .07, frames) if jitter else np.full(frames, .05)
            times = 4 + np.cumsum(cadence)
            measured = {mode: [] for mode in processors}
            for index, at_s in enumerate(times):
                power = background() + (amplitude * shape if index >= step_index else 0)
                dbm = 10 * np.log10(power) + 30
                envelope = SpectrumFrameEnvelope(40 + index, "signal", context.context_id, 0, float(at_s),
                    evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
                for mode, processor in processors.items():
                    if not processor.ingest(envelope, dbm):
                        rejected += 1
                        measured[mode].append(None)
                    else:
                        measured[mode].append(float(processor.snapshot().values_w @ projection) / amplitude)
            curves = {}
            for mode, values in measured.items():
                expected = expected_step_response(times, step_index, mode, tau_s=1., window_frames=32)
                if any(value is None for value in values):
                    rows[mode.value].append({"status": "rejected_frame", "maximum_fraction_error": None})
                    continue
                array = np.array(values)
                rows[mode.value].append({"status": "ok", "maximum_fraction_error": float(np.max(np.abs(array - expected))),
                    "rms_fraction_error": float(np.sqrt(np.mean((array - expected)**2))),
                    "observed_632_delay_s": first_crossing_delay(times, array, step_index),
                    "expected_632_delay_s": first_crossing_delay(times, expected, step_index)})
                curves[mode.value] = {"expected_fraction": expected.tolist(), "observed_fraction": values,
                                      "config": asdict(configs[mode])}
            if example is None:
                example = {"times_s": times.tolist(), "step_last_pre_sample_at_s": float(times[step_index - 1]),
                           "first_post_step_sample_at_s": float(times[step_index]), "curves": curves}
        summary = {}
        for mode, trials in rows.items():
            good = [row for row in trials if row["status"] == "ok"]
            summary[mode] = {"successful_trials": len(good), "failed_trials": repetitions - len(good),
                "mean_rms_fraction_error": float(np.mean([row["rms_fraction_error"] for row in good])) if good else None,
                "maximum_fraction_error": max((row["maximum_fraction_error"] for row in good), default=None),
                "trials": trials}
        scenarios[name] = {"step_amplitude_w": amplitude, "irregular_cadence": jitter,
                           "rejected_frames": rejected, "summary": summary, "example": example}
    return {"algorithm": "signed-spectral-step-response-v1", "seed": seed, "numpy_version": np.__version__,
            "repetitions": repetitions, "points": points, "step_index": step_index, "signal_frames": frames,
            "reference_sweeps": 40, "noise_enabled": noise, "gamma_shape": 16, "noise_mean_w": 2e-13,
            "frequency_noise_kernel": [.25, .5, .25], "scenarios": scenarios, "laboratory_qualified": False,
            "limitations": ["Synthetic amplitude projection on one known Gaussian; no automatic peak detection",
                            "Delay measured from last pre-step sample, not hardware settling or wall-clock latency",
                            "No confidence interval or independent-sweep qualification inferred",
                            "BLOCK intentionally dilutes a step over the entire retained block",
                            "A frame-count WINDOW has cadence-dependent time response; EMA uses actual sample times"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--points", type=int, default=101)
    parser.add_argument("--seed", type=int, default=20261013)
    parser.add_argument("--no-noise", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.exit(1, "Choose a new report destination.\n")
    try:
        report = temporal_response_report(repetitions=args.repetitions, points=args.points, seed=args.seed,
                                          noise=not args.no_noise)
        serialized = json.dumps(report, indent=2, allow_nan=False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Saved {args.output}; laboratory qualification remains false.")


if __name__ == "__main__":
    main()
