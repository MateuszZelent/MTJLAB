"""Render a completed characterization report without blocking device UI."""
from copy import deepcopy

from PySide6.QtCore import QThread


class SingleReportWorker(QThread):
    def __init__(self, dataset, parameters, path, parent=None):
        super().__init__(parent)
        self.dataset = deepcopy(dataset)
        self.parameters = deepcopy(parameters)
        self.path = path
        self.result = None
        self.error = None

    def run(self):
        try:
            from .report_pdf import KeithleyPdfReportGenerator
            self.result = KeithleyPdfReportGenerator.generate(self.dataset, self.parameters, self.path)
        except Exception as exc:
            self.error = str(exc)
