"""Persistent, presentation-only station safety status strip."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, Signal
from PySide6.QtGui import QFont, QFontMetrics, QResizeEvent
from PySide6.QtWidgets import QGridLayout, QSizePolicy, QWidget
from qfluentwidgets import BodyLabel, PrimaryPushButton, PushButton


@dataclass(frozen=True, slots=True)
class StationSafetySnapshot:
    """The station safety state displayed by :class:`StationSafetyStrip`."""

    ready: bool
    active_outputs: int
    simulation: bool
    actor: str
    roles: tuple[str, ...]
    unknown_outputs: int = 0


class _EmergencyStopButton(PrimaryPushButton):
    """Reserve room for the bold emergency caption in the Fluent layout."""

    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(parent=parent)
        self.setText(text)

    def _reserve_caption_width(self) -> None:
        font = QFont(self.font())
        font.setWeight(QFont.Weight.Bold)
        # Fluent horizontal padding plus the station emergency border.
        caption_width = QFontMetrics(font).horizontalAdvance(self.text()) + 32
        self.setMinimumWidth(max(184, caption_width))

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        super().setText(text)
        self._reserve_caption_width()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.StyleChange}:
            self._reserve_caption_width()


class StationSafetyStrip(QWidget):
    """Display station safety state and immediately request an E-STOP."""

    estop_requested = Signal()
    save_settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("stationSafetyStrip")

        self.readiness = BodyLabel()
        self.outputs = BodyLabel()
        self.mode = BodyLabel()
        self.actor = BodyLabel()
        self.estop = _EmergencyStopButton("E-STOP  |  ALL OUTPUTS OFF", self)
        self.estop.setObjectName("stationEmergencyStopButton")
        self.estop.setProperty("visualPriority", "high")
        self.estop.setProperty("controlState", "emergency")
        self.estop.setMinimumHeight(36)
        self.save_settings = PushButton("SAVE SETTINGS")
        self.save_settings.setAccessibleName("Save pending station settings")
        self.save_settings.setToolTip(
            "Validate and save pending Settings and device-form changes."
        )
        self.save_settings.clicked.connect(self.save_settings_requested)
        self.estop.setAccessibleName(
            "Emergency stop: disable all outputs and abort acquisition"
        )
        self.estop.setAccessibleDescription(
            "Always available. Opens a confirmation before sending emergency OFF "
            "and acquisition-abort requests to every instrument. Shortcut: Control Shift E."
        )
        self.estop.setToolTip(
            "Emergency stop (Ctrl+Shift+E): confirm, disable every instrument "
            "output and abort acquisition."
        )
        self.estop.clicked.connect(self.estop_requested)

        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(8, 4, 8, 4)
        self._layout.setHorizontalSpacing(12)
        self._layout.setVerticalSpacing(3)
        for widget in (
            self.readiness,
            self.outputs,
            self.mode,
            self.actor,
        ):
            widget.setMinimumWidth(0)
            widget.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Preferred,
            )
        self.save_settings.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        self.estop.setSizePolicy(
            QSizePolicy.Policy.MinimumExpanding,
            QSizePolicy.Policy.Preferred,
        )
        self.save_settings.setMinimumWidth(100)
        self._layout_mode: str | None = None
        self._reflow(mode="narrow")

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        width = event.size().width()
        self._reflow(
            mode="narrow" if width < 520 else "compact" if width < 900 else "wide"
        )

    def _reflow(self, *, mode: str) -> None:
        if self._layout_mode == mode:
            return
        self._layout_mode = mode
        widgets = (
            self.readiness,
            self.outputs,
            self.mode,
            self.actor,
            self.save_settings,
            self.estop,
        )
        for widget in widgets:
            self._layout.removeWidget(widget)
        for column in range(6):
            self._layout.setColumnStretch(column, 0)
        self._layout.setHorizontalSpacing(6 if mode == "narrow" else 12)
        if mode == "narrow":
            self.estop.setText("E-STOP | ALL OFF")
            self._layout.addWidget(self.readiness, 0, 0)
            self._layout.addWidget(self.outputs, 0, 1)
            self._layout.addWidget(self.estop, 1, 0, 1, 2)
            self._layout.addWidget(self.save_settings, 2, 0, 1, 2)
            self._layout.addWidget(self.mode, 3, 0)
            self._layout.addWidget(self.actor, 3, 1)
            self._layout.setColumnStretch(0, 1)
            self._layout.setColumnStretch(1, 1)
        elif mode == "compact":
            self.estop.setText("E-STOP  |  ALL OUTPUTS OFF")
            self._layout.addWidget(self.readiness, 0, 0)
            self._layout.addWidget(self.outputs, 0, 1)
            self._layout.addWidget(self.save_settings, 1, 0)
            self._layout.addWidget(self.estop, 1, 1)
            self._layout.addWidget(self.mode, 2, 0)
            self._layout.addWidget(self.actor, 2, 1)
            self._layout.setColumnStretch(0, 1)
            self._layout.setColumnStretch(1, 2)
        else:
            self.estop.setText("E-STOP  |  ALL OUTPUTS OFF")
            self._layout.addWidget(self.readiness, 0, 0)
            self._layout.addWidget(self.outputs, 0, 1)
            self._layout.addWidget(self.mode, 0, 2)
            self._layout.addWidget(self.actor, 0, 3)
            self._layout.addWidget(self.save_settings, 0, 4)
            self._layout.addWidget(self.estop, 0, 5)
            self._layout.setColumnStretch(0, 1)
            self._layout.setColumnStretch(1, 1)
            self._layout.setColumnStretch(2, 1)
            self._layout.setColumnStretch(3, 3)
            self._layout.setColumnStretch(4, 0)
            self._layout.setColumnStretch(5, 1)
        # A resize can switch from one to several rows while the parent layout
        # still holds the previous height. Reserve the new rows before paint.
        self._layout.invalidate()
        self.setMinimumHeight(self._layout.minimumSize().height())
        self.updateGeometry()

    def update_snapshot(self, snapshot: StationSafetySnapshot) -> None:
        """Synchronously render ``snapshot`` without performing station actions."""
        self.readiness.setText(
            "Station ready" if snapshot.ready else "Station blocked"
        )
        self.readiness.setProperty(
            "safetyState", "ready" if snapshot.ready else "danger"
        )
        self.outputs.setText(
            f"{snapshot.active_outputs} on · {snapshot.unknown_outputs} unknown"
            if snapshot.unknown_outputs and snapshot.active_outputs
            else "Outputs unknown"
            if snapshot.unknown_outputs
            else "Outputs off"
            if snapshot.active_outputs == 0
            else f"{snapshot.active_outputs} outputs active"
        )
        self.outputs.setProperty(
            "outputState", "unknown" if snapshot.unknown_outputs else "off" if snapshot.active_outputs == 0 else "active"
        )
        self.mode.setText("SIMULATION" if snapshot.simulation else "HARDWARE")
        roles = ", ".join(snapshot.roles) or "no role"
        self.actor.setText(f"{snapshot.actor or 'anonymous'} · {roles}")
        for widget in (self.readiness, self.outputs, self.mode, self.actor):
            widget.setToolTip(widget.text())

        for widget in (self.readiness, self.outputs):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
