"""Qt-thread ownership for all VISA sessions.

Every adapter is constructed once and all of its methods run on one dedicated
QObject/QThread pair.  GUI code only emits queued operation requests.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QMetaObject, QObject, QThread, Qt, Signal, Slot

from app.devices.anritsu.adapter import AnritsuAdapter
from app.devices.base import DeviceAdapter
from app.devices.keithley.adapter import KeithleyAdapter
from app.devices.rigol.adapter import RigolAdapter


class InstrumentWorker(QObject):
    completed = Signal(str, object)
    failed = Signal(str, str)
    state_changed = Signal(str)

    def __init__(self, adapter: DeviceAdapter) -> None:
        super().__init__()
        self._adapter = adapter

    @Slot(str, object)
    def execute(self, operation: str, payload: object) -> None:
        try:
            result = self._dispatch(operation, payload)
            self.state_changed.emit(self._adapter.state.value)
            self.completed.emit(operation, result)
        except Exception as exc:
            self.state_changed.emit(self._adapter.state.value)
            self.failed.emit(operation, str(exc))

    @Slot()
    def shutdown(self) -> None:
        try:
            self._adapter.emergency_off()
            self._adapter.disconnect()
        except Exception:
            pass

    def _dispatch(self, operation: str, payload: object) -> object:
        if operation == "connect":
            return self._adapter.connect()
        if operation == "disconnect":
            return self._adapter.disconnect()
        if operation == "emergency_off":
            return self._adapter.emergency_off()
        if isinstance(self._adapter, RigolAdapter):
            if operation == "configure":
                return self._adapter.configure_channel(payload)  # type: ignore[arg-type]
            if operation == "set_output":
                channel, enabled = payload  # type: ignore[misc]
                return self._adapter.set_output(channel, enabled)
        if isinstance(self._adapter, KeithleyAdapter):
            if operation == "configure":
                return self._adapter.configure_source(payload)  # type: ignore[arg-type]
            if operation == "set_output":
                channel, enabled = payload  # type: ignore[misc]
                return self._adapter.set_output(channel, enabled)
            if operation == "measure":
                return self._adapter.measure(payload)  # type: ignore[arg-type]
            if operation == "ramp_to_zero":
                return self._adapter.ramp_to_zero(payload)  # type: ignore[arg-type]
        if isinstance(self._adapter, AnritsuAdapter):
            if operation == "configure":
                return self._adapter.configure_spectrum(payload)  # type: ignore[arg-type]
            if operation == "start_live":
                return self._adapter.start_live()
            if operation == "stop_live":
                return self._adapter.stop_live()
            if operation == "fetch_trace":
                return self._adapter.fetch_trace(str(payload or "TRAC1"))
        raise ValueError(f"Nieobsługiwana operacja {operation!r}.")


class DeviceController(QObject):
    """Thread-safe façade used by pages in the main window."""

    request = Signal(str, object)
    result = Signal(str, object)
    error = Signal(str, str)
    state_changed = Signal(str)

    def __init__(self, adapter: DeviceAdapter, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = InstrumentWorker(adapter)
        self._worker.moveToThread(self._thread)
        self.request.connect(self._worker.execute, Qt.ConnectionType.QueuedConnection)
        self._worker.completed.connect(self.result)
        self._worker.failed.connect(self.error)
        self._worker.state_changed.connect(self.state_changed)
        self._thread.start()

    def call(self, operation: str, payload: object = None) -> None:
        self.request.emit(operation, payload)

    def close(self) -> None:
        if self._thread.isRunning():
            QMetaObject.invokeMethod(
                self._worker,
                "shutdown",
                Qt.ConnectionType.BlockingQueuedConnection,
            )
        self.request.disconnect(self._worker.execute)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.quit()
        self._thread.wait(3_000)
