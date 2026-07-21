"""Dashboard card for one registered instrument."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
)

from app.devices.discovery import DiscoveredInstrument


class DeviceCard(CardWidget):
    connect_requested = Signal()
    disconnect_requested = Signal()
    test_requested = Signal()
    assign_resource_requested = Signal(object)

    def __init__(self, title: str, resource: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._assignment_allowed = True
        self.setObjectName("stationDeviceCard")
        self.setMinimumHeight(190)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        name = BodyLabel(title, self)
        name.setAccessibleName(f"Instrument {title}")
        self.state = CaptionLabel("Disconnected", self)
        self.state.setProperty("stationState", "disconnected")
        self.state.setAccessibleName(f"{title} connection state")
        self.resource = BodyLabel(parent=self)
        self.resource.setWordWrap(True)
        self.identity = CaptionLabel("IDN: not connected", self)
        self.identity.setWordWrap(True)
        assignment_row = QHBoxLayout()
        self.detected_resources = ComboBox(self)
        self.detected_resources.setPlaceholderText("Scan VISA to select a detected instrument")
        self.detected_resources.setEnabled(False)
        self.detected_resources.setAccessibleName(f"Detected VISA resources for {title}")
        self.assign_button = PrimaryPushButton("Assign VISA", self)
        self.assign_button.setEnabled(False)
        self.assign_button.setToolTip("Save the selected detected resource to this instrument card.")
        assignment_row.addWidget(self.detected_resources, 1)
        assignment_row.addWidget(self.assign_button)
        self.assignment_hint = CaptionLabel(parent=self)
        self.assignment_hint.setWordWrap(True)
        self.assignment_hint.hide()
        layout.addWidget(name)
        layout.addWidget(self.state)
        layout.addWidget(self.resource)
        layout.addWidget(self.identity)
        layout.addStretch(1)
        layout.addLayout(assignment_row)
        layout.addWidget(self.assignment_hint)
        self.assign_button.clicked.connect(self._request_assignment)
        self.detected_resources.currentIndexChanged.connect(self._detected_selection_changed)
        self.update_resource(resource)

    def update_state(self, state: str) -> None:
        self.state.setText(state.replace("_", " ").title())
        self.state.setProperty("stationState", state)
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)

    def update_identity(self, value: object) -> None:
        idn = getattr(value, "idn", None)
        if idn:
            self.identity.setText(f"IDN: {idn}")

    def update_resource(self, resource: str | None, backend: str | None = None) -> None:
        suffix = f"  •  backend: {backend}" if backend else ""
        self.resource.setText(f"VISA: {resource}{suffix}" if resource else "No VISA resource configured")
        self.resource.setToolTip(resource or "")

    def set_reconfiguring(self, active: bool) -> None:
        if active:
            self.state.setText("Applying new VISA address…")

    def set_testing(self, active: bool) -> None:
        if active:
            self.state.setText("Testing communication…")

    def set_discovered_resources(
        self,
        instruments: tuple[DiscoveredInstrument, ...],
        *,
        configured_resource: str | None,
        configured_backend: str,
    ) -> None:
        self.detected_resources.clear()
        assigned_index = -1
        for result in instruments:
            label = f"{result.resource}  •  {result.idn or 'no IDN'}"
            payload = (result.resource, result.backend, result.idn)
            # Fluent ComboBox reserves its second positional argument for an
            # icon.  Assignment metadata must be passed explicitly or the
            # selected resource cannot be emitted from the top-card action.
            self.detected_resources.addItem(label, userData=payload)
            if result.resource == configured_resource and result.backend == configured_backend:
                assigned_index = self.detected_resources.count() - 1
        if self.detected_resources.count() == 0:
            self.detected_resources.setPlaceholderText("No matching instrument detected")
            self.detected_resources.setCurrentIndex(-1)
            self.detected_resources.setEnabled(False)
            self.assign_button.setEnabled(False)
            self.assign_button.setText("Assign VISA")
            self.assignment_hint.hide()
        elif assigned_index >= 0:
            self.detected_resources.setCurrentIndex(assigned_index)
            self.detected_resources.setEnabled(False)
            self.assign_button.setEnabled(False)
            self.assign_button.setText("Assigned ✓")
            self.assignment_hint.hide()
        else:
            self.detected_resources.setCurrentIndex(0)
            self.detected_resources.setEnabled(True)
            self.assign_button.setText("Assign VISA")
            self.assign_button.setEnabled(self._assignment_allowed)
            self._show_pending_assignment()

    def set_assignment_allowed(self, allowed: bool) -> None:
        self._assignment_allowed = allowed
        if self.assign_button.text() != "Assigned ✓":
            self.assign_button.setEnabled(
                allowed
                and self.detected_resources.isEnabled()
                and self.detected_resources.currentIndex() >= 0
            )
        self.assign_button.setToolTip(
            "Save the selected detected resource to this instrument card."
            if allowed
            else "An engineer or service role is required to change VISA assignments."
        )

    def _request_assignment(self) -> None:
        payload = self.detected_resources.currentData()
        if isinstance(payload, tuple) and len(payload) == 3:
            self.assign_resource_requested.emit(payload)

    def _detected_selection_changed(self, index: int) -> None:
        pending = index >= 0 and self.detected_resources.isEnabled()
        self.assign_button.setEnabled(pending and self._assignment_allowed)
        if pending:
            self._show_pending_assignment()

    def _show_pending_assignment(self) -> None:
        payload = self.detected_resources.currentData()
        resource = payload[0] if isinstance(payload, tuple) and payload else "selected resource"
        self.assignment_hint.setText(
            f"⚠ {resource} is selected but not active. Click Assign VISA before Connect or Test."
        )
        self.assignment_hint.show()


class DeviceConnectionPanel(CardWidget):
    """Device-local connection controls; never shown on the station dashboard."""

    connect_requested = Signal()
    disconnect_requested = Signal()
    test_requested = Signal()

    def __init__(self, title: str, resource: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("connectionPanel")
        self.setProperty("stationSurface", "surface")
        layout = QHBoxLayout(self)
        copy = QVBoxLayout()
        self.heading = StrongBodyLabel("Instrument connection", self)
        self.heading.setObjectName("sectionTitle")
        self.summary = BodyLabel(parent=self)
        self.identity = self.summary
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        copy.addWidget(self.heading)
        copy.addWidget(self.summary)
        layout.addLayout(copy, 1)
        self.state = CaptionLabel("DISCONNECTED", self)
        self.state.setObjectName("stateDisconnected")
        layout.addWidget(self.state)
        # Connection state must come from controller readback, not from a
        # permanently accented call-to-action class.
        self.connect_button = PushButton("Connect", self)
        self.disconnect_button = PushButton("Disconnect", self)
        self.test_button = PushButton("Test", self)
        self.test_button.setToolTip(
            "Open a temporary session, validate identity and protocol, force safe OFF, then disconnect."
        )
        layout.addWidget(self.connect_button)
        layout.addWidget(self.disconnect_button)
        layout.addWidget(self.test_button)
        self.connect_button.clicked.connect(self.connect_requested)
        self.disconnect_button.clicked.connect(self.disconnect_requested)
        self.test_button.clicked.connect(self.test_requested)
        self._connection_state = "disconnected"
        self._busy = False
        self.update_resource(resource)
        self.update_state("disconnected")

    def update_resource(self, resource: str | None, backend: str | None = None) -> None:
        detail = resource or "No VISA resource configured"
        if backend:
            detail += f"  •  {backend} backend"
        self.summary.setText(detail)
        self._set_summary_error(False)

    def update_state(self, state: str) -> None:
        normalized = state.strip().lower().replace(" ", "_")
        self._connection_state = normalized
        self.state.setText(normalized.replace("_", " ").upper())
        self.state.setObjectName("state" + "".join(part.title() for part in normalized.split("_")))
        self.state.setProperty("deviceState", normalized)
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)
        self._apply_connection_controls()

    def _apply_connection_controls(self) -> None:
        if self._busy:
            for button in (
                self.connect_button,
                self.disconnect_button,
                self.test_button,
            ):
                button.setEnabled(False)
            return
        connected = self._connection_state in {
            "connected",
            "verified",
            "output_off",
            "output_on",
            "compliance",
        }
        disconnected = self._connection_state == "disconnected"
        self.connect_button.setProperty(
            "controlState", "confirmed" if connected else "available"
        )
        self.connect_button.setEnabled(connected or disconnected)
        self.disconnect_button.setEnabled(connected or not disconnected)
        self.connect_button.setToolTip(
            "Connected and verified." if connected else "Connect and verify the instrument."
        )
        self.disconnect_button.setToolTip(
            "Disconnect the active instrument session."
            if connected
            else "No active instrument session to disconnect."
        )
        for button in (self.connect_button, self.disconnect_button):
            button.style().unpolish(button)
            button.style().polish(button)

    def update_identity(self, value: object) -> None:
        if idn := getattr(value, "idn", None):
            self.summary.setText(idn)
            self._set_summary_error(False)

    def set_reconfiguring(self, active: bool) -> None:
        self._set_busy(active, "Applying connection settings…")

    def set_testing(self, active: bool) -> None:
        self._set_busy(active, "Testing communication…")
        self.test_button.setText("Testing…" if active else "Test")

    def set_connecting(self, active: bool) -> None:
        self._set_busy(active, "Connecting…")
        self.connect_button.setText("Connecting…" if active else "Connect")

    def show_error(self, action: str, error: str) -> None:
        self.set_connecting(False)
        self.set_testing(False)
        self.update_state("fault")
        self.summary.setText(f"{action.upper()} FAILED: {error}")
        self._set_summary_error(True)

    def _set_summary_error(self, active: bool) -> None:
        self.summary.setProperty("connectionState", "error" if active else "normal")
        self.summary.style().unpolish(self.summary)
        self.summary.style().polish(self.summary)

    def _set_busy(self, active: bool, label: str) -> None:
        self._busy = active
        for button in (self.connect_button, self.disconnect_button, self.test_button):
            button.setEnabled(not active)
        if active:
            self.state.setText(label.upper())
        else:
            self.state.setText(self._connection_state.replace("_", " ").upper())
            self._apply_connection_controls()
