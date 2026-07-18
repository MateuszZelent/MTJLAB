"""Verify that the active Python environment matches an exact requirements lock."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path
import platform
import re

from packaging.utils import canonicalize_name


_PYTHON_PIN = re.compile(r"^#\s*python==([^\s]+)\s*$", re.IGNORECASE)
_PACKAGE_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;]+)$")
_INCLUDE = re.compile(r"^(?:-r|--requirement)\s+(.+)$")


def parse_lock(path: Path) -> tuple[str, dict[str, str]]:
    """Parse exact pins, recursively resolving relative ``-r`` includes."""

    packages: dict[str, str] = {}
    python_versions: set[str] = set()
    visited: set[Path] = set()

    def read(current: Path) -> None:
        resolved = current.resolve()
        if resolved in visited:
            return
        visited.add(resolved)
        for line_number, raw_line in enumerate(
            resolved.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw_line.strip()
            if not line:
                continue
            python_match = _PYTHON_PIN.fullmatch(line)
            if python_match:
                python_versions.add(python_match.group(1))
                continue
            if line.startswith("#"):
                continue
            include_match = _INCLUDE.fullmatch(line)
            if include_match:
                read(resolved.parent / include_match.group(1).strip())
                continue
            package_match = _PACKAGE_PIN.fullmatch(line)
            if not package_match:
                raise ValueError(
                    f"{resolved}:{line_number}: lock entries must be exact name==version pins"
                )
            name = canonicalize_name(package_match.group(1))
            package_version = package_match.group(2)
            previous = packages.get(name)
            if previous is not None and previous != package_version:
                raise ValueError(
                    f"{resolved}:{line_number}: conflicting pins for {name}: "
                    f"{previous} and {package_version}"
                )
            packages[name] = package_version

    read(path)
    if len(python_versions) != 1:
        raise ValueError(
            f"{path}: lock must resolve exactly one '# python==version' marker"
        )
    return next(iter(python_versions)), packages


def check_environment(
    lock_path: Path, python_version: str | None = None
) -> tuple[str, ...]:
    """Return every Python or installed-package mismatch in deterministic order."""

    locked_python, packages = parse_lock(lock_path)
    active_python = python_version or platform.python_version()
    mismatches: list[str] = []
    if active_python != locked_python:
        mismatches.append(f"Python {active_python} != locked {locked_python}")
    for name, locked_version in sorted(packages.items()):
        try:
            installed_version = metadata.version(name)
        except metadata.PackageNotFoundError:
            mismatches.append(f"{name} is not installed (locked {locked_version})")
            continue
        if installed_version != locked_version:
            mismatches.append(
                f"{name} {installed_version} != locked {locked_version}"
            )
    return tuple(mismatches)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    args = parser.parse_args()
    mismatches = check_environment(args.lock)
    if mismatches:
        for mismatch in mismatches:
            print(mismatch)
        return 1
    print(f"Environment matches {args.lock}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
