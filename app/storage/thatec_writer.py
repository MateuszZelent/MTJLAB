"""thaTEC:OS/PyThat-compatible public measurement schema.

The application's richer private index remains available under ``/run``,
``/points`` and ``/spectra``.  This writer owns the public thaTEC contract:
every checkpoint is represented on a stable outer coordinate and all SI
setpoints, scalar readings and spectra are available to PyThat as indicators.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
from typing import Any

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.storage.thatec_schema_mapper import ThatecSchemaMapper, ThatecSweepAxis


class ThatecHdf5Writer:
    """Append a crash-recoverable thaTEC view of measurement checkpoints."""

    def __init__(
        self,
        file: Any,
        *,
        device_idn: dict[str, str],
        plan_hash: str,
        expected_points: int | None = None,
        recipe_source: str = "",
        settings_source: str = "",
    ) -> None:
        import h5py
        import numpy as np

        self._file = file
        self._np = np
        self._text = h5py.string_dtype("utf-8")
        self._schema = ThatecSchemaMapper.from_recipe_source(
            recipe_source, expected_points=expected_points
        )
        self._expected_points = self._schema.point_count
        self._checkpoint_count = 0
        self._next_row = len(self._schema.axes)
        self._indicator_indent = len(self._schema.axes)
        self._scalar_rows: dict[tuple[str, str], str] = {}
        self._spectrum_row: str | None = None
        self._trace_points: int | None = None
        self._processed_spectrum_row: str | None = None
        self._processed_trace_points: int | None = None
        self._processed_unit: str | None = None
        self._last_scalar_rows: list[str] = []
        self._last_spectrum_appended = False
        self._last_processed_spectrum_appended = False
        self._last_checkpoint_incremented = False
        self._last_created: list[tuple[str, tuple[str, str] | None]] = []

        file.attrs["measurement running"] = np.uint8(1)
        file.attrs["thaTEC:OS version"] = "PyThat-compatible Lab Control schema 2"
        file.attrs["version information"] = "Lab Control 0.1"

        devices = file.create_group("devices")
        device_settings = self._device_settings(settings_source)
        for stable_name, idn in sorted(device_idn.items()):
            dataset_name = self._device_name(stable_name, idn)
            configuration = self._flatten(device_settings.get(stable_name, {}))
            devices.create_dataset(
                dataset_name,
                data=self._table(
                    (
                        ("VISA identity", json.dumps([idn])),
                        ("profile id", json.dumps([stable_name])),
                    ) + configuration
                ),
                dtype=self._text,
            )

        labbook = file.create_group("labbook")
        labbook.create_dataset("comments", shape=(0, 2), maxshape=(None, 2), dtype=self._text)
        # Preserve the declarative source in a standard public two-column
        # labbook table.  Generic THATEC tools may ignore the names, while this
        # application can restore the exact editable Sweep after reopening H5.
        labbook.create_dataset(
            "parameter",
            data=self._table(
                (
                    ("Lab Control recipe YAML", recipe_source),
                    ("Lab Control settings YAML", settings_source),
                )
            ),
            maxshape=(None, 2),
            dtype=self._text,
        )
        labbook.create_dataset(
            "metadata",
            data=self._table(
                (
                    ("date", datetime.now(timezone.utc).isoformat()),
                    ("application", "Lab Control"),
                    ("plan sha256", plan_hash),
                    ("schema mapper mode", self._schema.mode),
                    ("schema mapper detail", self._schema.detail),
                )
            ),
            dtype=self._text,
        )

        measurement = file.create_group("measurement")
        self._log = measurement.create_dataset(
            "log", shape=(0, 2), maxshape=(None, 2), dtype=self._text
        )
        self._append_log("Process started.")

        self._definition = file.create_group("scan_definition")
        for index, axis in enumerate(self._schema.axes):
            self._create_axis_row(index, axis)
        self._tree_view = self._definition.create_dataset(
            "tree_view", shape=(0, 3), maxshape=(None, 3), dtype=self._text
        )
        self._rebuild_tree_view()

    @classmethod
    def resume(
        cls,
        file: Any,
        *,
        checkpoint_count: int,
        expected_points: int | None = None,
        recipe_source: str = "",
    ) -> "ThatecHdf5Writer":
        """Attach to a validated file truncated at a safe checkpoint boundary."""

        import h5py
        import numpy as np

        self = cls.__new__(cls)
        self._file = file
        self._np = np
        self._text = h5py.string_dtype("utf-8")
        self._schema = ThatecSchemaMapper.from_recipe_source(
            recipe_source, expected_points=expected_points
        )
        self._expected_points = self._schema.point_count
        self._checkpoint_count = checkpoint_count
        self._indicator_indent = len(self._schema.axes)
        self._definition = file["scan_definition"]
        self._tree_view = self._definition["tree_view"]
        self._log = file["measurement/log"]
        row_numbers = [
            int(name.removeprefix("row_"))
            for name in self._definition
            if re.fullmatch(r"row_[0-9]+", name)
        ]
        self._next_row = max(row_numbers, default=-1) + 1
        self._scalar_rows = {}
        self._spectrum_row = None
        self._trace_points = None
        self._processed_spectrum_row = None
        self._processed_trace_points = None
        self._processed_unit = None
        for row_name in sorted(
            name for name in self._definition if re.fullmatch(r"row_[0-9]+", name)
        ):
            definition = dict(self._definition[row_name].asstr()[()])
            role = definition.get("lab control role")
            key = definition.get("lab control key")
            if role in {"setpoint", "measurement"} and key:
                self._scalar_rows[(role, key)] = row_name
            elif role == "spectrum":
                self._spectrum_row = row_name
                data = file[f"measurement/{row_name}/data"]
                if len(data.shape) != 2:
                    raise ValueError("Recovered spectrum row is not rank 2.")
                self._trace_points = int(data.shape[1])
            elif role == "spectrum_processed":
                self._processed_spectrum_row = row_name
                data = file[f"measurement/{row_name}/data"]
                if len(data.shape) != 2:
                    raise ValueError("Recovered processed spectrum row is not rank 2.")
                self._processed_trace_points = int(data.shape[1])
                self._processed_unit = definition.get("lab control unit", "dB")
        self._last_scalar_rows = []
        self._last_spectrum_appended = False
        self._last_processed_spectrum_appended = False
        self._last_checkpoint_incremented = False
        self._last_created = []
        file.attrs["measurement running"] = np.uint8(1)
        self._append_log(f"Process resumed from checkpoint {checkpoint_count}.")
        return self

    def append(
        self,
        point: MeasurementPoint,
        trace: SpectrumTrace | None,
        *,
        processed_values: tuple[float, ...] | None = None,
        processed_unit: str | None = None,
        processing_operation: str = "none",
    ) -> None:
        """Append one aligned public checkpoint and remember its rollback boundary."""

        self._last_scalar_rows = []
        self._last_spectrum_appended = False
        self._last_processed_spectrum_appended = False
        self._last_checkpoint_incremented = False
        self._last_created = []
        values = {
            **{
                ("setpoint", key): value
                for key, value in point.setpoints.items()
                if key not in self._schema.axis_targets
            },
            **{("measurement", key): value for key, value in point.measurements.items()},
        }
        for role_key in sorted(values):
            if role_key not in self._scalar_rows:
                self._create_scalar_row(*role_key)

        timestamp = point.timestamp_utc.timestamp()
        for role_key, row_name in tuple(self._scalar_rows.items()):
            value = values.get(role_key, math.nan)
            self._append_scalar(row_name, float(value), timestamp)
            self._last_scalar_rows.append(row_name)

        if trace is not None and self._spectrum_row is None:
            self._create_spectrum_row(trace)
        if self._spectrum_row is not None:
            self._append_spectrum(trace)
            self._last_spectrum_appended = True
        if processed_values is not None and self._processed_spectrum_row is None:
            if trace is None or not processed_unit:
                raise ValueError("Processed thaTEC spectrum requires raw trace and unit.")
            self._create_processed_spectrum_row(
                trace,
                processed_unit,
                processing_operation,
            )
        if self._processed_spectrum_row is not None:
            self._append_processed_spectrum(trace, processed_values)
            self._last_processed_spectrum_appended = True

        self._checkpoint_count += 1
        self._last_checkpoint_incremented = True
        suffix = " with spectrum" if trace is not None else ""
        self._append_log(f"Checkpoint {point.index}{suffix}: {point.status}.")

    def rollback_last(self, had_trace: bool) -> None:
        """Undo the last public append after the private commit failed."""

        del had_trace  # The transaction record is more precise than the caller hint.
        for row_name in self._last_scalar_rows:
            row = self._file[f"measurement/{row_name}"]
            row["data"].resize((max(0, len(row["data"]) - 1),))
            row["timestamp"].resize((max(0, len(row["timestamp"]) - 1),))
        if self._last_spectrum_appended and self._spectrum_row is not None:
            row = self._file[f"measurement/{self._spectrum_row}"]
            row["data"].resize((max(0, row["data"].shape[0] - 1), self._trace_points))
            row["timestamp"].resize((max(0, len(row["timestamp"]) - 1),))
            row["scale"].resize((max(0, len(row["scale"]) - 4),))
        if (
            self._last_processed_spectrum_appended
            and self._processed_spectrum_row is not None
        ):
            row = self._file[f"measurement/{self._processed_spectrum_row}"]
            row["data"].resize(
                (
                    max(0, row["data"].shape[0] - 1),
                    self._processed_trace_points,
                )
            )
            row["timestamp"].resize((max(0, len(row["timestamp"]) - 1),))
            row["scale"].resize((max(0, len(row["scale"]) - 4),))
        if self._last_checkpoint_incremented:
            self._checkpoint_count = max(0, self._checkpoint_count - 1)

        for row_name, role_key in reversed(self._last_created):
            if row_name in self._file["measurement"]:
                del self._file["measurement"][row_name]
            if row_name in self._definition:
                del self._definition[row_name]
            if role_key is None:
                self._spectrum_row = None
                self._trace_points = None
            elif role_key == ("__spectrum__", "processed"):
                self._processed_spectrum_row = None
                self._processed_trace_points = None
                self._processed_unit = None
            else:
                self._scalar_rows.pop(role_key, None)
        if self._last_created:
            self._rebuild_tree_view()
        self._last_scalar_rows = []
        self._last_spectrum_appended = False
        self._last_processed_spectrum_appended = False
        self._last_checkpoint_incremented = False
        self._last_created = []

    def close(self, status: str) -> None:
        self._append_log(f"Process closed with status: {status}.")
        self._file.attrs["measurement running"] = self._np.uint8(0)

    def _create_scalar_row(self, role: str, key: str) -> None:
        row_name = self._allocate_row()
        device, label, unit = self._describe_quantity(role, key)
        control_name = f"{label} ({unit})" if unit else label
        self._definition.create_dataset(
            row_name,
            data=self._table(
                (
                    ("device name", device),
                    ("control name", control_name),
                    ("dimensions", "0"),
                    ("data type", "11"),
                    ("tree indent level", str(self._indicator_indent)),
                    ("function", "indicator"),
                    ("lab control role", role),
                    ("lab control key", key),
                )
            ),
            dtype=self._text,
        )
        row = self._file["measurement"].create_group(row_name)
        data = row.create_dataset(
            "data", shape=(self._checkpoint_count,), maxshape=(None,), dtype="f8"
        )
        data.attrs["data type"] = self._np.int32(11)
        data.attrs["dim of data"] = self._np.int32(0)
        if self._checkpoint_count:
            data[:] = self._np.nan
        timestamps = row.create_dataset(
            "timestamp", shape=(self._checkpoint_count,), maxshape=(None,), dtype="f8"
        )
        if self._checkpoint_count:
            timestamps[:] = self._np.nan
        role_key = (role, key)
        self._scalar_rows[role_key] = row_name
        self._last_created.append((row_name, role_key))
        self._rebuild_tree_view()

    def _append_scalar(self, row_name: str, value: float, timestamp: float) -> None:
        row = self._file[f"measurement/{row_name}"]
        index = len(row["data"])
        row["data"].resize((index + 1,))
        row["data"][index] = value
        row["timestamp"].resize((index + 1,))
        row["timestamp"][index] = timestamp

    def _create_spectrum_row(self, trace: SpectrumTrace) -> None:
        self._trace_points = len(trace.powers_dbm)
        row_name = self._allocate_row()
        self._spectrum_row = row_name
        self._definition.create_dataset(
            row_name,
            data=self._table(
                (
                    ("device name", "Anritsu Spectrum Analyzer"),
                    ("control name", "Spectrum (dBm)"),
                    ("dimensions", "1"),
                    ("data type", "11"),
                    ("tree indent level", str(self._indicator_indent)),
                    ("function", "indicator"),
                    ("lab control role", "spectrum"),
                    ("lab control key", trace.trace_name),
                )
            ),
            dtype=self._text,
        )
        row = self._file["measurement"].create_group(row_name)
        data = row.create_dataset(
            "data",
            shape=(self._checkpoint_count, self._trace_points),
            maxshape=(None, self._trace_points),
            dtype="f8",
            compression="gzip",
        )
        data.attrs["data type"] = self._np.int32(11)
        data.attrs["dim of data"] = self._np.int32(1)
        if self._checkpoint_count:
            data[:] = self._np.nan
        timestamps = row.create_dataset(
            "timestamp", shape=(self._checkpoint_count,), maxshape=(None,), dtype="f8"
        )
        if self._checkpoint_count:
            timestamps[:] = self._np.nan
        scale = row.create_dataset(
            "scale", shape=(self._checkpoint_count * 4,), maxshape=(None,), dtype="f8"
        )
        if self._checkpoint_count:
            scale_values = (
                trace.frequencies_hz[0],
                self._frequency_step(trace),
                0.0,
                1.0,
            )
            scale[:] = self._np.tile(scale_values, self._checkpoint_count)
        row.create_dataset(
            "metadata",
            data=self._table(
                (
                    ("name", "Frequency"),
                    ("unit", "Hz"),
                    ("offset", f"{trace.frequencies_hz[0]:.12E}"),
                    ("multiplier", f"{self._frequency_step(trace):.12E}"),
                    ("name", "Power"),
                    ("unit", "dBm"),
                    ("offset", "0.000000000000E+00"),
                    ("multiplier", "1.000000000000E+00"),
                )
            ),
            dtype=self._text,
        )
        self._last_created.append((row_name, None))
        self._rebuild_tree_view()

    def _append_spectrum(self, trace: SpectrumTrace | None) -> None:
        assert self._spectrum_row is not None
        assert self._trace_points is not None
        row = self._file[f"measurement/{self._spectrum_row}"]
        index = row["data"].shape[0]
        row["data"].resize((index + 1, self._trace_points))
        row["timestamp"].resize((index + 1,))
        row["scale"].resize(((index + 1) * 4,))
        if trace is None:
            row["data"][index, :] = self._np.nan
            row["timestamp"][index] = self._np.nan
            row["scale"][index * 4 : (index + 1) * 4] = (0.0, 1.0, 0.0, 1.0)
            return
        if len(trace.powers_dbm) != self._trace_points:
            raise ValueError("thaTEC spectrum point count changed during one run")
        row["data"][index, :] = self._np.asarray(trace.powers_dbm, dtype="f8")
        row["timestamp"][index] = trace.acquired_at_utc.timestamp()
        row["scale"][index * 4 : (index + 1) * 4] = (
            trace.frequencies_hz[0],
            self._frequency_step(trace),
            0.0,
            1.0,
        )

    def _create_processed_spectrum_row(
        self,
        trace: SpectrumTrace,
        unit: str,
        operation: str,
    ) -> None:
        self._processed_trace_points = len(trace.powers_dbm)
        self._processed_unit = unit
        row_name = self._allocate_row()
        self._processed_spectrum_row = row_name
        label = (
            "Spectrum raw-reference"
            if operation == "difference_db"
            else f"Spectrum processed {operation}"
        )
        self._definition.create_dataset(
            row_name,
            data=self._table(
                (
                    ("device name", "Anritsu Spectrum Analyzer"),
                    ("control name", f"{label} ({unit})"),
                    ("dimensions", "1"),
                    ("data type", "11"),
                    ("tree indent level", str(self._indicator_indent)),
                    ("function", "indicator"),
                    ("lab control role", "spectrum_processed"),
                    ("lab control key", operation),
                    ("lab control unit", unit),
                )
            ),
            dtype=self._text,
        )
        row = self._file["measurement"].create_group(row_name)
        data = row.create_dataset(
            "data",
            shape=(self._checkpoint_count, self._processed_trace_points),
            maxshape=(None, self._processed_trace_points),
            dtype="f8",
            compression="gzip",
        )
        data.attrs["data type"] = self._np.int32(11)
        data.attrs["dim of data"] = self._np.int32(1)
        if self._checkpoint_count:
            data[:] = self._np.nan
        timestamps = row.create_dataset(
            "timestamp",
            shape=(self._checkpoint_count,),
            maxshape=(None,),
            dtype="f8",
        )
        if self._checkpoint_count:
            timestamps[:] = self._np.nan
        scale = row.create_dataset(
            "scale",
            shape=(self._checkpoint_count * 4,),
            maxshape=(None,),
            dtype="f8",
        )
        if self._checkpoint_count:
            scale_values = (
                trace.frequencies_hz[0],
                self._frequency_step(trace),
                0.0,
                1.0,
            )
            scale[:] = self._np.tile(scale_values, self._checkpoint_count)
        row.create_dataset(
            "metadata",
            data=self._table(
                (
                    ("name", "Frequency"),
                    ("unit", "Hz"),
                    ("offset", f"{trace.frequencies_hz[0]:.12E}"),
                    ("multiplier", f"{self._frequency_step(trace):.12E}"),
                    ("name", "Processed amplitude"),
                    ("unit", unit),
                    ("offset", "0.000000000000E+00"),
                    ("multiplier", "1.000000000000E+00"),
                )
            ),
            dtype=self._text,
        )
        self._last_created.append((row_name, ("__spectrum__", "processed")))
        self._rebuild_tree_view()

    def _append_processed_spectrum(
        self,
        trace: SpectrumTrace | None,
        values: tuple[float, ...] | None,
    ) -> None:
        assert self._processed_spectrum_row is not None
        assert self._processed_trace_points is not None
        row = self._file[f"measurement/{self._processed_spectrum_row}"]
        index = row["data"].shape[0]
        row["data"].resize((index + 1, self._processed_trace_points))
        row["timestamp"].resize((index + 1,))
        row["scale"].resize(((index + 1) * 4,))
        if trace is None or values is None:
            row["data"][index, :] = self._np.nan
            row["timestamp"][index] = self._np.nan
            row["scale"][index * 4 : (index + 1) * 4] = (0.0, 1.0, 0.0, 1.0)
            return
        if len(values) != self._processed_trace_points:
            raise ValueError("thaTEC processed spectrum point count changed during one run")
        row["data"][index, :] = self._np.asarray(values, dtype="f8")
        row["timestamp"][index] = trace.acquired_at_utc.timestamp()
        row["scale"][index * 4 : (index + 1) * 4] = (
            trace.frequencies_hz[0],
            self._frequency_step(trace),
            0.0,
            1.0,
        )

    def _rebuild_tree_view(self) -> None:
        rows: list[tuple[str, str, str]] = []
        for name in sorted(key for key in self._definition if key.startswith("row_")):
            definition = dict(self._definition[name].asstr()[()])
            number = int(name.removeprefix("row_"))
            function = str(definition["function"])
            kind = "indicator" if function == "indicator" else "control"
            indent = "          " * int(definition["tree indent level"])
            device = definition.get("device name", "internal")
            control = definition.get("control name", "checkpoint")
            rows.append((f"row {number:3d}", kind, f"- {indent}{device} - {control}"))
        self._tree_view.resize((len(rows), 3))
        if rows:
            self._tree_view[:] = self._np.asarray(rows, dtype=object)

    @staticmethod
    def _device_settings(source: str) -> dict[str, Any]:
        if not source.strip():
            return {}
        try:
            from ruamel.yaml import YAML

            decoded = YAML(typ="safe").load(source)
        except Exception:
            return {}
        devices = decoded.get("devices", {}) if isinstance(decoded, dict) else {}
        return devices if isinstance(devices, dict) else {}

    @staticmethod
    def _flatten(value: Any, prefix: str = "") -> tuple[tuple[str, str], ...]:
        if isinstance(value, dict):
            rows: list[tuple[str, str]] = []
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
                nested = f"{prefix}.{key}" if prefix else str(key)
                rows.extend(ThatecHdf5Writer._flatten(item, nested))
            return tuple(rows)
        return ((prefix, value if isinstance(value, str) else json.dumps(value)),)

    def _create_axis_row(self, index: int, axis: ThatecSweepAxis) -> None:
        row_name = f"row_{index:02d}"
        control_name = f"{axis.control_name} ({axis.unit})" if axis.unit else axis.control_name
        self._definition.create_dataset(
            row_name,
            data=self._table(
                (
                    ("device name", axis.device_name),
                    ("control name", control_name),
                    ("dimensions", "0"),
                    ("data type", "11"),
                    ("tree indent level", str(index)),
                    ("function", "scalar control"),
                    ("start", f"{axis.values_si[0]:.12E}"),
                    ("stop", f"{axis.values_si[-1]:.12E}"),
                    ("steps", str(axis.points)),
                    (
                        "equation",
                        "log(x)" if axis.spacing == "log" else "x",
                    ),
                )
            ),
            dtype=self._text,
        )
        row = self._file["measurement"].create_group(row_name)
        data = row.create_dataset("data", data=self._np.asarray(axis.values_si, dtype="f8"))
        data.attrs["data type"] = self._np.int32(11)
        data.attrs["dim of data"] = self._np.int32(0)
        row.create_dataset(
            "timestamp", data=self._np.full(axis.points, self._np.nan, dtype="f8")
        )

    def _allocate_row(self) -> str:
        row_name = f"row_{self._next_row:02d}"
        self._next_row += 1
        return row_name

    @staticmethod
    def _frequency_step(trace: SpectrumTrace) -> float:
        return (trace.frequencies_hz[-1] - trace.frequencies_hz[0]) / (
            len(trace.frequencies_hz) - 1
        )

    @staticmethod
    def _describe_quantity(role: str, key: str) -> tuple[str, str, str]:
        parts = key.split(".")
        device_key = parts[0].lower() if parts else "lab"
        device = {
            "rigol": "Rigol DG1032Z",
            "keithley": "Keithley 2602A",
            "anritsu": "Anritsu Spectrum Analyzer",
            "lakeshore": "Lake Shore 475",
        }.get(device_key, "Lab Control")
        leaf = parts[-1].lower()
        unit = ""
        unit_tokens = (
            (("frequency", "_hz"), "Hz"),
            (("current", "_a"), "A"),
            (("voltage", "high_level", "low_level", "offset", "_v"), "V"),
            (("power", "_w"), "W"),
            (("resistance", "impedance", "_ohm"), "Ω"),
            (("duration", "settling", "_s"), "s"),
            (("dbm", "reference_level"), "dBm"),
            (("field", "peak"), "T"),
        )
        for tokens, candidate in unit_tokens:
            if any(token in leaf for token in tokens):
                unit = candidate
                break
        display_parts = parts[1:] if len(parts) > 1 else [key]
        path = " ".join(display_parts).replace("_", " ")
        path = re.sub(r"\s+(?:v|a|w|ohm|hz|s|dbm)$", "", path, flags=re.IGNORECASE)
        prefix = "Setpoint" if role == "setpoint" else "Measured"
        return device, f"{prefix} {path}", unit

    def _append_log(self, message: str) -> None:
        index = len(self._log)
        self._log.resize((index + 1, 2))
        self._log[index] = (datetime.now(timezone.utc).isoformat(), message)

    def _table(self, rows: tuple[tuple[str, str], ...]) -> Any:
        return self._np.asarray(rows, dtype=object)

    @staticmethod
    def _device_name(key: str, idn: str) -> str:
        model = next((part.strip() for part in idn.split(",")[1:2] if part.strip()), key)
        names = {
            "rigol": f"Rigol {model}",
            "keithley": f"Keithley {model}",
            "anritsu": "Anritsu Spectrum Analyzer",
            "lakeshore_gaussmeter": f"Lake Shore {model}",
        }
        return re.sub(r"[/\\]", "_", names.get(key, key))
