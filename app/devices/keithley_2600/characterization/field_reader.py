"""Offline reader for committed field-series snapshots (no hardware access)."""

from dataclasses import dataclass
import json
import hashlib
import math
from pathlib import Path

from app.devices.keithley_2600.characterization.models import (
    CharacterizationDataset, CharacterizationPoint, CharacterizationSweepConfig,
    FieldLineObservation, SampleMetadata,
)


def _read_json(path):
    def invalid_constant(value):
        raise ValueError(f"Non-standard JSON numeric constant: {value}")
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=invalid_constant)


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Missing or non-finite {name} in stored measurement.")


def load_field_dataset(path: Path) -> CharacterizationDataset:
    """Read v1 dataset.json; null derived resistance means unavailable, not zero."""
    raw = _read_json(path)
    config = dict(raw.pop("config"))
    config["metadata"] = SampleMetadata(**config["metadata"])
    config = CharacterizationSweepConfig(**config)
    if config.channel not in {"A", "B"} or config.mode not in {"current", "voltage"}:
        raise ValueError("Unknown stored source channel or mode.")
    points = []
    for index, stored in enumerate(raw.pop("points")):
        values = dict(stored)
        if type(values["index"]) is not int or values["index"] != index:
            raise ValueError("Non-contiguous stored point indices.")
        for name in ("demanded_si", "measured_voltage_v", "measured_current_a", "power_w", "timestamp_epoch"):
            _finite(values[name], name)
        for name in ("compliance_active", "valid"):
            if name in values and type(values[name]) is not bool:
                raise ValueError(f"Invalid stored {name} flag.")
        for name in ("true_resistance_ohm", "apparent_resistance_ohm"):
            if values[name] is None:
                values[name] = math.nan
            else:
                _finite(values[name], name)
        for name in ("field_before", "field_after"):
            observation = values.get(name)
            if observation is not None:
                for key, value in observation.items():
                    if key == "compliance_active":
                        if type(value) is not bool:
                            raise ValueError("Invalid field compliance flag.")
                    else:
                        _finite(value, key)
                values[name] = FieldLineObservation(**observation)
        points.append(CharacterizationPoint(**values))
    dataset = CharacterizationDataset(config=config, points=tuple(points), **raw)
    if dataset.completion_status not in {"completed", "cancelled", "stopped_on_compliance", "stopped_on_field_compliance"}:
        raise ValueError("Unknown stored characterization status.")
    if dataset.checksum_sha256 and dataset.checksum_sha256 != dataset.calculate_checksum(points):
        raise ValueError("Stored characterization checksum mismatch.")
    return dataset


@dataclass(frozen=True, slots=True)
class StoredFieldCurve:
    index: int
    current_a: float
    status: str
    directory: Path | None
    dataset: CharacterizationDataset | None
    detail: str
    history_segment: int | None


@dataclass(frozen=True, slots=True)
class StoredFieldSeries:
    directory: Path
    manifest: dict
    curves: tuple[StoredFieldCurve, ...]


def load_field_series(directory: Path) -> StoredFieldSeries:
    """Read only manifest-committed curves; never guess that an interrupted run is safe."""
    directory = Path(directory).resolve()
    manifest = _read_json(directory / "series.json")
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported field-series schema version.")
    if manifest.get("status") not in {"running", "completed", "completed_with_skips", "cancelled", "fault", "stopped_on_compliance"}:
        raise ValueError("Unknown field-series status.")
    currents = manifest["config"]["currents_a"]
    entries = manifest["entries"]
    if len(entries) != len(currents):
        raise ValueError("Field-list length does not match manifest entries.")
    curves = []
    for index, entry in enumerate(entries):
        if type(entry["index"]) is not int or entry["index"] != index:
            raise ValueError("Non-contiguous field-list indices.")
        _finite(entry["current_a"], "field current")
        if entry["current_a"] != currents[index]:
            raise ValueError("Field target differs from stored configuration.")
        status = entry["status"]
        if status not in {"not_started", "completed", "sample_compliance", "skipped_field_compliance", "cancelled"}:
            raise ValueError("Unknown field entry status.")
        folder = None
        dataset = None
        if status != "not_started":
            folder = (directory / entry["directory"]).resolve()
            if folder == directory or folder.parent != directory:
                raise ValueError("Field curve directory escapes the series directory.")
            if not folder.is_dir():
                raise ValueError("Committed field directory is missing.")
            count = entry["point_count"]
            if type(count) is not int or count < 0:
                raise ValueError("Invalid stored point count.")
            snapshot = folder / "dataset.json"
            if snapshot.is_file():
                if snapshot.resolve().parent != folder:
                    raise ValueError("Dataset link escapes its field directory.")
                dataset = load_field_dataset(snapshot)
                if entry.get("csv_sha256") is not None:
                    csv_path = folder / "characterization.csv"
                    if csv_path.resolve().parent != folder:
                        raise ValueError("CSV link escapes its field directory.")
                    if not csv_path.is_file() or hashlib.sha256(csv_path.read_bytes()).hexdigest() != entry["csv_sha256"]:
                        raise ValueError("Stored characterization CSV checksum mismatch.")
                if (len(dataset.points) != count or dataset.field_sequence_index != index
                        or dataset.field_line_current_a != entry["current_a"]
                        or dataset.field_history_segment != entry["history_segment"]):
                    raise ValueError("Dataset does not match its committed field entry.")
            elif count or status != "skipped_field_compliance":
                raise ValueError("Committed field dataset is missing.")
        curves.append(StoredFieldCurve(index, entry["current_a"], status, folder, dataset,
                                       entry.get("detail", ""), entry.get("history_segment")))
    return StoredFieldSeries(directory, manifest, tuple(curves))
