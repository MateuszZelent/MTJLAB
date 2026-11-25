"""Durable storage for spectra captured from the manual Anritsu page."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import numpy as np

from app.devices.anritsu_ms2830a import ReferenceSpectrum, SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.manual_metadata import ManualMetadataValue
from app.domain.models import MeasurementPoint
from app.spectrum import frequency_grids_match

from .hdf5_writer import Hdf5RunWriter
from .reference_store import ReferenceHdf5Store
from .saved_spectrum_view import SavedSpectrumView

MANUAL_SPECTRUM_SCHEMA = "manual-spectrum-v1"
MANUAL_SPECTRUM_RECIPE = "schema_version: 1\n"
_CLOSE_CLEANUP_ERRORS = (
    ExecutionError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
)


def _ensure_hdf5_path(path: Path) -> Path:
    """Ensure an HDF5 extension without replacing arbitrary dotted suffixes."""
    if path.suffix.lower() in {".h5", ".hdf5"}:
        return path
    return path.with_name(f"{path.name}.h5")


class ManualSpectrumSaveMode(StrEnum):
    """How the next manual capture is materialised on disk."""

    APPEND = "append"
    TIMESTAMPED = "timestamped"


@dataclass(frozen=True, slots=True)
class ManualSpectrumSaveResult:
    path: Path
    point_index: int
    point_count: int
    mode: ManualSpectrumSaveMode


class ManualSpectrumArchive:
    """Keep one appendable manual archive and validate every capture.

    The class deliberately delegates the transaction boundary to
    :class:`Hdf5RunWriter`.  An append session remains open between saves, but
    ``close()`` leaves it as a valid, resumable ``incomplete`` artefact rather
    than an unclosed HDF5 handle.
    """

    def __init__(
        self,
        *,
        settings_source: str = "",
        device_idn: dict[str, str] | None = None,
        operator_context: dict[str, object] | None = None,
        simulation: bool = False,
        isolate_validation: bool = False,
    ) -> None:
        if not isinstance(simulation, bool):
            raise TypeError("Manual archive requires a boolean simulation mode.")
        self._settings_source = str(settings_source)
        self._device_idn = {
            str(key): str(value) for key, value in (device_idn or {}).items()
        }
        self._operator_context = dict(operator_context or {})
        self._simulation = simulation
        self._isolate_validation = isolate_validation
        self._writer: Hdf5RunWriter | None = None
        self._path: Path | None = None
        self._frequency_grid: tuple[float, ...] | None = None
        self._last_reference: tuple[ReferenceSpectrum, int] | None = None

    def __del__(self) -> None:
        # A page can be destroyed without receiving a Qt close event (for
        # example when its route is replaced).  Release the native HDF5 handle
        # defensively so a later append does not fail with a Windows file-lock
        # error.  Explicit close() remains the normal transaction boundary.
        writer = getattr(self, "_writer", None)
        if writer is None:
            return
        try:
            writer.close("incomplete")
        except _CLOSE_CLEANUP_ERRORS:
            file_handle = getattr(writer, "_file", None)
            with contextlib.suppress(*_CLOSE_CLEANUP_ERRORS):
                if file_handle is not None and bool(file_handle.id.valid):
                    file_handle.close()

    @property
    def active_path(self) -> Path | None:
        return self._path

    @property
    def point_count(self) -> int:
        return self._writer.point_count if self._writer is not None else 0

    def save(
        self,
        trace: SpectrumTrace,
        *,
        destination: str | Path,
        mode: ManualSpectrumSaveMode | str,
        metadata_values: Sequence[ManualMetadataValue] = (),
        metadata_scope: str = "none",
        trace_variant: str = "raw",
        processed_values: Sequence[float] | None = None,
        processed_unit: str | None = None,
        processing_operation: str = "none",
        capture_context: dict[str, object] | None = None,
        reference: ReferenceSpectrum | None = None,
        saved_display: SavedSpectrumView | None = None,
    ) -> ManualSpectrumSaveResult:
        """Append one complete trace and return the actual output path."""

        selected_mode = (
            mode
            if isinstance(mode, ManualSpectrumSaveMode)
            else ManualSpectrumSaveMode(str(mode))
        )
        self._validate_metadata(metadata_values)
        if not trace_variant.strip():
            raise ExecutionError("Manual spectrum save requires a trace variant.")
        if processed_values is not None:
            processed_arr = np.asarray(processed_values, dtype=np.float64)
            if processed_arr.ndim != 1 or not np.all(np.isfinite(processed_arr)):
                raise ExecutionError("The processed manual spectrum contains NaN or infinity.")
            processed: tuple[float, ...] | None = tuple(processed_arr.tolist())
        else:
            processed = None
        trace_frequencies = self._validate_trace_frequencies(trace)
        path = _ensure_hdf5_path(Path(destination).expanduser().resolve())
        if selected_mode is ManualSpectrumSaveMode.TIMESTAMPED:
            path = timestamped_path(path)

        pending_context: tuple[str, dict[str, str], dict[str, object]] | None = None
        if capture_context is not None:
            # Snapshot before file mutation. Run identity remains immutable for
            # an append session; a new file gets the current capture context.
            capture_context = json.loads(json.dumps(capture_context, allow_nan=False))
            if capture_context.get("simulation") is not self._simulation:
                raise ExecutionError("Manual capture context simulation provenance differs.")
            pending_context = (
                str(capture_context["settings_source"]),
                dict(capture_context["device_idn"]),
                dict(capture_context["operator_context"]),
            )

        if selected_mode is ManualSpectrumSaveMode.TIMESTAMPED:
            if path.exists():
                raise ExecutionError(
                    f"Timestamped manual spectrum target already exists: {path}"
                )
            if self._writer is not None:
                self.close()
            if pending_context is not None:
                self._settings_source, self._device_idn, self._operator_context = pending_context
            writer = self._create_new_writer(path)
            self._writer = writer
            self._path = path
            self._frequency_grid = trace_frequencies
            self._last_reference = None
            try:
                point_index = self._write_capture(
                    writer,
                    trace,
                    metadata_values=metadata_values,
                    metadata_scope=metadata_scope,
                    trace_variant=trace_variant,
                    processed=processed,
                    processed_unit=processed_unit,
                    processing_operation=processing_operation,
                    capture_context=capture_context,
                    reference=reference,
                    saved_display=saved_display,
                )
                writer.close("completed")
            except Exception as exc:
                try:
                    writer.close("faulted")
                except _CLOSE_CLEANUP_ERRORS as close_error:
                    exc.add_note(
                        f"Closing the failed timestamped manual archive also failed: {close_error}"
                    )
                raise
            finally:
                self._writer = None
                self._path = None
                self._frequency_grid = None
                self._last_reference = None
            return ManualSpectrumSaveResult(
                path=path,
                point_index=point_index,
                point_count=point_index + 1,
                mode=selected_mode,
            )

        writer = self._open_for(
            path,
            selected_mode,
            trace_frequencies=trace_frequencies,
            pending_context=pending_context,
        )
        point_index = self._write_capture(
            writer,
            trace,
            metadata_values=metadata_values,
            metadata_scope=metadata_scope,
            trace_variant=trace_variant,
            processed=processed,
            processed_unit=processed_unit,
            processing_operation=processing_operation,
            capture_context=capture_context,
            reference=reference,
            saved_display=saved_display,
        )
        return ManualSpectrumSaveResult(
            path=path,
            point_index=point_index,
            point_count=point_index + 1,
            mode=selected_mode,
        )

    def _write_capture(
        self,
        writer: Hdf5RunWriter,
        trace: SpectrumTrace,
        *,
        metadata_values: Sequence[ManualMetadataValue],
        metadata_scope: str,
        trace_variant: str,
        processed: tuple[float, ...] | None,
        processed_unit: str | None,
        processing_operation: str,
        capture_context: dict[str, object] | None,
        reference: ReferenceSpectrum | None,
        saved_display: SavedSpectrumView | None,
    ) -> int:
        point_index = writer.point_count
        descriptors = [
            {
                "key": value.key,
                "device": value.device,
                "label": value.label,
                "dimension": value.dimension,
                "unit": value.unit,
                "source": value.source,
                "recorded_at_utc": value.recorded_at_utc.isoformat() if value.recorded_at_utc is not None else None,
            }
            for value in metadata_values
        ]
        measurements = {value.key: float(value.value_si) for value in metadata_values}
        reference_metadata = {}
        if reference is not None:
            if self._last_reference is not None and self._last_reference[0] == reference:
                reference_index = self._last_reference[1]
            else:
                reference_index = writer.store_reference(
                    reference.trace,
                    kind="loaded" if reference.kind == "imported" else reference.kind,
                    average_count=reference.average_count,
                    acquisition_metadata={
                        **ReferenceHdf5Store.metadata(reference, simulation_mode=self._simulation),
                        "source_file": reference.source_file,
                    },
                )
                self._last_reference = reference, reference_index
            reference_metadata["reference_index"] = reference_index
        point = MeasurementPoint(
            index=point_index,
            setpoints={},
            measurements=measurements,
            status="manual",
            timestamp_utc=trace.acquired_at_utc,
            metadata={
                "manual_spectrum_schema": MANUAL_SPECTRUM_SCHEMA,
                "trace_variant": trace_variant,
                "metadata_scope": metadata_scope,
                "selected_metadata_keys": sorted(measurements),
                "metadata_descriptors": descriptors,
                "metadata_sampled_at_utc": datetime.now(UTC).isoformat(),
                "metadata_snapshot_kind": "last_confirmed_at_save",
                "capture_context_at_save": capture_context or {},
                **({"instrument_average": capture_context["instrument_average"]}
                   if capture_context is not None and "instrument_average" in capture_context else {}),
                **({"saved_display_v1": True} if saved_display is not None else {}),
                **reference_metadata,
            },
        )
        writer.append(
            point,
            trace,
            processed_values=processed,
            processed_unit=processed_unit,
            processing_operation=processing_operation,
            saved_display=saved_display,
        )
        writer.flush_checkpoint()
        return point_index

    def close(self) -> None:
        """Close the current append session while keeping it resumable."""

        writer = self._writer
        self._writer = None
        self._path = None
        self._frequency_grid = None
        self._last_reference = None
        if writer is not None:
            # This is an explicit operator stop with committed checkpoints.
            # Keep it resumable and importable; "incomplete" denotes a crash.
            writer.close("aborted")

    def _create_new_writer(self, path: Path) -> Hdf5RunWriter:
        plan_hash = hashlib.sha256(
            MANUAL_SPECTRUM_RECIPE.encode("utf-8")
        ).hexdigest()
        return Hdf5RunWriter(
            path,
            recipe_source=MANUAL_SPECTRUM_RECIPE,
            settings_source=self._settings_source,
            plan_hash=plan_hash,
            device_idn=self._device_idn,
            operator_context=self._operator_context,
            simulation_metadata={
                "enabled": self._simulation,
                "manual_spectrum": True,
            },
            run_attributes={"manual_spectrum_schema": MANUAL_SPECTRUM_SCHEMA},
            expected_points=None,
            isolate_validation=self._isolate_validation,
        )

    def _open_for(
        self,
        path: Path,
        mode: ManualSpectrumSaveMode,
        *,
        trace_frequencies: tuple[float, ...] | None = None,
        pending_context: tuple[str, dict[str, str], dict[str, object]] | None = None,
    ) -> Hdf5RunWriter:
        path = path.expanduser().resolve()
        if self._writer is not None and self._path == path:
            if trace_frequencies is not None:
                self._validate_grid_frequencies(self._frequency_grid, trace_frequencies)
                if self._frequency_grid is None:
                    self._frequency_grid = trace_frequencies
            if pending_context is not None:
                self._settings_source, self._device_idn, self._operator_context = pending_context
            return self._writer

        # Validate the target archive and incoming trace grid BEFORE closing any
        # existing append session, so a rejected target never disrupts the live session.
        if path.exists():
            if mode is not ManualSpectrumSaveMode.APPEND:
                raise ExecutionError(
                    f"Timestamped manual spectrum target already exists: {path}"
                )
            identity, checkpoint_count, frequency_grid = _read_archive_identity(path)
            simulation_metadata = json.loads(identity["simulation_json"])
            if simulation_metadata.get("enabled") is not self._simulation:
                raise ExecutionError("Manual archive simulation provenance differs; choose a new file.")
            if trace_frequencies is not None:
                self._validate_grid_frequencies(frequency_grid, trace_frequencies)
            if self._writer is not None:
                self.close()
            if pending_context is not None:
                self._settings_source, self._device_idn, self._operator_context = pending_context
            writer = Hdf5RunWriter.resume(
                path,
                recipe_source=identity["recipe_source"],
                settings_source=identity["settings_source"],
                plan_hash=identity["plan_hash"],
                checkpoint_count=checkpoint_count,
                expected_points=None,
                operator_context=self._operator_context,
                isolate_validation=self._isolate_validation,
            )
            self._frequency_grid = frequency_grid if frequency_grid is not None else trace_frequencies
        else:
            if self._writer is not None:
                self.close()
            if pending_context is not None:
                self._settings_source, self._device_idn, self._operator_context = pending_context
            writer = self._create_new_writer(path)
            self._frequency_grid = trace_frequencies
        self._writer = writer
        self._path = path
        return writer

    @staticmethod
    def _validate_trace_frequencies(trace: SpectrumTrace) -> tuple[float, ...]:
        frequencies = np.asarray(trace.frequencies_hz, dtype=np.float64)
        powers = np.asarray(trace.powers_dbm, dtype=np.float64)
        if frequencies.ndim != 1 or powers.ndim != 1 or frequencies.size != powers.size or frequencies.size < 2:
            raise ExecutionError("Manual spectrum trace must contain at least two matching frequency and power points.")
        if not (np.all(np.isfinite(frequencies)) and np.all(np.isfinite(powers))):
            raise ExecutionError("Manual spectrum trace contains NaN or infinity.")
        return tuple(frequencies.tolist())

    @staticmethod
    def _validate_grid_frequencies(
        existing_grid: tuple[float, ...] | None,
        incoming_frequencies: tuple[float, ...],
    ) -> None:
        if existing_grid is None:
            return
        if not frequency_grids_match(existing_grid, incoming_frequencies):
            raise ExecutionError(
                "The manual archive already contains a different frequency grid; "
                "start a new timestamped file for this analyser configuration."
            )

    def _validate_grid(self, trace: SpectrumTrace) -> None:
        frequencies = self._validate_trace_frequencies(trace)
        self._validate_grid_frequencies(self._frequency_grid, frequencies)
        if self._frequency_grid is None:
            self._frequency_grid = frequencies

    @staticmethod
    def _validate_metadata(values: Sequence[ManualMetadataValue]) -> None:
        keys: set[str] = set()
        for value in values:
            if value.key in keys:
                raise ExecutionError(
                    f"Manual metadata contains duplicate key {value.key!r}."
                )
            keys.add(value.key)
            if not math.isfinite(float(value.value_si)):
                raise ExecutionError(
                    f"Manual metadata {value.key!r} is not finite."
                )


def timestamped_path(
    base: str | Path, *, timestamp: datetime | None = None
) -> Path:
    """Return a collision-free ``stem_YYYYmmddTHHMMSS.ffffffZ.h5`` path."""

    candidate = _ensure_hdf5_path(Path(base).expanduser())
    moment = (timestamp or datetime.now(UTC)).astimezone(UTC)
    stamp = moment.strftime("%Y%m%dT%H%M%S.%fZ")
    output = candidate.with_name(f"{candidate.stem}_{stamp}{candidate.suffix}")
    serial = 2
    while output.exists():
        output = candidate.with_name(
            f"{candidate.stem}_{stamp}_{serial}{candidate.suffix}"
        )
        serial += 1
    return output


def _read_archive_identity(
    path: Path,
) -> tuple[dict[str, str], int, tuple[float, ...] | None]:
    try:
        import h5py
    except ImportError as exc:
        raise ExecutionError("Appending manual spectra requires h5py.") from exc
    try:
        with h5py.File(path, "r") as file:
            run = file.get("run")
            if run is None:
                raise ExecutionError("The selected HDF5 has no private /run group.")
            marker = _text(run.attrs.get("manual_spectrum_schema"))
            if marker != MANUAL_SPECTRUM_SCHEMA:
                raise ExecutionError(
                    "The selected HDF5 was not created by the manual spectrum saver."
                )
            status = _text(run.attrs.get("status")) or "incomplete"
            if status in {"completed", "faulted"}:
                raise ExecutionError(
                    f"The selected manual archive is closed with status {status!r}; "
                    "choose a new timestamped file."
                )
            recipe_source = _dataset_text(run, "recipe_yaml")
            settings_source = _dataset_text(run, "settings_yaml")
            plan_hash = _text(run.attrs.get("plan_sha256"))
            if not plan_hash:
                raise ExecutionError("The selected manual archive has no plan identity.")
            points = file.get("points")
            names = sorted(
                (int(name), name)
                for name in (points.keys() if points is not None else ())
                if str(name).isdigit()
            )
            expected = list(range(len(names)))
            if [number for number, _name in names] != expected:
                raise ExecutionError("Manual archive checkpoints are not contiguous.")
            for _number, name in names:
                if not bool(points[name].attrs.get("complete", False)):
                    raise ExecutionError(
                        f"Manual archive checkpoint {name} is not complete."
                    )
            frequency_grid: tuple[float, ...] | None = None
            spectra = file.get("spectra")
            if spectra is not None and names:
                first = spectra.get("0")
                if first is not None and "frequency_hz" in first:
                    frequency_grid = tuple(float(value) for value in first["frequency_hz"][:])
            return (
                {
                    "recipe_source": recipe_source,
                    "settings_source": settings_source,
                    "plan_hash": plan_hash,
                    "simulation_json": _dataset_text(run, "simulation_json"),
                },
                len(names),
                frequency_grid,
            )
    except OSError as exc:
        raise ExecutionError(f"Cannot inspect manual archive {path}: {exc}") from exc


def _dataset_text(group: object, name: str) -> str:
    try:
        value = group[name][()]
    except (KeyError, TypeError) as exc:
        raise ExecutionError(f"Manual archive is missing /run/{name}.") from exc
    return _text(value)


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")
