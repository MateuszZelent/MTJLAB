"""Non-blocking VISA discovery worker used by the Dashboard."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QThread, Signal

from app.devices.discovery import DiscoveredInstrument, discover_visa_resources


class VisaDiscoveryWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, backends: Iterable[str], parent: object | None = None) -> None:
        super().__init__(parent)
        self._backends = tuple(backends)

    def run(self) -> None:
        try:
            results: tuple[DiscoveredInstrument, ...] = discover_visa_resources(self._backends)
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit(results)
