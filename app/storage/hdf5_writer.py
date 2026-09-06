"""Crash-tolerant HDF5 writer with per-spectrum flushes."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import asdict, is_dataclass
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import csv
import hashlib
import json
from typing import TextIO

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.recipes.models import legacy_dut_limits_policy
from app.storage.thatec_writer import ThatecHdf5Writer
from app.version import get_full_version


def _spectrum_compression(point_count: int) -> dict[str, object]:
    """Avoid CPU-bound gzip for full 10k-point production traces."""

    if point_count >= 10_000:
        return {}
    return {"compression": "gzip", "compression_opts": 1}


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
        expected_points: int | None = None,
        operator_context: dict[str, object] | None = None,
        simulation_metadata: dict[str, object] | None = None,
        run_attributes: dict[str, object] | None = None,
    ) -> None:
        try:
            import h5py
            import numpy as np
        except ImportError as exc:
            raise ExecutionError(
                "Writing HDF5 requires h5py and numpy; install the project dependencies."
            ) from exc
        self._h5py = h5py
        self._np = np
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_summary_path = Path(csv_summary_path) if csv_summary_path is not None else None
        self._csv_stream: TextIO | None = None
        self._csv_writer: csv.DictWriter[str] | None = None
        try:
            self._file = h5py.File(self.path, "x", libver="latest")
        except (FileExistsError, OSError) as exc:
            raise ExecutionError(
                f"The result file already exists or cannot be created: {self.path}"
            ) from exc
        self._points = self._file.create_group("points")
        self._spectra = self._file.create_group("spectra")
        self._references = self._file.create_group("references")
        self._pending = self._file.create_group("_pending")
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
            run.attrs["application_version"] = get_full_version()
        run.create_dataset("recipe_yaml", data=recipe_source, dtype=h5py.string_dtype("utf-8"))
        run.create_dataset("settings_yaml", data=settings_source, dtype=h5py.string_dtype("utf-8"))
        run.create_dataset(
            "dut_limits_json",
            data=json.dumps(self._recipe_dut_limits(recipe_source), sort_keys=True),
            dtype=h5py.string_dtype("utf-8"),
        )
        run.create_dataset(
            "dut_limits_policy_json",
            data=json.dumps(legacy_dut_limits_policy(), sort_keys=True),
            dtype=h5py.string_dtype("utf-8"),
        )
        run.create_dataset("device_idn_json", data=json.dumps(device_idn, sort_keys=True), dtype=h5py.string_dtype("utf-8"))
        capabilities = device_capabilities or {}
        run.create_dataset(
            "capabilities_json",
            data=json.dumps(self._serializable(capabilities), sort_keys=True),
            dtype=h5py.string_dtype("utf-8"),
        )
        run.create_dataset(
            "operator_context_json",
            data=json.dumps(self._serializable(operator_context or {}), sort_keys=True),
            dtype=h5py.string_dtype("utf-8"),
        )
        if operator_context and isinstance(operator_context, dict) and "username" in operator_context:
            run.attrs["operator"] = str(operator_context["username"])
        run.create_dataset(
            "simulation_json",
            data=json.dumps(self._serializable(simulation_metadata or {"enabled": False}), sort_keys=True),
            dtype=h5py.string_dtype("utf-8"),
        )
        for attribute, value in (run_attributes or {}).items():
            if isinstance(value, (str, bytes, int, float, bool)):
                run.attrs[str(attribute)] = value
            else:
                run.attrs[str(attribute)] = json.dumps(
                    self._serializable(value), sort_keys=True
                )
        self._thatec = ThatecHdf5Writer(
            self._file,
            device_idn=device_idn,
            plan_hash=plan_hash,
            expected_points=expected_points,
            recipe_source=recipe_source,
            settings_source=settings_source,
        )
        self._point_count = 0
        self._closed = False
        if self.csv_summary_path is not None:
            self._open_csv_summary()
        self._file.flush()

    @property
    def point_count(self) -> int:
        """Number of committed private checkpoints currently in the file."""

        return self._point_count

    @classmethod
    def resume(
        cls,
        path: str | Path,
        *,
        recipe_source: str,
        settings_source: str,
        plan_hash: str,
        checkpoint_count: int,
        expected_points: int | None = None,
        csv_summary_path: str | Path | None = None,
        operator_context: dict[str, object] | None = None,
    ) -> "Hdf5RunWriter":
        """Resume an existing run only after an externally verified safe boundary."""

        try:
            import h5py
            import numpy as np
        except ImportError as exc:
            raise ExecutionError(
                "Writing HDF5 requires h5py and numpy; install the project dependencies."
            ) from exc
        target = Path(path)
        self = cls.__new__(cls)
        self._h5py = h5py
        self._np = np
        self.path = target
        self.csv_summary_path = Path(csv_summary_path) if csv_summary_path is not None else None
        self._csv_stream = None
        self._csv_writer = None
        try:
            self._file = h5py.File(target, "r+", libver="latest")
        except OSError as exc:
            raise ExecutionError(f"Cannot open the run file for resumption: {target}") from exc
        try:
            self._validate_resume_identity(recipe_source, settings_source, plan_hash)
            self._points = self._file["points"]
            self._spectra = self._file["spectra"]
            self._references = self._file.require_group("references")
            if "reference" in self._file and "0" not in self._references:
                self._references["0"] = self._file["reference"]
            self._pending = self._file["_pending"]
            events = self._file["events"]
            self._event_timestamps = events["timestamp"]
            self._event_severities = events["severity"]
            self._event_names = events["name"]
            self._event_messages = events["message"]
            self._truncate_to_checkpoint(checkpoint_count)
            self._thatec = ThatecHdf5Writer.resume(
                self._file,
                checkpoint_count=checkpoint_count,
                expected_points=expected_points,
                recipe_source=recipe_source,
            )
            self._point_count = checkpoint_count
            self._closed = False
            run = self._file["run"]
            previous_status = str(run.attrs.get("status", "incomplete"))
            run.attrs["status"] = "running"
            run.attrs["resumed_at_utc"] = datetime.now(timezone.utc).isoformat()
            run.attrs["resume_count"] = int(run.attrs.get("resume_count", 0)) + 1
            if "storage_validation_error" in run.attrs:
                del run.attrs["storage_validation_error"]
            if self.csv_summary_path is not None:
                self._open_csv_summary()
                self._rebuild_csv_summary_from_committed_points()
            self.append_event(
                "run_resumed",
                {
                    "checkpoint_count": checkpoint_count,
                    "previous_status": previous_status,
                    "operator_context": self._serializable(operator_context or {}),
                },
            )
            self._file.flush()
            return self
        except Exception:
            self._file.close()
            raise

    def _validate_resume_identity(
        self,
        recipe_source: str,
        settings_source: str,
        plan_hash: str,
    ) -> None:
        run = self._file.get("run")
        if run is None:
            raise ExecutionError("Run recovery requires the private /run metadata group.")
        expected = {
            "plan_sha256": plan_hash,
            "recipe_source_sha256": hashlib.sha256(recipe_source.encode("utf-8")).hexdigest(),
            "settings_sha256": hashlib.sha256(settings_source.encode("utf-8")).hexdigest(),
        }
        for attribute, value in expected.items():
            if str(run.attrs.get(attribute, "")) != value:
                raise ExecutionError(f"Run recovery rejected: {attribute} does not match.")
        status = str(run.attrs.get("status", "incomplete"))
        if status == "completed":
            raise ExecutionError("A completed run cannot be resumed.")

    def _truncate_to_checkpoint(self, checkpoint_count: int) -> None:
        if checkpoint_count < 0:
            raise ExecutionError("Recovery checkpoint count cannot be negative.")
        committed = sorted(
            (int(name), name) for name in self._points if str(name).isdigit()
        )
        if committed and [number for number, _name in committed] != list(
            range(committed[-1][0] + 1)
        ):
            raise ExecutionError("Committed private checkpoints are not contiguous.")
        if checkpoint_count > len(committed):
            raise ExecutionError("Recovery checkpoint exceeds committed point count.")
        for _number, name in committed:
            if not bool(self._points[name].attrs.get("complete", False)):
                raise ExecutionError(f"Private checkpoint {name} is not marked complete.")
        for _number, name in reversed(committed[checkpoint_count:]):
            if name in self._spectra:
                del self._spectra[name]
            del self._points[name]
        for name in tuple(self._pending):
            del self._pending[name]
        referenced = {
            int(self._spectra[name].attrs["reference_index"])
            for name in self._spectra
            if "reference_index" in self._spectra[name].attrs
        }
        if referenced:
            last_reference = max(referenced)
            for name in tuple(self._references):
                if name.isdigit() and int(name) > last_reference:
                    del self._references[name]
        elif checkpoint_count == 0:
            for name in tuple(self._references):
                del self._references[name]
            if "reference" in self._file:
                del self._file["reference"]
        self._truncate_public_thatec(checkpoint_count)

    def _truncate_public_thatec(self, checkpoint_count: int) -> None:
        definition = self._file["scan_definition"]
        measurement = self._file["measurement"]
        if bool(self._file.attrs.get("lab_control_dynamic_checkpoint_axis", False)):
            axis = measurement.get("row_00")
            if axis is not None:
                target_count = max(1, checkpoint_count)
                axis["data"].resize((target_count,))
                axis["timestamp"].resize((target_count,))
                axis["data"][:] = self._np.arange(target_count, dtype="f8")
                if checkpoint_count == 0:
                    axis["timestamp"][:] = self._np.nan
        resumable_rows = 0
        for row_name in sorted(name for name in definition if name.startswith("row_")):
            values = dict(definition[row_name].asstr()[()])
            role = values.get("lab control role")
            if role not in {
                "setpoint",
                "measurement",
                "spectrum",
                "spectrum_processed",
            }:
                continue
            resumable_rows += 1
            if row_name not in measurement:
                raise ExecutionError(f"Recovery row {row_name} has no measurement group.")
            row = measurement[row_name]
            if role in {"spectrum", "spectrum_processed"}:
                if len(row["data"].shape) != 2:
                    raise ExecutionError("Recovery spectrum is not rank 2.")
                row["data"].resize((checkpoint_count, row["data"].shape[1]))
                row["timestamp"].resize((checkpoint_count,))
                dimensions = int(row["data"].attrs["dim of data"])
                row["scale"].resize((checkpoint_count * 2 * (dimensions + 1),))
            else:
                row["data"].resize((checkpoint_count,))
                row["timestamp"].resize((checkpoint_count,))
        if checkpoint_count and not resumable_rows:
            raise ExecutionError(
                "This file predates resumable row metadata and cannot be resumed safely."
            )

    def _rebuild_csv_summary_from_committed_points(self) -> None:
        if self._csv_writer is None or self._csv_stream is None:
            return
        for index in range(self._point_count):
            group = self._points[str(index)]
            spectrum = self._spectra.get(str(index))
            powers = spectrum["power_dbm"][:] if spectrum is not None else ()
            self._csv_writer.writerow(
                {
                    "point_index": index,
                    "timestamp_utc": str(group.attrs["timestamp_utc"]),
                    "status": str(group.attrs["status"]),
                    "setpoints_json": group["setpoints_json"].asstr()[()],
                    "measurements_json": group["measurements_json"].asstr()[()],
                    "trace_name": spectrum.attrs["trace_name"] if spectrum is not None else "",
                    "trace_points": len(powers),
                    "trace_peak_dbm": max(powers) if len(powers) else "",
                }
            )
        self._csv_stream.flush()

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
            raise ExecutionError(f"Cannot create the CSV index: {exc}") from exc

    @staticmethod
    def _serializable(value: object) -> object:
        if is_dataclass(value):
            return Hdf5RunWriter._serializable(asdict(value))
        if isinstance(value, Mapping):
            return {str(key): Hdf5RunWriter._serializable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [Hdf5RunWriter._serializable(item) for item in value]
        return value

    @staticmethod
    def _event_payload(name: str, data: dict[str, object]) -> dict[str, object]:
        """Keep audit events bounded without dropping the durable spectrum.

        Preview events carry the complete trace to the GUI, but the same
        arrays are already atomically stored under ``/spectra`` by the point
        writer.  Serialising a 10,001-value trace into the append-only event
        log would duplicate megabytes of JSON and hold the runner's Python
        thread during every checkpoint.  Persist a compact, auditable summary
        for large preview arrays instead; the count and endpoints identify the
        rendered frame while the canonical values remain in the spectrum
        datasets.
        """

        if name not in {"spectrum_preview", "reference_preview"}:
            return data
        bounded = dict(data)
        for key in ("frequency_hz", "power_dbm"):
            values = bounded.get(key)
            if not isinstance(values, (list, tuple)) or len(values) <= 1_024:
                continue
            bounded.pop(key, None)
            bounded[f"{key}_count"] = len(values)
            bounded[f"{key}_first"] = float(values[0])
            bounded[f"{key}_last"] = float(values[-1])
        return bounded

    @staticmethod
    def _recipe_dut_limits(source: str) -> dict[str, object]:
        try:
            from ruamel.yaml import YAML

            decoded = YAML(typ="safe").load(source)
        except Exception:
            return {}
        if not isinstance(decoded, dict) or not isinstance(decoded.get("dut_limits"), dict):
            return {}
        return Hdf5RunWriter._serializable(decoded["dut_limits"])

    def store_reference(
        self,
        trace: SpectrumTrace,
        *,
        kind: str = "single",
        average_count: int = 1,
    ) -> int:
        """Durably append a reference and return its stable run-local index.

        ``/reference`` remains a hard-link alias for the first reference so
        existing readers keep working.  Repeated per-point references live in
        ``/references/<index>`` and are linked from processed spectra.
        """

        if self._closed:
            raise ExecutionError("Attempted to write a reference to a closed HDF5 file.")
        self._validate_trace(trace)
        if kind not in {"single", "averaged", "loaded"}:
            raise ExecutionError(f"Unsupported reference kind {kind!r}.")
        if average_count < 1:
            raise ExecutionError("Reference average_count must be positive.")
        indexes = [int(name) for name in self._references if name.isdigit()]
        index = max(indexes, default=-1) + 1
        name = str(index)
        try:
            group = self._references.create_group(name)
            group.attrs["reference_index"] = index
            group.attrs["trace_name"] = trace.trace_name
            group.attrs["acquired_at_utc"] = trace.acquired_at_utc.isoformat()
            group.attrs["kind"] = kind
            group.attrs["average_count"] = int(average_count)
            group.create_dataset(
                "frequency_hz",
                data=self._np.asarray(trace.frequencies_hz, dtype="f8"),
                **_spectrum_compression(len(trace.frequencies_hz)),
            )
            group.create_dataset(
                "power_dbm",
                data=self._np.asarray(trace.powers_dbm, dtype="f8"),
                **_spectrum_compression(len(trace.powers_dbm)),
            )
            if index == 0 and "reference" not in self._file:
                self._file["reference"] = group
            self._file.flush()
            return index
        except Exception as exc:
            if index == 0 and "reference" in self._file:
                del self._file["reference"]
            if name in self._references:
                del self._references[name]
            self._file.flush()
            raise ExecutionError(f"Could not store the reference spectrum: {exc}") from exc

    def append(
        self,
        point: MeasurementPoint,
        trace: SpectrumTrace | None = None,
        *,
        processed_values: tuple[float, ...] | None = None,
        processed_unit: str | None = None,
        processing_operation: str = "none",
        device_states: dict[str, object] | None = None,
    ) -> int:
        if self._closed:
            raise ExecutionError("Attempted to write to a closed HDF5 file.")
        index = self._point_count
        name = str(index)
        self._validate_point(point)
        if trace is not None:
            self._validate_trace(trace)
        self._validate_processed(
            trace,
            processed_values=processed_values,
            processed_unit=processed_unit,
            processing_operation=processing_operation,
        )
        try:
            group = self._pending.create_group(name)
            group.attrs["complete"] = False
            group.attrs["timestamp_utc"] = point.timestamp_utc.isoformat()
            group.attrs["status"] = point.status
            group.create_dataset("setpoints_json", data=json.dumps(point.setpoints, sort_keys=True))
            group.create_dataset("measurements_json", data=json.dumps(point.measurements, sort_keys=True))
            group.create_dataset(
                "metadata_json",
                data=json.dumps(self._serializable(point.metadata), sort_keys=True),
            )
            group.create_dataset(
                "device_states_json",
                data=json.dumps(self._serializable(device_states or {}), sort_keys=True),
            )
            if trace is not None:
                spectrum = group.create_group("spectrum")
                spectrum.attrs["trace_name"] = trace.trace_name
                spectrum.attrs["acquired_at_utc"] = trace.acquired_at_utc.isoformat()
                reference_index = point.metadata.get("reference_index")
                if reference_index is not None:
                    reference_index = int(reference_index)
                    if str(reference_index) not in self._references:
                        raise ExecutionError(
                            f"Point references missing spectrum reference {reference_index}."
                        )
                    spectrum.attrs["reference_index"] = reference_index
                spectrum.create_dataset(
                    "frequency_hz",
                    data=self._np.asarray(trace.frequencies_hz, dtype="f8"),
                    **_spectrum_compression(len(trace.frequencies_hz)),
                )
                spectrum.create_dataset(
                    "power_dbm",
                    data=self._np.asarray(trace.powers_dbm, dtype="f8"),
                    **_spectrum_compression(len(trace.powers_dbm)),
                )
                if processed_values is not None:
                    spectrum.attrs["processed_unit"] = str(processed_unit)
                    spectrum.attrs["processing_operation"] = processing_operation
                    spectrum.create_dataset(
                        "processed_values",
                        data=self._np.asarray(processed_values, dtype="f8"),
                        **_spectrum_compression(len(processed_values)),
                    )
            # Flush a self-contained pending checkpoint before exposing it to
            # readers.  Moving one HDF5 link is the commit boundary.
            self._file.flush()
            self._file.move(f"_pending/{name}", f"points/{name}")
            committed = self._points[name]
            if trace is not None:
                self._spectra[name] = committed["spectrum"]
                del committed["spectrum"]
            if processed_values is not None:
                self._thatec.append(
                    point,
                    trace,
                    processed_values=processed_values,
                    processed_unit=processed_unit,
                    processing_operation=processing_operation,
                )
            else:
                self._thatec.append(point, trace)
            committed.attrs["complete"] = True
            self._point_count += 1
            self._file.flush()
            self._thatec.commit_point()
        except Exception as exc:
            self._thatec.rollback_last(trace is not None)
            for container in (self._pending, self._spectra, self._points):
                if name in container:
                    del container[name]
            self._file.flush()
            # The checkpoint is rolled back atomically, but the attempted
            # index and failure remain durable for recovery diagnostics.
            try:
                self.append_event(
                    "checkpoint_write_failed",
                    {"point_index": index, "error": str(exc)},
                    severity="error",
                )
            except Exception:
                # A transport/filesystem failure may also prevent the event;
                # never mask the original storage exception.
                pass
            raise ExecutionError(f"Could not atomically write point {index}: {exc}") from exc
        try:
            self._append_csv_summary(index, point, trace)
        except Exception as exc:
            # CSV export is secondary: never invalidate or re-attempt an
            # already-committed HDF5 checkpoint because of a reporting error.
            self.append_event(
                "csv_append_failed",
                {"point_index": index, "error": str(exc)},
                severity="warning",
            )
        return index

    @staticmethod
    def _validate_processed(
        trace: SpectrumTrace | None,
        *,
        processed_values: tuple[float, ...] | None,
        processed_unit: str | None,
        processing_operation: str,
    ) -> None:
        import math

        if processed_values is None:
            if processed_unit is not None or processing_operation != "none":
                raise ExecutionError(
                    "Processed-spectrum metadata was provided without processed values."
                )
            return
        if trace is None:
            raise ExecutionError("Processed spectrum requires its corresponding raw trace.")
        if len(processed_values) != len(trace.powers_dbm):
            raise ExecutionError("Raw and processed spectra must have identical point counts.")
        if not processed_unit:
            raise ExecutionError("Processed spectrum requires an explicit unit.")
        if processing_operation == "none":
            raise ExecutionError("Processed spectrum requires an explicit operation.")
        if not all(math.isfinite(value) for value in processed_values):
            raise ExecutionError("The processed spectrum contains NaN or infinity.")

    @staticmethod
    def _validate_point(point: MeasurementPoint) -> None:
        import math

        for group_name, values in (
            ("setpoint", point.setpoints),
            ("measurement", point.measurements),
        ):
            for name, value in values.items():
                if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise ExecutionError(
                        f"Point contains invalid {group_name} {name!r}: {value!r}."
                    )

    @staticmethod
    def _validate_trace(trace: SpectrumTrace) -> None:
        import math

        if len(trace.frequencies_hz) != len(trace.powers_dbm) or len(trace.frequencies_hz) < 2:
            raise ExecutionError("A spectrum requires at least two matching axis and amplitude points.")
        if not all(math.isfinite(value) for value in (*trace.frequencies_hz, *trace.powers_dbm)):
            raise ExecutionError("The spectrum contains NaN or infinity.")
        if any(right <= left for left, right in zip(trace.frequencies_hz, trace.frequencies_hz[1:])):
            raise ExecutionError("The spectrum frequency axis must be strictly increasing.")

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

    def flush_checkpoint(self) -> None:
        """Durably flush the latest committed state without closing the run."""

        self._file.flush()
        if self._csv_stream is not None:
            self._csv_stream.flush()

    def append_event(self, name: str, data: dict[str, object], *, severity: str = "info") -> None:
        """Append and durably flush an engine event.

        Safety and operator events are deliberately flushed immediately: the
        last event before a process or transport failure is often the most
        important diagnostic record.
        """

        if self._closed:
            raise ExecutionError("Attempted to write an event to a closed HDF5 file.")
        timestamp = str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat())
        message = json.dumps(
            self._serializable(self._event_payload(name, data)),
            sort_keys=True,
        )
        index = len(self._event_names)
        try:
            for dataset, value in (
                (self._event_timestamps, timestamp),
                (self._event_severities, severity),
                (self._event_names, name),
                (self._event_messages, message),
            ):
                dataset.resize((index + 1,))
                dataset[index] = value
            self._file.flush()
        except Exception:
            for dataset in (
                self._event_timestamps,
                self._event_severities,
                self._event_names,
                self._event_messages,
            ):
                try:
                    if len(dataset) > index:
                        dataset.resize((index,))
                except Exception:
                    pass
            try:
                self._file.flush()
            except Exception:
                pass
            raise

    def close(self, status: str) -> None:
        if self._closed:
            return
        self._file["run"].attrs["status"] = status
        self._thatec.close(status)
        self._file.flush()
        self._file.close()
        if self._csv_stream is not None:
            self._csv_stream.flush()
            self._csv_stream.close()
            self._csv_stream = None
            self._csv_writer = None
        self._closed = True
        from app.storage.thatec_validator import ThatecCompatibilityValidator

        report = ThatecCompatibilityValidator().validate(
            self.path, require_pythat=True
        )
        if not report.valid:
            detail = "; ".join(
                f"{issue.path}: {issue.message}" for issue in report.errors
            )
            try:
                with self._h5py.File(self.path, "r+") as recovered:
                    recovered["run"].attrs["status"] = "faulted"
                    recovered["run"].attrs["storage_validation_error"] = detail
                    recovered.attrs["measurement running"] = self._np.uint8(0)
                    recovered.flush()
            except Exception:
                pass
            raise ExecutionError(f"Final HDF5 contract validation failed: {detail}")
