"""Fluent state surface shared by the immutable Results viewers."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    FluentIcon,
    IconWidget,
    IndeterminateProgressBar,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
)

from app.ui.design_system import SPACING


class ResultsStateCard(CardWidget):
    """Directional empty/loading/error state without ad-hoc theme colours."""

    action_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "raised")
        self.setMinimumHeight(176)

        self.card_layout = QVBoxLayout(self)
        self.card_layout.setContentsMargins(
            SPACING["xl"], SPACING["xl"], SPACING["xl"], SPACING["xl"]
        )
        self.card_layout.setSpacing(SPACING["sm"])
        self.card_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.icon = IconWidget(FluentIcon.DOCUMENT, self)
        self.icon.setFixedSize(30, 30)
        self.card_layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)

        self.title = StrongBodyLabel(parent=self)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card_layout.addWidget(self.title)

        self.description = BodyLabel(parent=self)
        self.description.setWordWrap(True)
        self.description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.description.setMaximumWidth(560)
        self.card_layout.addWidget(self.description)

        self.progress = IndeterminateProgressBar(self)
        self.progress.setFixedHeight(4)
        self.progress.setMaximumWidth(280)
        self.progress.hide()
        self.card_layout.addWidget(
            self.progress, 0, Qt.AlignmentFlag.AlignHCenter
        )
        self.read_progress = ProgressBar(self)
        self.read_progress.setAccessibleName("Spectra read")
        self.read_progress.setFixedSize(220, 4)
        self.read_progress.hide()
        self.card_layout.addWidget(self.read_progress, 0, Qt.AlignmentFlag.AlignHCenter)

        self.action = PushButton(parent=self)
        self.action.hide()
        self.action.clicked.connect(self.action_requested)
        self.card_layout.addWidget(self.action, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_compact(self, compact: bool) -> None:
        """Use a low-height status treatment above an already-visible viewer."""

        if compact:
            self.setMinimumHeight(0)
            self.card_layout.setContentsMargins(
                SPACING["lg"], SPACING["sm"], SPACING["lg"], SPACING["sm"]
            )
            self.card_layout.setSpacing(SPACING["xs"])
            self.icon.hide()
        else:
            self.setMinimumHeight(176)
            self.card_layout.setContentsMargins(
                SPACING["xl"], SPACING["xl"], SPACING["xl"], SPACING["xl"]
            )
            self.card_layout.setSpacing(SPACING["sm"])
            self.icon.show()

    def show_state(
        self,
        *,
        title: str,
        description: str,
        accessible_name: str,
        loading: bool = False,
        action_text: str = "",
        progress_total: int | None = None,
    ) -> None:
        """Update the state while keeping its geometry and focus behavior stable."""

        self.title.setText(title)
        self.description.setText(description)
        self.setAccessibleName(accessible_name)
        self.progress.setVisible(loading and progress_total is None)
        self.read_progress.setVisible(loading and progress_total is not None)
        if loading and progress_total is not None:
            self.set_read_progress(0, progress_total)
        self.action.setText(action_text)
        self.action.setVisible(bool(action_text) and not loading)
        self.action.setAccessibleName(action_text or accessible_name)

    def set_read_progress(self, completed: int, total: int) -> None:
        self.read_progress.setRange(0, total)
        self.read_progress.setValue(completed)
        if completed == total:
            self.description.setText(f"{total:,} spectra read (100%). Preparing heatmap…")
        else:
            self.description.setText(f"Spectra read: {completed:,} / {total:,} ({100 * completed // total}%)")
