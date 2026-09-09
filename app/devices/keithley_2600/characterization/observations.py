"""Operator observations stored separately from immutable acquired data."""

from datetime import datetime, timezone
import json
import math
import os
from uuid import uuid4


def save_observation(curve, first_index, last_index, *, author, description, hypothesis=""):
    dataset = curve.dataset
    if dataset is None or curve.directory is None:
        raise ValueError("Select an acquired curve before annotating it.")
    if type(first_index) is not int or type(last_index) is not int or not 0 <= first_index <= last_index < len(dataset.points):
        raise ValueError("Observation indices must select an existing contiguous point range.")
    if not author.strip() or not description.strip():
        raise ValueError("An observation requires an author and a description.")
    points = dataset.points[first_index:last_index + 1]
    def bounds(values):
        finite = [value for value in values if math.isfinite(value)]
        return [min(finite), max(finite)] if finite else None
    resistance = [p.measured_voltage_v / p.measured_current_a for p in points
                  if p.valid and not p.compliance_active and p.measured_current_a != 0]
    def endpoint_resistance(point):
        if not point.valid or point.compliance_active or point.measured_current_a == 0:
            return None
        value = point.measured_voltage_v / point.measured_current_a
        return value if math.isfinite(value) else None
    first_r, last_r = endpoint_resistance(points[0]), endpoint_resistance(points[-1])
    comparison_reason = ""
    if any(not p.valid or p.compliance_active or not math.isfinite(p.measured_current_a)
           or not math.isfinite(p.measured_voltage_v) for p in points):
        comparison_reason = "invalid_or_compliance_inside_selected_region"
    steps = [right.demanded_si - left.demanded_si for left, right in zip(points, points[1:])]
    if steps and not (all(step > 0 for step in steps) or all(step < 0 for step in steps)):
        comparison_reason = "multiple_or_repeated_sweep_branches"
    if first_r is None or last_r is None:
        comparison_reason = "unavailable_endpoint_resistance"
    delta = last_r - first_r if not comparison_reason else None
    record = {
        "schema_version": 1, "id": uuid4().hex, "source": "operator_annotation",
        "classification": "dc_anomaly", "author": author.strip(),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "description": description.strip(), "hypothesis": hypothesis.strip() or None,
        "mechanism_evidence": "unverified", "field_index": curve.index,
        "history_segment": curve.history_segment, "field_current_a": curve.current_a,
        "point_indices": [p.index for p in points],
        "source_checksum": dataset.checksum_sha256 or dataset.calculate_checksum(dataset.points),
        "demanded_range_si": bounds([p.demanded_si for p in points]),
        "source_unit": "A" if dataset.config.mode == "current" else "V",
        "measured_current_range_a": bounds([p.measured_current_a for p in points]),
        "measured_voltage_range_v": bounds([p.measured_voltage_v for p in points]),
        "qualified_resistance_range_ohm": bounds(resistance),
        "first_resistance_ohm": first_r, "last_resistance_ohm": last_r,
        "delta_resistance_ohm": delta,
        "comparison_unavailable_reason": comparison_reason or None,
        "relative_change_percent": 100 * delta / first_r if delta is not None and first_r != 0 else None,
        "relative_change_reference": "first_selected_point; measured V/I; no substitute endpoint",
        "repeatability": None, "hysteresis_verified": None, "spectrum_reference": None,
        "range_change_verified": None,
        "contains_compliance": any(p.compliance_active for p in points),
        "contains_invalid_points": any(not p.valid for p in points),
        "method": "operator_selected_acquired_indices_v1; no smoothing or interpolation",
    }
    folder = curve.directory / "observations"
    folder.mkdir(exist_ok=True)
    if folder.resolve().parent != curve.directory.resolve():
        raise ValueError("Observation directory escapes the curve directory.")
    target = folder / f"{record['id']}.json"
    temporary = target.with_suffix(".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_observations(curve):
    if curve.directory is None or curve.dataset is None:
        return ()
    folder = curve.directory / "observations"
    if not folder.exists():
        return ()
    if folder.resolve().parent != curve.directory.resolve():
        raise ValueError("Observation directory escapes the curve directory.")
    checksum = curve.dataset.checksum_sha256 or curve.dataset.calculate_checksum(curve.dataset.points)
    records = []
    for path in sorted(folder.glob("*.json")):
        if path.resolve().parent != folder.resolve():
            raise ValueError("Observation file escapes the observation directory.")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("schema_version") != 1 or record.get("source_checksum") != checksum or record.get("field_index") != curve.index:
            raise ValueError("Observation does not match its acquired curve.")
        records.append(record)
    return tuple(sorted(records, key=lambda record: (record["created_at_utc"], record["id"])))
