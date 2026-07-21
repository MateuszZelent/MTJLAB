"""Persistent, presentation-only station safety status strip."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QGridLayout, QSizePolicy, QWidget
from qfluentwidgets import BodyLabel, PrimaryPushButton, PushButton


@dataclass(frozen=True, slots=True)
class StationSafetySnapshot:
    """The station safety state displayed by :class:`StationSafetyStrip`."""

    ready: bool
    active_outputs: int
    profile_state: str
    simulation: bool
    actor: str
    roles: tuple[str, ...]


class StationSafetyStrip(QWidget):
    """Display station safety state and immediately request an E-STOP."""

    estop_requested = Signal()
    save_settings_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("stationSafetyStrip")

        self.readiness = BodyLabel()
        self.outputs = BodyLabel()
        self.profile = BodyLabel()
        self.mode = BodyLabel()
        self.actor = BodyLabel()
        self.estop = PrimaryPushButton("E-STOP — disable all outputs")
        self.save_settings = PushButton("SAVE SETTINGS")
        self.save_settings.setAccessibleName("Save pending station settings")
        self.save_settings.setToolTip(
            "Validate and save pending Settings and device-form changes."
        )
        self.save_settings.clicked.connect(self.save_settings_requested)
        self.estop.setAccessibleName("Emergency stop and disable all outputs")
        self.estop.clicked.connect(self.estop_requested)

        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(8, 4, 8, 4)
        self._layout.setHorizontalSpacing(12)
        self._layout.setVerticalSpacing(3)
        for widget in (
            self.readiness,
            self.outputs,
            self.profile,
            self.mode,
            self.actor,
            self.save_settings,
        ):
            widget.setMinimumWidth(0)
            widget.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Preferred,
            )
        self._compact_layout: bool | None = None
        self._reflow(compact=True)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reflow(compact=event.size().width() < 760)

    def _reflow(self, *, compact: bool) -> None:
        if self._compact_layout == compact:
            return
        self._compact_layout = compact
        widgets = (
            self.readiness,
            self.outputs,
            self.profile,
            self.mode,
            self.actor,
            self.save_settings,
            self.estop,
        )
        for widget in widgets:
            self._layout.removeWidget(widget)
        for column in range(7):
            self._layout.setColumnStretch(column, 0)
        if compact:
            self._layout.addWidget(self.readiness, 0, 0)
            self._layout.addWidget(self.outputs, 0, 1)
            self._layout.addWidget(self.save_settings, 0, 2)
            self._layout.addWidget(self.estop, 0, 3)
            self._layout.addWidget(self.profile, 1, 0)
            self._layout.addWidget(self.mode, 1, 1)
            self._layout.addWidget(self.actor, 1, 2, 1, 2)
            self._layout.setColumnStretch(0, 1)
            self._layout.setColumnStretch(1, 1)
            self._layout.setColumnStretch(2, 2)
            self._layout.setColumnStretch(3, 2)
        else:
            for column, widget in enumerate(widgets[:-2]):
                self._layout.addWidget(widget, 0, column)
                self._layout.setColumnStretch(column, 1 if column < 4 else 2)
            self._layout.addWidget(self.save_settings, 0, 5)
            self._layout.addWidget(self.estop, 0, 6)

    def update_snapshot(self, snapshot: StationSafetySnapshot) -> None:
        """Synchronously render ``snapshot`` without performing station actions."""
        self.readiness.setText(
            "Station ready" if snapshot.ready else "Station blocked"
        )
        self.readiness.setProperty(
            "safetyState", "ready" if snapshot.ready else "danger"
        )
        self.outputs.setText(
            "Outputs off"
            if snapshot.active_outputs == 0
            else f"{snapshot.active_outputs} outputs active"
        )
        self.outputs.setProperty(
            "outputState", "off" if snapshot.active_outputs == 0 else "active"
        )
        self.profile.setText(f"Profile {snapshot.profile_state}")
        self.mode.setText("SIMULATION" if snapshot.simulation else "HARDWARE")
        roles = ", ".join(snapshot.roles) or "no role"
        self.actor.setText(f"{snapshot.actor} · {roles}")

        for widget in (self.readiness, self.outputs):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
