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
    events: tuple["StoredEvent", ...]


@dataclass(frozen=True, slots=True)
class StoredPoint:
    index: int
    timestamp_utc: str | None
    status: str
    setpoints: dict[str, Any]
    measurements: dict[str, Any]
    metadata: dict[str, Any]
    has_spectrum: bool


@dataclass(frozen=True, slots=True)
class StoredSpectrum:
    index: int
    trace_name: str
    acquired_at_utc: str | None
    frequencies_hz: tuple[float, ...]
    powers_dbm: tuple[float, ...]
    source_point_count: int


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
        for path in sorted(output_dir.glob("*.h5"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                summaries.append(Hdf5RunReader.summary(path))
            except ExecutionError:
                # Keep an unreadable result visible to the operator so it can
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
                        has_spectrum=name in spectra,
                    )
                )
            return tuple(result)

    @staticmethod
    def spectrum(path: str | Path, index: int, *, max_points: int | None = None) -> StoredSpectrum | None:
        if index < 0:
            raise ExecutionError("Indeks widma nie może być ujemny.")
        with Hdf5RunReader._open(path) as file:
            spectra = file.get("spectra")
            if spectra is None or str(index) not in spectra:
                return None
            group = spectra[str(index)]
            try:
                frequencies = tuple(float(value) for value in group["frequency_hz"][:])
                powers = tuple(float(value) for value in group["power_dbm"][:])
            except KeyError as exc:
                raise ExecutionError(f"Widmo {index} nie zawiera kompletnej osi danych.") from exc
            if len(frequencies) != len(powers) or not frequencies:
                raise ExecutionError(f"Widmo {index} ma niezgodną lub pustą liczbę punktów.")
            source_count = len(frequencies)
            if max_points is not None and max_points > 0 and source_count > max_points:
                stride = max(1, source_count // max_points)
                selected = list(range(0, source_count, stride))
                if selected[-1] != source_count - 1:
                    selected.append(source_count - 1)
                frequencies = tuple(frequencies[item] for item in selected)
                powers = tuple(powers[item] for item in selected)
            return StoredSpectrum(
                index=index,
                trace_name=Hdf5RunReader._attribute_text(group.attrs.get("trace_name")) or "TRAC1",
                acquired_at_utc=Hdf5RunReader._attribute_text(group.attrs.get("acquired_at_utc")),
                frequencies_hz=frequencies,
                powers_dbm=powers,
                source_point_count=source_count,
            )

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
            raise ExecutionError("Dziennik zdarzeń HDF5 ma niespójne długości kolumn.")
        events: list[StoredEvent] = []
        for time, level, name, message in zip(timestamp, severity, names, messages, strict=True):
            try:
                payload = json.loads(message)
            except json.JSONDecodeError as exc:
                raise ExecutionError("Dziennik zdarzeń zawiera nieprawidłowy JSON.") from exc
            if not isinstance(payload, dict):
                raise ExecutionError("Wiadomość dziennika zdarzeń musi być obiektem JSON.")
            events.append(StoredEvent(time, level, name, payload))
        return tuple(events)

    @staticmethod
    def _open(path: str | Path):
        try:
            import h5py
        except ImportError as exc:
            raise ExecutionError("Odczyt wyników HDF5 wymaga pakietu h5py.") from exc
        try:
            return h5py.File(Path(path), "r")
        except OSError as exc:
            raise ExecutionError(f"Nie można odczytać pliku HDF5 {Path(path).name}: {exc}") from exc

    @staticmethod
    def _require_group(file: Any, name: str) -> Any:
        group = file.get(name)
        if group is None:
            raise ExecutionError(f"Plik HDF5 nie zawiera wymaganej grupy /{name}.")
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
            raise ExecutionError(f"Nieprawidłowy JSON w {name}.") from exc
        if not isinstance(decoded, dict):
            raise ExecutionError(f"{name} musi zawierać obiekt JSON.")
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
