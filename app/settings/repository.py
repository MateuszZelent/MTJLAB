"""Safe loading and atomic persistence for the station settings file."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
from functools import wraps
import os
from pathlib import Path
import tempfile
from threading import RLock
import time
from typing import Any, Callable

from pydantic import ValidationError
from ruamel.yaml import YAML

from app.domain.errors import ConfigurationError
from app.settings.models import StationSettings


_SETTINGS_IO_LOCK = RLock()


def _serialized_settings_io(method: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize settings transactions across GUI and background writers."""

    @wraps(method)
    def locked(*args: Any, **kwargs: Any) -> Any:
        with _SETTINGS_IO_LOCK:
            return method(*args, **kwargs)

    return locked


@dataclass(slots=True)
class LoadedSettings:
    settings: StationSettings
    raw: dict[str, Any]
    source: Path


class SettingsRepository:
    """Read/write station configuration without silently accepting invalid YAML."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._yaml = YAML()
        self._yaml.preserve_quotes = True
        self._yaml.default_flow_style = False

    @_serialized_settings_io
    def load(self) -> LoadedSettings:
        self.ensure_exists()
        if not self.path.is_file():
            raise ConfigurationError(f"Configuration file is missing: {self.path}")
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                raw = self._yaml.load(stream)
        except Exception as exc:
            raise ConfigurationError(f"Cannot read YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("The root settings.yml element must be a mapping.")
        # Upgrade every older profile from the packaged production template.
        # The recursive merge only fills absent keys: station-specific values,
        # comments and ordering remain intact, while new devices and newly
        # introduced nested fields arrive without per-device migration code.
        defaults = self._template_raw()
        migrated = self._merge_missing_defaults(raw, defaults)
        repaired = self.repair_known_issues(raw)
        try:
            settings = StationSettings.model_validate(raw)
        except ValidationError as exc:
            raise ConfigurationError(f"Invalid settings.yml:\n{exc}") from exc
        if migrated or repaired:
            # Persist only after the merged document validates.  This is a
            # schema upgrade, not an operator configuration change, so existing
            # approval metadata is preserved. _atomic_dump also keeps a .bak.
            self._atomic_dump(raw)
        return LoadedSettings(settings=settings, raw=raw, source=self.path)

    def _template_raw(self) -> dict[str, Any]:
        try:
            text = files("app").joinpath("resources/settings.template.yml").read_text(
                encoding="utf-8"
            )
            defaults = self._yaml.load(text)
        except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
            raise ConfigurationError("The packaged settings template is missing.") from exc
        if not isinstance(defaults, dict):
            raise ConfigurationError("The packaged settings template root must be a mapping.")
        return defaults

    @classmethod
    def _merge_missing_defaults(
        cls,
        target: dict[str, Any],
        defaults: dict[str, Any],
    ) -> bool:
        """Recursively add absent schema defaults without replacing user data."""

        changed = False
        for key, default in defaults.items():
            if key not in target:
                target[key] = deepcopy(default)
                changed = True
                continue
            current = target[key]
            if isinstance(current, dict) and isinstance(default, dict):
                changed = cls._merge_missing_defaults(current, default) or changed
        return changed

    @_serialized_settings_io
    def ensure_exists(self) -> bool:
        """Create an unverified local profile from the packaged template once."""

        if self.path.exists():
            return False
        try:
            template = files("app").joinpath("resources/settings.template.yml").read_bytes()
        except (FileNotFoundError, ModuleNotFoundError) as exc:
            raise ConfigurationError("The packaged settings template is missing.") from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(template)
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.exists():
                return False
            self._replace_with_windows_retry(temp_path, self.path)
            return True
        finally:
            temp_path.unlink(missing_ok=True)

    def save(self, settings: StationSettings) -> None:
        """Persist a validated station configuration."""

        self.save_raw(settings.model_dump(mode="python"))

    @_serialized_settings_io
    def save_raw(self, raw: dict[str, Any]) -> StationSettings:
        """Validate then atomically persist a UI-edited round-trip YAML document."""

        payload = deepcopy(raw)
        self.repair_known_issues(payload)
        try:
            settings = StationSettings.model_validate(payload)
        except ValidationError as exc:
            raise ConfigurationError(f"Invalid settings.yml:\n{exc}") from exc
        self._atomic_dump(payload)
        return settings

    @_serialized_settings_io
    def update_raw(
        self,
        transform: Callable[[dict[str, Any], StationSettings], dict[str, Any]],
    ) -> tuple[StationSettings, dict[str, Any]]:
        """Atomically load, transform, validate and save the latest document."""

        loaded = self.load()
        payload = transform(deepcopy(loaded.raw), loaded.settings)
        persisted = self.save_raw(payload)
        return persisted, payload

    @staticmethod
    def repair_known_issues(raw: dict[str, Any]) -> bool:
        """Repair deterministic legacy/contradictory values without inventing limits.

        An empty expected-input declaration cannot coexist with a request to
        require that declaration.  Disabling the requirement is the schema's
        explicit opt-out; it does not create a fictitious RF power limit and
        does not enable acquisition by itself.
        """

        try:
            safety = raw["devices"]["anritsu"]["safety"]
        except (KeyError, TypeError):
            return False
        if not isinstance(safety, dict):
            return False

        changed = False
        rf_input = safety.get("rf_input")
        if (
            isinstance(rf_input, dict)
            and safety.get("require_rf_input_limit_definition") is True
            and rf_input.get("max_expected_power_at_connector") in (None, "", "null")
        ):
            safety["require_rf_input_limit_definition"] = False
            changed = True

        documented_reference = {"min": "-120 dBm", "max": "+50 dBm"}
        if safety.get("reference_level") != documented_reference:
            safety["reference_level"] = documented_reference
            changed = True
        return changed

    def _atomic_dump(self, payload: dict[str, Any]) -> None:
        """Replace the file only after a complete temporary YAML write succeeds."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent, text=True
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                self._yaml.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.exists():
                backup = self.path.with_suffix(self.path.suffix + ".bak")
                backup.write_bytes(self.path.read_bytes())
            self._replace_with_windows_retry(temp_path, self.path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _replace_with_windows_retry(source: Path, destination: Path) -> None:
        """Atomically replace a profile despite short-lived Windows file locks."""

        for attempt in range(6):
            try:
                os.replace(source, destination)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
