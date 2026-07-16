"""Minimal thaTEC:OS/PyThat-compatible external schema.

The application's richer private index remains available under /run,
/points and /spectra.  This mapper owns the public thaTEC contract and keeps
the exact two-column tables expected by PyThat 0.2.14.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any

from app.devices.anritsu.adapter import SpectrumTrace
from app.domain.models import MeasurementPoint


class ThatecHdf5Writer:
    def __init__(
        self,
        file: Any,
        *,
        device_idn: dict[str, str],
        plan_hash: str,
    ) -> None:
        self._file = file
        self._h5py = type(file).__module__.split(".")[0]
        import h5py
        import numpy as np

        self._np = np
        self._text = h5py.string_dtype("utf-8")
        file.attrs["measurement running"] = np.uint8(1)
        file.attrs["thaTEC:OS version"] = "PyThat-compatible Lab Control schema 1"
        file.attrs["version information"] = "Lab Control 0.1"

        devices = file.create_group("devices")
        for stable_name, idn in sorted(device_idn.items()):
            dataset_name = self._device_name(stable_name, idn)
            devices.create_dataset(
                dataset_name,
                data=self._table((("VISA identity", json.dumps([idn])), ("profile id", json.dumps([stable_name])))),
                dtype=self._text,
            )

        labbook = file.create_group("labbook")
        labbook.create_dataset("comments", shape=(0, 2), maxshape=(None, 2), dtype=self._text)
        labbook.create_dataset("parameter", shape=(0, 2), maxshape=(None, 2), dtype=self._text)
        labbook.create_dataset(
            "metadata",
            data=self._table(
                (
                    ("date", datetime.now(timezone.utc).isoformat()),
                    ("application", "Lab Control"),
                    ("plan sha256", plan_hash),
                )
            ),
            dtype=self._text,
        )

        measurement = file.create_group("measurement")
        self._log = measurement.create_dataset("log", shape=(0, 2), maxshape=(None, 2), dtype=self._text)
        self._append_log("Process started.")

        definition = file.create_group("scan_definition")
        definition.create_dataset(
            "row_00",
            data=self._table(
                (
                    ("device name", "Anritsu Spectrum Analyzer"),
                    ("control name", "Power (dBm)"),
                    ("dimensions", "1"),
                    ("data type", "11"),
                    ("tree indent level", "0"),
                    ("function", "indicator"),
                )
            ),
            dtype=self._text,
        )
        definition.create_dataset(
            "tree_view",
            data=self._np.asarray(
                [["row   0", "indicator", "- Anritsu Spectrum Analyzer - Power (dBm)"]], dtype=object
            ),
            dtype=self._text,
        )
        self._trace_points: int | None = None
        self._trace_count = 0

    def append(self, point: MeasurementPoint, trace: SpectrumTrace | None) -> None:
        if trace is None:
            self._append_log(f"Checkpoint {point.index}: {point.status}.")
            return
        measurement = self._file["measurement"]
        if "row_00" not in measurement:
            self._create_spectrum_row(trace)
        if len(trace.powers_dbm) != self._trace_points:
            raise ValueError("thaTEC spectrum point count changed during one run")
        row = measurement["row_00"]
        data = row["data"]
        data.resize((self._trace_count + 1, self._trace_points))
        data[self._trace_count, :] = self._np.asarray(trace.powers_dbm, dtype="f8")
        timestamps = row["timestamp"]
        timestamps.resize((self._trace_count + 1,))
        timestamps[self._trace_count] = trace.acquired_at_utc.timestamp()
        frequency_step = (trace.frequencies_hz[-1] - trace.frequencies_hz[0]) / (self._trace_points - 1)
        scale = row["scale"]
        scale.resize(((self._trace_count + 1) * 4,))
        scale[self._trace_count * 4 : (self._trace_count + 1) * 4] = (
            trace.frequencies_hz[0],
            frequency_step,
            0.0,
            1.0,
        )
        self._trace_count += 1
        self._append_log(f"Spectrum checkpoint {point.index} stored.")

    def rollback_last(self, had_trace: bool) -> None:
        if not had_trace or self._trace_count == 0 or "row_00" not in self._file["measurement"]:
            return
        self._trace_count -= 1
        row = self._file["measurement/row_00"]
        row["data"].resize((self._trace_count, self._trace_points))
        row["timestamp"].resize((self._trace_count,))
        row["scale"].resize((self._trace_count * 4,))

    def close(self, status: str) -> None:
        self._append_log(f"Process closed with status: {status}.")
        self._file.attrs["measurement running"] = self._np.uint8(0)

    def _create_spectrum_row(self, trace: SpectrumTrace) -> None:
        self._trace_points = len(trace.powers_dbm)
        row = self._file["measurement"].create_group("row_00")
        data = row.create_dataset(
            "data",
            shape=(0, self._trace_points),
            maxshape=(None, self._trace_points),
            dtype="f8",
            compression="gzip",
        )
        data.attrs["data type"] = self._np.int32(11)
        data.attrs["dim of data"] = self._np.int32(1)
        row.create_dataset("timestamp", shape=(0,), maxshape=(None,), dtype="f8")
        row.create_dataset("scale", shape=(0,), maxshape=(None,), dtype="f8")
        row.create_dataset(
            "metadata",
            data=self._table(
                (
                    ("name", "Frequency"),
                    ("unit", "Hz"),
                    ("offset", f"{trace.frequencies_hz[0]:.12E}"),
                    ("multiplier", f"{(trace.frequencies_hz[-1] - trace.frequencies_hz[0]) / (self._trace_points - 1):.12E}"),
                    ("name", "Power"),
                    ("unit", "dBm"),
                    ("offset", "0.000000000000E+00"),
                    ("multiplier", "1.000000000000E+00"),
                )
            ),
            dtype=self._text,
        )

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
        }
        return re.sub(r"[/\\]", "_", names.get(key, key))
