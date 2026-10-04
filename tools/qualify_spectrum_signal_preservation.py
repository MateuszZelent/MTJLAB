"""Synthetic high-SNR signal-transfer measurements through the real correction core."""

from dataclasses import asdict
import argparse
import json
from pathlib import Path

import numpy as np

from app.domain.spectrum_correction import (
    CorrectionConfig, SpectrumAcquisitionContext, SpectrumFrameEnvelope, SpectrumFrameRole,
    SweepEvidence, TemporalAverageMode,
)
from app.spectrum.background_profile import BackgroundProfileBuilder
from app.spectrum.interference_training import InterferenceTrainingConfig, train_interference_basis
from app.spectrum.realtime_processor import RealtimeSpectrumProcessor
from app.spectrum.resonance_metrics import fit_linear_resonance, resonance_area, resonance_values
from app.spectrum.streaming_statistics import dbm_to_w

SCENARIOS = ("gaussian_positive", "lorentzian_positive", "gaussian_negative", "overlap_model",
             "overlap_calibrated_range", "overlap_insufficient_range", "coherent_interference",
             "broad_gaussian", "weak_gaussian", "reference_contaminated", "shifted_reference_resonance",
             "lorentzian_negative")


def _noise(random, size, hardware_averages):
    values = random.gamma(hardware_averages, 2e-13 / hardware_averages, size=size)
    # Positive, correlated neighboring-bin power. This is a declared synthetic
    # response, not a claim about a physical instrument's RBW kernel.
    return .25 * np.roll(values, 1) + .5 * values + .25 * np.roll(values, -1)


def signal_preservation_report(*, repetitions=100, points=1001, seed=20261004,
                               hardware_averages=16, scenarios=SCENARIOS):
    if type(repetitions) is not int or not 1 <= repetitions <= 10000 or type(points) is not int or not 1001 <= points <= 10001:
        raise ValueError("Qualification requires 1..10000 repetitions and 1001..10001 bins.")
    if type(hardware_averages) is not int or not 1 <= hardware_averages <= 1024:
        raise ValueError("Hardware power averages must be an integer in 1..1024.")
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ValueError("Generator seed must be an unsigned 64-bit integer.")
    if not scenarios or any(name not in SCENARIOS for name in scenarios) or len(set(scenarios)) != len(scenarios):
        raise ValueError("Choose unique supported synthetic scenarios.")
    random = np.random.default_rng(seed)
    frequencies = np.linspace(1e6, 2e6, points)
    center, width = 1.500123e6, 16000.0
    step = frequencies[1] - frequencies[0]
    context = SpectrumAcquisitionContext(frequencies, "synthetic-gamma-correlated-power")
    emi = resonance_values(frequencies, 1e-9, center, 100000, shape="gaussian")
    nuisance = np.abs(frequencies - center) < 200000
    protected = np.abs(frequencies - center) < 40000
    controls = np.ones(points, dtype=bool)
    floor = np.full(points, 1e-9)
    import scipy

    report = {"scope": "Synthetic gamma-power recovery and declared failure scenarios; no VISA",
              "generator_algorithm": "gamma-correlated-power-v2", "fit_algorithm": "normalized-resonance-lsq-v1",
              "scipy_version": scipy.__version__,
              "seed": seed, "numpy_version": np.__version__, "points": points, "repetitions": repetitions,
              "hardware_averages": hardware_averages, "reference_sweeps": 40, "signal_sweeps": 16,
              "frequency_grid_step_hz": step, "true_center_hz": center, "true_fwhm_hz": width,
              "samples_per_fwhm": width / step, "noise_mean_w": 2e-13,
              "frequency_noise_kernel": [.25, .5, .25], "scenarios": {},
              "units": {"amplitude": "W", "center": "Hz", "fwhm": "Hz", "finite_window_area": "W*Hz", "rmse": "W"},
              "coverage_95": None, "false_detection_rate": None, "laboratory_qualified": False,
              "limitations": ["Known single-resonance hypotheses; not an automatic peak detector",
                              "No CI coverage or false-alarm gate qualified by this report",
                              "No physical RBW, hardware averaging or coherent subtraction qualification",
                              "Coherent scenario is deliberately outside the additive power model"]}
    for name in scenarios:
        overlapping = name in {"overlap_model", "overlap_calibrated_range", "overlap_insufficient_range"}
        shape = "lorentzian" if name.startswith("lorentzian_") else "gaussian"
        amplitude = -1e-10 if name.endswith("_negative") else 1e-10
        if name == "weak_gaussian":
            amplitude = 1e-13
        scenario_width = 160000. if name == "broad_gaussian" else width
        signal = resonance_values(frequencies, amplitude, center, scenario_width, shape=shape)
        area = resonance_area(amplitude, center, scenario_width, frequencies[0], frequencies[-1], shape=shape)
        reference_signal = (signal if name == "reference_contaminated" else
                            resonance_values(frequencies, amplitude, center + 2 * scenario_width, scenario_width)
                            if name == "shifted_reference_resonance" else None)
        metrics = {"static_reference": [], "selected_correction": []}
        failures = {key: 0 for key in metrics}
        rejected_frames = 0
        for trial in range(repetitions):
            builder = BackgroundProfileBuilder(context,
                reference_state=("synthetic REF containing target resonance" if reference_signal is not None
                                 else "synthetic signal-free REF"), minimum_sweeps=2)
            reference_rows = []
            for index in range(40):
                powers = floor + _noise(random, points, hardware_averages)
                if reference_signal is not None:
                    powers = powers + reference_signal
                if overlapping:
                    coefficient = ([-.5, .5][index] if name == "overlap_calibrated_range" and index < 2
                                   else random.uniform(-.5, .5))
                    if name == "overlap_insufficient_range":
                        coefficient *= .2
                    powers = powers + emi * (1 + coefficient)
                envelope = SpectrumFrameEnvelope(index, f"ref-{trial}", context.context_id, 0, 1 + index * .05,
                                                 role=SpectrumFrameRole.REFERENCE, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
                raw = dbm_to_w(10 * np.log10(powers) + 30)
                builder.add(envelope, raw)
                reference_rows.append((envelope, raw))
            profile = builder.finish()
            static = RealtimeSpectrumProcessor(context, CorrectionConfig(average_mode=TemporalAverageMode.BLOCK))
            selected = RealtimeSpectrumProcessor(context, CorrectionConfig(average_mode=TemporalAverageMode.BLOCK))
            static.set_background_profile(profile)
            selected.set_background_profile(profile)
            if overlapping:
                calibration = train_interference_basis(
                    context, profile, lambda: iter(reference_rows), model_id="synthetic-local-emi",
                    nuisance_mask=nuisance, control_mask=controls, protected_mask=protected,
                    control_sigma_w=np.full(points, 1e-13),
                    config=InterferenceTrainingConfig(components=1, maximum_training_frames=32),
                    signal_control_regions_qualified=True, qualification_evidence="known synthetic signal support only",
                )
                selected.set_interference_calibration(calibration)
            for index in range(16):
                powers = floor + _noise(random, points, hardware_averages) + signal
                if overlapping:
                    powers = powers + emi * (1.3 + random.uniform(-.03, .03))
                elif name == "coherent_interference":
                    # |sqrt(b) + exp(i phi) sqrt(s)|², with fixed coherent phase.
                    powers = np.abs(np.sqrt(floor) + np.exp(.4j) * np.sqrt(signal))**2 + _noise(random, points, hardware_averages)
                envelope = SpectrumFrameEnvelope(40 + index, f"signal-{trial}", context.context_id, 0, 4 + index * .05,
                                                 evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
                raw_dbm = 10 * np.log10(powers) + 30
                assert static.ingest(envelope, raw_dbm)
                rejected_frames += not selected.ingest(envelope, raw_dbm)
            for method, processor in (("static_reference", static), ("selected_correction", selected)):
                result = processor.snapshot()
                if result is None or result.count != 16:
                    failures[method] += 1
                    continue
                try:
                    fit = fit_linear_resonance(frequencies, result.values_w, initial_center_hz=center,
                                               initial_fwhm_hz=scenario_width, shape=shape)
                except ValueError:
                    failures[method] += 1
                    continue
                metrics[method].append({"amplitude_relative_error": (fit.amplitude_w - amplitude) / abs(amplitude),
                                        "center_error_grid_steps": (fit.center_hz - center) / step,
                                        "fwhm_relative_error": (fit.fwhm_hz - scenario_width) / scenario_width,
                                        "area_relative_error": (fit.finite_window_area_w_hz - area) / abs(area),
                                        "spectrum_rmse_w": float(np.linalg.norm(result.values_w - signal) / np.sqrt(points)),
                                        "fit": asdict(fit)})
        summary = {}
        for method, rows in metrics.items():
            summary[method] = {"successful_repetitions": len(rows), "failed_repetitions": failures[method]}
            for key in ("amplitude_relative_error", "center_error_grid_steps", "fwhm_relative_error", "area_relative_error", "spectrum_rmse_w"):
                values = [row[key] for row in rows]
                summary[method][key] = None if not values else {"mean": float(np.mean(values)),
                                                              "p95_absolute": float(np.percentile(np.abs(values), 95))}
        values = summary["selected_correction"]
        applicable = name not in {"coherent_interference", "weak_gaussian", "reference_contaminated",
                                   "shifted_reference_resonance"}
        eligible = applicable and failures["selected_correction"] == 0 and rejected_frames == 0
        gates = {key: bool(eligible and abs(values[key]["mean"]) <= limit) for key, limit in (
            ("amplitude_relative_error", .01), ("area_relative_error", .01),
            ("fwhm_relative_error", .02), ("center_error_grid_steps", .1),
        )}
        report["scenarios"][name] = {"shape": shape, "true_amplitude_w": amplitude, "true_area_w_hz": area,
                                   "true_fwhm_hz": scenario_width, "samples_per_fwhm": scenario_width / step,
                                   "reference_contains_target": reference_signal is not None,
                                   "reference_resonance_center_hz": (center + 2 * scenario_width
                                       if name == "shifted_reference_resonance" else center
                                       if name == "reference_contaminated" else None),
                                   "high_snr_bias_gates_applicable": applicable,
                                   "interpretation": ("difference_of_states; REF contains the desired resonance"
                                       if reference_signal is not None else "low_SNR; no high-SNR accuracy qualification"
                                       if name == "weak_gaussian" else "outside additive power model"
                                       if name == "coherent_interference" else "synthetic additive signal recovery"),
                                   "emi_reference_sampling": ("anchored extremes and random interior"
                                       if name == "overlap_calibrated_range" else "insufficient narrow interior"
                                       if name == "overlap_insufficient_range" else "random interior only" if overlapping else None),
                                   "methods": summary, "rejected_signal_frames": rejected_frames,
                                   "additive_power_model_applicable": name != "coherent_interference",
                                   "high_snr_bias_gates": gates, "trial_metrics": metrics,
                                   "bias_gates_passed": all(gates.values())}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--points", type=int, default=1001)
    parser.add_argument("--seed", type=int, default=20261004)
    parser.add_argument("--hardware-averages", type=int, default=16)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=SCENARIOS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.exit(1, "Choose a new report path.\n")
    report = signal_preservation_report(repetitions=args.repetitions, points=args.points,
        seed=args.seed, hardware_averages=args.hardware_averages, scenarios=args.scenarios)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({name: value["bias_gates_passed"] for name, value in report["scenarios"].items()}, indent=2))


if __name__ == "__main__":
    main()
