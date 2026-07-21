"""Background workers for slow, atomic settings persistence."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from app.devices.keithley_2600.settings_defaults import (
    persist_keithley_default_snapshots,
)


class KeithleyDefaultsSaveWorker(QObject):
    """Persist coalesced Keithley form defaults outside the GUI thread."""

    succeeded = Signal(int, object, object)
    failed = Signal(int, str)

    def __init__(self, settings_path: str | Path) -> None:
        super().__init__()
        self._settings_path = Path(settings_path)

    @Slot(int, object)
    def save(self, generation: int, snapshots: object) -> None:
        try:
            settings, raw = persist_keithley_default_snapshots(
                self._settings_path, snapshots  # type: ignore[arg-type]
            )
        except Exception as exc:
            self.failed.emit(generation, str(exc))
            return
        self.succeeded.emit(generation, settings, raw)
