"""Automated and sanitized file naming for measurement runs, reports, and exports."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
import re


def sanitize_run_file_stem(raw_name: object, *, fallback: str = "run") -> str:
    """Convert a user- or recipe-provided name into a safe file stem."""

    candidate = Path(str(raw_name or "").strip()).name
    if candidate.lower().endswith((".h5", ".hdf5", ".csv", ".pdf")):
        candidate = Path(candidate).stem
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate).strip("_")
    return safe_name or fallback


def automated_run_file_stem(
    recipe_name: str,
    *,
    sample_target: object | None = None,
    file_stem_override: str | None = None,
    pattern: str | None = None,
) -> str:
    """Convert user input, recipe name, and sample target into an automated file stem.

    If file_stem_override is provided without format placeholders, it is used directly.
    If format placeholders (e.g. {sample_id}, {coord}, {recipe}) are present in
    file_stem_override or pattern, they are substituted using the active sample target and recipe.
    Otherwise, when an active sample target is present, builds an automated stem:
        {sample_id}_{coord}_{device}_{recipe}
    """
    clean_recipe = sanitize_run_file_stem(recipe_name, fallback="run")

    s_id = ""
    s_name = ""
    row = ""
    col = ""
    dev_label = ""
    row_label = ""
    col_label = ""

    if sample_target is not None:
        if hasattr(sample_target, "is_active") and getattr(sample_target, "is_active", False):
            s_id = str(getattr(sample_target, "sample_id", "") or "")
            s_name = str(getattr(sample_target, "sample_name", "") or s_id)
            row = str(getattr(sample_target, "row", "") or "")
            col = str(getattr(sample_target, "col", "") or "")
            dev_label = str(getattr(sample_target, "device_label", "") or "")
            row_label = str(getattr(sample_target, "row_label", "") or "")
            col_label = str(getattr(sample_target, "col_label", "") or "")
        elif isinstance(sample_target, Mapping) and sample_target.get("sample_id"):
            s_id = str(sample_target.get("sample_id") or "")
            s_name = str(sample_target.get("sample_name") or s_id)
            row = str(sample_target.get("row") or "")
            col = str(sample_target.get("col") or "")
            dev_label = str(sample_target.get("device_label") or "")
            row_label = str(sample_target.get("row_label") or "")
            col_label = str(sample_target.get("col_label") or "")

    coord = ""
    if row and col:
        coord = f"R{row}C{col}"
    elif row:
        coord = f"R{row}"
    elif col:
        coord = f"C{col}"

    clean_s_id = sanitize_run_file_stem(s_id, fallback="")
    clean_s_name = sanitize_run_file_stem(s_name, fallback="")
    clean_dev = sanitize_run_file_stem(dev_label, fallback="")
    clean_row_label = sanitize_run_file_stem(row_label, fallback="")
    clean_col_label = sanitize_run_file_stem(col_label, fallback="")

    now = datetime.now(timezone.utc)
    tokens = {
        "timestamp": now.strftime("%Y%m%dT%H%M%S"),
        "date": now.strftime("%Y%m%d"),
        "time": now.strftime("%H%M%S"),
        "sample_id": clean_s_id,
        "sample_name": clean_s_name,
        "coord": coord,
        "sample_coord": coord,
        "row": row,
        "col": col,
        "device": clean_dev or coord,
        "device_label": clean_dev,
        "row_label": clean_row_label,
        "col_label": clean_col_label,
        "recipe": clean_recipe,
        "recipe_name": clean_recipe,
    }

    override = str(file_stem_override or "").strip()
    if override:
        if "{" in override and "}" in override:
            try:
                formatted = override.format_map(tokens).strip()
                cleaned = re.sub(r"_+", "_", formatted).strip("_")
                return sanitize_run_file_stem(cleaned, fallback=clean_recipe)
            except Exception:
                pass
        return sanitize_run_file_stem(override, fallback=clean_recipe)

    if pattern and "{" in pattern and "}" in pattern:
        try:
            formatted = pattern.format_map(tokens).strip()
            cleaned = re.sub(r"_+", "_", formatted).strip("_")
            return sanitize_run_file_stem(cleaned, fallback=clean_recipe)
        except Exception:
            pass

    if clean_s_id:
        parts: list[str] = [clean_s_id]
        if coord:
            parts.append(coord)
        if clean_dev and clean_dev != coord and clean_dev not in parts:
            parts.append(clean_dev)
        if clean_recipe and clean_recipe.lower() != "run" and clean_recipe not in parts:
            parts.append(clean_recipe)
        return "_".join(parts)

    return clean_recipe

