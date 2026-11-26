"""Portable provenance next to explicitly exported, derived result data."""

from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re

from app.domain.quantities import Quantity, QuantityError, format_quantity_auto, parse_quantity
from app.storage.filesystem_paths import native_path
from app.storage.naming import sanitize_run_file_stem


def processing_manifest(state):
    return asdict(state) if is_dataclass(state) else state


def postprocessing_directory(source):
    """Mirror the source sweep folder below its DUT's postprocessing directory."""
    source = Path(source).resolve()
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
    return base / "postprocessing" / "sweeps" / sweep_name


def analysis_file_stem(title, *, limit=115):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", title).strip(" .")
    if not cleaned or len(cleaned) > limit:
        digest = hashlib.sha256(title.encode()).hexdigest()[:10]
        prefix = cleaned[:max(0, limit - 13)].rstrip(" .-")
        cleaned = f"{prefix} - {digest}" if prefix else digest
    return cleaned


def format_measurement_range(low, high, unit, *, precision=9):
    try:
        scale = parse_quantity(f"1 {unit}") if unit else None
    except QuantityError:
        scale = None
    if scale is not None:
        low, high = low * scale.si_value, high * scale.si_value
        dimension = scale.dimension
        if low == high:
            return format_quantity_auto(low, dimension, precision=precision, preferred_unit=unit)
        engineering = format_quantity_auto(max(abs(low), abs(high)), dimension).split(" ", 1)[-1]
        low, high = (Quantity(value, dimension).as_unit(engineering) for value in (low, high))
        unit = engineering
    if low == high:
        values = f"{low:.{precision}g}"
    else:
        upper = f"{high:+.{precision}g}" if low < 0 < high else f"{high:.{precision}g}"
        values = f"{low:.{precision}g} to {upper}"
    return f"{values} {unit}".strip()


def measurement_parameter_metadata(parameters):
    return [
        {"id": key, "label": label, "unit": unit, "minimum": low, "maximum": high}
        for key, label, unit, low, high in parameters
    ]


def _heatmap_parameter_name(metadata):
    parts = []
    priority = {"measurement.series": 0, "keithley.B.current": 2, "keithley.A.current": 3, "frequency": 5}
    records = metadata.get("parameters")
    if not isinstance(records, list):
        return None
    parameters = (
        (record.get("id"), record.get("label"), record.get("unit"),
         record.get("minimum"), record.get("maximum"))
        for record in records if isinstance(record, dict)
    )
    valid = (
        (key, label, unit, low, high) for key, label, unit, low, high in parameters
        if all(isinstance(value, str) for value in (key, label, unit))
        and all(type(value) in (float, int) and math.isfinite(value) for value in (low, high))
    )
    for key, label, unit, low, high in sorted(
        valid, key=lambda p: priority.get(p[0], 1 if p[0].startswith("moke_box.vout") else 4)
    ):
        if key == "frequency" and not parts:
            continue
        if key == "measurement.series":
            if low > 0:
                part = f"Series {int(low):02d}" if low == high else f"Series {int(low):02d} to {int(high):02d}"
            else:
                continue
        elif key.startswith("measurement.") and key != "measurement.visit":
            continue
        else:
            # Keep channel identification and SI engineering display while
            # using fewer digits than the exact values in the export metadata.
            label = label.replace("MOKE Box VOUT ", "MOKE VOUT")
            part = f"{label} {format_measurement_range(low, high, unit, precision=6)}"
        parts.append(part)
    if not parts:
        return None
    candidate = " - ".join(parts)
    if len(candidate) <= 115:
        return analysis_file_stem(candidate)
    # Trim at a parameter boundary and keep different omitted values distinct.
    digest = hashlib.sha256(candidate.encode()).hexdigest()[:10]
    while len(" - ".join(parts)) > 102 and len(parts) > 1:
        parts.pop()
    return analysis_file_stem(" - ".join((*parts, digest)))


def heatmap_export_path(metadata):
    directory = postprocessing_directory(metadata["source_file"])
    stem = _heatmap_parameter_name(metadata)
    if stem is None and metadata.get("title"):
        stem = analysis_file_stem(metadata["title"])
    if stem is None:
        stem = _processing_file_stem(metadata)
    candidate = stem
    ordinal = 2
    suffixes = (".h5", ".png", ".svg", ".csv", ".png.analysis.json", ".svg.analysis.json", ".csv.analysis.json")
    while any(native_path(directory / f"{candidate}{suffix}").exists() for suffix in suffixes):
        candidate = f"{stem} ({ordinal})"
        ordinal += 1
    return directory / f"{candidate}.png"


def _processing_file_stem(metadata):
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
    return sanitize_run_file_stem(label)[:48] + "_" + digest


def ensure_derived_destination(path, source_file=None):
    target = Path(path)
    if target.suffix.lower() in {".h5", ".hdf5", ".hdf"} or (
        source_file and os.path.normcase(str(target.resolve())) == os.path.normcase(str(Path(source_file).resolve()))
    ):
        raise ValueError("Choose a separate CSV, PNG or SVG export file; the measurement archive is read-only.")


def write_analysis_manifest(path, metadata):
    target = native_path(str(path) + ".analysis.json")
    target.write_text(json.dumps({"schema": "pylab.result-analysis.v1",
        "exported_at_utc": datetime.now(UTC).isoformat(),
        "source_data_modified": False, **metadata}, ensure_ascii=False, indent=2,
        allow_nan=False) + "\n", encoding="utf-8")

