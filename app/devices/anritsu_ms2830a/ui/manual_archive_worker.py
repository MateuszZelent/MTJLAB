"""One serialized archive operation, with no Qt widget access."""
from PySide6.QtCore import QObject, Signal, Slot


class ManualArchiveWorker(QObject):
    completed = Signal(object, object)
    finished = Signal()

    def __init__(self, archive, operation, payload):
        super().__init__()
        self.archive, self.operation, self.payload = archive, operation, payload

    @Slot()
    def run(self):
        result, error = None, None
        try:
            if self.operation == "save":
                result = self.archive.save(**self.payload)
            elif self.operation == "close":
                self.archive.close()
            else:
                raise ValueError("Unsupported manual archive operation.")
        except Exception as exc:
            error = str(exc)
        try:
            self.completed.emit(result, error)
        finally:
            self.finished.emit()
