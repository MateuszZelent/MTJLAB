"""Portable provenance next to explicitly exported, derived result data."""

from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
import json
import hashlib
import os
from pathlib import Path
from app.storage.naming import sanitize_run_file_stem


def processing_manifest(state):
    return asdict(state) if is_dataclass(state) else state


def heatmap_export_path(metadata):
    """Mirror the source sweep folder below its DUT's postprocessing directory."""
    source = Path(metadata["source_file"]).resolve()
    # Re-exported standalone snapshots retain the original measurement source.
    sweep_root = next((p for p in source.parents if p.name.casefold() == "sweeps"), None)
    if sweep_root is not None:
        relative = source.relative_to(sweep_root)
        sweep_name = relative.parts[0] if len(relative.parts) > 1 else source.stem
        base = sweep_root.parent
        if base.name.casefold() == "measurements":
            base = base.parent
    else:
        base, sweep_name = source.parent, source.stem
    directory = base / "postprocessing" / "sweeps" / sweep_name
    state = metadata.get("spectrum_processing", {})
    operation = state.get("operation", "none")
    label = "raw" if operation == "none" else operation
    if operation != "none":
        reference_index = state.get("reference_index")
        label += f"_ref{reference_index}" if reference_index is not None else "_auto"
        sweep = state.get("reference_sweep")
        label += "_mean" if sweep is None else f"_repeat{sweep + 1}"
    label += "_" + "_".join(state.get("modes", ())) if state.get("modes") else ""
    component = metadata.get("map_processing", {}).get("component", "none")
    if component != "none":
        label += "_" + component
    digest = hashlib.sha256(json.dumps(metadata, sort_keys=True, allow_nan=False).encode()).hexdigest()[:10]
    stem = sanitize_run_file_stem(label)[:48] + "_" + digest
    target = directory / (stem + ".png")
    ordinal = 2
    while target.exists() or target.with_suffix(".h5").exists():
        target = directory / f"{stem}_{ordinal}.png"
        ordinal += 1
    return target


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

