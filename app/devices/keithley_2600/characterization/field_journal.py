"""Offline diagnostic export of raw readings, including uncommitted field curves.

This does not resume a run, infer OUTPUT OFF, or qualify an incomplete reading.
"""

import csv
import hashlib
import json
import math
import os


def export_field_journal(series):
    path = series.directory / "events.jsonl"
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    rows = []
    seen = set()
    counts = {}
    completed = set()
    truncated = False
    def reject_constant(value):
        raise ValueError(f"Nonfinite journal constant: {value}")
    for number, line in enumerate(lines, 1):
        # Only newline-terminated records were fully published by the writer.
        if not line.endswith(b"\n"):
            if number != len(lines):
                raise ValueError("Incomplete journal record before EOF")
            truncated = True
            break
        record = json.loads(line, parse_constant=reject_constant)
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            raise ValueError(f"Invalid journal record at line {number}")
        kind, payload = record.get("kind"), record["payload"]
        if kind not in {"sample_raw", "sample_point"}:
            continue
        field, point = payload.get("field_index"), payload.get("index")
        if type(field) is not int or not 0 <= field < len(series.curves) or type(point) is not int or point < 0:
            raise ValueError(f"Invalid journal reading identity at line {number}")
        key = (field, point)
        if kind == "sample_point":
            if key not in seen or key in completed:
                raise ValueError("Unmatched or duplicate completed journal point")
            completed.add(key)
            continue
        if key in seen:
            raise ValueError("Duplicate raw journal reading")
        if point != counts.get(field, 0):
            raise ValueError("Noncontiguous raw journal readings")
        counts[field] = point + 1
        seen.add(key)
        values = {name: payload.get(name) for name in (
            "demanded_si", "measured_voltage_v", "measured_current_a", "power_w", "timestamp_epoch")}
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values.values()):
            raise ValueError("Invalid raw journal measurement")
        if type(payload.get("compliance_active")) is not bool:
            raise ValueError("Invalid raw journal compliance flag")
        rows.append({"field_index": field, "point_index": point,
                     "field_target_a": series.curves[field].current_a,
                     "source_unit": "A" if series.manifest["config"]["sweep"]["mode"] == "current" else "V",
                     **values, "compliance_active": payload["compliance_active"]})
    for row in rows:
        key = row["field_index"], row["point_index"]
        dataset = series.curves[key[0]].dataset
        committed = dataset is not None and key[1] < len(dataset.points)
        if committed:
            saved = dataset.points[key[1]]
            for name in ("demanded_si", "measured_voltage_v", "measured_current_a", "power_w", "compliance_active"):
                if row[name] != getattr(saved, name):
                    raise ValueError("Journal reading differs from committed dataset")
        row["record_state"] = "committed_dataset" if committed else (
            "uncommitted_full_point" if key in completed else "raw_only_field_after_unconfirmed")
    target = series.directory / "journal_readings.csv"
    temporary = series.directory / ".journal_readings.tmp.csv"
    names = ["field_index", "point_index", "field_target_a", "source_unit", "demanded_si",
             "measured_voltage_v", "measured_current_a", "power_w", "timestamp_epoch",
             "compliance_active", "record_state"]
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=names)
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"csv": target.name, "raw_readings": len(rows), "incomplete_final_record": truncated,
            "source_sha256": hashlib.sha256(data).hexdigest(),
            "purpose": "diagnostic_only_no_resume_or_output_state_inference"}
