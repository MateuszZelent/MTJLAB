"""Read-only source finalization into an exclusive, self-contained HDF5 artifact."""

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import h5py
import numpy as np

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.models import MeasurementPoint
from app.domain.spectrum_correction import SpectrumAcquisitionContext
from app.domain.spectrum_interleaved import RecordedInterleavedSignalBlock
from app.spectrum.finalize import finalize_bracketed_block
from app.spectrum.background_profile import BackgroundProfileBuilder
from app.spectrum.streaming_statistics import dbm_to_w
from .hdf5_writer import Hdf5RunWriter
from .spectrum_correction_codec import read_envelope, read_profile, read_selected_profile
from .spectrum_decision_store import iter_decisions, read_decision_context


def file_sha256(path, *, cancellation_check=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if cancellation_check is not None:
                cancellation_check()
            digest.update(chunk)
    return digest.hexdigest()


def _interleaved_reference_history(file, *, cancellation_check=None):
    """Completed block boundaries, even when a later acquisition was aborted."""
    run = file["run"]
    if (run.attrs.get("spectrum_correction_acquisition_mode") != "operator-interleaved-v1"
            or run.attrs.get("spectrum_processing_decision_schema") != "spectrum-processing-decisions-v1"
            or run.attrs.get("status") not in {"completed", "aborted", "incomplete", "faulted"}):
        raise ExecutionError("A closed interleaved archive and intact decision history are required.")
    context, config = read_decision_context(file)
    bounds, active = {}, None
    for record in iter_decisions(file):
        if cancellation_check is not None:
            cancellation_check()
        operation, params, boundary = record["operation"], record["parameters"], record["before_point_index"]
        if operation in {"initialize", "begin_reference"}:
            if active is not None or (operation == "initialize" and params.get("profile_id") is not None):
                raise ExecutionError("Interleaved references must have explicit, nonoverlapping acquisition boundaries.")
            state, qualified = params.get("reference_state"), params.get("signal_free_qualified")
            if type(state) is not str or not state.strip() or len(state) > 32768 or qualified is not False:
                raise ExecutionError("Interleaved operator reports cannot imply signal-free qualification.")
            active = (boundary, state)
        elif operation == "finish_reference":
            if (active is None or set(params) != {"profile_id", "profile_hash"}
                    or type(params["profile_id"]) is not str or type(params["profile_hash"]) is not str
                    or boundary - active[0] < config.minimum_reference_sweeps
                    or params["profile_id"] in bounds or len(bounds) >= 256):
                raise ExecutionError("Interleaved REF completion history is inconsistent.")
            bounds[params["profile_id"]] = (*active, boundary, params["profile_hash"])
            active = None
        elif operation not in {"reset_segment", "status_at"} or (active is not None and operation == "reset_segment"):
            raise ExecutionError("Unexpected processing operation in the operator-interleaved protocol.")
    return context, config, bounds


def _profile(file, profile_id=None, *, cancellation_check=None):
    interleaved = file["run"].attrs.get("spectrum_correction_acquisition_mode") == "operator-interleaved-v1"
    if not interleaved and file["run"].attrs.get("status") != "completed":
        raise ExecutionError("Finalization requires a completed reference archive.")
    profiles = file.get("spectrum_processing_v1/profiles")
    context, profile = read_selected_profile(profiles, profile_id)
    if interleaved:
        history_context, config, bounds = _interleaved_reference_history(file, cancellation_check=cancellation_check)
        if context.context_id != history_context.context_id or profile.profile_id not in bounds:
            raise ExecutionError("Selected REF has no matching committed completion decision.")
        start, state, stop, identity = bounds[profile.profile_id]
        if identity != profile.content_hash:
            raise ExecutionError("Selected REF differs from its committed processing decision.")
        builder = BackgroundProfileBuilder(context, reference_state=state,
            minimum_sweeps=config.minimum_reference_sweeps, signal_free_qualified=False)
        for envelope, dbm in _frames(file, range(start, stop), context, cancellation_check=cancellation_check):
            builder.add(envelope, dbm_to_w(dbm))
        if builder.finish().content_hash != profile.content_hash:
            raise ExecutionError("Selected completed REF cannot be reproduced from its raw sweeps.")
    return context, profile


def inspect_finalization_sources(paths, *, cancellation_check=None):
    """Read bounded validated profile summaries off the GUI thread."""
    if len(paths) != 3:
        raise ExecutionError("Choose SIGNAL, REF before and REF after sources.")
    summaries = []
    try:
        for position, path in enumerate(paths):
            if cancellation_check is not None:
                cancellation_check()
            with h5py.File(path, "r") as file:
                status = file["run"].attrs.get("status")
                interleaved = file["run"].attrs.get("spectrum_correction_acquisition_mode") == "operator-interleaved-v1"
                allowed = {"completed", "aborted", "incomplete", "faulted"} if position == 0 or interleaved else {"completed"}
                if status not in allowed:
                    raise ExecutionError("Close SIGNAL and complete both REF sources before selecting profiles.")
                root = file.get("spectrum_processing_v1/profiles")
                if root is None or not 1 <= len(root) <= 256:
                    raise ExecutionError("Profile selection requires 1..256 committed profiles per source.")
                choices = []
                history = _interleaved_reference_history(file, cancellation_check=cancellation_check)[2] if interleaved else None
                for key in root:
                    if cancellation_check is not None:
                        cancellation_check()
                    # Eight full vectors reserve <=64 MiB while decoding one
                    # profile. Never retain full arrays across profile choices.
                    record = root[key]
                    if not isinstance(record, h5py.Group):
                        raise ExecutionError("Each profile selection record must be a committed HDF5 group.")
                    axis = record.get("frequency_hz")
                    if not isinstance(axis, h5py.Dataset) or len(axis.shape) != 1 or axis.shape[0] > 1048576:
                        raise ExecutionError("Profile selection exceeds the bounded vector memory budget.")
                    for name in ("frequency_hz", "mean_w", "sample_variance_w2", "mean_variance_w2"):
                        if name not in record and name == "mean_variance_w2":
                            continue
                        dataset = record.get(name)
                        if not isinstance(dataset, h5py.Dataset) or dataset.shape != axis.shape or dataset.dtype.kind not in "fiu":
                            raise ExecutionError("Profile selection requires bounded numeric vectors on one frequency grid.")
                    context, profile = read_selected_profile(root, key)
                    if history is not None:
                        if profile.profile_id not in history:
                            # A crash between profile commit and completion
                            # decision leaves an orphan, not a usable REF.
                            del context, profile
                            continue
                        if history[profile.profile_id][3] != profile.content_hash:
                            raise ExecutionError("A selected interleaved REF differs from its completion decision.")
                    choices.append((profile.profile_id, profile.reference_state,
                        profile.sweep_count, profile.started_at_s, profile.completed_at_s,
                        context.context_id))
                    del context, profile
                if not choices:
                    raise ExecutionError("This source has no REF profile with a committed completion boundary.")
                summaries.append((Path(path).resolve(), tuple(choices), len(file["points"])))
        return tuple(summaries)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ExecutionError(f"Cannot inspect finalization sources: {exc}") from exc


def inspect_interleaved_blocks(path, *, cancellation_check=None):
    """Return metadata-only SIGNAL selections with their recorded REF neighbours."""
    try:
        with h5py.File(path, "r") as file:
            context, config, bounds = _interleaved_reference_history(file, cancellation_check=cancellation_check)
            profile_at = {}
            for identity, (start, _state, stop, _hash) in bounds.items():
                profile_at[stop] = identity
            blocks, active_profile, current = [], None, None
            for index in range(len(file["points"])):
                if cancellation_check is not None:
                    cancellation_check()
                if index in profile_at:
                    active_profile = profile_at[index]
                    if blocks and blocks[-1]["after_profile_id"] is None:
                        blocks[-1]["after_profile_id"] = active_profile
                raw = file[f"spectra/{index}"]
                if not file[f"points/{index}"].attrs.get("complete", False):
                    raise ExecutionError("Interleaved selection contains an incomplete raw checkpoint.")
                envelope = read_envelope(raw)
                if envelope is None:
                    raise ExecutionError("Interleaved selection requires compatible complete raw acquisition evidence.")
                if (not envelope.complete or envelope.context_id != context.context_id
                        or envelope.configuration_generation != context.configuration_generation):
                    # Retain selectable earlier blocks after a recorded fault;
                    # do not include incompatible raw in a SIGNAL selection.
                    break
                if envelope.role.value == "signal":
                    if active_profile is None:
                        raise ExecutionError("Interleaved SIGNAL has no preceding completed REF.")
                    if current is None or current["segment_id"] != envelope.segment_id:
                        if len(blocks) >= 256:
                            raise ExecutionError("Interleaved selection exceeds the 256 block budget.")
                        current = {"start_point": index, "stop_point": index, "segment_id": envelope.segment_id,
                            "before_profile_id": active_profile, "after_profile_id": None,
                            "started_at_s": (envelope.started_at_s if envelope.started_at_s is not None
                                             else envelope.acquired_at_s),
                            "completed_at_s": envelope.acquired_at_s}
                        blocks.append(current)
                    else:
                        if (index != current["stop_point"] + 1 or envelope.acquired_at_s <= current["completed_at_s"]
                                or envelope.acquired_at_s - current["completed_at_s"] > config.maximum_gap_s):
                            raise ExecutionError("Interleaved SIGNAL block is discontinuous.")
                        current["stop_point"] = index
                        current["completed_at_s"] = envelope.acquired_at_s
                else:
                    current = None
            if len(file["points"]) in profile_at and blocks and blocks[-1]["after_profile_id"] is None:
                blocks[-1]["after_profile_id"] = profile_at[len(file["points"])]
            return Path(path).resolve(), tuple(RecordedInterleavedSignalBlock(**block) for block in blocks)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ExecutionError(f"Cannot inspect interleaved blocks: {exc}") from exc


def _frames(file, indices, context, *, cancellation_check=None):
    for index in indices:
        if cancellation_check is not None:
            cancellation_check()
        key = f"points/{index}"
        if key not in file or not file[key].attrs.get("complete", False):
            raise ExecutionError("Selected signal checkpoint is not committed.")
        raw = file[f"spectra/{index}"]
        if not np.array_equal(raw["frequency_hz"][:], context.frequencies_hz):
            raise ExecutionError("Selected signal axis differs from the reference context.")
        envelope = read_envelope(raw)
        if envelope is None:
            raise ExecutionError("Selected signal requires acquisition evidence.")
        yield envelope, raw["power_dbm"][:]


def finalize_spectrum_archives(signal_path, before_path, after_path, destination, *, point_indices=None,
                               before_profile_id=None, after_profile_id=None, signal_profile_id=None,
                               cancellation_check=None, expected_source_hashes=None):
    """Finalize one explicitly selected continuous SIGNAL block and two REF profiles.

    Raw source files are never mutated. The output embeds every selected raw
    sweep and both profiles, plus one explicitly derived mean/result row for
    thaTEC/PyThat. Qualification is deliberately not inferred from filenames
    or descriptions: this operator workflow publishes no qualified CI.
    Full-source hashes identify the original files; embedded sweeps support
    replay even when those original files have moved.
    Multi-profile sources require explicit IDs. Their selected identities and
    content hashes are recorded in provenance; profile order is never inferred.
    """
    paths = tuple(Path(path).resolve() for path in (signal_path, before_path, after_path))
    target = Path(destination).resolve()
    if target in paths or target.exists():
        raise ExecutionError("Finalization destination must be a new file separate from all sources.")
    try:
        if cancellation_check is not None:
            cancellation_check()
        manifest = [{"role": role, "path": str(path), "sha256": file_sha256(path, cancellation_check=cancellation_check)}
                    for role, path in zip(("signal", "before", "after"), paths)]
        if expected_source_hashes is not None and tuple(item["sha256"] for item in manifest) != tuple(expected_source_hashes):
            raise ExecutionError("A source archive changed since the batch was planned.")
        with ExitStack() as stack:
            signal, before_file, after_file = [stack.enter_context(h5py.File(path, "r")) for path in paths]
            before_context, before = _profile(before_file, before_profile_id, cancellation_check=cancellation_check)
            after_context, after = _profile(after_file, after_profile_id, cancellation_check=cancellation_check)
            if signal["run"].attrs.get("status") not in {"completed", "aborted", "incomplete", "faulted"}:
                raise ExecutionError("Close the signal archive before finalization.")
            profiles = signal.get("spectrum_processing_v1/profiles")
            context, live_profile = read_selected_profile(profiles, signal_profile_id)
            for item, selected_context, selected_profile in zip(manifest,
                    (context, before_context, after_context), (live_profile, before, after)):
                item.update({"profile_id": selected_profile.profile_id,
                             "profile_content_hash": selected_profile.content_hash,
                             "context_id": selected_context.context_id})
            indices = tuple(range(len(signal["points"]))) if point_indices is None else tuple(point_indices)
            if not indices or any(type(index) is not int or index < 0 for index in indices) or any(
                first >= second for first, second in zip(indices, indices[1:])
            ):
                raise ExecutionError("Select strictly ordered integer signal checkpoints.")
            config = json.loads(signal["run"].attrs["spectrum_correction_config_json"])
            maximum_gap_s = config["maximum_gap_s"]
            block = finalize_bracketed_block(context, before, after, _frames(signal, indices, context),
                                              maximum_gap_s=maximum_gap_s, cancellation_check=cancellation_check)
            policy = {"algorithm_version": block.result.algorithm_version, "maximum_gap_s": maximum_gap_s,
                      "independent_blocks_qualified": False, "interpolation_error_variance_known": False}
            attributes = {"finalization_schema": "spectrum-finalization-artifact-v1",
                          "finalization_sources_json": json.dumps(manifest, sort_keys=True),
                          "finalization_policy_json": json.dumps(policy, sort_keys=True),
                          "original_signal_point_indices_json": json.dumps(indices)}
            attributes["finalization_signal_context_json"] = json.dumps({
                "configuration_fingerprint": context.configuration_fingerprint,
                "configuration_generation": context.configuration_generation,
                "settings_verified": context.settings_verified,
                "independent_sweeps_qualified": context.independent_sweeps_qualified,
                "context_id": context.context_id,
            }, sort_keys=True)
            if cancellation_check is not None:
                cancellation_check()
            writer = Hdf5RunWriter(
                target, recipe_source=signal["run/recipe_yaml"].asstr()[()],
                settings_source=signal["run/settings_yaml"].asstr()[()],
                plan_hash=hashlib.sha256(json.dumps(attributes, sort_keys=True).encode()).hexdigest(),
                device_idn=json.loads(signal["run/device_idn_json"].asstr()[()]),
                device_capabilities=json.loads(signal["run/capabilities_json"].asstr()[()]),
                operator_context=json.loads(signal["run/operator_context_json"].asstr()[()]),
                simulation_metadata=json.loads(signal["run/simulation_json"].asstr()[()]),
                expected_points=len(indices) + 1, run_attributes=attributes,
            )
            try:
                writer.store_background_profile(before_context, before)
                writer.store_background_profile(after_context, after)
                for output_index, (source_index, frame) in enumerate(zip(
                    indices, _frames(signal, indices, context, cancellation_check=cancellation_check)
                )):
                    envelope, dbm = frame
                    trace = SpectrumTrace(
                        tuple(context.frequencies_hz), tuple(dbm),
                        datetime.fromisoformat(str(signal[f"spectra/{source_index}"].attrs["acquired_at_utc"])),
                        str(signal[f"spectra/{source_index}"].attrs["trace_name"]),
                        sweep_evidence=envelope.evidence, sweep_id=envelope.sweep_id,
                        configuration_generation=envelope.configuration_generation,
                    )
                    writer.append(MeasurementPoint(output_index, {}, {}, metadata={
                        "role": "raw_signal", "source_point_index": source_index,
                        "source_archive_sha256": manifest[0]["sha256"],
                    }), trace, acquisition_envelope=envelope)
                if cancellation_check is not None:
                    cancellation_check()
                identity = writer.store_finalized_block(block, tuple(range(len(indices))))
                mean_trace = SpectrumTrace(
                    tuple(context.frequencies_hz), tuple(10 * np.log10(block.raw_mean_w) + 30),
                    datetime.fromtimestamp(block.result.completed_at_s, timezone.utc), "RAW_BLOCK_MEAN",
                )
                writer.append(MeasurementPoint(len(indices), {}, {}, metadata={
                    "role": "derived_block", "finalized_block_id": identity,
                    "source_frame_ids": block.source_frame_ids, "count": block.result.count,
                }), mean_trace, processed_values=tuple(block.result.values_w), processed_unit="W",
                              processing_operation="bracketed_reference_block")
                if any(file_sha256(path, cancellation_check=cancellation_check) != item["sha256"]
                       for path, item in zip(paths, manifest)):
                    raise ExecutionError("A source archive changed during finalization.")
                if cancellation_check is not None:
                    cancellation_check()
                writer.close("completed")
            except Exception as exc:
                try:
                    writer.close("aborted" if isinstance(exc, ProcessingCancelled) else "faulted")
                except Exception as close_error:
                    exc.add_note(f"Finalization close also failed: {close_error}")
                    if isinstance(exc, ProcessingCancelled):
                        raise ExecutionError(f"Cancellation requested, but output close failed: {close_error}") from exc
                raise
            return block
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ExecutionError(f"Cannot finalize spectrum archives: {exc}") from exc


def replay_finalized_artifact(path, *, cancellation_check=None):
    """Verify finalization using only its embedded raw and immutable profiles."""
    from .finalized_spectrum_codec import read_finalized
    from app.spectrum.replay import _verify_result

    try:
        if cancellation_check is not None:
            cancellation_check()
        with h5py.File(path, "r") as file:
            if file["run"].attrs.get("finalization_schema") != "spectrum-finalization-artifact-v1" or (
                file["run"].attrs.get("status") != "completed"
            ):
                raise ExecutionError("Not a completed finalization artifact.")
            attrs = file["run"].attrs
            provenance = {key: str(attrs[key]) for key in (
                "finalization_schema", "finalization_sources_json", "finalization_policy_json",
                "original_signal_point_indices_json",
                "finalization_signal_context_json",
            )}
            expected_hash = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
            if expected_hash != attrs["plan_sha256"]:
                raise ExecutionError("Finalization source/policy provenance checksum is corrupted.")
            root = file["spectrum_processing_v1/finalized_blocks"]
            if len(root) != 1:
                raise ExecutionError("Finalization artifact requires one final block.")
            stored, indices = read_finalized(next(iter(root.values())))
            profiles = file["spectrum_processing_v1/profiles"]
            before_id, after_id = [name for name, _ in stored.result.profile_weights]
            _before_context, before = read_profile(profiles[before_id])
            _after_context, after = read_profile(profiles[after_id])
            first_envelope = read_envelope(file[f"spectra/{indices[0]}"])
            if first_envelope is None:
                raise ExecutionError("Finalization artifact lost its acquisition evidence.")
            source_context = json.loads(attrs["finalization_signal_context_json"])
            identity = source_context.pop("context_id")
            context = SpectrumAcquisitionContext(file[f"spectra/{indices[0]}/frequency_hz"][:], **source_context)
            if context.context_id != identity:
                raise ExecutionError("Finalized signal acquisition context is corrupted.")
            policy = json.loads(file["run"].attrs["finalization_policy_json"])
            if policy["algorithm_version"] != stored.result.algorithm_version:
                raise ExecutionError("Finalization policy algorithm differs from the result.")
            if policy["independent_blocks_qualified"] or policy["interpolation_error_variance_known"]:
                raise ExecutionError("Unsupported qualified finalization policy.")
            reconstructed = finalize_bracketed_block(context, before, after,
                _frames(file, indices, context, cancellation_check=cancellation_check),
                maximum_gap_s=policy["maximum_gap_s"], cancellation_check=cancellation_check)
            _verify_result(stored.result, reconstructed.result)
            if stored.source_frame_ids != reconstructed.source_frame_ids or not np.allclose(
                stored.raw_mean_w, reconstructed.raw_mean_w, rtol=1e-12, atol=0,
            ):
                raise ExecutionError("Finalized source identities or raw mean differ from replay.")
            return reconstructed
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ExecutionError(f"Cannot replay finalization artifact: {exc}") from exc
