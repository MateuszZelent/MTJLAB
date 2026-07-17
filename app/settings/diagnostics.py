"""Configuration diagnostics, structural diffing and safe support export."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any


_SECRET_MARKERS = ("password", "secret", "token", "credential", "private_key", "api_key")


def redacted_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy with credential-bearing values removed."""

    def visit(value: Any, key: str = "") -> Any:
        if any(marker in key.lower() for marker in _SECRET_MARKERS):
            return "<redacted>"
        if isinstance(value, dict):
            return {str(nested_key): visit(nested, str(nested_key)) for nested_key, nested in value.items()}
        if isinstance(value, list):
            return [visit(nested) for nested in value]
        return deepcopy(value)

    return visit(raw)


def configuration_sha256(raw: dict[str, Any]) -> str:
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def structural_diff(before: Any, after: Any) -> tuple[str, ...]:
    """Return stable human-readable leaf changes without dumping whole files."""

    changes: list[str] = []

    def walk(left: Any, right: Any, path: tuple[str, ...]) -> None:
        location = ".".join(path) or "<root>"
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right), key=str):
                nested_path = path + (str(key),)
                if key not in left:
                    changes.append(f"+ {'.'.join(nested_path)} = {right[key]!r}")
                elif key not in right:
                    changes.append(f"- {'.'.join(nested_path)} = {left[key]!r}")
                else:
                    walk(left[key], right[key], nested_path)
            return
        if isinstance(left, list) and isinstance(right, list):
            common = min(len(left), len(right))
            for index in range(common):
                walk(left[index], right[index], path + (f"[{index}]",))
            for index in range(common, len(left)):
                changes.append(f"- {location}[{index}] = {left[index]!r}")
            for index in range(common, len(right)):
                changes.append(f"+ {location}[{index}] = {right[index]!r}")
            return
        if left != right:
            changes.append(f"~ {location}: {left!r} -> {right!r}")

    walk(before, after, ())
    return tuple(changes)


def configuration_diagnostics(path: Path, raw: dict[str, Any]) -> tuple[str, ...]:
    backup = path.with_suffix(path.suffix + ".bak")
    return (
        f"Settings file: {path.resolve()}",
        f"Settings SHA-256: {configuration_sha256(raw)}",
        f"Backup: {backup.resolve()} ({'available' if backup.is_file() else 'not created yet'})",
        f"File size: {path.stat().st_size if path.is_file() else 0} bytes",
        "Support export: credentials are redacted; VISA resources and safety limits remain visible.",
    )
