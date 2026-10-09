"""Read-only access to durable HDF5 measurement artefacts.

The GUI uses this module rather than opening HDF5 files directly.  Keeping
the parsing here gives operators one consistent view of both completed and
interrupted runs, and keeps an accidentally malformed result file from
crashing the Qt event loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.errors import ExecutionError
from app.spectrum import peak_preserving_indices


def iter_recipe_spectrum_sweeps(path, *, selected_indices=None):
    """Read committed raw recipe sources one sweep at a time, including interrupted blocks."""
    import h5py

    from .recipe_spectrum_store import iter_recipe_sweeps

    with h5py.File(path, "r") as file:
        yield from iter_recipe_sweeps(file, selected_indices=selected_indices)


@dataclass(frozen=True, slots=True)
class RunSummary:
    path: Path
    created_at_utc: str | None
    status: str
    point_count: int
    spectrum_count: int
    plan_sha256: str | None
    application_version: str | None
    operator: str | None = None
    sample_id: str | None = None
    sample_name: str | None = None
    sample_row: str | None = None
    sample_col: str | None = None
    sample_coordinate_label: str | None = None
    sample_row_label: str | None = None
    sample_col_label: str | None = None
    sample_description: str | None = None
    sample_tags: tuple[str, ...] = ()
    sample_cell_notes: str | None = None


@dataclass(frozen=True, slots=True)
class RunDetail:
    summary: RunSummary
    recipe_yaml: str
    settings_yaml: str
    device_idn: dict[str, str]
    capabilities: dict[str, Any]
    operator_context: dict[str, Any]
    simulation_metadata: dict[str, Any]
    events: tuple["StoredEvent", ...]


@dataclass(frozen=True, slots=True)
class StoredPoint:
    index: int
    timestamp_utc: str | None
    status: str
    setpoints: dict[str, Any]
    measurements: dict[str, Any]
    metadata: dict[str, Any]
    device_states: dict[str, Any]
    has_spectrum: bool
    details_loaded: bool = True


@dataclass(frozen=True, slots=True)
class StoredSpectrum:
    index: int
    trace_name: str
    acquired_at_utc: str | None
    frequencies_hz: tuple[float, ...]
    powers_dbm: tuple[float, ...]
    source_point_count: int
    processed_values: tuple[float, ...] | None = None
    processed_unit: str | None = None
    processing_operation: str = "none"
    reference_index: int | None = None


@dataclass(frozen=True, slots=True)
class StoredReference:
    index: int
    trace_name: str
    acquired_at_utc: str | None
    kind: str
    average_count: int
    frequencies_hz: tuple[float, ...]
    powers_dbm: tuple[float, ...]
    purpose: str = "reference"
    configuration_fingerprint: str | None = None
    source_sweep_indices: tuple[int, ...] = ()
    selected_sweep: int | None = None
    collection_average_count: int | None = None


@dataclass(frozen=True, slots=True)
class StoredReferenceSummary:
    """Catalogue metadata; scientific arrays are validated on explicit read."""
    index: int
    trace_name: str
    acquired_at_utc: str | None
    kind: str
    average_count: int
    source_point_count: int
    purpose: str
    configuration_fingerprint: str | None
    source_sweep_indices: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredEvent:
    timestamp_utc: str
    severity: str
    name: str
    data: dict[str, Any]


class Hdf5RunReader:
    @staticmethod
    def finalized_spectrum_blocks(path: str | Path):
        import h5py

        from .finalized_spectrum_codec import read_finalized

        with h5py.File(path, "r") as file:
            root = file.get("spectrum_processing_v1/finalized_blocks")
            if root is None:
                return ()
            blocks = tuple(read_finalized(root[name]) for name in sorted(root))
            for block, indices in blocks:
                from .spectrum_correction_codec import read_envelope, read_profile

                for identity, _weight in block.result.profile_weights:
                    key = f"spectrum_processing_v1/profiles/{identity}"
                    if key not in file:
                        raise ExecutionError("Finalized block lost its reference profile.")
                    context, _profile = read_profile(file[key])
                    if context.context_id != block.result.context_id:
                        raise ExecutionError("Finalized reference context is corrupted.")
                for index, frame_id in zip(indices, block.source_frame_ids):
                    key = f"points/{index}"
                    if key not in file or not file[key].attrs.get("complete", False):
                        raise ExecutionError("Finalized block references an uncommitted raw checkpoint.")
                    envelope = read_envelope(file[f"spectra/{index}"])
                    if envelope is None or not envelope.complete or (
                        envelope.frame_id != frame_id or envelope.context_id != block.result.context_id
                        or envelope.segment_id != block.result.segment_id or envelope.role.value != "signal"
                    ):
                        raise ExecutionError("Finalized block source identity is corrupted.")
            return blocks

    @staticmethod
    def spectrum_correction(path: str | Path, index: int):
        """Read a full-resolution signed result from a committed raw checkpoint."""
        if index < 0:
            raise ExecutionError("Spectrum index cannot be negative.")
        import h5py

        from .spectrum_correction_codec import read_corrected

        with h5py.File(path, "r") as file:
            key = f"spectra/{index}/correction_v1"
            if key not in file:
                return None
            Hdf5RunReader._require_committed_spectrum(file, index)
            result = read_corrected(file[key])
            if result.interference_model_id is not None:
                from .spectrum_correction_codec import read_profile
                from .spectrum_interference_codec import (
                    read_interference_calibration,
                    validate_interference_result,
                )

                model_key = f"spectrum_processing_v1/interference_models/{result.interference_model_id}"
                if model_key not in file:
                    raise ExecutionError("Correction result lost its interference model.")
                calibration = read_interference_calibration(file[model_key])
                validate_interference_result(result, calibration)
                for profile_id, content_hash in calibration.source_profiles:
                    source_key = f"spectrum_processing_v1/profiles/{profile_id}"
                    if source_key not in file:
                        raise ExecutionError("Correction model lost its reference profile.")
                    context, profile = read_profile(file[source_key])
                    if context.context_id != calibration.context.context_id or profile.content_hash != content_hash:
                        raise ExecutionError("Correction model reference dependency is corrupted.")
            return result

    @staticmethod
    def spectrum_acquisition(path: str | Path, index: int):
        if index < 0:
            raise ExecutionError("Spectrum index cannot be negative.")
        import h5py

        from .spectrum_correction_codec import read_envelope

        with h5py.File(path, "r") as file:
            key = f"spectra/{index}"
            if key not in file:
                return None
            Hdf5RunReader._require_committed_spectrum(file, index)
            return read_envelope(file[key])

    @staticmethod
    def background_profiles(path: str | Path):
        import h5py

        from .spectrum_correction_codec import read_profile

        with h5py.File(path, "r") as file:
            root = file.get("spectrum_processing_v1/profiles")
            if root is None:
                return ()
            return tuple(read_profile(root[name]) for name in sorted(root))

    @staticmethod
    def interference_calibrations(path: str | Path, *, model_id: str | None = None):
        """Read committed models and verify their embedded reference dependencies."""
        import h5py

        from .spectrum_correction_codec import read_profile
        from .spectrum_interference_codec import read_interference_calibration

        with h5py.File(path, "r") as file:
            if file["run"].attrs.get("interference_calibration_schema") == "reference-trained-calibration-v1" and (
                file["run"].attrs.get("status") != "completed"
            ):
                raise ExecutionError("Reference-trained calibration artifact is not completed.")
            root = file.get("spectrum_processing_v1/interference_models")
            if root is None:
                if model_id is not None:
                    raise ExecutionError("Requested interference model is absent.")
                return ()
            if model_id is not None:
                from .spectrum_correction_codec import safe_record_id

                safe_record_id(model_id)
                if model_id not in root:
                    raise ExecutionError("Requested interference model is absent.")
            names = (model_id,) if model_id is not None else sorted(root)
            calibrations = tuple(read_interference_calibration(root[name]) for name in names)
            if any(calibration.model_id != name for name, calibration in zip(names, calibrations, strict=True)):
                raise ExecutionError("Interference model key differs from its recorded identity.")
            for calibration in calibrations:
                for profile_id, content_hash in calibration.source_profiles:
                    key = f"spectrum_processing_v1/profiles/{profile_id}"
                    if key not in file:
                        raise ExecutionError("Interference calibration lost its reference profile.")
                    context, profile = read_profile(file[key])
                    if context.context_id != calibration.context.context_id or profile.content_hash != content_hash:
                        raise ExecutionError("Interference reference dependency is corrupted.")
            return calibrations

    """Read schema-version-1 HDF5 runs without ever modifying them."""

    @staticmethod
    def list_runs(
        directory: str | Path, *, recursive: bool = False, cancelled=None
    ) -> tuple[RunSummary, ...]:
        """Index immutable HDF5 runs below ``directory``.

        The station normally writes one run per file, but a sample catalogue
        deliberately nests those files below ``<sample>/measurements``.  The
        default remains a direct-directory scan for callers that use a
        dedicated output folder; catalogue browsers can opt into the
        recursive scan without changing the reader's compatibility surface.
        """
        output_dir = Path(directory)
        cancelled = cancelled or (lambda: False)
        def check_cancelled():
            if cancelled():
                raise InterruptedError("Result catalogue indexing cancelled")

        check_cancelled()
        if not output_dir.exists():
            return ()
        summaries: list[RunSummary] = []
        glob = output_dir.rglob if recursive else output_dir.glob
        paths = set()
        for extension in ("*.h5", "*.hdf5"):
            for path in glob(extension):
                check_cancelled()
                paths.add(path)
        from app.storage.run_bundle import is_bundle_companion
        ordered = []
        for path in paths:
            check_cancelled()
            if is_bundle_companion(path):
                continue
            try:
                modified = path.stat().st_mtime
            except OSError:
                modified = 0
            ordered.append((modified, str(path), path))
        for _, _, path in sorted(ordered, reverse=True):
            check_cancelled()
            try:
                summaries.append(Hdf5RunReader.summary(path))
            except ExecutionError:
                try:
                    from app.storage.thatec_reader import ThatecRunReader

                    run = ThatecRunReader.describe(path)
                    shapes = [row.shape[0] for row in run.rows.values() if row.shape]
                    created_at = Hdf5RunReader._extract_timestamp(path, None)
                    summaries.append(
                        RunSummary(
                            path=path,
                            created_at_utc=created_at,
                            status="THATEC",
                            point_count=max(shapes, default=0),
                            spectrum_count=sum(
                                len(row.shape) >= 2 for row in run.rows.values()
                            ),
                            plan_sha256=None,
                            application_version=None,
                            operator=None,
                        )
                    )
                except ExecutionError:
                    # Keep a genuinely unreadable result visible to the operator so it can
                    # be recovered externally, rather than silently hiding it.
                    created_at = Hdf5RunReader._extract_timestamp(path, None)
                    summaries.append(
                        RunSummary(
                            path=path,
                            created_at_utc=created_at,
                            status="unreadable",
                            point_count=0,
                            spectrum_count=0,
                            plan_sha256=None,
                            application_version=None,
                            operator=None,
                        )
                    )
        return tuple(summaries)

    @staticmethod
    def _extract_operator(run: Any) -> str | None:
        if run is None or not hasattr(run, "attrs"):
            return None
        op = Hdf5RunReader._attribute_text(run.attrs.get("operator"))
        if op:
            return op
        if "operator_context_json" in run:
            try:
                ctx = Hdf5RunReader._dataset_json(run, "operator_context_json")
                if isinstance(ctx, dict):
                    username = ctx.get("username")
                    if username:
                        return str(username)
            except Exception:
                pass
        return None

    @staticmethod
    def _extract_timestamp(path: Path, run: Any | None) -> str | None:
        if run is not None and hasattr(run, "attrs"):
            ts = Hdf5RunReader._attribute_text(run.attrs.get("created_at_utc"))
            if ts:
                return ts
        name = path.name
        if len(name) >= 15 and name[8] == "T":
            date_part = name[:8]
            time_part = name[9:15]
            if date_part.isdigit() and time_part.isdigit():
                return f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}T{time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}Z"
        try:
            from datetime import datetime, timezone
            mtime = path.stat().st_mtime
            return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return None

    @staticmethod
    def summary(path: str | Path) -> RunSummary:
        path_obj = Path(path)
        with Hdf5RunReader._open(path_obj) as file:
            run = Hdf5RunReader._require_group(file, "run")
            return Hdf5RunReader.summary_from_open_file(path_obj, file, run)

    @staticmethod
    def detail(path: str | Path) -> RunDetail:
        with Hdf5RunReader._open(path) as file:
            run = Hdf5RunReader._require_group(file, "run")
            summary = Hdf5RunReader.summary_from_open_file(Path(path), file, run)
            return RunDetail(
                summary=summary,
                recipe_yaml=Hdf5RunReader._dataset_text(run, "recipe_yaml"),
                settings_yaml=Hdf5RunReader._dataset_text(run, "settings_yaml"),
                device_idn=Hdf5RunReader._dataset_json(run, "device_idn_json"),
                capabilities=Hdf5RunReader._dataset_json(run, "capabilities_json"),
                operator_context=Hdf5RunReader._dataset_json(run, "operator_context_json"),
                simulation_metadata=Hdf5RunReader._dataset_json(run, "simulation_json"),
                events=Hdf5RunReader._events(file),
            )

    @staticmethod
    def summary_from_open_file(path: Path, file: Any, run: Any) -> RunSummary:
        committed_names = Hdf5RunReader._committed_point_names(file)
        return RunSummary(
            path=path,
            created_at_utc=Hdf5RunReader._extract_timestamp(path, run),
            status=Hdf5RunReader._attribute_text(run.attrs.get("status")) or "incomplete",
            point_count=len(committed_names),
            spectrum_count=sum(name in file.get("spectra", {}) for name in committed_names),
            plan_sha256=Hdf5RunReader._attribute_text(run.attrs.get("plan_sha256")),
            application_version=Hdf5RunReader._attribute_text(run.attrs.get("application_version")),
            operator=Hdf5RunReader._extract_operator(run),
            sample_id=Hdf5RunReader._attribute_text(run.attrs.get("sample_id")),
            sample_name=Hdf5RunReader._attribute_text(run.attrs.get("sample_name")),
            sample_row=Hdf5RunReader._attribute_text(run.attrs.get("sample_row")),
            sample_col=Hdf5RunReader._attribute_text(run.attrs.get("sample_col")),
            sample_coordinate_label=Hdf5RunReader._attribute_text(run.attrs.get("sample_coordinate_label")),
            sample_row_label=Hdf5RunReader._attribute_text(run.attrs.get("sample_row_label")),
            sample_col_label=Hdf5RunReader._attribute_text(run.attrs.get("sample_col_label")),
            sample_description=Hdf5RunReader._attribute_text(run.attrs.get("sample_description")),
            sample_tags=Hdf5RunReader._attribute_tags(run.attrs.get("sample_tags")),
            sample_cell_notes=Hdf5RunReader._attribute_text(run.attrs.get("sample_cell_notes")),
        )

    @staticmethod
    def points(path: str | Path, *, include_details: bool = True) -> tuple[StoredPoint, ...]:
        with Hdf5RunReader._open(path) as file:
            points = file.get("points")
            if points is None:
                return ()
            spectra = file.get("spectra", {})
            result: list[StoredPoint] = []
            for name in Hdf5RunReader._committed_point_names(file):
                result.append(Hdf5RunReader._read_point(points[name], name, spectra, include_details=include_details))
            return tuple(result)

    @staticmethod
    def point(path: str | Path, index: int) -> StoredPoint:
        """Read full details of one checkpoint in the committed prefix."""
        if index < 0:
            raise ExecutionError("Checkpoint index cannot be negative.")
        with Hdf5RunReader._open(path) as file:
            names = Hdf5RunReader._committed_point_names(file, stop_after=index + 1)
            if index >= len(names):
                raise ExecutionError(f"Checkpoint {index} is not committed.")
            name = str(index)
            return Hdf5RunReader._read_point(file["points"][name], name, file.get("spectra", {}))

    @staticmethod
    def single_point(path: str | Path) -> StoredPoint:
        """Read a one-checkpoint artefact without importing a full run first."""
        with Hdf5RunReader._open(path) as file:
            names = Hdf5RunReader._committed_point_names(file, stop_after=2)
            if len(names) != 1:
                raise ExecutionError("A reference file must contain exactly one checkpoint.")
            name = names[0]
            return Hdf5RunReader._read_point(file["points"][name], name, file.get("spectra", {}))

    @staticmethod
    def _read_point(group, name, spectra, *, include_details=True) -> StoredPoint:
        metadata = Hdf5RunReader._dataset_json(group, "metadata_json")
        if not include_details:
            # Keep the recorded evidence required by spectral post-processing.
            # Arbitrary additional provenance is loaded on checkpoint selection.
            metadata = {key: metadata[key] for key in ("raw_recipe_sweep_indices", "spectrum_processing_v1") if key in metadata}
        return StoredPoint(
            index=int(name),
            timestamp_utc=Hdf5RunReader._attribute_text(group.attrs.get("timestamp_utc")),
            status=Hdf5RunReader._attribute_text(group.attrs.get("status")) or "unknown",
            setpoints=Hdf5RunReader._dataset_json(group, "setpoints_json"),
            measurements=Hdf5RunReader._dataset_json(group, "measurements_json"),
            metadata=metadata,
            device_states=Hdf5RunReader._dataset_json(group, "device_states_json") if include_details else {},
            has_spectrum=name in spectra,
            details_loaded=include_details,
        )

    @staticmethod
    def spectrum_point_count(path: str | Path, index: int) -> int:
        """Return a stored spectrum size without materialising either data axis."""

        if index < 0:
            raise ExecutionError("Spectrum index cannot be negative.")
        with Hdf5RunReader._open(path) as file:
            spectra = file.get("spectra")
            if spectra is None or str(index) not in spectra:
                return 0
            Hdf5RunReader._require_committed_spectrum(file, index)
            group = spectra[str(index)]
            frequency = group.get("frequency_hz")
            power = group.get("power_dbm")
            if frequency is None or power is None:
                raise ExecutionError(
                    f"Spectrum {index} does not contain a complete data axis."
                )
            if frequency.ndim != 1 or power.ndim != 1:
                raise ExecutionError(f"Spectrum {index} axes must be one-dimensional.")
            if frequency.shape != power.shape or not frequency.shape[0]:
                raise ExecutionError(
                    f"Spectrum {index} has a mismatched or empty point count."
                )
            return int(frequency.shape[0])

    @staticmethod
    def spectrum(path: str | Path, index: int, *, max_points: int | None = None) -> StoredSpectrum | None:
        if index < 0:
            raise ExecutionError("Spectrum index cannot be negative.")
        with Hdf5RunReader._open(path) as file:
            spectra = file.get("spectra")
            if spectra is None or str(index) not in spectra:
                return None
            Hdf5RunReader._require_committed_spectrum(file, index)
            group = spectra[str(index)]
            frequencies, powers = Hdf5RunReader._read_spectrum_axes(group, f"Spectrum {index}")
            source_count = len(frequencies)
            processed: tuple[float, ...] | None = None
            if "processed_values" in group:
                import numpy as np

                dataset = group["processed_values"]
                if (not hasattr(dataset, "dtype") or dataset.ndim != 1
                        or dataset.shape != (source_count,) or dataset.dtype.kind not in "fiu"):
                    raise ExecutionError(
                        f"Spectrum {index} processed values must be a matching real numeric vector."
                    )
                values = dataset[:]
                if not np.all(np.isfinite(values)):
                    raise ExecutionError(f"Spectrum {index} processed values contain NaN or infinity.")
                unit = Hdf5RunReader._attribute_text(group.attrs.get("processed_unit"))
                operation = Hdf5RunReader._attribute_text(group.attrs.get("processing_operation"))
                if not unit or not unit.strip() or not operation or not operation.strip() or operation == "none":
                    raise ExecutionError(f"Spectrum {index} processed values require an explicit unit and operation.")
                processed = tuple(float(value) for value in values)
            if max_points is not None and max_points > 0 and source_count > max_points:
                if processed is None or max_points < 2:
                    selected = peak_preserving_indices(powers, max_points)
                else:
                    # Both curves share one returned frequency axis. Split
                    # the interior budget and merge extrema from each unit
                    # separately: comparing W and dBm magnitudes is invalid.
                    raw_budget = 2 + (max_points - 2) // 2
                    processed_budget = max_points - raw_budget + 2
                    selected = tuple(sorted(
                        set(peak_preserving_indices(powers, raw_budget))
                        | set(peak_preserving_indices(processed, processed_budget))
                    ))
                frequencies = tuple(frequencies[item] for item in selected)
                powers = tuple(powers[item] for item in selected)
                if processed is not None:
                    processed = tuple(processed[item] for item in selected)
            return StoredSpectrum(
                index=index,
                trace_name=Hdf5RunReader._attribute_text(group.attrs.get("trace_name")) or "TRAC1",
                acquired_at_utc=Hdf5RunReader._attribute_text(group.attrs.get("acquired_at_utc")),
                frequencies_hz=frequencies,
                powers_dbm=powers,
                source_point_count=source_count,
                processed_values=processed,
                processed_unit=(
                    Hdf5RunReader._attribute_text(group.attrs.get("processed_unit"))
                    if processed is not None
                    else None
                ),
                processing_operation=(
                    Hdf5RunReader._attribute_text(
                        group.attrs.get("processing_operation")
                    )
                    or "none"
                ),
                reference_index=(
                    int(group.attrs["reference_index"])
                    if "reference_index" in group.attrs
                    else None
                ),
            )

    @staticmethod
    def references(path: str | Path, *, metadata_only: bool = False) -> tuple[StoredReference | StoredReferenceSummary, ...]:
        with Hdf5RunReader._open(path) as file:
            container = file.get("references")
            groups: list[tuple[int, Any]] = []
            if container is not None:
                groups = [
                    (int(name), container[name])
                    for name in Hdf5RunReader._numeric_names(container)
                ]
            elif "reference" in file:
                groups = [(0, file["reference"])]
            result: list[StoredReference | StoredReferenceSummary] = []
            for index, group in groups:
                from .reference_transaction import require_committed_reference

                require_committed_reference(group)
                if metadata_only:
                    frequency, power = group.get("frequency_hz"), group.get("power_dbm")
                    if (frequency is None or power is None or not hasattr(frequency, "dtype")
                            or not hasattr(power, "dtype") or frequency.ndim != 1 or power.ndim != 1
                            or frequency.shape != power.shape or frequency.shape[0] < 2
                            or frequency.dtype.kind not in "fiu" or power.dtype.kind not in "fiu"):
                        raise ExecutionError(f"Reference {index} has invalid spectrum axes.")
                    result.append(StoredReferenceSummary(
                        index, Hdf5RunReader._attribute_text(group.attrs.get("trace_name")) or "TRAC1",
                        Hdf5RunReader._attribute_text(group.attrs.get("acquired_at_utc")),
                        Hdf5RunReader._attribute_text(group.attrs.get("kind")) or "single",
                        int(group.attrs.get("average_count", 1)), int(frequency.shape[0]),
                        Hdf5RunReader._attribute_text(group.attrs.get("purpose")) or "reference",
                        Hdf5RunReader._reference_fingerprint(group),
                        Hdf5RunReader._reference_source_indices(group),
                    ))
                    continue
                frequencies, powers = Hdf5RunReader._read_spectrum_axes(group, f"Reference {index}")
                result.append(
                    StoredReference(
                        index=index,
                        trace_name=Hdf5RunReader._attribute_text(
                            group.attrs.get("trace_name")
                        ) or "TRAC1",
                        acquired_at_utc=Hdf5RunReader._attribute_text(
                            group.attrs.get("acquired_at_utc")
                        ),
                        kind=Hdf5RunReader._attribute_text(
                            group.attrs.get("kind")
                        ) or "single",
                        average_count=int(group.attrs.get("average_count", 1)),
                        frequencies_hz=frequencies,
                        powers_dbm=powers,
                        purpose=Hdf5RunReader._attribute_text(group.attrs.get("purpose")) or "reference",
                        configuration_fingerprint=Hdf5RunReader._reference_fingerprint(group),
                        source_sweep_indices=Hdf5RunReader._reference_source_indices(group),
                        collection_average_count=int(group.attrs.get("average_count", 1)),
                    )
                )
            return tuple(result)

    @staticmethod
    def reference(
        path: str | Path,
        index: int = 0,
        *,
        max_points: int | None = None,
    ) -> StoredReference | None:
        """Read one immutable reference spectrum, optionally decimated.

        The browser uses this narrow accessor when an operator asks to compare
        a checkpoint with its stored reference.  Keeping the lookup lazy avoids
        loading every reference from a large run merely to render one plot.
        ``/reference`` is retained as the legacy alias for reference ``0``.
        """

        if index < 0:
            raise ExecutionError("Reference index cannot be negative.")
        with Hdf5RunReader._open(path) as file:
            container = file.get("references")
            group = None
            if container is not None and str(index) in container:
                group = container[str(index)]
            elif index == 0:
                group = file.get("reference")
            if group is None:
                return None
            from .reference_transaction import require_committed_reference

            require_committed_reference(group)
            frequencies, powers = Hdf5RunReader._read_spectrum_axes(group, f"Reference {index}")
            source_count = len(frequencies)
            if max_points is not None and max_points > 0 and source_count > max_points:
                selected = peak_preserving_indices(powers, max_points)
                frequencies = tuple(frequencies[item] for item in selected)
                powers = tuple(powers[item] for item in selected)
            return StoredReference(
                index=index,
                trace_name=Hdf5RunReader._attribute_text(
                    group.attrs.get("trace_name")
                )
                or "TRAC1",
                acquired_at_utc=Hdf5RunReader._attribute_text(
                    group.attrs.get("acquired_at_utc")
                ),
                kind=Hdf5RunReader._attribute_text(group.attrs.get("kind"))
                or "single",
                average_count=int(group.attrs.get("average_count", 1)),
                frequencies_hz=frequencies,
                powers_dbm=powers,
                purpose=Hdf5RunReader._attribute_text(group.attrs.get("purpose")) or "reference",
                configuration_fingerprint=Hdf5RunReader._reference_fingerprint(group),
                source_sweep_indices=Hdf5RunReader._reference_source_indices(group),
                collection_average_count=int(group.attrs.get("average_count", 1)),
            )

    @staticmethod
    def _reference_source_indices(group) -> tuple[int, ...]:
        """Legacy/imported means can legitimately have no individual sources."""
        if "source_recipe_sweep_indices" not in group:
            return ()
        source = group["source_recipe_sweep_indices"]
        count = int(group.attrs.get("average_count", 1))
        if (not hasattr(source, "dtype") or source.ndim != 1 or source.dtype.kind not in "iu"
                or not 1 <= count <= 9999 or source.shape != (count,)):
            raise ExecutionError("Reference source sweep identities are malformed.")
        indices = tuple(int(value) for value in source[:])
        if any(not 0 <= value < 2**63 for value in indices) or tuple(sorted(set(indices))) != indices:
            raise ExecutionError("Reference source sweep identities are not unique and ordered.")
        return indices

    @staticmethod
    def reference_sweep(path: str | Path, index: int, sweep: int) -> StoredReference:
        """Read one linked raw repeat, retaining its collection and source identity."""
        from datetime import UTC, datetime
        from .recipe_spectrum_store import read_recipe_sweep
        from .reference_transaction import require_committed_reference
        from app.domain.spectrum_correction import SpectrumFrameRole
        from app.spectrum.processing import frequency_grids_match

        if type(index) is not int or index < 0 or type(sweep) is not int or sweep < 0:
            raise ExecutionError("Reference and repeat indices must be nonnegative integers.")
        with Hdf5RunReader._open(path) as file:
            container = file.get("references")
            group = container.get(str(index)) if container is not None else file.get("reference") if index == 0 else None
            if group is None:
                raise ExecutionError(f"Recorded reference {index} is missing.")
            require_committed_reference(group)
            indices = Hdf5RunReader._reference_source_indices(group)
            if not indices:
                raise ExecutionError("Individual repeats are not stored for this baseline; use its stored mean.")
            if sweep >= len(indices):
                raise ExecutionError(f"Repeat {sweep + 1} is outside this baseline's {len(indices)} recorded repeats.")
            record = read_recipe_sweep(file, indices[sweep])
            frequencies, _mean = Hdf5RunReader._read_spectrum_axes(group, f"Reference {index}")
            fingerprint = Hdf5RunReader._reference_fingerprint(group)
            acquisition = json.loads(Hdf5RunReader._attribute_text(group.attrs.get("acquisition_metadata_json")) or "{}")
            if not isinstance(acquisition, dict):
                raise ExecutionError("Reference acquisition metadata is malformed.")
            duration = acquisition.get("minimum_duration_s", 0.)
            first = record if sweep == 0 else read_recipe_sweep(file, indices[0])
            def identity(item):
                return item.execution_id, item.recipe_node_id, item.configuration_generation, item.setpoints_si
            if (record.role != SpectrumFrameRole.REFERENCE or first.role != SpectrumFrameRole.REFERENCE
                    or first.average_index != 0 or record.average_index != sweep
                    or record.average_count != (None if duration else len(indices))
                    or first.average_count != record.average_count or first.minimum_duration_s != duration
                    or record.minimum_duration_s != duration
                    or acquisition.get("recipe_node_id", record.recipe_node_id) != record.recipe_node_id
                    or identity(record) != identity(first)
                    or acquisition.get("configuration_generation", record.configuration_generation) != record.configuration_generation):
                raise ExecutionError("Selected raw repeat does not belong to this reference acquisition block.")
            if not frequency_grids_match(frequencies, record.frequencies_hz):
                raise ExecutionError("Individual reference repeat has a different frequency grid from its stored mean.")
            return StoredReference(index, Hdf5RunReader._attribute_text(group.attrs.get("trace_name")) or "TRAC1",
                datetime.fromtimestamp(record.acquired_at_s, UTC).isoformat(), "single", 1,
                tuple(float(value) for value in record.frequencies_hz), tuple(float(value) for value in record.powers_dbm),
                Hdf5RunReader._attribute_text(group.attrs.get("purpose")) or "reference",
                fingerprint, (indices[sweep],), sweep, len(indices))

    @staticmethod
    def _read_spectrum_axes(group: Any, label: str) -> tuple[tuple[float, ...], tuple[float, ...]]:
        import numpy as np

        frequency = group.get("frequency_hz")
        power = group.get("power_dbm")
        if frequency is None or power is None:
            raise ExecutionError(f"{label} does not contain complete frequency and power axes.")
        if frequency.ndim != 1 or power.ndim != 1:
            raise ExecutionError(f"{label} axes must be one-dimensional.")
        if frequency.shape != power.shape or frequency.shape[0] < 2:
            raise ExecutionError(f"{label} requires at least two matching frequency and power samples.")
        if frequency.dtype.kind not in "fiu" or power.dtype.kind not in "fiu":
            raise ExecutionError(f"{label} axes must contain real numeric values.")
        frequencies, powers = frequency[:], power[:]
        if not np.isfinite(frequencies).all() or not np.isfinite(powers).all():
            raise ExecutionError(f"{label} contains NaN or infinity.")
        if not (frequencies[1:] > frequencies[:-1]).all():
            raise ExecutionError(f"{label} frequency axis must be strictly increasing.")
        return tuple(float(value) for value in frequencies), tuple(float(value) for value in powers)

    @staticmethod
    def _reference_fingerprint(group):
        source = Hdf5RunReader._attribute_text(group.attrs.get("acquisition_metadata_json"))
        if not source:
            return None
        try:
            metadata = json.loads(source)
            if not isinstance(metadata, dict):
                raise ValueError("Expected an acquisition metadata object.")
            fingerprint = metadata.get("configuration_fingerprint")
            if fingerprint is not None and (not isinstance(fingerprint, str) or not fingerprint):
                raise ValueError("Invalid analyzer configuration fingerprint.")
            return fingerprint
        except (TypeError, ValueError) as exc:
            raise ExecutionError("Reference acquisition metadata is malformed.") from exc

    @staticmethod
    def _events(file: Any) -> tuple[StoredEvent, ...]:
        group = file.get("events")
        if group is None:
            return ()
        timestamp = Hdf5RunReader._dataset_texts(group, "timestamp")
        severity = Hdf5RunReader._dataset_texts(group, "severity")
        names = Hdf5RunReader._dataset_texts(group, "name")
        messages = Hdf5RunReader._dataset_texts(group, "message")
        lengths = {len(timestamp), len(severity), len(names), len(messages)}
        if len(lengths) != 1:
            raise ExecutionError("HDF5 event-log columns have inconsistent lengths.")
        events: list[StoredEvent] = []
        for time, level, name, message in zip(timestamp, severity, names, messages, strict=True):
            try:
                payload = json.loads(message)
            except json.JSONDecodeError as exc:
                raise ExecutionError("The event log contains invalid JSON.") from exc
            if not isinstance(payload, dict):
                raise ExecutionError("An event-log message must be a JSON object.")
            events.append(StoredEvent(time, level, name, payload))
        return tuple(events)

    @staticmethod
    def _open(path: str | Path):
        try:
            import h5py
        except ImportError as exc:
            raise ExecutionError("Reading HDF5 results requires the h5py package.") from exc
        try:
            return h5py.File(Path(path), "r")
        except OSError as exc:
            raise ExecutionError(f"Cannot read HDF5 file {Path(path).name}: {exc}") from exc

    @staticmethod
    def _committed_point_names(file: Any, *, stop_after: int | None = None) -> tuple[str, ...]:
        """Expose the contiguous committed prefix, excluding interrupted work."""
        points = file.get("points")
        if points is None:
            return ()
        names: list[str] = []
        while stop_after is None or len(names) < stop_after:
            name = str(len(names))
            point = points.get(name)
            if point is None or not bool(point.attrs.get("complete", False)):
                break
            names.append(name)
        return tuple(names)

    @staticmethod
    def _require_committed_spectrum(file: Any, index: int) -> None:
        if index < 0 or index >= len(Hdf5RunReader._committed_point_names(file, stop_after=index + 1)):
            raise ExecutionError(f"Spectrum {index} belongs to an uncommitted checkpoint.")

    @staticmethod
    def _require_group(file: Any, name: str) -> Any:
        group = file.get(name)
        if group is None:
            raise ExecutionError(f"The HDF5 file does not contain required group /{name}.")
        return group

    @staticmethod
    def _dataset_text(group: Any, name: str) -> str:
        dataset = group.get(name)
        if dataset is None:
            return ""
        value = dataset[()]
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _dataset_json(group: Any, name: str) -> dict[str, Any]:
        source = Hdf5RunReader._dataset_text(group, name)
        if not source:
            return {}
        try:
            decoded = json.loads(source)
        except json.JSONDecodeError as exc:
            raise ExecutionError(f"Invalid JSON in {name}.") from exc
        if not isinstance(decoded, dict):
            raise ExecutionError(f"{name} must contain a JSON object.")
        return decoded

    @staticmethod
    def _dataset_texts(group: Any, name: str) -> tuple[str, ...]:
        dataset = group.get(name)
        if dataset is None:
            return ()
        values = dataset.asstr()[:]
        return tuple(str(value) for value in values)

    @staticmethod
    def _attribute_text(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _attribute_tags(value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        text = Hdf5RunReader._attribute_text(value)
        if not text:
            return ()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (list, tuple)):
                return tuple(str(x) for x in parsed if str(x).strip())
        except Exception:
            pass
        return tuple(p.strip() for p in text.split(",") if p.strip())

    @staticmethod
    def _numeric_names(group: Any) -> tuple[str, ...]:
        names: list[tuple[int, str]] = []
        for name in group.keys():
            try:
                names.append((int(name), str(name)))
            except ValueError:
                continue
        return tuple(name for _, name in sorted(names))
