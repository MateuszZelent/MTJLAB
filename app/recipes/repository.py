"""Atomic recipe persistence with immutable history and edit recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import uuid

from app.recipes.models import Recipe, parse_recipe_text


@dataclass(frozen=True, slots=True)
class SavedRecipe:
    path: Path
    sha256: str
    backup_path: Path | None


class RecipeRepository:
    """Persist operator YAML without silently overwriting its previous version."""

    def load(self, path: str | Path) -> Recipe:
        target = Path(path)
        return parse_recipe_text(target.read_text(encoding="utf-8"), origin=str(target))

    def save(self, path: str | Path, source: str) -> SavedRecipe:
        target = Path(path)
        parse_recipe_text(source, origin=str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None
        if target.exists():
            previous = target.read_text(encoding="utf-8")
            if previous != source:
                backup = self._history_path(target, previous)
                backup.parent.mkdir(parents=True, exist_ok=True)
                self._atomic_write(backup, previous)
        self._atomic_write(target, source)
        self.clear_recovery(target)
        return SavedRecipe(target, self._sha256(source), backup)

    def autosave(self, path: str | Path, source: str) -> Path:
        target = self.recovery_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, source)
        return target

    def load_recovery(self, path: str | Path) -> str | None:
        recovery = self.recovery_path(path)
        return recovery.read_text(encoding="utf-8") if recovery.is_file() else None

    def has_newer_recovery(self, path: str | Path) -> bool:
        target = Path(path)
        recovery = self.recovery_path(target)
        if not recovery.is_file():
            return False
        if not target.is_file():
            return True
        if recovery.read_bytes() == target.read_bytes():
            return False
        return recovery.stat().st_mtime_ns >= target.stat().st_mtime_ns

    def clear_recovery(self, path: str | Path) -> None:
        recovery = self.recovery_path(path)
        if recovery.exists():
            recovery.unlink()

    def versions(self, path: str | Path) -> tuple[Path, ...]:
        target = Path(path)
        directory = target.parent / ".history" / target.stem
        if not directory.is_dir():
            return ()
        return tuple(sorted(directory.glob("*.yml"), reverse=True))

    @staticmethod
    def recovery_path(path: str | Path) -> Path:
        target = Path(path)
        return target.with_name(f".{target.name}.recovery")

    @staticmethod
    def _history_path(path: Path, source: str) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        digest = RecipeRepository._sha256(source)[:12]
        return path.parent / ".history" / path.stem / f"{timestamp}_{digest}.yml"

    @staticmethod
    def _atomic_write(path: Path, source: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(source, encoding="utf-8", newline="\n")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _sha256(source: str) -> str:
        return hashlib.sha256(source.encode("utf-8")).hexdigest()
