"""Durable raw journal and per-field artifacts for a characterization series."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import hashlib
import math
import os
from pathlib import Path

from app.devices.keithley_2600.characterization.export import KeithleyDataExporter
from app.devices.keithley_2600.characterization.field_series import FieldSeriesConfig, FieldSeriesEntry


def _json_values(value):
    """JSON has no NaN; missing derived quantities remain explicitly null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_values(item) for item in value]
    return value


class FieldSeriesStore:
    """The caller must stop outputs when any write raises; PDF is a later step."""

    def __init__(self, directory: Path, config: FieldSeriesConfig, provenance: dict):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=False)
        self.manifest = {
            "schema_version": 1, "status": "running", "config": asdict(config),
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "provenance": provenance,
            "entries": [{"index": index, "current_a": current, "status": "not_started"}
                        for index, current in enumerate(config.currents_a)],
        }
        self._publish_manifest()
        self._journal = (directory / "events.jsonl").open("x", encoding="utf-8", newline="\n")

    def _publish_manifest(self):
        temporary = self.directory / ".series.json.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(_json_values(self.manifest), stream, sort_keys=True, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.directory / "series.json")

    def write_event(self, kind: str, payload: dict) -> None:
        record = {"kind": kind, "payload": _json_values(payload)}
        self._journal.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
        self._journal.flush()
        os.fsync(self._journal.fileno())

    def save_curve(self, entry: FieldSeriesEntry) -> None:
        folder = self.directory / f"{entry.index + 1:04d}_Ib_{entry.demanded_current_a:+.9g}A"
        folder.mkdir(exist_ok=False)
        csv_digest = None
        if entry.dataset is not None:
            KeithleyDataExporter.export_csv(entry.dataset, folder / "characterization.csv")
            csv_digest = hashlib.sha256((folder / "characterization.csv").read_bytes()).hexdigest()
            # Full immutable snapshot permits later PDF regeneration without
            # referring to mutable UI settings or reconstructed derived columns.
            snapshot = folder / "dataset.json"
            with snapshot.open("x", encoding="utf-8") as stream:
                json.dump(_json_values(asdict(entry.dataset)), stream, sort_keys=True,
                          allow_nan=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        self.manifest["entries"][entry.index] = {
            "index": entry.index, "current_a": entry.demanded_current_a,
            "status": entry.status, "detail": entry.detail,
            "history_segment": entry.history_segment, "directory": folder.name,
            "point_count": len(entry.dataset.points) if entry.dataset else 0,
            "report_status": "pending" if entry.dataset else "no_curve",
            "csv_sha256": csv_digest,
        }
        self._publish_manifest()

    def close(self, status: str, *, outputs_confirmed_off: bool, detail: str = "") -> None:
        if status not in {"completed", "completed_with_skips", "cancelled", "fault", "stopped_on_compliance"}:
            raise ValueError("Unknown field-series terminal status.")
        try:
            self.manifest.update({
                "status": status if outputs_confirmed_off else "fault",
                "outputs_confirmed_off": outputs_confirmed_off,
                "ended_at_utc": datetime.now(timezone.utc).isoformat(), "detail": detail,
            })
            self.write_event("series_closed", {"status": self.manifest["status"],
                                              "outputs_confirmed_off": outputs_confirmed_off})
            self._publish_manifest()
        finally:
            self._journal.close()
