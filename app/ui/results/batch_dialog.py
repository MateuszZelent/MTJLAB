"""Responsive batch export preview and completion actions."""

from threading import Event

from PySide6.QtCore import Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SubtitleLabel,
)

from app.storage.filesystem_paths import native_path
from app.ui.dialogs import StationDialog

from .batch_processing import HeatmapBatch, export_batch
from .workers import ResultReadTask


class BatchPostprocessDialog(StationDialog):
    preview_ready = Signal(str, object)

    def __init__(self, identity, items, state, image, parent=None, *, power_point=False):
        super().__init__(parent)
        self.setWindowTitle("Postprocess all measurements")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumSize(520, 460)
        self.resize(864, 686)
        self._identity, self._items, self._state = identity, items, state
        self._image = image
        self._stop = Event()
        self._task = None
        self.folder = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)

        layout = self.modal_content_layout(spacing=10)
        surface = self.modal_shell.surface
        kind = "heatmaps" if isinstance(state, HeatmapBatch) else "spectra"
        layout.addWidget(SubtitleLabel("Postprocess all measurements", surface))
        subtitle = BodyLabel(f"Export {len(items):,} {kind} with the current processing settings.", surface)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        card = CardWidget(surface)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        self.current = CaptionLabel("Current selection" if image is not None else "Preview", card)
        self.current.setWordWrap(True)
        self.current.setTextFormat(Qt.TextFormat.PlainText)
        card_layout.addWidget(self.current)
        self.preview = BodyLabel("" if image is not None else "The first plot will appear here during export.", card)
        self.preview.setWordWrap(True)
        self.preview.setMinimumHeight(180)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setAccessibleName("Preview of the last exported plot")
        card_layout.addWidget(self.preview, 1)
        layout.addWidget(card, 1)

        self.progress = ProgressBar(surface, useAni=False)
        self.progress.setRange(0, len(items))
        self.progress.setAccessibleName("Measurements processed")
        layout.addWidget(self.progress)
        self.status = BodyLabel("Ready to export", surface)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)

        actions = QHBoxLayout()
        self.power_point = CheckBox("Create PowerPoint (.pptx)", surface)
        self.power_point.setChecked(power_point)
        self.power_point.setToolTip("Add one slide per exported plot.")
        actions.addWidget(self.power_point)
        actions.addStretch(1)
        self.open_folder = PushButton("Open folder", surface)
        self.open_folder.clicked.connect(self._open_folder)
        self.open_folder.hide()
        actions.addWidget(self.open_folder)
        self.close_button = PushButton("Close", surface)
        self.close_button.clicked.connect(self.close)
        actions.addWidget(self.close_button)
        self.start_button = PrimaryPushButton("Start export", surface)
        self.start_button.clicked.connect(self._start)
        actions.addWidget(self.start_button)
        layout.addLayout(actions)
        self.preview_ready.connect(self._show_preview)

    def _start(self):
        if self._task is not None:
            return
        self.start_button.setEnabled(False)
        self.power_point.setEnabled(False)
        self.close_button.setText("Cancel")
        self.status.setText("Preparing recorded spectra…")
        self._task = ResultReadTask(
            0, export_batch, self._identity[0], self._items, self._state,
            power_point=self.power_point.isChecked(), expected_identity=self._identity,
            cancelled=self._stop.is_set, preview=self.preview_ready.emit, report_progress=True)
        self._task.signals.progress.connect(self._show_progress)
        self._task.signals.loaded.connect(self._completed)
        self._task.signals.failed.connect(self._failed)
        self._task.signals.finished.connect(self._finished)
        self.pool.start(self._task)

    def _show_progress(self, _request, done, total):
        self.progress.setValue(done)
        if done and not self._stop.is_set():
            self.status.setText(f"{done:,} / {total:,} processed")

    def _show_preview(self, title, path):
        if path is None:
            if not self._stop.is_set():
                self.status.setText(title)
            return
        self.current.setText(title)
        image = QPixmap()
        try:
            loaded = image.loadFromData(native_path(path).read_bytes())
        except OSError:
            loaded = False
        if loaded:
            self._image = image
            self._fit_preview()

    def _fit_preview(self):
        if hasattr(self, "preview") and self._image is not None and not self._image.isNull():
            self.preview.setPixmap(self._image.scaled(
                self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_preview()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_preview()

    def _completed(self, _request, outcome):
        self.folder, index = outcome
        errors = [item for item in index["items"] if "error" in item]
        headline = "Export cancelled" if index["cancelled"] else "Export finished"
        if not index["cancelled"] and (index.get("error") or errors):
            headline = "Export finished with errors"
            self.progress.error()
        status = f"{headline}. {index['exported']:,} of {index['planned']:,} saved."
        if errors:
            status += f" {len(errors):,} failed. Details in index.json."
        if index.get("error"):
            status += f" {index['error']}"
        if index.get("powerpoint"):
            status += " PowerPoint saved."
        self.status.setText(status)
        self.open_folder.show()

    def _failed(self, _request, message):
        self.status.setText(f"Export could not start. {message}")

    def _finished(self, _request):
        self._task = None
        self.close_button.setText("Close")
        self.close_button.setEnabled(True)
        self.start_button.hide()

    def _open_folder(self):
        if self.folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))

    def request_stop(self):
        if self._task is not None:
            self._stop.set()
            self.close_button.setEnabled(False)
            self.progress.pause()
            self.status.setText("Cancelling…")

    def reject(self):
        if self._task is not None:
            self.request_stop()
            return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            self.request_stop()
            event.ignore()
            return
        super().closeEvent(event)
