"""Lightweight, optimized series reader for fast interactive curve plotting."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

from app.storage.hdf5_reader import Hdf5RunReader
from app.recipes.parameter_registry import persisted_quantity_unit


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
                    scalar = Hdf5SeriesReader._extract_points_series(
                        p.name, points_grp, preferred_y_channel
                    )
                    if preferred_y_channel is not None or scalar.available_y_channels:
                        return scalar
                    # A reference or spectrum-only checkpoint has provenance
                    # but no scalar Y channel. Do not mask its recorded trace.
                    if "spectra" not in file:
                        return scalar

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
        numeric_names = Hdf5RunReader._committed_point_names(points_grp.file)
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

        # Channels can appear after the first checkpoint. Keep stable order,
        # but never use an uncommitted row to choose a plotted quantity.
        setpoint_keys: dict[str, None] = {}
        measurement_keys: dict[str, None] = {}
        for name in numeric_names:
            group = points_grp[name]
            setpoint_keys.update(dict.fromkeys(Hdf5SeriesReader._parse_json(group, "setpoints_json")))
            measurement_keys.update(dict.fromkeys(Hdf5SeriesReader._parse_json(group, "measurements_json")))

        # Pick X channel (first setpoint, or 'field', 'voltage', or fallback to index)
        x_channel = Hdf5SeriesReader._select_x_channel(setpoint_keys)
        all_y_channels = tuple(measurement_keys)

        # Pick Y channel (preferred or 'resistance', 'current', 'voltage')
        y_channel = preferred_y if preferred_y is not None else Hdf5SeriesReader._select_y_channel(all_y_channels)

        xs: list[float] = []
        ys: list[float] = []

        for idx, name in enumerate(numeric_names):
            grp = points_grp[name]
            sp = Hdf5SeriesReader._parse_json(grp, "setpoints_json")
            meas = Hdf5SeriesReader._parse_json(grp, "measurements_json")

            # Preserve row alignment and gaps. Missing values are neither a
            # different measurement nor an index expressed in physical units.
            xs.append(Hdf5SeriesReader._finite_or_gap(sp.get(x_channel) if x_channel else idx))
            ys.append(Hdf5SeriesReader._finite_or_gap(meas.get(y_channel)))

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
                y_values=(),
                point_count=0,
                curve_kind="empty",
            )
        first_trace = spectra_grp[names[0]]
        Hdf5RunReader._require_committed_spectrum(spectra_grp.file, int(names[0]))
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
                y_values=(),
                point_count=0,
                curve_kind="empty",
            )
        xs, ys = Hdf5RunReader._read_spectrum_axes(first_trace, f"Spectrum {names[0]}")
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
            decoded = json.loads(str(raw)) if raw else {}
            return decoded if isinstance(decoded, dict) else {}
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
                leaf = c.rsplit(".", 1)[-1].lower()
                if leaf == priority or leaf.startswith(priority + "_"):
                    return c
        return channels[0]

    @staticmethod
    def _format_channel_label(channel_name: str) -> tuple[str, str]:
        c = channel_name.lower()
        unit = persisted_quantity_unit(channel_name)
        if c.startswith("moke_box.") and c.endswith("_t"):
            direction = "ascending" if "ascending" in c else "descending"
            return f"Estimated magnetic field ({direction})", "T"
        if c.startswith("lakeshore.") and c.endswith("_t"):
            return "Measured magnetic field", "T"
        if "field" in c or "magnet" in c or c.endswith("_b") or c.endswith("_h"):
            return "Magnetic Field", unit
        if "resistance" in c or c == "r":
            return "Resistance (R)", unit
        if "voltage" in c or c == "v":
            return "Voltage (V)", unit
        if "current" in c or c == "i":
            return "Current (I)", unit
        if "freq" in c:
            return "Frequency (f)", unit
        if "power" in c:
            return "Power (P)", unit
        return channel_name.replace("_", " ").title(), unit

    @staticmethod
    def _finite_or_gap(value: object) -> float:
        try:
            result = float(value)
        except (ValueError, TypeError, OverflowError):
            return math.nan
        return result if math.isfinite(result) else math.nan
