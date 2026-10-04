"""Repeatable known-signal transfer check through the actual preview pipeline.

Run with: python -m tools.qualify_spectrum_preview --output report.json
This is a synthetic software qualification, never a laboratory calibration.
"""

import argparse
import json
from pathlib import Path

import numpy as np

from app.domain.spectrum_correction import BackgroundProfile, SpectrumAcquisitionContext
from app.spectrum.analysis import SpectrumAnalysisParameters
from app.spectrum.preview_processing import SpectrumPreviewProcessor
from app.spectrum.resonance_metrics import fit_linear_resonance, resonance_area, resonance_values


def preview_transfer_report(*, repetitions=12, seed=20261005):
    if type(repetitions) is not int or not 1 <= repetitions <= 256:
        raise ValueError("Choose 1 to 256 repetitions.")
    random = np.random.default_rng(seed)
    frequencies = np.linspace(400e6, 600e6, 1001)
    context = SpectrumAcquisitionContext(frequencies, "synthetic-preview-transfer-v1", settings_verified=True)
    results = {}
    for name, shape, width in (("narrow_gaussian", "gaussian", 2e6),
                               ("narrow_lorentzian", "lorentzian", 2e6),
                               ("broad_gaussian", "gaussian", 30e6)):
        amplitude, center = 50e-12, 500e6
        truth = resonance_values(frequencies, amplitude, center, width, shape=shape)
        area = resonance_area(amplitude, center, width, frequencies[0], frequencies[-1], shape=shape)
        methods = {key: [] for key in ("single_minus_background", "average_minus_background",
                                       "unprotected_display_filters", "protected_display_filters")}
        for _ in range(repetitions):
            ref = 1e-9 + random.normal(0, 2e-12, (32, len(frequencies)))
            profile = BackgroundProfile("synthetic-reference", context.context_id, ref.mean(axis=0),
                ref.var(axis=0, ddof=1), None, 32, 1., 10., "Known synthetic blank", True)
            signal = 1e-9 + truth + random.normal(0, 2e-12, (16, len(frequencies)))
            rows = tuple(tuple(10*np.log10(row)+30) for row in signal)
            for method, samples, protected, filters in (
                ("single_minus_background", 1, False, ()),
                ("average_minus_background", 16, False, ()),
                ("unprotected_display_filters", 16, False, ("narrow_reject", "denoise")),
                ("protected_display_filters", 16, True, ("narrow_reject", "denoise")),
            ):
                band = (center-max(20e6, 3*width), center+max(20e6, 3*width))
                parameters = SpectrumAnalysisParameters(temporal_average_frames=samples,
                    narrow_protected_regions_hz=(band,) if protected else ())
                cleaned, _stats = SpectrumPreviewProcessor().process(rows[-1], frequencies_hz=frequencies,
                    parameters=parameters, power_rows=rows, timestamps_s=tuple(range(20, 36)),
                    modes=("background", *filters), background_context=context, background_profile=profile)
                values = np.asarray(cleaned.values)
                noise_mask = np.abs(frequencies-center) > 3*width
                metrics = {"noise_rms_w": float(np.sqrt(np.mean((values[noise_mask]-truth[noise_mask])**2)))}
                try:
                    fit = fit_linear_resonance(frequencies, values, initial_center_hz=center,
                                               initial_fwhm_hz=width, shape=shape)
                    metrics.update(amplitude_relative_error=float((fit.amplitude_w-amplitude)/amplitude),
                                   fwhm_relative_error=float((fit.fwhm_hz-width)/width),
                                   area_relative_error=float((fit.finite_window_area_w_hz-area)/area))
                except ValueError:
                    metrics.update(amplitude_relative_error=None, fwhm_relative_error=None, area_relative_error=None)
                methods[method].append(metrics)
        summary = {}
        for method, rows in methods.items():
            summary[method] = {key: float(np.mean([row[key] for row in rows if row[key] is not None]))
                              if any(row[key] is not None for row in rows) else None for key in rows[0]}
            summary[method]["fit_failures"] = sum(row["amplitude_relative_error"] is None for row in rows)
        chosen = summary["protected_display_filters"]
        gates = chosen["fit_failures"] == 0 and all(
            chosen[key] is not None and abs(chosen[key]) < .03
            for key in ("amplitude_relative_error", "fwhm_relative_error", "area_relative_error"))
        results[name] = {"true_amplitude_w": amplitude, "true_fwhm_hz": width,
                         "methods": summary, "protected_signal_bias_gates_passed": gates}
    return {"algorithm": "preview-processing-v1", "scope": "Synthetic known-signal transfer; no hardware",
            "laboratory_qualified": False, "confidence_interval_qualified": False,
            "seed": seed, "repetitions": repetitions, "scenarios": results,
            "acceptance": "Mean amplitude, FWHM and finite-window area bias below 3% for protected high-SNR test signals."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=12)
    args = parser.parse_args()
    report = preview_transfer_report(repetitions=args.repetitions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    print(f"Saved synthetic preview transfer report: {args.output}")


if __name__ == "__main__":
    main()
