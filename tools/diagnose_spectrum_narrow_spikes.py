"""Read-only REF residual example and bounded live-filter timing.

python -m tools.diagnose_spectrum_narrow_spikes REF.h5 --output REPORT.json
Uses first completed REF as the fixed earlier reference; all subsequent REF
are diagnostic dB ratios. This never establishes magnetic preservation.
"""

import argparse
import json
from pathlib import Path
import time

import h5py
import numpy as np

from app.spectrum.narrow_spikes import filter_narrow_spikes
from app.storage.finalized_spectrum_store import file_sha256
from app.storage.spectrum_correction_codec import read_envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-width", default="6 MHz")
    parser.add_argument("--threshold", type=float, default=6)
    args = parser.parse_args()
    from app.domain.quantities import DIMENSION_FREQUENCY, parse_quantity
    width_hz = parse_quantity(args.max_width, DIMENSION_FREQUENCY).si_value
    output = args.output.resolve()
    figure_path = output.with_suffix(".png")
    if output.exists() or figure_path.exists() or args.source.resolve() in (output, figure_path):
        raise ValueError("Choose new destinations separate from the source.")
    source_hash = file_sha256(args.source)
    traces, frame_ids = [], []
    with h5py.File(args.source, "r") as file:
        if file["run"].attrs["status"] != "completed" or len(file["_pending"]):
            raise ValueError("Completed committed REF required.")
        if len(file["points"]) < 3 or len(file["points"]) > 256:
            raise ValueError("Choose a bounded 3..256-sweep REF.")
        points = file["spectra/0/frequency_hz"].shape[0]
        if not 5 <= points <= 100_001 or points * len(file["points"]) * 8 > 64 * 1024 * 1024:
            raise ValueError("Diagnostic source exceeds the 64 MiB trace buffer budget.")
        for index in range(len(file["points"])):
            if not file[f"points/{index}"].attrs.get("complete", False):
                raise ValueError("Uncommitted checkpoint.")
            raw = file[f"spectra/{index}"]
            envelope = read_envelope(raw)
            if envelope is None or envelope.role.value != "reference" or not envelope.complete:
                raise ValueError("Completed raw REFERENCE evidence required.")
            grid = raw["frequency_hz"][:]
            if index == 0:
                frequencies = grid
                context_id = envelope.context_id
                first_reference = raw["power_dbm"][:]
            elif not np.array_equal(grid, frequencies) or envelope.context_id != context_id:
                raise ValueError("Acquisition context changed.")
            else:
                traces.append(raw["power_dbm"][:] - first_reference)
                frame_ids.append(envelope.frame_id)
    samples, results = [], []
    for _ in range(2):
        filter_narrow_spikes(frequencies, traces[0], maximum_width_hz=width_hz, threshold_sigma=args.threshold)
    for trace in traces:
        started = time.perf_counter()
        result = filter_narrow_spikes(frequencies, trace, maximum_width_hz=width_hz, threshold_sigma=args.threshold)
        samples.append(time.perf_counter() - started)
        results.append(result)
    if file_sha256(args.source) != source_hash:
        raise ValueError("Source changed during diagnostics.")
    # Fixed first subsequent REF, never chosen by cancellation quality.
    before, result = traces[0], results[0]
    report = {
        "schema": "narrow-spike-ref-diagnostic-v1", "source": str(args.source.resolve()),
        "source_sha256_unchanged": source_hash, "context_id": context_id,
        "source_role": "reference", "reference_definition": "first raw REF sweep; later REF minus it in dB",
        "qualification": "diagnostic_only", "signal_preservation_verified": False,
        "parameters": {"maximum_width_hz": width_hz, "threshold_sigma": args.threshold,
                       "protected_regions_hz": []},
        "points": len(frequencies), "evaluated_frames": len(results),
        "timing_s": {"median": float(np.median(samples)), "p95": float(np.quantile(samples, .95)),
                     "max": max(samples), "samples": samples, "includes_tuple_conversion": True},
        "frames": [{"frame_id": frame_id, "replaced_bins": len(r.modified_indices),
                    "narrow_extrema": len(r.peak_indices), "notes": r.notes}
                   for frame_id, r in zip(frame_ids, results)],
        "example": {"frame_id": frame_ids[0], "frequencies_hz": frequencies.tolist(),
                    "before_db": before.tolist(), "after_db": list(result.values),
                    "modified_bin_indices": result.modified_indices},
        "limitations": ["No physical SIGNAL was acquired for this comparison.",
            "Timing excludes GUI paint, VISA, storage and separately enabled peak fitting.",
            "Noise scale and width are heuristics; neighboring RBW bins may be correlated.",
            "A real narrow signal outside protection is also a candidate for removal."]
    }
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure, axes = plt.subplots(2, 1, figsize=(11, 7), layout="constrained", sharex=True)
    selected = (frequencies >= 200e6) & (frequencies <= 1700e6)
    x = frequencies[selected] / 1e9
    axes[0].plot(x, before[selected], color="#a830c5", linewidth=.8, label="REF frame - earlier REF (dB)")
    axes[1].plot(x, np.asarray(result.values)[selected], color="#156aab", linewidth=.8, label="Narrow-peak preview (dB)")
    for axis in axes:
        axis.legend(loc="upper right")
        axis.set_ylabel("Relative level (dB)")
        axis.grid(alpha=.2)
    axes[1].set_xlabel("Frequency (GHz)")
    figure.suptitle("Actual REF-only example: morphology filtering; magnetic preservation untested")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
    with figure_path.open("xb") as stream:
        figure.savefig(stream, format="png", dpi=160)
    plt.close(figure)
    print(json.dumps({"report": str(output), "points": len(frequencies), "frames": len(results),
                      "timing_s": {k: v for k, v in report["timing_s"].items() if k != "samples"},
                      "example_replaced_bins": len(result.modified_indices),
                      "source_unchanged": True, "qualification": "diagnostic_only"}))


if __name__ == "__main__":
    main()
