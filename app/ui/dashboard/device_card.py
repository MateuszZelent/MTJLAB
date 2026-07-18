"""Dashboard card for one registered instrument."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.devices.discovery import DiscoveredInstrument


class DeviceCard(QFrame):
    connect_requested = Signal()
    disconnect_requested = Signal()
    test_requested = Signal()
    assign_resource_requested = Signal(object)

    def __init__(self, title: str, resource: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._assignment_allowed = True
        self.setObjectName("deviceCard")
        layout = QVBoxLayout(self)
        name = QLabel(title)
        name.setObjectName("cardTitle")
        self.state = QLabel("DISCONNECTED")
        self.state.setObjectName("stateDisconnected")
        self.resource = QLabel()
        self.resource.setWordWrap(True)
        self.resource.setObjectName("muted")
        self.identity = QLabel("IDN: not connected")
        self.identity.setWordWrap(True)
        self.identity.setObjectName("muted")
        assignment_row = QHBoxLayout()
        self.detected_resources = QComboBox()
        self.detected_resources.setPlaceholderText("Scan VISA to select a detected instrument")
        self.detected_resources.setEnabled(False)
        self.detected_resources.setAccessibleName(f"Detected VISA resources for {title}")
        self.assign_button = QPushButton("Assign VISA")
        self.assign_button.setProperty("compact", True)
        self.assign_button.setEnabled(False)
        self.assign_button.setToolTip("Save the selected detected resource to this instrument card.")
        assignment_row.addWidget(self.detected_resources, 1)
        assignment_row.addWidget(self.assign_button)
        self.assignment_hint = QLabel()
        self.assignment_hint.setObjectName("assignmentPendingHint")
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
        self.state.setText(state.upper())
        self.state.setObjectName("state" + "".join(part.title() for part in state.split("_")))
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
            self.state.setText("APPLYING NEW VISA ADDRESS…")

    def set_testing(self, active: bool) -> None:
        if active:
            self.state.setText("TESTING COMMUNICATION…")

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
            self.detected_resources.addItem(label, payload)
            if result.resource == configured_resource and result.backend == configured_backend:
                assigned_index = self.detected_resources.count() - 1
        if self.detected_resources.count() == 0:
            self.detected_resources.setPlaceholderText("No matching instrument detected")
            self.detected_resources.setCurrentIndex(-1)
            self.detected_resources.setEnabled(False)
            self.assign_button.setEnabled(False)
            self.assign_button.setText("Assign VISA")
            self.assignment_hint.hide()
            if hasattr(self, "connect_button"):
                self.connect_button.setEnabled(True)
                self.test_button.setEnabled(True)
        elif assigned_index >= 0:
            self.detected_resources.setCurrentIndex(assigned_index)
            self.detected_resources.setEnabled(False)
            self.assign_button.setEnabled(False)
            self.assign_button.setText("Assigned ✓")
            self.assignment_hint.hide()
            if hasattr(self, "connect_button"):
                self.connect_button.setEnabled(True)
                self.test_button.setEnabled(True)
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
        if hasattr(self, "connect_button"):
            self.connect_button.setEnabled(False)
            self.test_button.setEnabled(False)


class DeviceConnectionPanel(QFrame):
    """Device-local connection controls; never shown on the station dashboard."""

    connect_requested = Signal()
    disconnect_requested = Signal()
    test_requested = Signal()

    def __init__(self, title: str, resource: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("connectionPanel")
        layout = QHBoxLayout(self)
        copy = QVBoxLayout()
        heading = QLabel("Instrument connection")
        heading.setObjectName("sectionTitle")
        self.summary = QLabel()
        self.identity = self.summary
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        copy.addWidget(heading)
        copy.addWidget(self.summary)
        layout.addLayout(copy, 1)
        self.state = QLabel("DISCONNECTED")
        self.state.setObjectName("stateDisconnected")
        layout.addWidget(self.state)
        self.connect_button = QPushButton("Connect")
        self.disconnect_button = QPushButton("Disconnect")
        self.test_button = QPushButton("Test")
        self.test_button.setToolTip(
            "Open a temporary session, validate identity and protocol, force safe OFF, then disconnect."
        )
        layout.addWidget(self.connect_button)
        layout.addWidget(self.disconnect_button)
        layout.addWidget(self.test_button)
        self.connect_button.clicked.connect(self.connect_requested)
        self.disconnect_button.clicked.connect(self.disconnect_requested)
        self.test_button.clicked.connect(self.test_requested)
        self.update_resource(resource)

    def update_resource(self, resource: str | None, backend: str | None = None) -> None:
        detail = resource or "No VISA resource configured"
        if backend:
            detail += f"  •  {backend} backend"
        self.summary.setText(detail)

    def update_state(self, state: str) -> None:
        self.state.setText(state.replace("_", " ").upper())
        self.state.setObjectName("state" + "".join(part.title() for part in state.split("_")))
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)

    def update_identity(self, value: object) -> None:
        if idn := getattr(value, "idn", None):
            self.summary.setText(idn)

    def set_reconfiguring(self, active: bool) -> None:
        self._set_busy(active, "Applying new VISA address…")

    def set_testing(self, active: bool) -> None:
        self._set_busy(active, "Testing communication…")
        self.test_button.setText("Testing…" if active else "Test")

    def _set_busy(self, active: bool, label: str) -> None:
        for button in (self.connect_button, self.disconnect_button, self.test_button):
            button.setEnabled(not active)
        if active:
            self.state.setText(label.upper())
