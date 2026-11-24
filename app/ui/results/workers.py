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
    progress = Signal(int, int, int)


class ResultReadTask(QRunnable):
    """Execute one immutable reader operation outside the Qt GUI thread."""

    def __init__(
        self,
        request_id: int,
        operation: Callable[..., Any],
        *args: object,
        cooperative_cancel: bool = False,
        report_progress: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.operation = operation
        self.args = args
        self.kwargs = kwargs
        self.cooperative_cancel = cooperative_cancel
        self.report_progress = report_progress
        self.signals = ResultReadSignals()
        self._cancelled = Event()

    def cancel(self) -> None:
        """Suppress queued/stale work and any result that finishes afterwards."""

        self._cancelled.set()

    def _report_progress(self, completed: int, total: int) -> None:
        if not self._cancelled.is_set():
            self.signals.progress.emit(self.request_id, completed, total)

    @Slot()
    def run(self) -> None:
        try:
            if self._cancelled.is_set():
                return
            kwargs = self.kwargs.copy()
            if self.cooperative_cancel:
                kwargs["cancelled"] = self._cancelled.is_set
            if self.report_progress:
                kwargs["progress"] = self._report_progress
            result = self.operation(*self.args, **kwargs)
        except Exception as exc:
            if not self._cancelled.is_set():
                self.signals.failed.emit(self.request_id, str(exc))
        else:
            if not self._cancelled.is_set():
                self.signals.loaded.emit(self.request_id, result)
        finally:
            self.signals.finished.emit(self.request_id)
