"""Qt-independent reference I-V curve discovery and readers for Rigol Load Calculator.

Reads recorded HDF5 sweep runs, Keithley characterization datasets/CSVs, and
sweep-export CSVs (such as `keithley-a.csv`) strictly read-only, splitting
multi-dimensional sweeps into selectable `(Keithley B, MOKE Box VOUT0)` slices.
"""

from __future__ import annotations

import csv
import hashlib
import math
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.devices.keithley_2600.characterization.field_reader import load_field_dataset
from app.devices.rigol_dg1000z.mtj_calculator import (
    ReferenceIVCurve,
    ReferencePoint,
    split_curve_passes,
)
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_RESISTANCE,
    DIMENSION_VOLTAGE,
    QuantityError,
    format_quantity_auto,
    parse_quantity,
)
from app.inventory.models import SampleRunRecord
from app.inventory.store import InventoryStore
from app.storage.hdf5_reader import Hdf5RunReader, StoredPoint

_CACHE_MAX_ENTRIES = 512

_UNIT_SCALE_BY_SUFFIX: dict[str, tuple[str, float]] = {
    "v": (DIMENSION_VOLTAGE, 1.0),
    "mv": (DIMENSION_VOLTAGE, 1e-3),
    "uv": (DIMENSION_VOLTAGE, 1e-6),
    "µv": (DIMENSION_VOLTAGE, 1e-6),
    "μv": (DIMENSION_VOLTAGE, 1e-6),
    "kv": (DIMENSION_VOLTAGE, 1e3),
    "a": (DIMENSION_CURRENT, 1.0),
    "ma": (DIMENSION_CURRENT, 1e-3),
    "ua": (DIMENSION_CURRENT, 1e-6),
    "µa": (DIMENSION_CURRENT, 1e-6),
    "μa": (DIMENSION_CURRENT, 1e-6),
    "na": (DIMENSION_CURRENT, 1e-9),
    "ohm": (DIMENSION_RESISTANCE, 1.0),
    "ω": (DIMENSION_RESISTANCE, 1.0),
    "Ω": (DIMENSION_RESISTANCE, 1.0),
    "kohm": (DIMENSION_RESISTANCE, 1e3),
    "kΩ": (DIMENSION_RESISTANCE, 1e3),
    "mohm": (DIMENSION_RESISTANCE, 1e6),
}

_BRACKET_UNIT_RE = re.compile(r"[\[\(]\s*([^\]\)]+?)\s*[\]\)]\s*$")
_MOKE_VOUT_KEY_RE = re.compile(r"^moke_box\.vout([0-7])\.voltage$")


@dataclass(frozen=True, slots=True)
class LibraryScanResult:
    """Output of scanning inventory records and measurement directories."""

    curves: tuple[ReferenceIVCurve, ...]
    skipped_files: tuple[tuple[Path, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ReferenceFilter:
    """Criteria for filtering reference MTJ curves in the browser."""

    text: str = ""
    sample_query: str = ""
    source_kind: str = "all"  # "all" | "hdf5" | "characterization" | "csv"
    keithley_b_target_a: float | None = None
    keithley_b_tol_a: float = 0.5e-3
    moke_vout_target_v: float | None = None
    moke_vout_tol_v: float = 0.5e-3


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_float(raw: object) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if math.isfinite(val) else None
    text = str(raw).strip().replace(",", ".")
    if not text:
        return None
    try:
        val = float(text)
    except ValueError:
        return None
    return val if math.isfinite(val) else None


def _parse_bool_flag(raw: object, *, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(round(raw)) if math.isfinite(raw) else default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


def _column_unit(header: str) -> tuple[str, float] | None:
    """Extract an explicit (dimension, SI_scale) pair from a CSV column name."""
    cleaned = header.strip()
    if not cleaned:
        return None
    bracket = _BRACKET_UNIT_RE.search(cleaned)
    if bracket is not None:
        unit_text = bracket.group(1).strip()
        try:
            qty = parse_quantity(f"1 {unit_text}")
            return qty.dimension, qty.si_value
        except QuantityError:
            pass
        low_u = unit_text.lower()
        if low_u in _UNIT_SCALE_BY_SUFFIX:
            return _UNIT_SCALE_BY_SUFFIX[low_u]
        return None

    parts = [part for part in re.split(r"[\s_\-/]+", cleaned) if part]
    if len(parts) < 2:
        return None
    tail = parts[-1]
    # Preserve prefix case sensitivity for m vs M (mV vs MV, mA vs MA, Mohm vs mohm).
    try:
        qty = parse_quantity(f"1 {tail}")
        return qty.dimension, qty.si_value
    except QuantityError:
        pass
    if tail in _UNIT_SCALE_BY_SUFFIX:
        return _UNIT_SCALE_BY_SUFFIX[tail]
    if tail.lower() in {"v", "mv", "uv", "kv", "a", "ma", "ua", "na", "ohm", "kohm"}:
        # Reject uppercase "M" misread as milli; only match lowercase or unambiguous aliases.
        if tail.startswith("M") and tail[1:].lower() in {"v", "a"}:
            return None
        return _UNIT_SCALE_BY_SUFFIX[tail.lower()]
    return None


def _quantize_condition(value: float | None, step: float) -> int | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value / step)


def _format_slice_title(
    base_title: str,
    *,
    kb_a: float | None,
    moke_v: float | None,
    slice_count: int,
    slice_index: int,
) -> str:
    parts = [base_title]
    if kb_a is not None:
        parts.append(f"KB {format_quantity_auto(kb_a, DIMENSION_CURRENT, precision=4)}")
    if moke_v is not None:
        parts.append(f"MOKE {format_quantity_auto(moke_v, DIMENSION_VOLTAGE, precision=4)}")
    if slice_count > 1 and kb_a is None and moke_v is None:
        parts.append(f"Slice {slice_index + 1}")
    return " · ".join(parts)


def _select_csv_columns(
    headers: Sequence[str],
) -> tuple[
    tuple[str, float],
    tuple[str, float],
    tuple[str, float] | None,
    tuple[str, float] | None,
    str | None,
    str | None,
    str | None,
    tuple[str, float] | None,
    tuple[str, float] | None,
    tuple[str, float] | None,
]:
    """Identify required MTJ I/V columns (with explicit units) and optional metadata columns."""
    current_candidates: list[tuple[int, str, float]] = []
    voltage_candidates: list[tuple[int, str, float]] = []
    resistance_col: tuple[str, float] | None = None
    setpoint_col: tuple[str, float] | None = None
    resistance_valid_col: str | None = None
    compliance_col: str | None = None
    valid_col: str | None = None
    kb_set_col: tuple[str, float] | None = None
    kb_meas_col: tuple[str, float] | None = None
    moke_col: tuple[str, float] | None = None

    for header in headers:
        low = header.strip().lower()
        unit_info = _column_unit(header)

        if "keithley_b" in low or "field_before" in low or "field_after" in low or "field_line" in low:
            if unit_info and unit_info[0] == DIMENSION_CURRENT:
                if "setpoint" in low or "demanded" in low:
                    if kb_set_col is None:
                        kb_set_col = (header, unit_info[1])
                elif "current" in low and kb_meas_col is None:
                    kb_meas_col = (header, unit_info[1])
            continue

        if "moke" in low and ("vout" in low or "voltage" in low):
            if unit_info and unit_info[0] == DIMENSION_VOLTAGE and moke_col is None:
                moke_col = (header, unit_info[1])
            continue

        if "compliance" in low and compliance_col is None:
            compliance_col = header
            continue
        if "resistance_valid" in low and resistance_valid_col is None:
            resistance_valid_col = header
            continue
        if low == "valid" and valid_col is None:
            valid_col = header
            continue

        if "setpoint" in low or "demanded" in low:
            if unit_info and unit_info[0] in {DIMENSION_CURRENT, DIMENSION_VOLTAGE} and setpoint_col is None:
                setpoint_col = (header, unit_info[1])
            continue

        if "resistance" in low and "apparent" not in low:
            if unit_info and unit_info[0] == DIMENSION_RESISTANCE and resistance_col is None:
                resistance_col = (header, unit_info[1])
            continue

        if "current" in low and unit_info and unit_info[0] == DIMENSION_CURRENT:
            priority = 0 if "keithley_a" in low else (1 if low.startswith("current") else 2)
            current_candidates.append((priority, header, unit_info[1]))
            continue

        if "voltage" in low and unit_info and unit_info[0] == DIMENSION_VOLTAGE:
            priority = 0 if "keithley_a" in low else (1 if low.startswith("voltage") else 2)
            voltage_candidates.append((priority, header, unit_info[1]))
            continue

    if not current_candidates or not voltage_candidates:
        missing = []
        if not current_candidates:
            missing.append("current column with an explicit unit (e.g. Current_A or keithley_a_current_mA)")
        if not voltage_candidates:
            missing.append("voltage column with an explicit unit (e.g. Voltage_V or keithley_a_voltage_mV)")
        raise ValueError(
            "CSV file cannot be imported without explicit units: missing " + " and ".join(missing) + "."
        )

    current_candidates.sort(key=lambda item: item[0])
    voltage_candidates.sort(key=lambda item: item[0])
    _, curr_header, curr_scale = current_candidates[0]
    _, volt_header, volt_scale = voltage_candidates[0]
    return (
        (curr_header, curr_scale),
        (volt_header, volt_scale),
        resistance_col,
        setpoint_col,
        resistance_valid_col,
        compliance_col,
        valid_col,
        kb_set_col,
        kb_meas_col,
        moke_col,
    )


def _parse_csv_comment_metadata(comment_lines: Sequence[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in comment_lines:
        stripped = line.lstrip("#").strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        metadata[key.strip().lower()] = value.strip()
    return metadata


def load_csv_reference_curves(path: str | Path) -> tuple[ReferenceIVCurve, ...]:
    """Load one or more MTJ I-V curves from a sweep-export or characterization CSV."""
    file_path = Path(path).resolve()
    raw_lines = file_path.read_text(encoding="utf-8-sig").splitlines()
    comment_lines: list[str] = []
    data_lines: list[str] = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            comment_lines.append(stripped)
        else:
            data_lines.append(line)

    if len(data_lines) < 3:
        raise ValueError(f"CSV file {file_path.name} does not contain a header and at least 2 data rows.")

    reader = csv.DictReader(data_lines)
    headers = [h for h in (reader.fieldnames or ()) if h is not None]
    (
        (curr_col, curr_scale),
        (volt_col, volt_scale),
        res_col,
        set_col,
        res_valid_col,
        comp_col,
        valid_col,
        kb_set_col,
        kb_meas_col,
        moke_col,
    ) = _select_csv_columns(headers)

    meta = _parse_csv_comment_metadata(comment_lines)
    is_char_csv = any("keithley sample characterization" in line.lower() for line in comment_lines) or (
        "Voltage_V" in headers and "Current_A" in headers and "Demanded_SI" in headers
    )
    source_kind = "characterization_csv" if is_char_csv else "sweep_csv"
    sample_id = meta.get("sample id", "")
    sense_raw = meta.get("sense mode", "")
    sense_mode = "4wire" if "4-wire" in sense_raw.lower() or "kelvin" in sense_raw.lower() else (
        "2wire" if "2-wire" in sense_raw.lower() or "local" in sense_raw.lower() else "unknown"
    )
    mtj_channel = meta.get("channel", "A").strip().upper()
    if mtj_channel not in {"A", "B"}:
        mtj_channel = "A"
    header_kb_a = _parse_float(meta.get("field line current [a]"))
    created_at = meta.get("started at", "")
    digest = _file_sha256(file_path)

    # Group valid points by (keithley_b_key, moke_vout_key)
    grouped: OrderedDict[
        tuple[int | None, int | None],
        list[tuple[ReferencePoint, float | None, float | None, float | None]],
    ] = OrderedDict()
    excluded = 0
    reasons: set[str] = set()

    for row_index, row in enumerate(reader):
        if valid_col is not None and not _parse_bool_flag(row.get(valid_col), default=True):
            excluded += 1
            reasons.add("invalid_flag")
            continue
        if comp_col is not None and _parse_bool_flag(row.get(comp_col), default=False):
            excluded += 1
            reasons.add("compliance")
            continue

        raw_i = _parse_float(row.get(curr_col))
        raw_v = _parse_float(row.get(volt_col))
        if raw_i is None or raw_v is None:
            excluded += 1
            reasons.add("nonfinite_iv")
            continue

        current_a = raw_i * curr_scale
        voltage_v = raw_v * volt_scale
        resistance_ohm: float | None = None
        if res_col is not None:
            raw_r = _parse_float(row.get(res_col[0]))
            if raw_r is not None and raw_r > 0.0:
                resistance_ohm = raw_r * res_col[1]
        res_valid = _parse_bool_flag(row.get(res_valid_col), default=True) if res_valid_col else True

        setpoint_si: float | None = None
        if set_col is not None:
            raw_sp = _parse_float(row.get(set_col[0]))
            if raw_sp is not None:
                setpoint_si = raw_sp * set_col[1]

        kb_set_a: float | None = header_kb_a
        if kb_set_col is not None:
            raw_kb = _parse_float(row.get(kb_set_col[0]))
            if raw_kb is not None:
                kb_set_a = raw_kb * kb_set_col[1]
        kb_meas_a: float | None = None
        if kb_meas_col is not None:
            raw_kbm = _parse_float(row.get(kb_meas_col[0]))
            if raw_kbm is not None:
                kb_meas_a = raw_kbm * kb_meas_col[1]
        moke_v: float | None = None
        if moke_col is not None:
            raw_m = _parse_float(row.get(moke_col[0]))
            if raw_m is not None:
                moke_v = raw_m * moke_col[1]

        kb_key = _quantize_condition(kb_set_a if kb_set_a is not None else kb_meas_a, 1e-4 if kb_set_a is None else 1e-7)
        moke_key = _quantize_condition(moke_v, 1e-6)
        point = ReferencePoint(
            index=row_index,
            current_a=current_a,
            voltage_v=voltage_v,
            resistance_ohm=resistance_ohm,
            resistance_valid=res_valid,
            keithley_b_a=kb_set_a if kb_set_a is not None else kb_meas_a,
            moke_vout_v=moke_v,
        )
        grouped.setdefault((kb_key, moke_key), []).append((point, setpoint_si, kb_set_a, kb_meas_a))

    curves: list[ReferenceIVCurve] = []
    base_name = file_path.parent.name if file_path.name.lower() == "characterization.csv" else file_path.stem
    slice_count = len(grouped)
    for slice_idx, ((_kb_k, _m_k), entries) in enumerate(grouped.items()):
        pts = [e[0] for e in entries]
        if len(pts) < 2:
            continue
        setpoints = [e[1] for e in entries]
        passes = split_curve_passes(pts, setpoints=setpoints)
        kb_sets = [e[2] for e in entries if e[2] is not None]
        kb_meas = [e[3] for e in entries if e[3] is not None]
        mokes = [e[0].moke_vout_v for e in entries if e[0].moke_vout_v is not None]
        kb_set_val = sum(kb_sets) / len(kb_sets) if kb_sets else None
        kb_meas_val = sum(kb_meas) / len(kb_meas) if kb_meas else None
        moke_val = sum(mokes) / len(mokes) if mokes else None
        title = _format_slice_title(
            base_name,
            kb_a=kb_set_val if kb_set_val is not None else kb_meas_val,
            moke_v=moke_val,
            slice_count=slice_count,
            slice_index=slice_idx,
        )
        curves.append(
            ReferenceIVCurve(
                curve_id=f"{source_kind}:{file_path}:{slice_idx}",
                title=title,
                source_kind=source_kind,
                source_path=file_path,
                source_sha256=digest,
                sample_id=sample_id,
                sample_name=sample_id,
                coordinate_label=sample_id if re.match(r"^R\d+C\d+$", sample_id, re.IGNORECASE) else "",
                mtj_channel=mtj_channel,
                keithley_b_setpoint_a=kb_set_val,
                keithley_b_measured_a=kb_meas_val,
                moke_vout_v=moke_val,
                moke_hall_v=None,
                sense_mode=sense_mode,
                passes=passes,
                excluded_point_count=excluded,
                exclusion_reasons=tuple(sorted(reasons)),
                created_at_utc=created_at,
            )
        )

    if not curves:
        raise ValueError(
            f"CSV file {file_path.name} contains fewer than 2 valid finite (I, V) points."
        )
    return tuple(curves)


def load_characterization_json_curve(
    path: str | Path,
    *,
    run_record: SampleRunRecord | None = None,
) -> ReferenceIVCurve:
    """Load one Keithley characterization `dataset.json` into a ReferenceIVCurve."""
    file_path = Path(path).resolve()
    dataset = load_field_dataset(file_path)
    points: list[ReferencePoint] = []
    setpoints: list[float | None] = []
    excluded = 0
    reasons: set[str] = set()
    kb_meas_list: list[float] = []

    for pt in dataset.points:
        if not pt.valid:
            excluded += 1
            reasons.add("invalid_flag")
            continue
        if pt.compliance_active:
            excluded += 1
            reasons.add("compliance")
            continue
        if dataset.field_line_current_a is not None and (pt.field_before is None or pt.field_after is None):
            excluded += 1
            reasons.add("field_conditions_undocumented")
            continue
        if any(
            obs is not None and obs.compliance_active
            for obs in (pt.field_before, pt.field_after)
        ):
            excluded += 1
            reasons.add("field_compliance")
            continue
        if any(
            obs is not None
            and not all(
                math.isfinite(val)
                for val in (obs.measured_current_a, obs.measured_voltage_v, obs.power_w)
            )
            for obs in (pt.field_before, pt.field_after)
        ):
            excluded += 1
            reasons.add("invalid_field_observation")
            continue
        if not (math.isfinite(pt.measured_current_a) and math.isfinite(pt.measured_voltage_v)):
            excluded += 1
            reasons.add("nonfinite_iv")
            continue
        if pt.field_before is not None and math.isfinite(pt.field_before.measured_current_a):
            kb_meas_list.append(pt.field_before.measured_current_a)
        r_val = pt.true_resistance_ohm if math.isfinite(pt.true_resistance_ohm) else None
        points.append(
            ReferencePoint(
                index=pt.index,
                current_a=pt.measured_current_a,
                voltage_v=pt.measured_voltage_v,
                resistance_ohm=r_val,
                resistance_valid=r_val is not None and r_val > 0.0,
                keithley_b_a=dataset.field_line_current_a,
            )
        )
        setpoints.append(pt.demanded_si)

    if len(points) < 2:
        raise ValueError(f"Characterization dataset {file_path.name} has fewer than 2 valid points.")

    passes = split_curve_passes(points, setpoints=setpoints)
    kb_set = dataset.field_line_current_a
    kb_meas = sum(kb_meas_list) / len(kb_meas_list) if kb_meas_list else None
    sample_id = (run_record.sample_id if run_record else "") or dataset.config.metadata.sample_id
    sample_name = (run_record.sample_name if run_record else "") or sample_id
    coord = ""
    if run_record and run_record.row and run_record.col:
        coord = f"R{run_record.row}C{run_record.col}"
    elif run_record and run_record.device_label:
        coord = run_record.device_label
    base_title = (run_record.recipe_name if run_record and run_record.recipe_name else file_path.parent.name)
    title = _format_slice_title(
        base_title,
        kb_a=kb_set if kb_set is not None else kb_meas,
        moke_v=None,
        slice_count=1,
        slice_index=0,
    )
    return ReferenceIVCurve(
        curve_id=f"characterization_json:{file_path}",
        title=title,
        source_kind="characterization_json",
        source_path=file_path,
        source_sha256=dataset.checksum_sha256 or _file_sha256(file_path),
        sample_id=sample_id,
        sample_name=sample_name,
        coordinate_label=coord,
        mtj_channel=dataset.config.channel,
        keithley_b_setpoint_a=kb_set,
        keithley_b_measured_a=kb_meas,
        moke_vout_v=None,
        moke_hall_v=None,
        sense_mode=dataset.config.sense_mode,
        passes=passes,
        excluded_point_count=excluded,
        exclusion_reasons=tuple(sorted(reasons)),
        created_at_utc=dataset.completed_at_iso or dataset.started_at_iso,
    )


def _extract_moke_vout(point: StoredPoint) -> float | None:
    if "moke_box.vout0.voltage" in point.setpoints:
        val = _parse_float(point.setpoints.get("moke_box.vout0.voltage"))
        if val is not None:
            return val
    for key, raw in point.setpoints.items():
        if _MOKE_VOUT_KEY_RE.match(key):
            val = _parse_float(raw)
            if val is not None:
                return val
    direct = _parse_float(point.measurements.get("moke_box.vout_voltage_v"))
    if direct is not None:
        return direct
    # Fixed (pre-loop) set_moke_voltage persists in safety_context & device_states
    # after point_measurements.clear() wipes checkpoint 0's measurement dict.
    safety_ctx = point.metadata.get("safety_context")
    if isinstance(safety_ctx, Mapping):
        moke_ctx = safety_ctx.get("moke_box")
        if isinstance(moke_ctx, Mapping):
            val = _parse_float(moke_ctx.get("actual_v") or moke_ctx.get("requested_v"))
            if val is not None:
                return val
    moke_state = point.device_states.get("moke_box")
    if isinstance(moke_state, Mapping):
        if "dac_shutdown" in moke_state:
            return 0.0
        vc = moke_state.get("voltage_control")
        if isinstance(vc, Mapping):
            actual = vc.get("actual")
            if isinstance(actual, Mapping):
                val = _parse_float(actual.get("actual_v") or actual.get("requested_v"))
                if val is not None:
                    return val
    return None


def _extract_fixed_keithley_condition(
    point: StoredPoint,
    channel: str,
) -> tuple[float | None, float | None]:
    """Return (setpoint_a, measured_a) including persistent safety_context / device_states."""
    sets = point.setpoints
    meas = point.measurements
    kb_set = _parse_float(
        sets.get(f"keithley.{channel}.current")
        or sets.get(f"keithley.{channel}.level")
    )
    kb_meas = _parse_float(meas.get(f"keithley.{channel}.current_a"))
    if kb_set is None:
        safety_ctx = point.metadata.get("safety_context")
        if isinstance(safety_ctx, Mapping):
            ch_ctx = safety_ctx.get(f"keithley.{channel}")
            if isinstance(ch_ctx, Mapping) and str(ch_ctx.get("mode", "")).lower() == "current":
                kb_set = _parse_float(ch_ctx.get("level_si"))
    return kb_set, kb_meas


def _extract_hdf5_sense_mode(points: Sequence[StoredPoint], channel: str) -> str:
    for pt in points:
        safety_ctx = pt.metadata.get("safety_context")
        if isinstance(safety_ctx, Mapping):
            ch_ctx = safety_ctx.get(f"keithley.{channel}")
            if isinstance(ch_ctx, Mapping):
                mode = str(ch_ctx.get("sense_mode", "")).strip().lower()
                if mode in {"2wire", "4wire"}:
                    return mode
        k_state = pt.device_states.get("keithley")
        if isinstance(k_state, Mapping):
            for key in (f"measurement_{channel}", f"source_{channel}", f"configure_{channel}"):
                entry = k_state.get(key)
                if isinstance(entry, Mapping):
                    actual = entry.get("actual")
                    if isinstance(actual, Mapping):
                        mode = str(actual.get("sense_mode", "")).strip().lower()
                        if mode in {"2wire", "4wire"}:
                            return mode
    return "2wire"


def _detect_mtj_channel(points: Sequence[StoredPoint]) -> str | None:
    for channel in ("A", "B"):
        iv = [
            (_parse_float(p.measurements.get(f"keithley.{channel}.current_a")),
             _parse_float(p.measurements.get(f"keithley.{channel}.voltage_v")))
            for p in points
        ]
        valid = [(i, v) for i, v in iv if i is not None and v is not None]
        if len(valid) >= 2:
            currents = [i for i, _ in valid]
            voltages = [v for _, v in valid]
            if (max(currents) - min(currents) > 1e-12) or (max(voltages) - min(voltages) > 1e-12):
                return channel
    return None


def load_hdf5_reference_curves(
    path: str | Path,
    *,
    run_record: SampleRunRecord | None = None,
) -> tuple[ReferenceIVCurve, ...]:
    """Load Keithley MTJ I-V curves from an HDF5 sweep run, preserving contiguous magnetic visits."""
    file_path = Path(path).resolve()
    summary = Hdf5RunReader.summary(file_path)
    # Use include_details=True so fixed pre-loop MOKE VOUT0, Keithley B, and sense_mode
    # stored in metadata["safety_context"] / device_states are available on every checkpoint.
    points = Hdf5RunReader.points(file_path, include_details=True)
    if len(points) < 2:
        return ()

    mtj_channel = _detect_mtj_channel(points)
    if mtj_channel is None:
        return ()

    sense_mode = _extract_hdf5_sense_mode(points, mtj_channel)
    other_channel = "B" if mtj_channel == "A" else "A"

    # Preserve contiguous visits to (Keithley B, MOKE VOUT0) so field-hysteresis loops
    # visiting the same field on ascending/descending branches stay distinct magnetic states.
    segments: list[
        tuple[
            tuple[int | None, int | None],
            list[tuple[ReferencePoint, float | None, float | None, float | None, float | None]],
        ]
    ] = []
    excluded = 0
    reasons: set[str] = set()
    held_moke_v: float | None = None

    for pt in points:
        meas = pt.measurements
        sets = pt.setpoints
        if pt.status and pt.status.strip().lower() not in {"ok", "completed", "success", "unknown"}:
            excluded += 1
            reasons.add(f"status_{pt.status.strip().lower()}")
            continue
        if f"keithley.{mtj_channel}.output_enabled" in meas and not _parse_bool_flag(
            meas.get(f"keithley.{mtj_channel}.output_enabled"), default=True
        ):
            excluded += 1
            reasons.add("output_disabled")
            continue
        if f"keithley.{mtj_channel}.measurement_path_connected" in meas and not _parse_bool_flag(
            meas.get(f"keithley.{mtj_channel}.measurement_path_connected"), default=True
        ):
            excluded += 1
            reasons.add("measurement_path_disconnected")
            continue
        if _parse_bool_flag(meas.get(f"keithley.{mtj_channel}.compliance_detected"), default=False):
            excluded += 1
            reasons.add("compliance")
            continue
        if mtj_channel == "A" and _parse_bool_flag(
            meas.get("keithley.B.compliance_detected"), default=False
        ):
            excluded += 1
            reasons.add("field_compliance")
            continue
        current_a = _parse_float(meas.get(f"keithley.{mtj_channel}.current_a"))
        voltage_v = _parse_float(meas.get(f"keithley.{mtj_channel}.voltage_v"))
        if current_a is None or voltage_v is None:
            excluded += 1
            reasons.add("nonfinite_iv")
            continue
        res_ohm = _parse_float(meas.get(f"keithley.{mtj_channel}.resistance_ohm"))
        if res_ohm is not None and res_ohm <= 0.0:
            res_ohm = None
        res_valid = _parse_bool_flag(meas.get(f"keithley.{mtj_channel}.resistance_valid"), default=True)

        sp = _parse_float(
            sets.get(f"keithley.{mtj_channel}.current")
            or sets.get(f"keithley.{mtj_channel}.level")
            or sets.get(f"keithley.{mtj_channel}.voltage")
        )

        kb_set: float | None = None
        kb_meas: float | None = None
        if mtj_channel == "A":
            kb_set, kb_meas = _extract_fixed_keithley_condition(pt, other_channel)

        moke_v = _extract_moke_vout(pt)
        if moke_v is not None:
            held_moke_v = moke_v
        elif held_moke_v is not None:
            # Older runs without safety_context wrote moke_box.vout_voltage_v on point 0 only.
            moke_v = held_moke_v
        hall_v = _parse_float(meas.get("moke_box.hall1_voltage_v"))

        kb_key = _quantize_condition(
            kb_set if kb_set is not None else kb_meas,
            1e-7 if kb_set is not None else 1e-4,
        )
        moke_key = _quantize_condition(moke_v, 1e-6)
        cond_key = (kb_key, moke_key)
        ref_pt = ReferencePoint(
            index=pt.index,
            current_a=current_a,
            voltage_v=voltage_v,
            resistance_ohm=res_ohm,
            resistance_valid=res_valid,
            keithley_b_a=kb_set if kb_set is not None else kb_meas,
            moke_vout_v=moke_v,
        )
        if not segments or segments[-1][0] != cond_key:
            segments.append((cond_key, []))
        segments[-1][1].append((ref_pt, sp, kb_set, kb_meas, hall_v))

    if not segments:
        return ()

    sample_id = (run_record.sample_id if run_record else "") or (summary.sample_id or "")
    sample_name = (run_record.sample_name if run_record else "") or (summary.sample_name or sample_id)
    coord = ""
    if run_record and run_record.row and run_record.col:
        coord = f"R{run_record.row}C{run_record.col}"
    elif summary.sample_coordinate_label:
        coord = summary.sample_coordinate_label
    elif summary.sample_row and summary.sample_col:
        coord = f"R{summary.sample_row}C{summary.sample_col}"
    elif run_record and run_record.device_label:
        coord = run_record.device_label

    if run_record and run_record.recipe_name:
        base_title = run_record.recipe_name
    elif file_path.name == "data.h5":
        base_title = file_path.parent.name
    else:
        base_title = file_path.stem

    digest = (run_record.run_sha256 if run_record and run_record.run_sha256 else "") or _file_sha256(file_path)
    created_at = (run_record.created_at_utc if run_record and run_record.created_at_utc else "") or (
        summary.created_at_utc or ""
    )

    # Track repeated visits to identical conditions to label magnetic-history segments clearly.
    visit_totals: dict[tuple[int | None, int | None], int] = {}
    for cond_key, entries in segments:
        if len(entries) >= 2:
            visit_totals[cond_key] = visit_totals.get(cond_key, 0) + 1
    visit_seen: dict[tuple[int | None, int | None], int] = {}

    curves: list[ReferenceIVCurve] = []
    slice_count = len(segments)
    for slice_idx, (cond_key, entries) in enumerate(segments):
        pts = [e[0] for e in entries]
        if len(pts) < 2:
            continue
        currents = [p.current_a for p in pts]
        voltages = [p.voltage_v for p in pts]
        if (max(currents) - min(currents) <= 1e-12) and (max(voltages) - min(voltages) <= 1e-12):
            continue
        setpoints = [e[1] for e in entries]
        passes = split_curve_passes(pts, setpoints=setpoints)
        kb_sets = [e[2] for e in entries if e[2] is not None]
        kb_meas = [e[3] for e in entries if e[3] is not None]
        mokes = [e[0].moke_vout_v for e in entries if e[0].moke_vout_v is not None]
        halls = [e[4] for e in entries if e[4] is not None]
        kb_set_val = sum(kb_sets) / len(kb_sets) if kb_sets else None
        kb_meas_val = sum(kb_meas) / len(kb_meas) if kb_meas else None
        moke_val = sum(mokes) / len(mokes) if mokes else None
        hall_val = sum(halls) / len(halls) if halls else None
        visit_seen[cond_key] = visit_seen.get(cond_key, 0) + 1
        title = _format_slice_title(
            base_title,
            kb_a=kb_set_val if kb_set_val is not None else kb_meas_val,
            moke_v=moke_val,
            slice_count=slice_count,
            slice_index=slice_idx,
        )
        if visit_totals.get(cond_key, 0) > 1:
            title = f"{title} · Visit {visit_seen[cond_key]}"
        curves.append(
            ReferenceIVCurve(
                curve_id=f"hdf5:{file_path}:{slice_idx}",
                title=title,
                source_kind="hdf5",
                source_path=file_path,
                source_sha256=digest,
                sample_id=sample_id,
                sample_name=sample_name,
                coordinate_label=coord,
                mtj_channel=mtj_channel,
                keithley_b_setpoint_a=kb_set_val,
                keithley_b_measured_a=kb_meas_val,
                moke_vout_v=moke_val,
                moke_hall_v=hall_val,
                sense_mode=sense_mode,
                passes=passes,
                excluded_point_count=excluded,
                exclusion_reasons=tuple(sorted(reasons)),
                created_at_utc=created_at,
            )
        )
    return tuple(curves)


def filter_reference_curves(
    curves: Sequence[ReferenceIVCurve],
    criteria: ReferenceFilter,
) -> tuple[ReferenceIVCurve, ...]:
    """Filter reference curves by text, sample/cell, source kind, Keithley B, and MOKE VOUT0."""
    text_q = criteria.text.strip().casefold()
    sample_q = criteria.sample_query.strip().casefold()
    kind_q = criteria.source_kind.strip().lower()

    matched: list[ReferenceIVCurve] = []
    for curve in curves:
        if kind_q and kind_q != "all":
            if kind_q == "characterization":
                if curve.source_kind not in {"characterization_csv", "characterization_json"}:
                    continue
            elif kind_q == "csv":
                if curve.source_kind not in {"sweep_csv", "characterization_csv"}:
                    continue
            elif curve.source_kind != kind_q:
                continue

        if sample_q:
            hay_sample = f"{curve.sample_id} {curve.sample_name} {curve.coordinate_label}".casefold()
            if sample_q not in hay_sample:
                continue

        if criteria.keithley_b_target_a is not None:
            kb_val = (
                curve.keithley_b_setpoint_a
                if curve.keithley_b_setpoint_a is not None
                else curve.keithley_b_measured_a
            )
            if kb_val is None or abs(kb_val - criteria.keithley_b_target_a) > abs(criteria.keithley_b_tol_a) + 1e-12:
                continue

        if criteria.moke_vout_target_v is not None and (
            curve.moke_vout_v is None
            or abs(curve.moke_vout_v - criteria.moke_vout_target_v)
            > abs(criteria.moke_vout_tol_v) + 1e-12
        ):
            continue

        if text_q:
            haystack = " ".join(
                (
                    curve.title,
                    curve.sample_id,
                    curve.sample_name,
                    curve.coordinate_label,
                    curve.source_kind,
                    str(curve.source_path.name) if curve.source_path else "",
                )
            ).casefold()
            if not all(token in haystack for token in text_q.split()):
                continue

        matched.append(curve)

    return tuple(matched)


class ReferenceLibraryScanner:
    """Read-only scanner for recorded HDF5 runs and characterization datasets with mtime caching."""

    def __init__(self, *, max_cache_entries: int = _CACHE_MAX_ENTRIES) -> None:
        self._max_cache_entries = max(16, max_cache_entries)
        self._cache: OrderedDict[tuple[Path, int, int, str], tuple[ReferenceIVCurve, ...]] = OrderedDict()

    def clear_cache(self) -> None:
        self._cache.clear()

    def _cache_get(self, key: tuple[Path, int, int, str]) -> tuple[ReferenceIVCurve, ...] | None:
        value = self._cache.get(key)
        if value is not None:
            self._cache.move_to_end(key)
        return value

    def _cache_put(self, key: tuple[Path, int, int, str], value: tuple[ReferenceIVCurve, ...]) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_cache_entries:
            self._cache.popitem(last=False)

    def scan(
        self,
        *,
        roots: Sequence[str | Path] = (),
        inventory_store: InventoryStore | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> LibraryScanResult:
        """Index reference curves across inventory runs and directory roots without touching hardware."""
        is_cancelled = cancelled or (lambda: False)
        run_records_by_path: dict[Path, SampleRunRecord] = {}
        candidate_paths: OrderedDict[Path, str] = OrderedDict()

        if inventory_store is not None:
            for sample in inventory_store.list_samples():
                if is_cancelled():
                    raise InterruptedError("Reference library scan cancelled.")
                for run in inventory_store.list_runs_for_sample(sample.sample_id):
                    for raw_path in (run.run_path, run.csv_path):
                        if not raw_path:
                            continue
                        resolved = Path(raw_path).expanduser().resolve()
                        if not resolved.is_file():
                            continue
                        run_records_by_path.setdefault(resolved, run)
                        suffix = resolved.suffix.lower()
                        if suffix in {".h5", ".hdf5"}:
                            candidate_paths.setdefault(resolved, "hdf5")
                        elif resolved.name.lower() == "dataset.json":
                            candidate_paths.setdefault(resolved, "characterization_json")
                        elif suffix == ".csv":
                            candidate_paths.setdefault(resolved, "csv")

        for raw_root in roots:
            root = Path(raw_root).expanduser().resolve()
            if not root.is_dir():
                continue
            for summary in Hdf5RunReader.list_runs(root, recursive=True, cancelled=is_cancelled):
                resolved = summary.path.resolve()
                candidate_paths.setdefault(resolved, "hdf5")
            for json_path in root.rglob("dataset.json"):
                if is_cancelled():
                    raise InterruptedError("Reference library scan cancelled.")
                if json_path.is_file():
                    candidate_paths.setdefault(json_path.resolve(), "characterization_json")
            for csv_path in root.rglob("characterization.csv"):
                if is_cancelled():
                    raise InterruptedError("Reference library scan cancelled.")
                if csv_path.is_file():
                    sibling_json = (csv_path.parent / "dataset.json").resolve()
                    if sibling_json not in candidate_paths:
                        candidate_paths.setdefault(csv_path.resolve(), "csv")

        curves: list[ReferenceIVCurve] = []
        skipped: list[tuple[Path, str]] = []

        for file_path, kind in candidate_paths.items():
            if is_cancelled():
                raise InterruptedError("Reference library scan cancelled.")
            try:
                stat = file_path.stat()
            except OSError as exc:
                skipped.append((file_path, str(exc)))
                continue

            record = run_records_by_path.get(file_path)
            record_tag = (
                f"{record.sample_id}:{record.row}:{record.col}:{record.recipe_name}"
                if record is not None
                else ""
            )
            cache_key = (file_path, stat.st_mtime_ns, stat.st_size, record_tag)
            cached = self._cache_get(cache_key)
            if cached is not None:
                curves.extend(cached)
                continue

            try:
                if kind == "hdf5":
                    loaded = load_hdf5_reference_curves(file_path, run_record=record)
                elif kind == "characterization_json":
                    loaded = (load_characterization_json_curve(file_path, run_record=record),)
                else:
                    loaded = load_csv_reference_curves(file_path)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                skipped.append((file_path, str(exc)))
                continue

            self._cache_put(cache_key, loaded)
            curves.extend(loaded)

        curves.sort(key=lambda c: (c.created_at_utc or "", c.title.casefold()), reverse=True)
        return LibraryScanResult(curves=tuple(curves), skipped_files=tuple(skipped))
