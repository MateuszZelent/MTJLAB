"""Durable storage for spectra captured from the manual Anritsu page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import math
from pathlib import Path
from typing import Sequence

from app.domain.errors import ExecutionError
from app.domain.manual_metadata import ManualMetadataValue
from app.domain.models import MeasurementPoint
from app.devices.anritsu_ms2830a import SpectrumTrace
from app.spectrum import frequency_grids_match

from .hdf5_writer import Hdf5RunWriter


MANUAL_SPECTRUM_SCHEMA = "manual-spectrum-v1"
MANUAL_SPECTRUM_RECIPE = "schema_version: 1\n"


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
    ) -> None:
        self._settings_source = str(settings_source)
        self._device_idn = {
            str(key): str(value) for key, value in (device_idn or {}).items()
        }
        self._operator_context = dict(operator_context or {})
        self._writer: Hdf5RunWriter | None = None
        self._path: Path | None = None
        self._frequency_grid: tuple[float, ...] | None = None

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
        except Exception:
            file_handle = getattr(writer, "_file", None)
            try:
                if file_handle is not None and bool(file_handle.id.valid):
                    file_handle.close()
            except Exception:
                pass

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
            processed = tuple(float(value) for value in processed_values)
            if not all(math.isfinite(value) for value in processed):
                raise ExecutionError("The processed manual spectrum contains NaN or infinity.")
        else:
            processed = None
        path = Path(destination).expanduser().resolve()
        if not path.suffix:
            path = path.with_suffix(".h5")
        if path.suffix.lower() not in {".h5", ".hdf5"}:
            path = path.with_suffix(".h5")
        if selected_mode is ManualSpectrumSaveMode.TIMESTAMPED:
            path = timestamped_path(path)

        writer = self._open_for(path, selected_mode)
        self._validate_grid(trace)
        point_index = writer.point_count
        descriptors = [
            {
                "key": value.key,
                "device": value.device,
                "label": value.label,
                "dimension": value.dimension,
                "unit": value.unit,
                "source": value.source,
            }
            for value in metadata_values
        ]
        measurements = {value.key: float(value.value_si) for value in metadata_values}
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
            },
        )
        writer.append(
            point,
            trace,
            processed_values=processed,
            processed_unit=processed_unit,
            processing_operation=processing_operation,
        )
        writer.flush_checkpoint()
        if selected_mode is ManualSpectrumSaveMode.TIMESTAMPED:
            writer.close("completed")
            self._writer = None
            self._path = None
            self._frequency_grid = None
        return ManualSpectrumSaveResult(
            path=path,
            point_index=point_index,
            point_count=point_index + 1,
            mode=selected_mode,
        )

    def close(self) -> None:
        """Close the current append session while keeping it resumable."""

        writer = self._writer
        self._writer = None
        self._path = None
        self._frequency_grid = None
        if writer is not None:
            writer.close("incomplete")

    def _open_for(
        self, path: Path, mode: ManualSpectrumSaveMode
    ) -> Hdf5RunWriter:
        path = path.expanduser().resolve()
        if self._writer is not None and self._path == path:
            return self._writer
        if self._writer is not None:
            self.close()

        if path.exists():
            if mode is not ManualSpectrumSaveMode.APPEND:
                raise ExecutionError(
                    f"Timestamped manual spectrum target already exists: {path}"
                )
            identity, checkpoint_count, frequency_grid = _read_archive_identity(path)
            writer = Hdf5RunWriter.resume(
                path,
                recipe_source=identity["recipe_source"],
                settings_source=identity["settings_source"],
                plan_hash=identity["plan_hash"],
                checkpoint_count=checkpoint_count,
                expected_points=None,
                operator_context=self._operator_context,
            )
            self._frequency_grid = frequency_grid
        else:
            plan_hash = hashlib.sha256(
                MANUAL_SPECTRUM_RECIPE.encode("utf-8")
            ).hexdigest()
            writer = Hdf5RunWriter(
                path,
                recipe_source=MANUAL_SPECTRUM_RECIPE,
                settings_source=self._settings_source,
                plan_hash=plan_hash,
                device_idn=self._device_idn,
                operator_context=self._operator_context,
                simulation_metadata={
                    "enabled": False,
                    "manual_spectrum": True,
                },
                run_attributes={"manual_spectrum_schema": MANUAL_SPECTRUM_SCHEMA},
                expected_points=None,
            )
            self._frequency_grid = None
        self._writer = writer
        self._path = path
        return writer

    def _validate_grid(self, trace: SpectrumTrace) -> None:
        frequencies = tuple(float(value) for value in trace.frequencies_hz)
        if self._frequency_grid is None:
            self._frequency_grid = frequencies
            return
        if not frequency_grids_match(self._frequency_grid, frequencies):
            raise ExecutionError(
                "The manual archive already contains a different frequency grid; "
                "start a new timestamped file for this analyser configuration."
            )

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

    candidate = Path(base).expanduser()
    if not candidate.suffix:
        candidate = candidate.with_suffix(".h5")
    if candidate.suffix.lower() not in {".h5", ".hdf5"}:
        candidate = candidate.with_suffix(".h5")
    moment = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
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
