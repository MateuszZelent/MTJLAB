"""Reader for durable Keithley characterization CSV artifacts."""

from __future__ import annotations

import csv
from pathlib import Path

from app.storage.hdf5_series_reader import MeasurementSeries


class CharacterizationCsvReader:
    """Expose characterization columns through the common measurement browser model."""

    _Y_CHANNELS = (
        "Voltage_V",
        "Current_A",
        "True_Resistance_Ohm",
        "Apparent_Resistance_Ohm",
        "Power_W",
    )
    _LABELS = {
        "Voltage_V": ("Voltage", "V"),
        "Current_A": ("Current", "A"),
        "True_Resistance_Ohm": ("True Resistance", "Ω"),
        "Apparent_Resistance_Ohm": ("Apparent Resistance", "Ω"),
        "Power_W": ("Power", "W"),
    }

    @classmethod
    def read_series(
        cls, path: str | Path, *, preferred_y_channel: str | None = None
    ) -> MeasurementSeries:
        target = Path(path)
        metadata: dict[str, str] = {}
        rows: list[dict[str, str]] = []
        try:
            with target.open("r", encoding="utf-8", newline="") as stream:
                data_lines: list[str] = []
                for line in stream:
                    if line.startswith("#"):
                        content = line[1:].strip()
                        if ":" in content:
                            key, value = content.split(":", 1)
                            metadata[key.strip()] = value.strip()
                    elif line.strip():
                        data_lines.append(line)
                rows = list(csv.DictReader(data_lines))
        except (OSError, csv.Error, UnicodeError):
            rows = []

        mode = metadata.get("Mode", "current").lower()
        default_y = "Voltage_V" if mode == "current" else "Current_A"
        y_channel = (
            preferred_y_channel
            if preferred_y_channel in cls._Y_CHANNELS
            else default_y
        )
        xs: list[float] = []
        ys: list[float] = []
        for row in rows:
            try:
                xs.append(float(row["Demanded_SI"]))
                ys.append(float(row[y_channel]))
            except (KeyError, TypeError, ValueError):
                continue

        x_label, x_unit = (
            ("Demanded Current", "A")
            if mode == "current"
            else ("Demanded Voltage", "V")
        )
        y_label, y_unit = cls._LABELS[y_channel]
        return MeasurementSeries(
            title=target.name,
            x_label=x_label,
            x_unit=x_unit,
            x_values=tuple(xs),
            y_label=y_label,
            y_unit=y_unit,
            y_values=tuple(ys),
            point_count=len(xs),
            curve_kind="scalar",
            available_y_channels=cls._Y_CHANNELS,
        )
