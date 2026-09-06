"""Lightweight, optimized series reader for fast interactive curve plotting."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from app.storage.hdf5_reader import Hdf5RunReader


@dataclass(frozen=True, slots=True)
class MeasurementSeries:
    """A 1-D or 2-D curve extracted from an HDF5 measurement for interactive display."""

    title: str
    x_label: str
    x_unit: str
    x_values: tuple[float, ...]
    y_label: str
    y_unit: str
    y_values: tuple[float, ...]
    point_count: int
    curve_kind: str = "scalar"  # "scalar", "spectrum", "empty"
    available_y_channels: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.point_count == 0 or len(self.x_values) == 0 or len(self.y_values) == 0

    @property
    def x_data(self) -> tuple[float, ...]:
        return self.x_values

    @property
    def y_data(self) -> tuple[float, ...]:
        return self.y_values

    @property
    def x_name(self) -> str:
        return self.x_label

    @property
    def y_name(self) -> str:
        return self.y_label


class Hdf5SeriesReader:
    """Extract interactive plot vectors from durable HDF5 files without blocking."""

    @staticmethod
    def read_series(
        path: str | Path,
        *,
        preferred_y_channel: str | None = None,
    ) -> MeasurementSeries:
        p = Path(path)
        if not p.is_file():
            return MeasurementSeries(
                title=p.name,
                x_label="Index",
                x_unit="",
                x_values=(),
                y_label="Value",
                y_unit="",
                y_values=(),
                point_count=0,
                curve_kind="empty",
            )

        try:
            with Hdf5RunReader._open(p) as file:
                # 1. Try reading scalar sweep points
                points_grp = file.get("points")
                if points_grp is not None and len(points_grp) > 0:
                    return Hdf5SeriesReader._extract_points_series(
                        p.name, points_grp, preferred_y_channel
                    )

                # 2. Try reading first spectrum trace if scalar points absent
                spectra_grp = file.get("spectra")
                if spectra_grp is not None and len(spectra_grp) > 0:
                    return Hdf5SeriesReader._extract_spectrum_series(p.name, spectra_grp)

        except Exception:
            pass

        return MeasurementSeries(
            title=p.name,
            x_label="Index",
            x_unit="",
            x_values=(),
            y_label="Value",
            y_unit="",
            y_values=(),
            point_count=0,
            curve_kind="empty",
        )

    @staticmethod
    def _extract_points_series(
        title: str, points_grp: Any, preferred_y: str | None
    ) -> MeasurementSeries:
        numeric_names = Hdf5RunReader._numeric_names(points_grp)
        if not numeric_names:
            return MeasurementSeries(
                title=title,
                x_label="Index",
                x_unit="",
                x_values=(),
                y_label="Value",
                y_unit="",
                y_values=(),
                point_count=0,
                curve_kind="empty",
            )

        # Inspect first point to detect available channels
        first_grp = points_grp[numeric_names[0]]
        setpoints_first = Hdf5SeriesReader._parse_json(first_grp, "setpoints_json")
        measurements_first = Hdf5SeriesReader._parse_json(first_grp, "measurements_json")

        # Pick X channel (first setpoint, or 'field', 'voltage', or fallback to index)
        x_channel = Hdf5SeriesReader._select_x_channel(setpoints_first)
        all_y_channels = tuple(measurements_first.keys())

        # Pick Y channel (preferred or 'resistance', 'current', 'voltage')
        y_channel = preferred_y if preferred_y in all_y_channels else Hdf5SeriesReader._select_y_channel(all_y_channels)

        xs: list[float] = []
        ys: list[float] = []

        for idx, name in enumerate(numeric_names):
            grp = points_grp[name]
            sp = Hdf5SeriesReader._parse_json(grp, "setpoints_json")
            meas = Hdf5SeriesReader._parse_json(grp, "measurements_json")

            # Extract X
            if x_channel and x_channel in sp:
                x_val = sp[x_channel]
            else:
                x_val = float(idx)

            # Extract Y
            if y_channel and y_channel in meas:
                y_val = meas[y_channel]
            elif meas:
                y_val = next(iter(meas.values()))
            else:
                y_val = 0.0

            try:
                xs.append(float(x_val))
                ys.append(float(y_val))
            except (ValueError, TypeError):
                continue

        x_lbl, x_un = Hdf5SeriesReader._format_channel_label(x_channel or "Index")
        y_lbl, y_un = Hdf5SeriesReader._format_channel_label(y_channel or "Signal")

        return MeasurementSeries(
            title=title,
            x_label=x_lbl,
            x_unit=x_un,
            x_values=tuple(xs),
            y_label=y_lbl,
            y_unit=y_un,
            y_values=tuple(ys),
            point_count=len(xs),
            curve_kind="scalar",
            available_y_channels=all_y_channels,
        )

    @staticmethod
    def _extract_spectrum_series(title: str, spectra_grp: Any) -> MeasurementSeries:
        names = Hdf5RunReader._numeric_names(spectra_grp)
        if not names:
            return MeasurementSeries(
                title=title,
                x_label="Frequency",
                x_unit="Hz",
                x_values=(),
                y_label="Power",
                y_unit="dBm",
                point_count=0,
                curve_kind="empty",
            )
        first_trace = spectra_grp[names[0]]
        freqs = first_trace.get("frequency_hz")
        powers = first_trace.get("power_dbm")
        if freqs is None or powers is None:
            return MeasurementSeries(
                title=title,
                x_label="Frequency",
                x_unit="Hz",
                x_values=(),
                y_label="Power",
                y_unit="dBm",
                point_count=0,
                curve_kind="empty",
            )
        xs = tuple(float(v) for v in freqs[:])
        ys = tuple(float(v) for v in powers[:])
        return MeasurementSeries(
            title=title,
            x_label="Frequency",
            x_unit="Hz",
            x_values=xs,
            y_label="Power",
            y_unit="dBm",
            y_values=ys,
            point_count=len(xs),
            curve_kind="spectrum",
            available_y_channels=("power_dbm",),
        )

    @staticmethod
    def _parse_json(group: Any, dataset_name: str) -> dict[str, Any]:
        if dataset_name not in group:
            return {}
        try:
            raw = group[dataset_name][()]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return json.loads(str(raw)) if raw else {}
        except Exception:
            return {}

    @staticmethod
    def _select_x_channel(setpoints: dict[str, Any]) -> str | None:
        if not setpoints:
            return None
        keys = list(setpoints.keys())
        # Priority for magnetic field or voltage setpoints
        for priority in ("field", "b_field", "h_field", "magnet_field", "voltage", "v_source", "current"):
            for k in keys:
                if priority in k.lower():
                    return k
        return keys[0]

    @staticmethod
    def _select_y_channel(channels: tuple[str, ...]) -> str | None:
        if not channels:
            return None
        # Priority for resistance, current, voltage
        for priority in ("resistance", "r_dut", "r_mtj", "r", "current", "i_dut", "voltage", "v_dut"):
            for c in channels:
                if priority in c.lower():
                    return c
        return channels[0]

    @staticmethod
    def _format_channel_label(channel_name: str) -> tuple[str, str]:
        c = channel_name.lower()
        if "field" in c or "magnet" in c or c.endswith("_b") or c.endswith("_h"):
            return "Magnetic Field (B)", "Oe"
        if "resistance" in c or c == "r":
            return "Resistance (R)", "Ω"
        if "voltage" in c or c == "v":
            return "Voltage (V)", "V"
        if "current" in c or c == "i":
            return "Current (I)", "A"
        if "freq" in c:
            return "Frequency (f)", "Hz"
        if "power" in c:
            return "Power (P)", "dBm"
        return channel_name.replace("_", " ").title(), ""
