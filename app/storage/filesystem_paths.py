"""Keep portable file references while addressing long paths on Windows."""

import os
from pathlib import Path


def native_path(path: str | Path, *, reserved: int = 0) -> Path:
    value = Path(path)
    if os.name != "nt":
        return value
    absolute = os.path.abspath(value)
    if len(absolute.encode("utf-16-le")) // 2 + reserved < 248 or absolute.startswith("\\\\?\\"):
        return value
    if absolute.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{absolute[2:]}")
    return Path(f"\\\\?\\{absolute}")
