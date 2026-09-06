"""Image and PDF attachment viewer dialog and utilities."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    FluentIcon,
    PushButton,
    SubtitleLabel,
    ToolButton,
)

from app.inventory.models import SampleAttachment
from app.inventory.store import InventoryStore


class ImageViewerDialog(QDialog):
    """High-resolution zoomable viewer for microscope, SEM, and chip layout images."""

    def __init__(
        self,
        image_path: Path,
        title: str = "Image Preview",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(800, 650)
        self.setModal(True)

        self._original_pixmap = QPixmap(str(image_path))
        self._zoom_factor = 1.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.addWidget(SubtitleLabel(image_path.name, self))
        toolbar.addStretch(1)

        self.zoom_out_btn = ToolButton(FluentIcon.ZOOM_OUT, self)
        self.zoom_out_btn.setToolTip("Zoom Out")
        self.zoom_out_btn.clicked.connect(self._zoom_out)

        self.zoom_label = CaptionLabel("100%", self)
        self.zoom_label.setMinimumWidth(48)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.zoom_in_btn = ToolButton(FluentIcon.ZOOM_IN, self)
        self.zoom_in_btn.setToolTip("Zoom In")
        self.zoom_in_btn.clicked.connect(self._zoom_in)

        self.fit_btn = PushButton("Fit", self)
        self.fit_btn.clicked.connect(self._fit_to_window)

        self.reset_btn = PushButton("1:1", self)
        self.reset_btn.clicked.connect(self._reset_zoom)

        toolbar.addWidget(self.zoom_out_btn)
        toolbar.addWidget(self.zoom_label)
        toolbar.addWidget(self.zoom_in_btn)
        toolbar.addWidget(self.fit_btn)
        toolbar.addWidget(self.reset_btn)
        layout.addLayout(toolbar)

        # Scroll area with image
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll_area.setStyleSheet("QScrollArea { border: 1px solid palette(mid); }")

        self.image_label = QLabel(self.scroll_area)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.image_label.setScaledContents(True)
        self.scroll_area.setWidget(self.image_label)

        layout.addWidget(self.scroll_area, 1)

        # Close button
        bottom_bar = QHBoxLayout()
        bottom_bar.addStretch(1)
        close_btn = PushButton("Close", self)
        close_btn.clicked.connect(self.accept)
        bottom_bar.addWidget(close_btn)
        layout.addLayout(bottom_bar)

        self._update_display()

    def _update_display(self) -> None:
        if self._original_pixmap.isNull():
            self.image_label.setText("Unable to load image.")
            return

        new_size = self._original_pixmap.size() * self._zoom_factor
        self.image_label.resize(new_size)
        self.image_label.setPixmap(self._original_pixmap)
        self.zoom_label.setText(f"{int(self._zoom_factor * 100)}%")

    def _zoom_in(self) -> None:
        self._zoom_factor = min(self._zoom_factor * 1.25, 10.0)
        self._update_display()

    def _zoom_out(self) -> None:
        self._zoom_factor = max(self._zoom_factor / 1.25, 0.1)
        self._update_display()

    def _reset_zoom(self) -> None:
        self._zoom_factor = 1.0
        self._update_display()

    def _fit_to_window(self) -> None:
        if self._original_pixmap.isNull():
            return
        area_size = self.scroll_area.viewport().size()
        scale_w = area_size.width() / max(self._original_pixmap.width(), 1)
        scale_h = area_size.height() / max(self._original_pixmap.height(), 1)
        self._zoom_factor = min(scale_w, scale_h, 1.0)
        self._update_display()


def open_attachment(
    attachment: SampleAttachment,
    store: InventoryStore,
    parent: QWidget | None = None,
) -> None:
    """Open attachment in an internal image viewer dialog or the system document viewer."""
    file_path = store.get_attachment_path(attachment)
    if not file_path.is_file():
        return

    if attachment.is_image:
        dialog = ImageViewerDialog(file_path, title=attachment.filename, parent=parent)
        dialog.exec()
    else:
        # Launch external OS PDF reader or document handler
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(file_path)))
