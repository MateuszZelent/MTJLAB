"""Run-specific report names, with read compatibility for older fixed names."""

from pathlib import Path
from app.storage.naming import sanitize_run_file_stem


def report_path(directory: Path, *, field_curve: bool = False, summary: bool = False,
                existing: bool = False) -> Path:
    directory = Path(directory)
    prefix = "field_series_report" if summary else "characterization_report"
    identity = f"{directory.parent.name}_{directory.name}" if field_curve else directory.name
    identity = sanitize_run_file_stem(identity, fallback="measurement")
    target = directory / f"{prefix}_{identity}.pdf"
    legacy = directory / f"{prefix}.pdf"
    if existing and not target.is_file() and legacy.is_file():
        return legacy
    return target


def create_run_directory(proposed: Path) -> Path:
    """Reserve a fresh directory without ever reusing another acquisition."""
    proposed = Path(proposed)
    candidate = proposed
    index = 2
    while True:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            candidate = proposed.with_name(f"{proposed.name}_{index:03d}")
            index += 1
