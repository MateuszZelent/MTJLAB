"""Crash-tolerant HDF5 writer with per-spectrum flushes."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import asdict, is_dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import csv
import hashlib
import json
from typing import Any, TextIO

from app.devices.anritsu.adapter import SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint


class Hdf5RunWriter:
    """Persist immutable run metadata and append one checkpoint per spectrum."""

    def __init__(
        self,
        path: str | Path,
        *,
        recipe_source: str,
        settings_source: str,
        plan_hash: str,
        device_idn: dict[str, str],
        device_capabilities: dict[str, object] | None = None,
        csv_summary_path: str | Path | None = None,
    ) -> None:
        try:
            import h5py
            import numpy as np
        except ImportError as exc:
            raise ExecutionError("Zapis HDF5 wymaga h5py i numpy; zainstaluj zależności projektu.") from exc
        self._h5py = h5py
        self._np = np
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_summary_path = Path(csv_summary_path) if csv_summary_path is not None else None
        self._csv_stream: TextIO | None = None
        self._csv_writer: csv.DictWriter[str] | None = None
        self._file = h5py.File(self.path, "w", libver="latest")
        self._points = self._file.create_group("points")
        self._spectra = self._file.create_group("spectra")
        events = self._file.create_group("events")
        string_dtype = h5py.string_dtype("utf-8")
        self._event_timestamps = events.create_dataset("timestamp", shape=(0,), maxshape=(None,), dtype=string_dtype)
        self._event_severities = events.create_dataset("severity", shape=(0,), maxshape=(None,), dtype=string_dtype)
        self._event_names = events.create_dataset("name", shape=(0,), maxshape=(None,), dtype=string_dtype)
        self._event_messages = events.create_dataset("message", shape=(0,), maxshape=(None,), dtype=string_dtype)
        run = self._file.create_group("run")
        run.attrs["created_at_utc"] = datetime.now(timezone.utc).isoformat()
        run.attrs["plan_sha256"] = plan_hash
        run.attrs["recipe_source_sha256"] = hashlib.sha256(recipe_source.encode("utf-8")).hexdigest()
        run.attrs["settings_sha256"] = hashlib.sha256(settings_source.encode("utf-8")).hexdigest()
        try:
            run.attrs["application_version"] = version("lab-control")
        except PackageNotFoundError:
            run.attrs["application_version"] = "0.1.0+source"
        run.create_dataset("recipe_yaml", data=recipe_source, dtype=h5py.string_dtype("utf-8"))
        run.create_dataset("settings_yaml", data=settings_source, dtype=h5py.string_dtype("utf-8"))
        run.create_dataset("device_idn_json", data=json.dumps(device_idn, sort_keys=True), dtype=h5py.string_dtype("utf-8"))
        capabilities = device_capabilities or {}
        run.create_dataset(
            "capabilities_json",
            data=json.dumps(self._serializable(capabilities), sort_keys=True),
            dtype=h5py.string_dtype("utf-8"),
        )
        self._point_count = 0
        self._closed = False
        if self.csv_summary_path is not None:
            self._open_csv_summary()
        self._file.flush()

    def _open_csv_summary(self) -> None:
        assert self.csv_summary_path is not None
        self.csv_summary_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stream = self.csv_summary_path.open("w", encoding="utf-8", newline="")
            writer = csv.DictWriter(
                stream,
                fieldnames=(
                    "point_index",
                    "timestamp_utc",
                    "status",
                    "setpoints_json",
                    "measurements_json",
                    "trace_name",
                    "trace_points",
                    "trace_peak_dbm",
                ),
            )
            writer.writeheader()
            stream.flush()
            self._csv_stream = stream
            self._csv_writer = writer
        except Exception as exc:
            self._file.close()
            raise ExecutionError(f"Nie można utworzyć indeksu CSV: {exc}") from exc

    @staticmethod
    def _serializable(value: object) -> object:
        if is_dataclass(value):
            return Hdf5RunWriter._serializable(asdict(value))
        if isinstance(value, dict):
            return {str(key): Hdf5RunWriter._serializable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [Hdf5RunWriter._serializable(item) for item in value]
        return value

    def append(self, point: MeasurementPoint, trace: SpectrumTrace | None = None) -> int:
        if self._closed:
            raise ExecutionError("Próba zapisu do zamkniętego pliku HDF5.")
        index = self._point_count
        group = self._points.create_group(str(index))
        group.attrs["timestamp_utc"] = point.timestamp_utc.isoformat()
        group.attrs["status"] = point.status
        group.create_dataset("setpoints_json", data=json.dumps(point.setpoints, sort_keys=True))
        group.create_dataset("measurements_json", data=json.dumps(point.measurements, sort_keys=True))
        group.create_dataset("metadata_json", data=json.dumps(point.metadata, sort_keys=True))
        if trace is not None:
            spectrum = self._spectra.create_group(str(index))
            spectrum.attrs["trace_name"] = trace.trace_name
            spectrum.attrs["acquired_at_utc"] = trace.acquired_at_utc.isoformat()
            spectrum.create_dataset("frequency_hz", data=self._np.asarray(trace.frequencies_hz, dtype="f8"), compression="gzip")
            spectrum.create_dataset("power_dbm", data=self._np.asarray(trace.powers_dbm, dtype="f8"), compression="gzip")
        self._append_csv_summary(index, point, trace)
        self._point_count += 1
        self._file.flush()
        return index

    def _append_csv_summary(self, index: int, point: MeasurementPoint, trace: SpectrumTrace | None) -> None:
        if self._csv_writer is None or self._csv_stream is None:
            return
        self._csv_writer.writerow(
            {
                "point_index": index,
                "timestamp_utc": point.timestamp_utc.isoformat(),
                "status": point.status,
                "setpoints_json": json.dumps(point.setpoints, sort_keys=True),
                "measurements_json": json.dumps(point.measurements, sort_keys=True),
                "trace_name": trace.trace_name if trace is not None else "",
                "trace_points": len(trace.powers_dbm) if trace is not None else 0,
                "trace_peak_dbm": max(trace.powers_dbm) if trace is not None else "",
            }
        )
        self._csv_stream.flush()

    def append_event(self, name: str, data: dict[str, object], *, severity: str = "info") -> None:
        """Append an engine event without forcing an extra disk flush.

        The following per-spectrum checkpoint (or ``close``) flushes it with
        the rest of the run state.  Event messages are JSON so future fields
        can be added without breaking older result readers.
        """

        if self._closed:
            raise ExecutionError("Próba zapisu zdarzenia do zamkniętego pliku HDF5.")
        timestamp = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
        message = json.dumps(self._serializable(data), sort_keys=True)
        index = len(self._event_names)
        for dataset, value in (
            (self._event_timestamps, timestamp),
            (self._event_severities, severity),
            (self._event_names, name),
            (self._event_messages, message),
        ):
            dataset.resize((index + 1,))
            dataset[index] = value

    def close(self, status: str) -> None:
        if self._closed:
            return
        self._file["run"].attrs["status"] = status
        self._file.flush()
        self._file.close()
        if self._csv_stream is not None:
            self._csv_stream.flush()
            self._csv_stream.close()
            self._csv_stream = None
            self._csv_writer = None
        self._closed = True
