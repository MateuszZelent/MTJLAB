"""Portable provenance next to explicitly exported, derived result data."""

from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path


def processing_manifest(state):
    return asdict(state) if is_dataclass(state) else state


def ensure_derived_destination(path, source_file=None):
    target = Path(path)
    if target.suffix.lower() in {".h5", ".hdf5", ".hdf"} or (
        source_file and os.path.normcase(str(target.resolve())) == os.path.normcase(str(Path(source_file).resolve()))
    ):
        raise ValueError("Choose a separate CSV, PNG or SVG export file; the measurement archive is read-only.")


def write_analysis_manifest(path, metadata):
    target = Path(str(path) + ".analysis.json")
    target.write_text(json.dumps({"schema": "pylab.result-analysis.v1",
        "exported_at_utc": datetime.now(UTC).isoformat(),
        "source_data_modified": False, **metadata}, ensure_ascii=False, indent=2,
        allow_nan=False) + "\n", encoding="utf-8")

