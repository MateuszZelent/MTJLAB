"""Central release version, application metadata, and build provenance."""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import subprocess

__version__ = "0.1.0"
APP_NAME = "MTJLAB"
APP_DISPLAY_NAME = "MTJLAB Station"
APP_DESCRIPTION = "Safe local control software for a Rigol, Keithley and Anritsu measurement station."


@lru_cache(maxsize=1)
def get_git_commit() -> str:
    """Return the current git commit short hash or build identifier."""
    build_meta = Path(__file__).parent / "_build_metadata.json"
    if build_meta.is_file():
        try:
            data = json.loads(build_meta.read_text(encoding="utf-8"))
            commit = str(data.get("commit", "")).strip()
            if commit:
                return commit
        except Exception:
            pass

    try:
        commit = (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=Path(__file__).parent,
                stderr=subprocess.DEVNULL,
                timeout=2,
                text=True,
            )
            .strip()
        )
        if commit:
            return commit
    except Exception:
        pass

    return "source"


def get_full_version() -> str:
    """Return version with build/commit suffix, e.g. '0.1.0+2b33fe6'."""
    commit = get_git_commit()
    return f"{__version__}+{commit}" if commit else __version__


def get_version_info() -> dict[str, str]:
    """Return structured version dictionary for logging, telemetry and UI about."""
    return {
        "app_name": APP_NAME,
        "version": __version__,
        "commit": get_git_commit(),
        "full_version": get_full_version(),
    }
