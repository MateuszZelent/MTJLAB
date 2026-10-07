"""Filesystem readiness evidence for display, never a run authorization."""
from queue import Empty, Queue
from threading import Thread

from PySide6.QtCore import QObject, QTimer, Signal

from app.domain.readiness import _probe_output_directory


class StorageReadinessProbe(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = ""
        self.result = (False, "Directory check pending")
        self._busy = False
        self._closed = False
        self._results = Queue(maxsize=1)
        self._poll = QTimer(self)
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._collect)
        self._refresh = QTimer(self)
        self._refresh.setInterval(30_000)
        self._refresh.timeout.connect(self._start)
        self._refresh.start()

    def set_path(self, path):
        path = str(path)
        if path != self.path:
            self.path = path
            self.result = (False, "Directory check pending")
        self._start()

    def _start(self):
        if self._closed or self._busy or not self.path:
            return
        self._busy = True
        # The thread owns only Python values, not widgets. A slow/disconnected
        # network filesystem must neither freeze Qt nor block window shutdown.
        path, results = self.path, self._results
        def probe():
            try:
                result = _probe_output_directory(path)
            except Exception as exc:
                result = (False, str(exc))
            results.put((path, result))
        Thread(target=probe, name="StorageReadinessProbe", daemon=True).start()
        self._poll.start()

    def _collect(self):
        try:
            path, result = self._results.get_nowait()
        except Empty:
            return
        self._poll.stop()
        self._busy = False
        if path != self.path:
            self._start()
            return
        self.result = result
        self.changed.emit()

    def close(self):
        self._closed = True
        self._poll.stop()
        self._refresh.stop()
