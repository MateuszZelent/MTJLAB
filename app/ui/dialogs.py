"""Shared Fluent-compatible popup infrastructure."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox as QtMessageBox, QWidget
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout
from qfluentwidgets import BodyLabel, PrimaryPushButton, PushButton, SubtitleLabel


class StationDialog(QDialog):
    """Theme-aware host for every station-owned popup or floating window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.StyleChange,
        }:
            self.update()
            for child in self.findChildren(QWidget):
                child.update()


class StationAlertDialog(StationDialog):
    """Reliable Fluent alert that keeps its text above the modal surface."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        text: str,
        primary: QtMessageBox.StandardButton,
        secondary: QtMessageBox.StandardButton | None,
        default_button: QtMessageBox.StandardButton,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setProperty("stationSurface", "raised")
        parent_width = parent.width() if parent is not None else 720
        preferred_width = max(360, min(640, parent_width - 48))
        self.setMinimumWidth(preferred_width)
        self.setMaximumWidth(preferred_width)

        self.title_label = SubtitleLabel(title, self)
        self.title_label.setWordWrap(True)
        self.content_label = BodyLabel(text, self)
        self.content_label.setWordWrap(True)
        self.content_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )

        self.primary_button = PrimaryPushButton(
            StationMessageBox._button_label(primary), self
        )
        self.primary_button.setMinimumWidth(120)
        self.primary_button.clicked.connect(lambda: self._finish(primary, True))
        self.secondary_button: PushButton | None = None
        if secondary is not None:
            self.secondary_button = PushButton(
                StationMessageBox._button_label(secondary), self
            )
            self.secondary_button.setMinimumWidth(120)
            self.secondary_button.clicked.connect(
                lambda: self._finish(secondary, False)
            )

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addStretch(1)
        if self.secondary_button is not None:
            buttons.addWidget(self.secondary_button)
        buttons.addWidget(self.primary_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(self.title_label)
        layout.addWidget(self.content_label)
        layout.addSpacing(8)
        layout.addLayout(buttons)

        self.selected_button = QtMessageBox.StandardButton.NoButton
        if default_button == secondary and self.secondary_button is not None:
            self.secondary_button.setFocus()
        else:
            self.primary_button.setFocus()

    def _finish(self, button: QtMessageBox.StandardButton, accepted: bool) -> None:
        self.selected_button = button
        if accepted:
            self.accept()
        else:
            self.reject()


class StationSettingsGuidanceDialog(StationDialog):
    """An alert with a direct, non-destructive route to a configuration fix."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        text: str,
        action_label: str = "Go to settings",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setProperty("stationSurface", "raised")
        self.setMinimumWidth(max(360, min(640, (parent.width() if parent else 720) - 48)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        heading = SubtitleLabel(title, self)
        body = BodyLabel(text, self)
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addSpacing(8)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = PushButton("Close", self)
        self.go_to_settings_button = PrimaryPushButton(action_label, self)
        close.clicked.connect(self.reject)
        self.go_to_settings_button.clicked.connect(self.accept)
        buttons.addWidget(close)
        buttons.addWidget(self.go_to_settings_button)
        layout.addLayout(buttons)
        self.go_to_settings_button.setFocus()


class StationMessageBox:
    """Fluent modal prompts with the return contract of ``QMessageBox``.

    Existing callers use standard-button values to protect destructive or
    hardware-adjacent actions.  Keeping that contract at this single boundary
    lets the pages use a Fluent presentation without changing their decisions.
    """

    StandardButton = QtMessageBox.StandardButton

    @classmethod
    def information(
        cls,
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QtMessageBox.StandardButton = StandardButton.Ok,
        defaultButton: QtMessageBox.StandardButton = StandardButton.NoButton,
    ) -> QtMessageBox.StandardButton:
        return cls._show(parent, title, text, buttons, defaultButton)

    @classmethod
    def warning(
        cls,
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QtMessageBox.StandardButton = StandardButton.Ok,
        defaultButton: QtMessageBox.StandardButton = StandardButton.NoButton,
    ) -> QtMessageBox.StandardButton:
        return cls._show(parent, title, text, buttons, defaultButton)

    @classmethod
    def critical(
        cls,
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QtMessageBox.StandardButton = StandardButton.Ok,
        defaultButton: QtMessageBox.StandardButton = StandardButton.NoButton,
    ) -> QtMessageBox.StandardButton:
        return cls._show(parent, title, text, buttons, defaultButton)

    @classmethod
    def question(
        cls,
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QtMessageBox.StandardButton = (
            StandardButton.Yes | StandardButton.No
        ),
        defaultButton: QtMessageBox.StandardButton = StandardButton.NoButton,
    ) -> QtMessageBox.StandardButton:
        return cls._show(parent, title, text, buttons, defaultButton)

    @classmethod
    def _show(
        cls,
        parent: QWidget | None,
        title: str,
        text: str,
        buttons: QtMessageBox.StandardButton,
        default_button: QtMessageBox.StandardButton,
    ) -> QtMessageBox.StandardButton:
        available = tuple(
            button
            for button in (
                cls.StandardButton.Yes,
                cls.StandardButton.Ok,
                cls.StandardButton.Save,
                cls.StandardButton.No,
                cls.StandardButton.Cancel,
            )
            if buttons & button
        )
        if not available:
            available = (cls.StandardButton.Ok,)
        primary = next(
            (
                button
                for button in available
                if button not in {cls.StandardButton.No, cls.StandardButton.Cancel}
            ),
            available[0],
        )
        secondary = next((button for button in available if button != primary), None)

        dialog = StationAlertDialog(
            parent,
            title,
            text,
            primary,
            secondary,
            default_button,
        )
        dialog.exec()
        return dialog.selected_button

    @classmethod
    def _button_label(cls, button: QtMessageBox.StandardButton) -> str:
        return {
            cls.StandardButton.Ok: "OK",
            cls.StandardButton.Yes: "Yes",
            cls.StandardButton.No: "No",
            cls.StandardButton.Save: "Save",
            cls.StandardButton.Cancel: "Cancel",
        }.get(button, "OK")

    @classmethod
    def settings_guidance(cls, parent: QWidget | None, title: str, text: str) -> bool:
        """Show an actionable safety/configuration alert and return its choice."""

        dialog = StationSettingsGuidanceDialog(parent, title, text)
        return dialog.exec() == QDialog.DialogCode.Accepted

    @classmethod
    def action_guidance(
        cls,
        parent: QWidget | None,
        title: str,
        text: str,
        action_label: str,
    ) -> bool:
        dialog = StationSettingsGuidanceDialog(
            parent, title, text, action_label=action_label
        )
        return dialog.exec() == QDialog.DialogCode.Accepted


class StationFileDialog(QFileDialog):
    """Force Qt-rendered file pickers so station light/dark tokens are respected."""

    @staticmethod
    def getOpenFileName(
        parent: QWidget | None = None,
        caption: str = "",
        directory: str = "",
        filter: str = "",
        selectedFilter: str = "",
        options: QFileDialog.Option = QFileDialog.Option(0),
    ) -> tuple[str, str]:
        return QFileDialog.getOpenFileName(
            parent,
            caption,
            directory,
            filter,
            selectedFilter,
            options | QFileDialog.Option.DontUseNativeDialog,
        )

    @staticmethod
    def getSaveFileName(
        parent: QWidget | None = None,
        caption: str = "",
        directory: str = "",
        filter: str = "",
        selectedFilter: str = "",
        options: QFileDialog.Option = QFileDialog.Option(0),
    ) -> tuple[str, str]:
        return QFileDialog.getSaveFileName(
            parent,
            caption,
            directory,
            filter,
            selectedFilter,
            options | QFileDialog.Option.DontUseNativeDialog,
        )

    @staticmethod
    def getExistingDirectory(
        parent: QWidget | None = None,
        caption: str = "",
        directory: str = "",
        options: QFileDialog.Option = QFileDialog.Option.ShowDirsOnly,
    ) -> str:
        return QFileDialog.getExistingDirectory(
            parent,
            caption,
            directory,
            options | QFileDialog.Option.DontUseNativeDialog,
        )
