"""Platform-standard filesystem path resolvers for PyLab (REL-02)."""

from __future__ import annotations

import os
from pathlib import Path
import sys

_APP_NAME = "PyLab"
_ORGANIZATION = "MTJLAB"


def _qstandardpaths_location(location_name: str) -> Path | None:
    """Attempt resolution via PySide6.QtCore.QStandardPaths without failing when Qt is headless/uninitialized."""
    try:
        from PySide6.QtCore import QStandardPaths

        location_enum = getattr(QStandardPaths.StandardLocation, location_name, None)
        if location_enum is not None:
            raw = QStandardPaths.writableLocation(location_enum)
            if raw:
                return Path(raw)
    except Exception:
        pass
    return None


def app_config_dir() -> Path:
    r"""Return the writable application configuration directory (per-user).

    On Windows: %APPDATA%\PyLab
    On Linux/macOS: ~/.config/pylab or QStandardPaths AppConfigLocation
    """
    qt_path = _qstandardpaths_location("AppConfigLocation")
    if qt_path is not None:
        return qt_path

    if sys.platform == "win32":
        app_data = os.environ.get("APPDATA")
        if app_data:
            return Path(app_data) / _APP_NAME
        return Path.home() / "AppData" / "Roaming" / _APP_NAME
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / _APP_NAME.lower()
    return Path.home() / ".config" / _APP_NAME.lower()


def app_data_dir() -> Path:
    """Return the writable application data directory."""
    qt_path = _qstandardpaths_location("AppDataLocation")
    if qt_path is not None:
        return qt_path

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / _APP_NAME
        return Path.home() / "AppData" / "Local" / _APP_NAME
    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data) / _APP_NAME.lower()
    return Path.home() / ".local" / "share" / _APP_NAME.lower()


def default_settings_path(cli_override: str | Path | None = None) -> Path:
    """Resolve the default settings.yml path.

    1. If explicit CLI path is provided, use it.
    2. If a local checkout config exists (.config/settings.yml in CWD), use it to preserve repo workflow.
    3. Otherwise, return the standard per-user config location.
    """
    if cli_override is not None:
        return Path(cli_override).expanduser()

    repo_config = Path.cwd() / ".config" / "settings.yml"
    if repo_config.is_file():
        return repo_config

    return app_config_dir() / "settings.yml"


def default_measurements_dir() -> Path:
    """Resolve the default measurements directory.

    1. If a local './measurements' directory exists in CWD, use it.
    2. Otherwise, use standard user Documents/PyLab/measurements.
    """
    cwd_measurements = Path.cwd() / "measurements"
    if cwd_measurements.is_dir():
        return cwd_measurements

    docs = _qstandardpaths_location("DocumentsLocation")
    if docs is None:
        docs = Path.home() / "Documents"
    return docs / _APP_NAME / "measurements"


def resolve_platform_env_path(path: str | Path = ".env") -> Path:
    """Resolve the .env credential file securely and consistently.

    1. If target is absolute, return it.
    2. If explicit relative path exists in CWD, use it.
    3. If .env exists in the user app configuration directory, use it.
    4. If .env exists in repository root, use it.
    5. Fallback to app_config_dir() / target.
    """
    target = Path(path).expanduser()
    if target.is_absolute():
        return target

    # Local CWD override (for development / portable run)
    cwd_candidate = (Path.cwd() / target).resolve()
    if cwd_candidate.is_file():
        return cwd_candidate

    # User AppConfig location
    config_candidate = (app_config_dir() / target).resolve()
    if config_candidate.is_file():
        return config_candidate

    # Repository root fallback (for tests and dev)
    repo_candidate = (Path(__file__).resolve().parents[2] / target).resolve()
    if repo_candidate.is_file():
        return repo_candidate

    return config_candidate
