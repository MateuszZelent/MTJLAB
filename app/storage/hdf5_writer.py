"""Crash-tolerant HDF5 writer with per-spectrum flushes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
from typing import Any

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
        self._file = h5py.File(self.path, "w", libver="latest")
        self._points = self._file.create_group("points")
        self._spectra = self._file.create_group("spectra")
        run = self._file.create_group("run")
        run.attrs["created_at_utc"] = datetime.now(timezone.utc).isoformat()
        run.attrs["plan_sha256"] = plan_hash
        run.create_dataset("recipe_yaml", data=recipe_source, dtype=h5py.string_dtype("utf-8"))
        run.create_dataset("settings_yaml", data=settings_source, dtype=h5py.string_dtype("utf-8"))
        run.create_dataset("device_idn_json", data=json.dumps(device_idn, sort_keys=True), dtype=h5py.string_dtype("utf-8"))
        self._point_count = 0
        self._closed = False
        self._file.flush()

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
        self._point_count += 1
        self._file.flush()
        return index

    def close(self, status: str) -> None:
        if self._closed:
            return
        self._file["run"].attrs["status"] = status
        self._file.flush()
        self._file.close()
        self._closed = True

