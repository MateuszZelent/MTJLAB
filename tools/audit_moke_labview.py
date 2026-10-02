"""Read-only evidence inventory for the MOKE LabVIEW migration plan.

No instrument connection, VI execution, firmware command or calibration write.
RSRC/zlib scans expose strings/constants, not a decoded LabVIEW wiring graph.
Run with the project venv (numpy) and an explicitly selected source directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

import numpy as np

CONSTANTS = (0.025, 0.05, 0.7, 0.8, 0.9, 1.6, 6.0, 6.66, 3276.7, 3276.8, 32767.0, 32768.0)
TERMS = (
    "float32", "step=", "Next=", "step >=", "step <=", "Soll", "Wait for Hall",
    "Direct", "Wait Time", "Voltage Limit", "Interpolation Array", "Coil Factor",
    "Read Delimited Spreadsheet.vi", "Linear Fit.vi", "field_calibration.mcal",
    "Field Control Channel", "Voltage Channel", "Set Mode", "TCP", "VISA",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def chunk_evidence(data: bytes, base_offset: int = 0) -> list[dict]:
    chunks = []
    for match in re.finditer(rb"\x78[\x01\x5e\x9c\xda]", data):
        decoder = zlib.decompressobj()
        try:
            expanded = decoder.decompress(data[match.start():], 8_000_000)
        except zlib.error:
            continue
        if not decoder.eof:
            continue
        strings = [item.decode("latin1") for item in re.findall(rb"[ -~]{5,}", expanded)]
        references = sorted(set(re.findall(r"[A-Za-z0-9_ -]+\.vi", "\n".join(strings))))
        constants = {}
        for value in CONSTANTS:
            offsets = [m.start() for m in re.finditer(re.escape(struct.pack(">d", value)), expanded)]
            if offsets:
                constants[str(value)] = offsets
        excerpts = [s[:400] for s in strings if any(term in s for term in TERMS)]
        if references or constants or excerpts:
            chunks.append({
                "zlib_file_offset_hex": hex(base_offset + match.start()),
                "expanded_size": len(expanded), "expanded_sha256": digest(expanded),
                "double_be_occurrences": constants, "strings": excerpts,
                "vi_name_strings_not_proven_calls": references,
            })
    return chunks


def llb_evidence(data: bytes) -> list[dict]:
    result = []
    for match in re.finditer(rb"RSRC\r\n\x00\x03LVIN", data):
        start = match.start()
        # Each resource header occurs twice; the data header begins with LVSR.
        if data[start + 32:start + 36] != b"\x00\x00\x00\x90":
            continue
        info_offset, info_size, _, _ = struct.unpack_from(">IIII", data, start + 16)
        end = start + info_offset + info_size
        if end > len(data):
            raise ValueError("Embedded VI exceeds library bounds")
        segment = data[start:end]
        names = re.findall(rb"[A-Za-z0-9_ -]+\.vi", segment)
        result.append({
            "name_hint": names[-1].decode("ascii").strip() if names else None,
            "file_offset_hex": hex(start), "size": len(segment), "sha256": digest(segment),
            "chunks": chunk_evidence(segment[:info_offset], start),
        })
    return result


def mcal_evidence(data: bytes) -> dict:
    arrays = {}
    position = 0
    for name in ("hall_coefficients", "interpolation_x", "interpolation_y", "control_coefficients"):
        if position + 4 > len(data):
            raise ValueError("Truncated MCAL count")
        count = struct.unpack_from(">I", data, position)[0]
        position += 4
        if count > 100_000 or position + 8 * count > len(data):
            raise ValueError("Invalid MCAL array length")
        arrays[name] = list(struct.unpack_from(f">{count}d", data, position))
        position += 8 * count
        if not all(np.isfinite(arrays[name])):
            raise ValueError("Non-finite MCAL value")
    if position != len(data):
        raise ValueError("Trailing MCAL data")
    if len(arrays["hall_coefficients"]) != 4 or len(arrays["control_coefficients"]) != 4:
        raise ValueError("Expected cubic coefficients")
    x, y = arrays["interpolation_x"], arrays["interpolation_y"]
    if len(x) != len(y):
        raise ValueError("Mismatched MCAL interpolation lengths")
    differences = np.diff(x)
    arrays["interpolation_summary"] = {
        "count": len(x), "x_min": min(x), "x_max": max(x),
        "y_min": min(y), "y_max": max(y),
        "strictly_increasing": bool(np.all(differences > 0)),
        "strictly_decreasing": bool(np.all(differences < 0)),
        "duplicate_x_count": len(x) - len(set(x)),
        "note": "MCAL stores no unit, branch, device identity, approval or validity metadata",
    }
    return arrays


def table_evidence(path: Path) -> dict:
    array = np.loadtxt(path)
    if array.ndim != 2 or array.shape[1] != 2 or not np.isfinite(array).all():
        raise ValueError(f"Not a finite two-column export: {path}")
    return {
        "rows": len(array), "column_min": array.min(axis=0).tolist(),
        "column_max": array.max(axis=0).tolist(),
        "first_two": array[:2].tolist(), "last_two": array[-2:].tolist(),
        "strictly_increasing_x": bool(np.all(np.diff(array[:, 0]) > 0)),
        "strictly_decreasing_x": bool(np.all(np.diff(array[:, 0]) < 0)),
    }


def paired_fit_evidence(hall_path: Path, field_path: Path) -> dict:
    hall, field = np.loadtxt(hall_path), np.loadtxt(field_path)
    if hall.shape != field.shape or not np.array_equal(hall[:, 0], field[:, 0]):
        raise ValueError("Calibration exports have different voltage grids")
    result = {"rows": len(hall), "voltage_grids_identical": True}
    for name, x, y in (
        ("b_from_control_voltage", field[:, 0], field[:, 1]),
        ("b_from_hall_voltage", hall[:, 1], field[:, 1]),
        ("control_voltage_from_b", field[:, 1], field[:, 0]),
    ):
        coefficients = np.polynomial.polynomial.polyfit(x, y, 3)
        residual = np.polynomial.polynomial.polyval(x, coefficients) - y
        result[name] = {
            "coefficients_ascending": coefficients.tolist(),
            "rmse_output_units": float(np.sqrt(np.mean(residual ** 2))),
            "max_abs_residual_output_units": float(np.max(np.abs(residual))),
            "status": "exploratory fit of both branches; not an approved calibration",
        }
    midpoint = len(field) // 2
    forward, backward = field[:midpoint], field[midpoint:][::-1]
    if forward.shape == backward.shape and np.array_equal(forward[:, 0], backward[:, 0]):
        result["max_branch_difference_t"] = float(np.max(np.abs(forward[:, 1] - backward[:, 1])))
    return result


def audit(root: Path) -> dict:
    paths = []
    for directory in ("MOKE-Box_in_progress/project", "build", "build_2021-12", "Field calibration"):
        paths.extend(sorted((root / directory).rglob("*")))
    result = {"source_root": str(root), "method": "read-only static inventory; no VI execution or wiring decompilation", "files": {}}
    for path in paths:
        if not path.is_file() or path.suffix.lower() not in {
            ".lvproj", ".llb", ".vi", ".zip", ".mcal", ".txt", ".tha", ".exe", ".ini", ".aliases"
        }:
            continue
        data = path.read_bytes()
        entry = {"size": len(data), "sha256": digest(data)}
        result["files"][path.relative_to(root).as_posix()] = entry
        if path.suffix == ".mcal":
            entry["decoded"] = mcal_evidence(data)
        elif path.suffix == ".lvproj":
            project = ET.fromstring(data)
            entry["labview_version_attribute"] = project.attrib.get("LVVersion")
            entry["local_item_references"] = []
            for item in project.iter("Item"):
                url = item.attrib.get("URL", "")
                if not url or url.startswith("/"):
                    continue
                # Library/member references resolve the outer LLB as a file.
                container = url.split(".llb/", 1)[0]
                if ".llb/" in url:
                    container += ".llb"
                resolved = (path.parent / container).resolve()
                entry["local_item_references"].append({
                    "name": item.attrib.get("Name"), "url": url,
                    "resolved_container": str(resolved), "exists": resolved.is_file(),
                })
        elif path.suffix == ".llb":
            entry["embedded_vis"] = llb_evidence(data)
        elif path.suffix == ".vi":
            entry["chunks"] = chunk_evidence(data)
        elif path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                entry["members"] = [
                    {"name": member.filename, "size": member.file_size,
                     "sha256": digest(archive.read(member)),
                     "chunks": chunk_evidence(archive.read(member))
                     if member.filename.endswith(".vi") else []}
                    for member in archive.infolist() if not member.is_dir()
                ]
        elif path.suffix == ".txt" and path.stem not in {"20190527", "20190712"}:
            entry["table"] = table_evidence(path)
        elif path.suffix == ".tha":
            entry["is_hdf5_signature"] = data.startswith(b"\x89HDF\r\n\x1a\n")
            entry["raw_control_occurrences"] = [
                {"label": label.decode(), "offset": m.start(),
                 "following_bytes_hex": data[m.end():m.end() + 32].hex(" ")}
                for label in (b"Field Control Channel", b"Voltage Channel", b"Gain Hall", b"Set Mode", b"Wait Time")
                for m in re.finditer(re.escape(label), data)
            ]
        elif path.suffix == ".exe":
            entry["mz_signature"] = data.startswith(b"MZ")
            entry["rsrc_offsets"] = [m.start() for m in re.finditer(rb"RSRC\r\n", data)]
            entry["note"] = "Packaged compiled LVAR; no source/wiring equivalence established"
    result["paired_exports"] = {}
    for date, field_name, hall_name in (
        ("20190527", "LS455.txt", "moke.txt"),
        ("20190712", "Lakeshore.txt", "moke.txt"),
        ("20230427", "LS_over_CV.txt", "HV_over_CL.txt"),
    ):
        folder = root / "Field calibration" / date
        result["paired_exports"][date] = paired_fit_evidence(folder / hall_name, folder / field_name)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    evidence = audit(arguments.source.resolve(strict=True))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Inventoried {len(evidence['files'])} files; saved {arguments.output}")
