"""Test and assign a MOKE Box TCP address from Station settings."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import BodyLabel, LineEdit, PrimaryPushButton, PushButton

from app.devices.discovery import (
    MOKE_DEFAULT_TCP_PORT,
    DiscoveredTcpEndpoint,
    parse_moke_ipv4_endpoint,
)
from app.ui.dialogs import StationDialog
from app.ui.discovery_worker import MokeIdentificationWorker


class MokeAddressDialog(StationDialog):
    def __init__(
        self, endpoint: str | None, parent: QWidget, *, discovery_available: bool = True
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Change MOKE Box TCP address")
        self.resize(540, 290)
        self.verified_endpoint: str | None = None
        self._worker: MokeIdentificationWorker | None = None
        self._discovery_available = discovery_available
        try:
            _, self._default_port = parse_moke_ipv4_endpoint(endpoint or "")
        except ValueError:
            self._default_port = MOKE_DEFAULT_TCP_PORT

        layout = self.modal_content_layout(spacing=10)
        instructions = BodyLabel(
            f"Enter an IPv4 address or paste IP:port. An IP alone uses port {self._default_port}. "
            "Disconnect LabVIEW and other MOKE Box TCP clients before testing."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        self.address = LineEdit()
        self.address.setAccessibleName("New MOKE Box TCP address")
        self.address.setProperty("precisionArrowStepping", False)
        self.address.setPlaceholderText(f"IPv4 address or IPv4:{self._default_port}")
        self.address.setText(endpoint or "")
        layout.addWidget(self.address)
        self.feedback = BodyLabel(
            "A new address starts with read-only access. Your channel profiles and limits are kept."
            if discovery_available else "Address testing is available in hardware mode."
        )
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = PushButton("Cancel")
        self.test_button = PushButton("Test address")
        self.assign_button = PrimaryPushButton("Assign address")
        for button in (self.cancel_button, self.test_button, self.assign_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.test_button.setEnabled(discovery_available)
        self.assign_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.reject)
        self.test_button.clicked.connect(self._test_address)
        self.assign_button.clicked.connect(self.accept)
        self.address.returnPressed.connect(self.test_button.click)
        self.address.textChanged.connect(self._clear_verification)
        self.address.setFocus()
        self.address.selectAll()

    def _clear_verification(self) -> None:
        if self.verified_endpoint:
            self.feedback.setText("Test the new address before assigning it.")
        self.verified_endpoint = None
        self.assign_button.setEnabled(False)

    def _test_address(self) -> None:
        if not self._discovery_available or self._worker is not None:
            return
        self._clear_verification()
        try:
            host, port = parse_moke_ipv4_endpoint(self.address.text(), self._default_port)
        except ValueError as exc:
            self.feedback.setText(str(exc))
            self.address.setFocus()
            return
        self.feedback.setText(f"Testing read-only MOKE response at {host}:{port}…")
        self._set_testing(True)
        self._worker = MokeIdentificationWorker(host, port, 1.0, self)
        self._worker.completed.connect(self._test_completed)
        self._worker.failed.connect(self.feedback.setText)
        self._worker.finished.connect(self._test_finished)
        self._worker.start()

    def _test_completed(self, result: DiscoveredTcpEndpoint) -> None:
        if result.moke_verified:
            self.verified_endpoint = result.endpoint
            self.feedback.setText(
                f"MOKE Box verified at {result.endpoint}. Assigning a new address starts with read-only access."
            )
        else:
            self.feedback.setText(result.verification_detail or "The MOKE readback could not be verified.")

    def _test_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        self._set_testing(False)
        self.assign_button.setEnabled(self.verified_endpoint is not None)

    def _set_testing(self, testing: bool) -> None:
        for control in (self.address, self.cancel_button, self.test_button, self.titleBar.closeBtn):
            control.setEnabled(not testing)

    def reject(self) -> None:
        if self._worker is None:
            super().reject()
