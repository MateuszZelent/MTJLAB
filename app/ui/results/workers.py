"""Small read-only background jobs used by large Results datasets."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class ResultReadSignals(QObject):
    """Queued result boundary for a :class:`ResultReadTask`."""

    loaded = Signal(int, object)
    failed = Signal(int, str)
    finished = Signal(int)


class ResultReadTask(QRunnable):
    """Execute one immutable reader operation outside the Qt GUI thread."""

    def __init__(
        self,
        request_id: int,
        operation: Callable[..., Any],
        *args: object,
        cooperative_cancel: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.operation = operation
        self.args = args
        self.kwargs = kwargs
        self.cooperative_cancel = cooperative_cancel
        self.signals = ResultReadSignals()
        self._cancelled = Event()

    def cancel(self) -> None:
        """Suppress queued/stale work and any result that finishes afterwards."""

        self._cancelled.set()

    @Slot()
    def run(self) -> None:
        try:
            if self._cancelled.is_set():
                return
            if self.cooperative_cancel:
                result = self.operation(
                    *self.args,
                    cancelled=self._cancelled.is_set,
                    **self.kwargs,
                )
            else:
                result = self.operation(*self.args, **self.kwargs)
        except Exception as exc:
            if not self._cancelled.is_set():
                self.signals.failed.emit(self.request_id, str(exc))
        else:
            if not self._cancelled.is_set():
                self.signals.loaded.emit(self.request_id, result)
        finally:
            self.signals.finished.emit(self.request_id)
