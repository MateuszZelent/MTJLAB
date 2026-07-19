"""Non-blocking VISA discovery worker used by the Dashboard."""

from __future__ import annotations

from collections.abc import Iterable
import threading

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

    def __init__(
        self,
        backends: Iterable[str],
        parent: object | None = None,
        *,
        preferred_lakeshore_baud: int | None = None,
    ) -> None:
        super().__init__(parent)
        self._backends = tuple(backends)
        self._preferred_lakeshore_baud = preferred_lakeshore_baud

    def run(self) -> None:
        try:
            results: tuple[DiscoveredInstrument, ...] = discover_visa_resources(
                self._backends,
                preferred_lakeshore_baud=self._preferred_lakeshore_baud,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit(results)


class TcpDiscoveryWorker(QThread):
    """Bounded TCP-port discovery off the GUI thread."""

    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)
    host_activity = Signal(str, str, str)
    cancelled = Signal()

    def __init__(
        self,
        network: str,
        port: int,
        *,
        timeout_s: float = 0.5,
        range_end: str | None = None,
        allow_non_private: bool = False,
        verify_moke: bool = False,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._network = network
        self._port = port
        self._timeout_s = timeout_s
        self._range_end = range_end
        self._allow_non_private = allow_non_private
        self._verify_moke = verify_moke
        self._stop_requested = threading.Event()

    def request_stop(self) -> None:
        self._stop_requested.set()

    def run(self) -> None:
        try:
            if self._range_end:
                results = discover_tcp_ip_range(
                    self._network, self._range_end, self._port,
                    timeout_s=self._timeout_s,
                    allow_non_private=self._allow_non_private,
                    verify_moke=self._verify_moke,
                    progress_callback=self.progress.emit,
                    activity_callback=self.host_activity.emit,
                    cancellation_requested=self._stop_requested.is_set,
                )
            else:
                results = discover_tcp_endpoints(
                    self._network, self._port, allow_non_private=self._allow_non_private,
                    timeout_s=self._timeout_s,
                    verify_moke=self._verify_moke,
                    progress_callback=self.progress.emit,
                    activity_callback=self.host_activity.emit,
                    cancellation_requested=self._stop_requested.is_set,
                )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            if self._stop_requested.is_set():
                self.cancelled.emit()
            else:
                self.completed.emit(results)


class MokeIdentificationWorker(QThread):
    """Verify one already-discovered endpoint without repeating a subnet scan."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self, host: str, port: int, timeout_s: float, parent: object | None = None
    ) -> None:
        super().__init__(parent)
        self._host = host
        self._port = port
        self._timeout_s = timeout_s

    def run(self) -> None:
        try:
            results = discover_tcp_endpoints(
                f"{self._host}/32",
                self._port,
                timeout_s=self._timeout_s,
                allow_non_private=True,
                verify_moke=True,
            )
            if not results:
                raise OSError("The endpoint stopped accepting TCP connections.")
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit(results[0])
