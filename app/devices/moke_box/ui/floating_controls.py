"""Modeless Fluent host for the station's single MOKE voltage panel."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, PushButton, StrongBodyLabel, SwitchButton

from app.ui.dialogs import StationDialog


class MokeFloatingControlsWindow(StationDialog):
    closed = Signal()
    history_changed = Signal(bool)

    def __init__(self, panel: QWidget, parent: QWidget, *, show_history=True):
        super().__init__(parent, resizable=True)
        self.setObjectName("mokeFloatingControlsWindow")
        self.setWindowTitle("MOKE Box — voltage control")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumSize(540, 640)
        surface = self.use_modal_shell_content().surface
        self.panel_layout = self.modal_content_layout(spacing=8)
        header = QHBoxLayout()
        header.addWidget(StrongBodyLabel("MOKE Box · voltage control", surface), 1)
        self.show_history = SwitchButton(surface)
        self.show_history.setOnText("Time plot")
        self.show_history.setOffText("Time plot")
        self.show_history.setChecked(show_history)
        self.show_history.setAccessibleName("Show voltage and field time plot")
        self.show_history.setToolTip("Show or hide history; output control and data collection continue.")
        self.show_history.checkedChanged.connect(self.history_changed)
        header.addWidget(self.show_history)
        self.panel_layout.addLayout(header)
        self.panel_layout.addWidget(panel, 1)
        footer = QHBoxLayout()
        note = CaptionLabel("Shared with the main page · closing docks this panel", surface)
        note.setWordWrap(True)
        footer.addWidget(note, 1)
        self.dock_button = PushButton("Dock panel", surface)
        self.dock_button.clicked.connect(self.close)
        footer.addWidget(self.dock_button)
        self.panel_layout.addLayout(footer)
        self.resize(1120, 840) if show_history else self.resize(540, 840)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)
