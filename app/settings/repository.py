"""Safe loading and atomic persistence for the station settings file."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from app.domain.errors import ConfigurationError
from app.settings.models import StationSettings


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

    def load(self) -> LoadedSettings:
        if not self.path.is_file():
            raise ConfigurationError(f"Brak pliku konfiguracji: {self.path}")
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                raw = self._yaml.load(stream)
        except Exception as exc:
            raise ConfigurationError(f"Nie można odczytać YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("Główny element settings.yml musi być mapą.")
        try:
            settings = StationSettings.model_validate(raw)
        except ValidationError as exc:
            raise ConfigurationError(f"Nieprawidłowy settings.yml:\n{exc}") from exc
        return LoadedSettings(settings=settings, raw=raw, source=self.path)

    def save(self, settings: StationSettings) -> None:
        """Atomically replace the profile and retain one last-known-good backup."""

        self._atomic_dump(settings.model_dump(mode="python"))

    def save_raw(self, raw: dict[str, Any]) -> StationSettings:
        """Validate then atomically persist a UI-edited round-trip YAML document."""

        try:
            settings = StationSettings.model_validate(raw)
        except ValidationError as exc:
            raise ConfigurationError(f"Nieprawidłowy settings.yml:\n{exc}") from exc
        self._atomic_dump(raw)
        return settings

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
            os.replace(temp_path, self.path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
