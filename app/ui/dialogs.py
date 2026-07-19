"""Shared Fluent-compatible popup infrastructure."""

from __future__ import annotations

from PySide6.QtWidgets import QFileDialog, QWidget


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
