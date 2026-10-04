"""Read REF raw checkpoints and export a separate, bounded diagnostic report."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile

import h5py
import numpy as np

from app.domain.errors import ExecutionError
from app.spectrum.reference_diagnostics import ReferenceDiagnosticConfig, diagnose_reference
from .finalized_spectrum_store import file_sha256
from .spectrum_correction_codec import read_envelope, read_profile


def diagnose_reference_archive(source, *, bin_indices=None, config=ReferenceDiagnosticConfig(),
                               destination=None, cancellation_check=None):
    """Only completed raw REF archives; never infer independence, CI or TTL."""
    path = Path(source).resolve()
    target = Path(destination).resolve() if destination is not None else None
    if target is not None and (target == path or target.exists()):
        raise ExecutionError("Diagnostic report requires a new destination separate from its source.")
    try:
        source_hash = file_sha256(path, cancellation_check=cancellation_check)
        with h5py.File(path, "r") as file:
            if file["run"].attrs.get("status") != "completed" or len(file["_pending"]):
                raise ExecutionError("Close and complete the reference acquisition before diagnostics.")
            root = file.get("spectrum_processing_v1/profiles")
            if root is None or len(root) != 1:
                raise ExecutionError("Reference diagnostics require exactly one committed profile.")
            group = next(iter(root.values()))
            points = group["frequency_hz"].shape[0]
            bound = points * 8 * 8 + config.maximum_blocks * (config.maximum_bins * 8 * 8 + 64)
            if bound > 64 * 1024 * 1024:
                raise ExecutionError("Reference diagnostic buffers exceed the 64 MiB resource budget.")
            context, profile = read_profile(group)
            if bin_indices is None:
                count = min(config.maximum_bins, points)
                uniform = np.linspace(0, points - 1, max(1, count - 1), dtype=int)
                bins = tuple(sorted(set(map(int, uniform)) | {int(np.argmax(profile.mean_w))}))
                # With a one-bin budget, use the maximum rather than two bins.
                bins = (int(np.argmax(profile.mean_w)),) if count == 1 else bins
                selection = "uniform_grid_plus_max_reference_power"
            else:
                bins = tuple(bin_indices)
                selection = "explicit_bin_indices"

            def frames():
                for index in range(len(file["points"])):
                    if cancellation_check is not None:
                        cancellation_check()
                    point = file.get(f"points/{index}")
                    if point is None or not point.attrs.get("complete", False):
                        raise ExecutionError("Reference diagnostic source contains an uncommitted checkpoint.")
                    raw = file[f"spectra/{index}"]
                    if raw["frequency_hz"].shape != (points,) or raw["power_dbm"].shape != (points,):
                        raise ExecutionError("Reference diagnostic raw vector dimensions differ from its profile.")
                    if not np.array_equal(raw["frequency_hz"][:], context.frequencies_hz):
                        raise ExecutionError("Reference diagnostic source frequency axis changed.")
                    envelope = read_envelope(raw)
                    if envelope is None:
                        raise ExecutionError("Diagnostics need raw sweep history, not an exported mean profile.")
                    powers_dbm = raw["power_dbm"][:]
                    yield envelope, powers_dbm

            diagnostics = diagnose_reference(context, frames(), bins, config,
                                              cancellation_check=cancellation_check, expected_profile=profile)
            report = {
                "schema": "reference-diagnostic-report-v1",
                "algorithm_version": diagnostics.algorithm_version,
                "source": {"path": str(path), "sha256": source_hash,
                           "profile_id": profile.profile_id, "profile_hash": profile.content_hash},
                "configuration_fingerprint": context.configuration_fingerprint,
                "context_id": diagnostics.context_id, "segment_id": diagnostics.segment_id,
                "configuration_generation": context.configuration_generation,
                "config": asdict(config), "selection": selection,
                "qualification": "diagnostic_only", "qualified_ttl_s": None,
                "raw_profile_verified": True,
                "sweep_independence_inferred": False,
                "notes": ["Allan variance and correlation describe time-block mean powers, not individual sweeps.",
                          "Overlapping pair counts are not independent sample counts or confidence intervals.",
                          "Final partial time bucket is excluded; missing buckets are never interpolated.",
                          "No detector/trace-mode/ENBW or signal-free qualification is inferred."],
                "units": {"frequencies_hz": "Hz", "block_times_s": "s (Unix UTC)", "block_mean_w": "W",
                          "allan_tau_s": "s", "allan_variance_w2": "W^2", "correlation_lag_s": "s",
                          "autocorrelation": "1"},
            }
            for name in ("bin_indices", "frequencies_hz", "block_indices", "block_times_s", "block_counts",
                         "block_mean_w", "source_frame_ranges", "total_sweeps", "discarded_tail_sweeps",
                         "cadence_valid", "issues", "allan_tau_s", "allan_pair_counts", "allan_variance_w2",
                         "correlation_lag_s", "autocorrelation", "correlation_valid_bins"):
                value = getattr(diagnostics, name)
                report[name] = value.tolist() if isinstance(value, np.ndarray) else value
        if file_sha256(path, cancellation_check=cancellation_check) != source_hash:
            raise ExecutionError("Reference diagnostic source changed during analysis.")
        if target is not None:
            if cancellation_check is not None:
                cancellation_check()
            text = json.dumps(report, indent=2, allow_nan=False) + "\n"
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, pending_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".pending",
                                                        dir=target.parent)
            pending = Path(pending_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                if cancellation_check is not None:
                    cancellation_check()
                # Atomic publication on the same filesystem, with no replacement
                # even if another process creates the destination during analysis.
                os.link(pending, target)
            finally:
                pending.unlink(missing_ok=True)
        return diagnostics, report
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ExecutionError(f"Cannot diagnose reference archive: {exc}") from exc
