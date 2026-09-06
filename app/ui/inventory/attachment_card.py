"""Visual card widget for sample attachments (microscope images, PDFs)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    IconWidget,
    PushButton,
    SimpleCardWidget,
    ToolButton,
)

from app.inventory.models import SampleAttachment


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


class AttachmentCard(SimpleCardWidget):
    """Card displaying attachment preview thumbnail, metadata, and action buttons."""

    open_requested = Signal(object)    # SampleAttachment
    delete_requested = Signal(object)  # SampleAttachment

    def minimumSizeHint(self) -> QSize:
        return QSize(120, 50)

    def __init__(
        self,
        attachment: SampleAttachment,
        file_path: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.attachment = attachment
        self.file_path = file_path

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(12)

        # Thumbnail or Icon
        if attachment.is_image and file_path.is_file():
            self.thumb = QLabel(self)
            self.thumb.setFixedSize(64, 64)
            self.thumb.setStyleSheet(
                "border: 1px solid palette(mid); border-radius: 4px; background: palette(midlight);"
            )
            self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pixmap = QPixmap(str(file_path))
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    60, 60,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.thumb.setPixmap(scaled)
            layout.addWidget(self.thumb)
        else:
            icon = FluentIcon.DOCUMENT if attachment.is_pdf else FluentIcon.FOLDER
            self.icon_widget = IconWidget(icon, self)
            self.icon_widget.setFixedSize(48, 48)
            layout.addWidget(self.icon_widget)

        # Details
        details_layout = QVBoxLayout()
        details_layout.setSpacing(3)

        self.name_label = BodyLabel(attachment.filename, self)
        self.name_label.setStyleSheet("font-weight: 600;")
        self.name_label.setWordWrap(True)
        self.name_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        details_layout.addWidget(self.name_label)

        sub_info = f"{attachment.file_type.upper()} · {_format_size(attachment.size_bytes)}"
        if attachment.uploaded_at_utc:
            sub_info += f" · {attachment.uploaded_at_utc[:10]}"
        sub_info_label = CaptionLabel(sub_info, self)
        sub_info_label.setWordWrap(True)
        sub_info_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        details_layout.addWidget(sub_info_label)

        if attachment.caption:
            caption_label = CaptionLabel(f"Note: {attachment.caption}", self)
            caption_label.setWordWrap(True)
            caption_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            details_layout.addWidget(caption_label)

        layout.addLayout(details_layout, 1)

        # Actions
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(6)

        self.open_btn = PushButton(
            "View" if attachment.is_image else "Open",
            self,
            FluentIcon.VIEW if attachment.is_image else FluentIcon.DOCUMENT,
        )
        self.open_btn.setFixedHeight(30)
        self.open_btn.clicked.connect(lambda: self.open_requested.emit(self.attachment))
        btn_layout.addWidget(self.open_btn)

        self.delete_btn = ToolButton(FluentIcon.DELETE, self)
        self.delete_btn.setFixedHeight(30)
        self.delete_btn.setToolTip("Delete Attachment")
        self.delete_btn.clicked.connect(lambda: self.delete_requested.emit(self.attachment))
        btn_layout.addWidget(self.delete_btn)

        layout.addLayout(btn_layout)
