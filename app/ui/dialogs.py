"""Shared Fluent-compatible popup infrastructure."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QFileDialog, QMessageBox as QtMessageBox, QWidget
from PySide6.QtWidgets import QDialog
from qfluentwidgets import MessageBox


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

        dialog = MessageBox(title, text, parent)
        dialog.yesButton.setText(cls._button_label(primary))
        if secondary is None:
            dialog.hideCancelButton()
        else:
            dialog.cancelButton.setText(cls._button_label(secondary))
            if default_button == secondary:
                dialog.cancelButton.setFocus()
            else:
                dialog.yesButton.setFocus()
        return primary if dialog.exec() == QDialog.DialogCode.Accepted else (
            secondary or cls.StandardButton.NoButton
        )

    @classmethod
    def _button_label(cls, button: QtMessageBox.StandardButton) -> str:
        return {
            cls.StandardButton.Ok: "OK",
            cls.StandardButton.Yes: "Yes",
            cls.StandardButton.No: "No",
            cls.StandardButton.Save: "Save",
            cls.StandardButton.Cancel: "Cancel",
        }.get(button, "OK")


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
