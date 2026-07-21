"""Read-only access to durable HDF5 measurement artefacts.

The GUI uses this module rather than opening HDF5 files directly.  Keeping
the parsing here gives operators one consistent view of both completed and
interrupted runs, and keeps an accidentally malformed result file from
crashing the Qt event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from app.domain.errors import ExecutionError
from app.spectrum import peak_preserving_indices


@dataclass(frozen=True, slots=True)
class RunSummary:
    path: Path
    created_at_utc: str | None
    status: str
    point_count: int
    spectrum_count: int
    plan_sha256: str | None
    application_version: str | None


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


@dataclass(frozen=True, slots=True)
class StoredEvent:
    timestamp_utc: str
    severity: str
    name: str
    data: dict[str, Any]


class Hdf5RunReader:
    """Read schema-version-1 HDF5 runs without ever modifying them."""

    @staticmethod
    def list_runs(directory: str | Path) -> tuple[RunSummary, ...]:
        output_dir = Path(directory)
        if not output_dir.exists():
            return ()
        summaries: list[RunSummary] = []
        paths = {
            *output_dir.glob("*.h5"),
            *output_dir.glob("*.hdf5"),
        }
        for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                summaries.append(Hdf5RunReader.summary(path))
            except ExecutionError:
                try:
                    from app.storage.thatec_reader import ThatecRunReader

                    run = ThatecRunReader.describe(path)
                    shapes = [row.shape[0] for row in run.rows.values() if row.shape]
                    summaries.append(
                        RunSummary(
                            path=path,
                            created_at_utc=None,
                            status="THATEC",
                            point_count=max(shapes, default=0),
                            spectrum_count=sum(
                                len(row.shape) >= 2 for row in run.rows.values()
                            ),
                            plan_sha256=None,
                            application_version=None,
                        )
                    )
                except ExecutionError:
                    # Keep a genuinely unreadable result visible to the operator so it can
                    # be recovered externally, rather than silently hiding it.
                    summaries.append(
                        RunSummary(
                            path=path,
                            created_at_utc=None,
                            status="unreadable",
                            point_count=0,
                            spectrum_count=0,
                            plan_sha256=None,
                            application_version=None,
                        )
                    )
        return tuple(summaries)

    @staticmethod
    def summary(path: str | Path) -> RunSummary:
        with Hdf5RunReader._open(path) as file:
            run = Hdf5RunReader._require_group(file, "run")
            return RunSummary(
                path=Path(path),
                created_at_utc=Hdf5RunReader._attribute_text(run.attrs.get("created_at_utc")),
                status=Hdf5RunReader._attribute_text(run.attrs.get("status")) or "incomplete",
                point_count=len(file.get("points", {})),
                spectrum_count=len(file.get("spectra", {})),
                plan_sha256=Hdf5RunReader._attribute_text(run.attrs.get("plan_sha256")),
                application_version=Hdf5RunReader._attribute_text(run.attrs.get("application_version")),
            )

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
        return RunSummary(
            path=path,
            created_at_utc=Hdf5RunReader._attribute_text(run.attrs.get("created_at_utc")),
            status=Hdf5RunReader._attribute_text(run.attrs.get("status")) or "incomplete",
            point_count=len(file.get("points", {})),
            spectrum_count=len(file.get("spectra", {})),
            plan_sha256=Hdf5RunReader._attribute_text(run.attrs.get("plan_sha256")),
            application_version=Hdf5RunReader._attribute_text(run.attrs.get("application_version")),
        )

    @staticmethod
    def points(path: str | Path) -> tuple[StoredPoint, ...]:
        with Hdf5RunReader._open(path) as file:
            points = file.get("points")
            if points is None:
                return ()
            spectra = file.get("spectra", {})
            result: list[StoredPoint] = []
            for name in Hdf5RunReader._numeric_names(points):
                group = points[name]
                result.append(
                    StoredPoint(
                        index=int(name),
                        timestamp_utc=Hdf5RunReader._attribute_text(group.attrs.get("timestamp_utc")),
                        status=Hdf5RunReader._attribute_text(group.attrs.get("status")) or "unknown",
                        setpoints=Hdf5RunReader._dataset_json(group, "setpoints_json"),
                        measurements=Hdf5RunReader._dataset_json(group, "measurements_json"),
                        metadata=Hdf5RunReader._dataset_json(group, "metadata_json"),
                        device_states=Hdf5RunReader._dataset_json(group, "device_states_json"),
                        has_spectrum=name in spectra,
                    )
                )
            return tuple(result)

    @staticmethod
    def spectrum_point_count(path: str | Path, index: int) -> int:
        """Return a stored spectrum size without materialising either data axis."""

        if index < 0:
            raise ExecutionError("Spectrum index cannot be negative.")
        with Hdf5RunReader._open(path) as file:
            spectra = file.get("spectra")
            if spectra is None or str(index) not in spectra:
                return 0
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
            group = spectra[str(index)]
            try:
                frequencies = tuple(float(value) for value in group["frequency_hz"][:])
                powers = tuple(float(value) for value in group["power_dbm"][:])
            except KeyError as exc:
                raise ExecutionError(f"Spectrum {index} does not contain a complete data axis.") from exc
            if len(frequencies) != len(powers) or not frequencies:
                raise ExecutionError(f"Spectrum {index} has a mismatched or empty point count.")
            source_count = len(frequencies)
            processed: tuple[float, ...] | None = None
            if "processed_values" in group:
                processed = tuple(float(value) for value in group["processed_values"][:])
                if len(processed) != source_count:
                    raise ExecutionError(
                        f"Spectrum {index} has a mismatched processed point count."
                    )
            if max_points is not None and max_points > 0 and source_count > max_points:
                selected = peak_preserving_indices(powers, max_points)
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
    def references(path: str | Path) -> tuple[StoredReference, ...]:
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
            result: list[StoredReference] = []
            for index, group in groups:
                try:
                    frequencies = tuple(float(value) for value in group["frequency_hz"][:])
                    powers = tuple(float(value) for value in group["power_dbm"][:])
                except KeyError as exc:
                    raise ExecutionError(
                        f"Reference {index} does not contain a complete spectrum."
                    ) from exc
                if len(frequencies) != len(powers) or not frequencies:
                    raise ExecutionError(
                        f"Reference {index} has a mismatched or empty point count."
                    )
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
                    )
                )
            return tuple(result)

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
    def _numeric_names(group: Any) -> tuple[str, ...]:
        names: list[tuple[int, str]] = []
        for name in group.keys():
            try:
                names.append((int(name), str(name)))
            except ValueError:
                continue
        return tuple(name for _, name in sorted(names))
