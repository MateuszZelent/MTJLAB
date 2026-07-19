"""Persistent, presentation-only station safety status strip."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget
from qfluentwidgets import PrimaryPushButton


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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("stationSafetyStrip")

        self.readiness = QLabel()
        self.outputs = QLabel()
        self.profile = QLabel()
        self.mode = QLabel()
        self.actor = QLabel()
        self.estop = PrimaryPushButton("E-STOP — disable all outputs")
        self.estop.setAccessibleName("Emergency stop and disable all outputs")
        self.estop.clicked.connect(self.estop_requested)

        layout = QHBoxLayout(self)
        for widget in (
            self.readiness,
            self.outputs,
            self.profile,
            self.mode,
            self.actor,
        ):
            widget.setMinimumWidth(0)
            widget.setSizePolicy(
                QSizePolicy.Policy.Ignored,
                QSizePolicy.Policy.Preferred,
            )
            layout.addWidget(widget)
        layout.setStretch(0, 1)
        layout.setStretch(1, 1)
        layout.setStretch(2, 1)
        layout.setStretch(4, 2)
        layout.addStretch(1)
        layout.addWidget(self.estop)

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
