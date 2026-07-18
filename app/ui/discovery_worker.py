"""Non-blocking VISA discovery worker used by the Dashboard."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QThread, Signal

from app.devices.discovery import (
    DiscoveredInstrument,
    discover_tcp_ip_range,
    discover_tcp_endpoints,
    discover_visa_resources,
)


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


class TcpDiscoveryWorker(QThread):
    """Bounded TCP-port discovery off the GUI thread."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        network: str,
        port: int,
        *,
        range_end: str | None = None,
        allow_non_private: bool = False,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._network = network
        self._port = port
        self._range_end = range_end
        self._allow_non_private = allow_non_private

    def run(self) -> None:
        try:
            if self._range_end:
                results = discover_tcp_ip_range(
                    self._network, self._range_end, self._port,
                    allow_non_private=self._allow_non_private,
                )
            else:
                results = discover_tcp_endpoints(
                    self._network, self._port, allow_non_private=self._allow_non_private
                )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit(results)
