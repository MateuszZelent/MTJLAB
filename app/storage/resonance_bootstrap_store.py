"""Read closed raw REF/SIGNAL into bounded, equal-sized spectral block means."""

from dataclasses import asdict
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile

import h5py
import numpy as np

from app.domain.errors import ExecutionError
from app.spectrum.background_profile import BackgroundProfileBuilder
from app.spectrum.resonance_bootstrap import ResonanceBootstrapConfig, bootstrap_buffer_bytes, bootstrap_resonance_blocks
from app.spectrum.streaming_statistics import dbm_to_w
from app.spectrum.sweep_counter_guard import SweepCounterGuard
from .finalized_spectrum_store import file_sha256
from .spectrum_correction_codec import read_envelope, read_profile


def _profile(file, config):
    root = file.get("spectrum_processing_v1/profiles")
    if root is None or len(root) != 1:
        raise ExecutionError("Bootstrap source requires exactly one committed reference profile.")
    group = next(iter(root.values()))
    if group["frequency_hz"].size * 24 * 8 > config.working_memory_limit_bytes:
        raise ExecutionError("Bootstrap profile vectors exceed the memory budget.")
    return read_profile(group)


def _source_records(file, role, cancellation_check):
    allowed = {"completed"} if role == "reference" else {"completed", "aborted"}
    if file["run"].attrs.get("status") not in allowed or len(file["_pending"]):
        raise ExecutionError("Bootstrap requires cleanly closed raw REF/SIGNAL archives.")
    if file["run"].attrs.get("spectrum_correction_initial_interference_model_id", ""):
        raise ExecutionError("This bootstrap supports static REF subtraction; model uncertainty is unavailable.")
    count = 0
    for index in range(len(file["points"])):
        if cancellation_check:
            cancellation_check()
        point = file.get(f"points/{index}")
        if point is None or not point.attrs.get("complete", False):
            raise ExecutionError("Bootstrap source has an uncommitted or discontinuous checkpoint.")
        raw = file[f"spectra/{index}"]
        envelope = read_envelope(raw)
        if envelope is None or envelope.role.value != role:
            raise ExecutionError("Bootstrap sources require separate raw REF and SIGNAL roles.")
        accepted = json.loads(point["metadata_json"].asstr()[()]).get("quantitative_accepted")
        if type(accepted) is not bool:
            raise ExecutionError("Bootstrap source lacks an explicit contribution decision.")
        count += accepted
    return count


def _collect(file, role, context, profile, count, block_sweeps, discard_tail, cancellation_check):
    blocks, tail = divmod(count, block_sweeps)
    if tail and not discard_tail:
        raise ExecutionError("Source has a partial final block; explicitly allow tail discard or choose another block size.")
    if blocks < 2:
        raise ExecutionError("Bootstrap source has fewer than two complete blocks.")
    means = np.empty((blocks, context.frequencies_hz.size))
    accumulator = np.zeros(context.frequencies_hz.size)
    frame_ranges = []
    time_ranges = []
    ordinal = 0
    first_in_block = None
    start_in_block = None
    last = None
    guard = SweepCounterGuard()
    builder = (BackgroundProfileBuilder(context, reference_state=profile.reference_state, minimum_sweeps=2,
                                        signal_free_qualified=profile.signal_free_qualified) if role == "reference" else None)
    first, final = None, None
    for index in range(len(file["points"])):
        if cancellation_check:
            cancellation_check()
        point = file[f"points/{index}"]
        if not json.loads(point["metadata_json"].asstr()[()])["quantitative_accepted"]:
            continue
        raw = file[f"spectra/{index}"]
        envelope = read_envelope(raw)
        if not envelope.complete or envelope.context_id != context.context_id or envelope.configuration_generation != context.configuration_generation:
            raise ExecutionError("Accepted bootstrap raw lacks matching complete acquisition evidence.")
        if last is not None and (envelope.frame_id <= last.frame_id or envelope.acquired_at_s <= last.acquired_at_s
                                 or envelope.segment_id != last.segment_id):
            raise ExecutionError("Bootstrap source must be ordered within one physical segment.")
        if not guard.allows(envelope):
            raise ExecutionError("Bootstrap source repeats or reverses an instrument counter.")
        guard.record(envelope)
        last = envelope
        if not np.array_equal(raw["frequency_hz"][:], context.frequencies_hz):
            raise ExecutionError("Bootstrap raw frequency grid changed.")
        watts = dbm_to_w(raw["power_dbm"][:])
        if builder is not None:
            builder.add(envelope, watts)
        if first is None:
            first = envelope.started_at_s if envelope.started_at_s is not None else envelope.acquired_at_s
        final = envelope.acquired_at_s
        if ordinal < blocks * block_sweeps:
            if first_in_block is None:
                first_in_block = envelope.frame_id
                start_in_block = envelope.started_at_s if envelope.started_at_s is not None else envelope.acquired_at_s
            accumulator += watts
            if (ordinal + 1) % block_sweeps == 0:
                means[ordinal // block_sweeps] = accumulator / block_sweeps
                frame_ranges.append([first_in_block, envelope.frame_id])
                time_ranges.append([start_in_block, envelope.acquired_at_s])
                accumulator.fill(0)
                first_in_block = None
        ordinal += 1
    if ordinal != count or builder is not None and builder.finish().content_hash != profile.content_hash:
        raise ExecutionError("Bootstrap raw does not reproduce its source profile or contribution count.")
    return means, {"role": role, "accepted_sweeps": count, "used_sweeps": blocks * block_sweeps,
                   "discarded_tail_sweeps": tail, "source_frame_ranges": frame_ranges,
                   "source_time_ranges_s": time_ranges, "interval_s": [first, final]}


def analyze_spectral_archives(reference_source, signal_source, destination, *, block_sweeps,
                              discard_partial_tail, config, maximum_blocks, buffer_bytes, analyzer,
                              cancellation_check=None):
    """Shared closed-archive integrity checks and exclusive report publication.

    ``buffer_bytes`` preflights the selected analysis before block allocation;
    ``analyzer`` consumes those exact verified block means and frequency grid.
    """
    reference_source, signal_source, destination = map(lambda path: Path(path).resolve(),
                                                       (reference_source, signal_source, destination))
    if reference_source == signal_source or destination in (reference_source, signal_source) or destination.exists():
        raise ExecutionError("Bootstrap needs separate REF/SIGNAL and a new report destination.")
    if type(block_sweeps) is not int or not 1 <= block_sweeps <= 1000000 or type(discard_partial_tail) is not bool:
        raise ValueError("Choose a bounded integer block size and explicit tail policy.")
    paths = (reference_source, signal_source)
    hashes = {path: file_sha256(path, cancellation_check=cancellation_check) for path in paths}
    with ExitStack() as stack:
        reference, signal = [stack.enter_context(h5py.File(path, "r")) for path in paths]
        context, profile = _profile(reference, config)
        signal_context, signal_profile = _profile(signal, config)
        if context.context_id != signal_context.context_id or profile.content_hash != signal_profile.content_hash:
            raise ExecutionError("SIGNAL must use the same committed reference and acquisition context.")
        counts = [_source_records(file, role, cancellation_check) for file, role in
                  ((reference, "reference"), (signal, "signal"))]
        numbers = [count // block_sweeps for count in counts]
        estimate_bytes = buffer_bytes(*numbers, context.frequencies_hz.size)
        if max(numbers) > maximum_blocks or estimate_bytes > config.working_memory_limit_bytes:
            raise ExecutionError("Archive block matrices exceed the configured block/memory budget.")
        references, reference_manifest = _collect(reference, "reference", context, profile, counts[0], block_sweeps,
                                                   discard_partial_tail, cancellation_check)
        signals, signal_manifest = _collect(signal, "signal", signal_context, signal_profile, counts[1], block_sweeps,
                                             discard_partial_tail, cancellation_check)
        if reference_manifest["interval_s"][1] >= signal_manifest["interval_s"][0]:
            raise ExecutionError("Bootstrap REF must precede SIGNAL without temporal overlap.")
        report = analyzer(context.frequencies_hz, references, signals)
        report["source_run_metadata"] = [{"role": role,
            "simulation": json.loads(file["run/simulation_json"].asstr()[()]),
            "device_idn": json.loads(file["run/device_idn_json"].asstr()[()]),
            "settings_sha256": str(file["run"].attrs["settings_sha256"]),
        } for file, role in ((reference, "reference"), (signal, "signal"))]
        report["recorded_context_qualification"] = {"settings_verified": context.settings_verified,
            "independent_sweeps_qualified": context.independent_sweeps_qualified,
            "reference_signal_free_qualified": profile.signal_free_qualified}
    report.update(block_sweeps=block_sweeps, discard_partial_tail=discard_partial_tail,
                  source_manifests=[reference_manifest, signal_manifest],
                  source_sha256={str(path): digest for path, digest in hashes.items()}, profile_hash=profile.content_hash,
                  config=asdict(config))
    for path, digest in hashes.items():
        if file_sha256(path, cancellation_check=cancellation_check) != digest:
            raise ExecutionError("Bootstrap source changed during analysis.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".pending", dir=destination.parent)
    pending = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if cancellation_check:
            cancellation_check()
        os.link(pending, destination)
    finally:
        pending.unlink(missing_ok=True)
    return report


def bootstrap_resonance_archives(reference_source, signal_source, destination, *, block_sweeps,
                                 initial_center_hz, initial_fwhm_hz, shape="gaussian", discard_partial_tail=False,
                                 config=ResonanceBootstrapConfig(), cancellation_check=None, progress_callback=None):
    def analyzer(frequencies, references, signals):
        return bootstrap_resonance_blocks(frequencies, references, signals,
            initial_center_hz=initial_center_hz, initial_fwhm_hz=initial_fwhm_hz, shape=shape, config=config,
            cancellation_check=cancellation_check, progress_callback=progress_callback)

    return analyze_spectral_archives(reference_source, signal_source, destination, block_sweeps=block_sweeps,
        discard_partial_tail=discard_partial_tail, config=config, maximum_blocks=config.maximum_blocks,
        buffer_bytes=lambda refs, signals, points: bootstrap_buffer_bytes(refs, signals, points, config),
        analyzer=analyzer, cancellation_check=cancellation_check)
