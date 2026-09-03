"""Qt threads for network-bound eLabFTW work."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from app.integrations.elab import (
    ElabApiClient,
    ElabCredentials,
    ElabUploadRequest,
    upload_result,
)


class ElabTemplatesWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, credentials: ElabCredentials, parent=None) -> None:
        super().__init__(parent)
        self._credentials = credentials

    def run(self) -> None:
        try:
            self.loaded.emit(ElabApiClient(self._credentials).list_experiment_templates())
        except Exception as exc:
            self.failed.emit(str(exc))


class ElabUploadWorker(QThread):
    progress = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, request: ElabUploadRequest, parent=None) -> None:
        super().__init__(parent)
        self._request = request

    def run(self) -> None:
        try:
            result = upload_result(self._request, progress=self.progress.emit)
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
