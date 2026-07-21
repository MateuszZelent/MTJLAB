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
        self.card_layout.addWidget(
            self.description, 0, Qt.AlignmentFlag.AlignHCenter
        )

        self.progress = IndeterminateProgressBar(self)
        self.progress.setFixedHeight(4)
        self.progress.setMaximumWidth(280)
        self.progress.hide()
        self.card_layout.addWidget(
            self.progress, 0, Qt.AlignmentFlag.AlignHCenter
        )

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
    ) -> None:
        """Update the state while keeping its geometry and focus behavior stable."""

        self.title.setText(title)
        self.description.setText(description)
        self.setAccessibleName(accessible_name)
        self.progress.setVisible(loading)
        self.action.setText(action_text)
        self.action.setVisible(bool(action_text) and not loading)
        self.action.setAccessibleName(action_text or accessible_name)
