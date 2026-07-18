"""Station dashboard page independent of the application shell."""

from __future__ import annotations

import ipaddress

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from app.devices.discovery import DiscoveredInstrument, DiscoveredTcpEndpoint
from app.domain.readiness import ReadinessLevel, StationReadiness, evaluate_station_readiness
from app.engine.compiler import ExecutionPlan
from app.engine.estimation import PlanEstimate
from app.settings.models import StationSettings
from app.ui.dashboard.device_card import DeviceCard
from app.ui.discovery_worker import TcpDiscoveryWorker, VisaDiscoveryWorker


class DashboardPage(QWidget):
    emergency_requested = Signal()
    assignments_requested = Signal(object)
    status = Signal(str)

    def __init__(
        self, settings: StationSettings, parent: QWidget | None = None, *, discovery_enabled: bool = True
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._discovery_enabled = discovery_enabled
        self._discovery_worker: VisaDiscoveryWorker | None = None
        self._tcp_discovery_worker: TcpDiscoveryWorker | None = None
        self._discovery_results: tuple[DiscoveredInstrument, ...] = ()
        self._device_states = {name: "disconnected" for name in ("rigol", "keithley", "anritsu")}
        self._verified_resources: dict[str, str] = {}
        self._device_errors: dict[str, str] = {}
        self._audit_healthy = True
        self._assignment_allowed = True
        self._compiled_plan = None
        self._plan_estimate = None
        layout = QVBoxLayout(self)
        title = QLabel("Measurement station")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Connect the instruments, verify the profile, then prepare a measurement recipe.")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        grid = QGridLayout()
        self.cards = {
            "rigol": DeviceCard(settings.rigol.display_name, settings.rigol.connection.resource),
            "keithley": DeviceCard(settings.keithley.display_name, settings.keithley.connection.resource),
            "anritsu": DeviceCard(settings.anritsu.display_name, settings.anritsu.connection.resource),
        }
        for column, (device, card) in enumerate(self.cards.items()):
            grid.addWidget(card, 0, column)
            card.assign_resource_requested.connect(
                lambda payload, device=device: self._card_assignment_requested(device, payload)
            )
        layout.addLayout(grid)

        discovery = QFrame()
        discovery.setObjectName("discoveryCard")
        discovery_layout = QVBoxLayout(discovery)
        discovery_header = QHBoxLayout()
        discovery_title = QLabel("VISA instrument discovery")
        discovery_title.setObjectName("sectionTitle")
        discovery_header.addWidget(discovery_title)
        discovery_header.addStretch(1)
        self.scan_button = QPushButton("Scan VISA")
        self.scan_button.setToolTip(
            "Enumerate VISA resources and send only *IDN? with a short timeout. No output is enabled."
        )
        self.scan_button.setAccessibleName("Scan VISA instruments")
        self.save_assignments = QPushButton("Save assignments")
        self.save_assignments.setEnabled(False)
        self.save_assignments.setToolTip(
            "Persist selected VISA addresses. This changes the safety profile and revokes its approval."
        )
        discovery_header.addWidget(self.scan_button)
        discovery_header.addWidget(self.save_assignments)
        discovery_layout.addLayout(discovery_header)
        self.discovery_info = QLabel(
            "No scan performed. USB/GPIB resources are normally discoverable; LAN discovery depends on the VISA backend."
        )
        self.discovery_info.setObjectName("muted")
        self.discovery_info.setWordWrap(True)
        discovery_layout.addWidget(self.discovery_info)
        self.discovery_table = QTableWidget(0, 6)
        self.discovery_table.setHorizontalHeaderLabels(
            ["Assignment", "Action", "Status", "VISA resource", "Backend", "Identity / error"]
        )
        self.discovery_table.setAlternatingRowColors(True)
        self.discovery_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.discovery_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.discovery_table.setShowGrid(False)
        self.discovery_table.verticalHeader().setVisible(False)
        self.discovery_table.verticalHeader().setDefaultSectionSize(38)
        header = self.discovery_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.discovery_table.setMinimumHeight(190)
        discovery_layout.addWidget(self.discovery_table)
        layout.addWidget(discovery)

        tcp_discovery = QFrame()
        tcp_discovery.setObjectName("discoveryCard")
        tcp_layout = QVBoxLayout(tcp_discovery)
        tcp_header = QHBoxLayout()
        tcp_title = QLabel("TCP/IP port discovery")
        tcp_title.setObjectName("sectionTitle")
        tcp_header.addWidget(tcp_title)
        tcp_header.addStretch(1)
        self.tcp_network = QLineEdit(self._moke_network_default(settings))
        self.tcp_network.setPlaceholderText("192.168.1.0/24 or start IP")
        self.tcp_network.setAccessibleName("Network CIDR or first IP for TCP port scan")
        self.tcp_network.setMaximumWidth(170)
        self.tcp_range_end = QLineEdit()
        self.tcp_range_end.setPlaceholderText("optional end IP")
        self.tcp_range_end.setAccessibleName("Last IP for TCP port scan range")
        self.tcp_range_end.setMaximumWidth(135)
        self.tcp_port = QSpinBox()
        self.tcp_port.setRange(1, 65_535)
        self.tcp_port.setValue(10_001)
        self.tcp_port.setAccessibleName("TCP port for MOKE Box discovery")
        self.tcp_scan_button = QPushButton("Scan TCP/IP")
        self.tcp_scan_button.setToolTip(
            "Test one port on each host in the supplied private subnet. No MOKE command is sent."
        )
        tcp_header.addWidget(QLabel("CIDR / from:"))
        tcp_header.addWidget(self.tcp_network)
        tcp_header.addWidget(QLabel("to:"))
        tcp_header.addWidget(self.tcp_range_end)
        tcp_header.addWidget(QLabel("Port:"))
        tcp_header.addWidget(self.tcp_port)
        tcp_header.addWidget(self.tcp_scan_button)
        tcp_layout.addLayout(tcp_header)
        self.tcp_discovery_info = QLabel(
            "No TCP/IP scan performed. The reconstructed MOKE Box protocol uses TCP port 10001."
        )
        self.tcp_discovery_info.setObjectName("muted")
        self.tcp_discovery_info.setWordWrap(True)
        tcp_layout.addWidget(self.tcp_discovery_info)
        self.tcp_discovery_table = QTableWidget(0, 2)
        self.tcp_discovery_table.setHorizontalHeaderLabels(["Open endpoint", "Result"])
        self.tcp_discovery_table.setAlternatingRowColors(True)
        self.tcp_discovery_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tcp_discovery_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tcp_discovery_table.verticalHeader().setVisible(False)
        tcp_table_header = self.tcp_discovery_table.horizontalHeader()
        tcp_table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tcp_table_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tcp_discovery_table.setMinimumHeight(110)
        tcp_layout.addWidget(self.tcp_discovery_table)
        layout.addWidget(tcp_discovery)
        self.checklist = QLabel()
        self.checklist.setObjectName("checklist")
        self.checklist.setWordWrap(True)
        layout.addWidget(self.checklist)
        emergency = QPushButton("E-STOP — disable all outputs")
        emergency.setObjectName("emergencyButton")
        emergency.setProperty("compact", True)
        emergency.setMaximumWidth(250)
        emergency_row = QHBoxLayout()
        emergency_row.addStretch(1)
        emergency_row.addWidget(emergency)
        layout.addLayout(emergency_row)
        layout.addStretch(1)
        emergency.clicked.connect(self.emergency_requested)
        self.scan_button.clicked.connect(self._scan_visa)
        self.tcp_scan_button.clicked.connect(self._scan_tcp)
        self.save_assignments.clicked.connect(self._emit_assignments)
        if not discovery_enabled:
            self.scan_button.setEnabled(False)
            self.scan_button.setToolTip("VISA discovery is disabled in simulation mode.")
            self.tcp_scan_button.setEnabled(False)
            self.tcp_scan_button.setToolTip("TCP/IP discovery is disabled in simulation mode.")
        self.update_settings(settings)

    def update_settings(self, settings: StationSettings) -> None:
        previous_resources = {
            name: getattr(self._settings, name).connection.resource
            for name in ("rigol", "keithley", "anritsu")
        }
        self._settings = settings
        for name, device in (
            ("rigol", settings.rigol),
            ("keithley", settings.keithley),
            ("anritsu", settings.anritsu),
        ):
            self.cards[name].update_resource(device.connection.resource, device.connection.visa_backend)
            if previous_resources.get(name) != device.connection.resource:
                self._verified_resources.pop(name, None)
                self._device_errors.pop(name, None)
        self._refresh_card_resource_choices()
        self._refresh_readiness()

    def set_assignment_allowed(self, allowed: bool) -> None:
        self._assignment_allowed = allowed
        self.save_assignments.setToolTip(
            "Persist selected VISA addresses. This changes the safety profile and revokes its approval."
            if allowed
            else "An engineer or service role is required to change VISA assignments."
        )
        for card in self.cards.values():
            card.set_assignment_allowed(allowed)
        if not allowed:
            self.save_assignments.setEnabled(False)

    def update_device_state(self, device: str, state: str) -> None:
        self._device_states[device] = state
        if state not in {"fault", "unknown", "compliance"}:
            self._device_errors.pop(device, None)
        self._refresh_readiness()

    def mark_identity_verified(self, device: str) -> None:
        resource = getattr(self._settings, device).connection.resource
        if resource:
            self._verified_resources[device] = resource
            self._device_errors.pop(device, None)
        self._refresh_readiness()

    def record_device_error(self, device: str, error: str) -> None:
        self._device_errors[device] = error
        self._refresh_readiness()

    def update_audit_health(self, healthy: bool) -> None:
        self._audit_healthy = healthy
        self._refresh_readiness()

    def update_plan_preflight(self, payload: object) -> None:
        if isinstance(payload, tuple) and len(payload) == 2:
            self._compiled_plan, self._plan_estimate = payload
        else:
            self._compiled_plan = None
            self._plan_estimate = None
        self._refresh_readiness()

    def _refresh_readiness(self) -> None:
        readiness = self.evaluate_readiness()
        icon = {
            ReadinessLevel.PASS: "✓",
            ReadinessLevel.WARNING: "△",
            ReadinessLevel.FAIL: "✕",
            ReadinessLevel.INFO: "•",
        }
        state = "READY" if readiness.ready else "BLOCKED"
        lines = [f"Station readiness — {state}"]
        lines.extend(
            f"{icon[item.level]} {item.label}: {item.detail}" for item in readiness.items
        )
        self.checklist.setText("\n".join(lines))

    def evaluate_readiness(
        self,
        plan: ExecutionPlan | None = None,
        estimate: PlanEstimate | None = None,
    ) -> StationReadiness:
        return evaluate_station_readiness(
            self._settings,
            device_states=self._device_states,
            verified_resources=self._verified_resources,
            audit_healthy=self._audit_healthy,
            device_errors=self._device_errors,
            plan=self._compiled_plan if plan is None else plan,
            estimate=self._plan_estimate if estimate is None else estimate,
        )

    def _scan_visa(self) -> None:
        if self._discovery_worker is not None and self._discovery_worker.isRunning():
            return
        backends = tuple(
            dict.fromkeys(
                (
                    self._settings.rigol.connection.visa_backend,
                    self._settings.keithley.connection.visa_backend,
                    self._settings.anritsu.connection.visa_backend,
                    "system",
                )
            )
        )
        self.scan_button.setEnabled(False)
        self.save_assignments.setEnabled(False)
        self.discovery_info.setText("Scanning VISA resources… only *IDN? will be sent.")
        self._discovery_worker = VisaDiscoveryWorker(backends, self)
        self._discovery_worker.completed.connect(self._scan_completed)
        self._discovery_worker.failed.connect(self._scan_failed)
        self._discovery_worker.finished.connect(lambda: self.scan_button.setEnabled(self._discovery_enabled))
        self._discovery_worker.start()

    def _scan_completed(self, payload: object) -> None:
        self._discovery_results = tuple(payload) if isinstance(payload, tuple) else ()
        self.discovery_table.setRowCount(0)
        usable = 0
        assignable = 0
        for result in self._discovery_results:
            row = self.discovery_table.rowCount()
            self.discovery_table.insertRow(row)
            assignment = QComboBox()
            assignment.addItem("Do not assign", None)
            assignment.addItem("Rigol", "rigol")
            assignment.addItem("Keithley", "keithley")
            assignment.addItem("Anritsu", "anritsu")
            if result.device:
                assignment.setCurrentIndex(assignment.findData(result.device))
            assignment.setEnabled(
                self._assignment_allowed and result.resource != "—" and result.idn is not None
            )
            self.discovery_table.setCellWidget(row, 0, assignment)
            assign_button = QPushButton("Assign")
            assign_button.setProperty("compact", True)
            assign_button.setEnabled(
                self._assignment_allowed and result.resource != "—" and result.idn is not None
            )
            assign_button.setToolTip(
                "Assign this VISA resource to the device selected in the first column and save it immediately."
            )
            assign_button.clicked.connect(
                lambda _checked=False, instrument=result, combo=assignment: self._emit_single_assignment(
                    instrument, combo
                )
            )
            self.discovery_table.setCellWidget(row, 1, assign_button)
            status = "Recognized" if result.device else ("Unknown" if result.idn else "Unavailable")
            self.discovery_table.setItem(row, 2, QTableWidgetItem(status))
            self.discovery_table.setItem(row, 3, QTableWidgetItem(result.resource))
            self.discovery_table.setItem(row, 4, QTableWidgetItem(result.backend))
            self.discovery_table.setItem(row, 5, QTableWidgetItem(result.idn or result.error or "No response"))
            if result.idn:
                usable += 1
            assigned_device = self._configured_device_for(result)
            if assigned_device is not None:
                self._set_row_assigned(row, assigned_device)
            elif result.idn:
                assignable += 1
        self.save_assignments.setEnabled(self._assignment_allowed and assignable > 0)
        self.discovery_info.setText(
            f"Scan complete: {usable} responding instrument(s), {assignable} available for assignment."
        )
        self.status.emit(f"VISA discovery completed: {usable} instrument(s) responded to *IDN?")
        self._refresh_card_resource_choices()

    def _scan_failed(self, error: str) -> None:
        self.discovery_info.setText(f"VISA scan failed: {error}")
        self.status.emit(f"VISA discovery failed: {error}")

    @staticmethod
    def _moke_network_default(settings: StationSettings) -> str:
        endpoint = settings.moke_box.endpoint
        if endpoint:
            host, separator, _port = endpoint.rpartition(":")
            octets = host.split(".") if separator else ()
            if len(octets) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in octets):
                return ".".join((*octets[:3], "0")) + "/24"
        return "192.168.0.0/24"

    def _scan_tcp(self) -> None:
        if self._tcp_discovery_worker is not None and self._tcp_discovery_worker.isRunning():
            return
        network = self.tcp_network.text().strip()
        range_end = self.tcp_range_end.text().strip() or None
        port = self.tcp_port.value()
        try:
            scan_is_non_private = (
                not ipaddress.IPv4Address(network).is_private
                if range_end
                else not ipaddress.ip_network(network, strict=False).is_private
            )
        except ValueError:
            scan_is_non_private = False
        allow_non_private = False
        if scan_is_non_private:
            scan_scope = f"{network}–{range_end}" if range_end else network
            answer = QMessageBox.warning(
                self,
                "Confirm TCP/IP scan",
                f"{scan_scope} is not an RFC1918 private range. It may still be your "
                "campus or company LAN, but scanning it makes TCP connection attempts "
                f"to every host on port {port}. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            allow_non_private = True
        self.tcp_scan_button.setEnabled(False)
        self.tcp_discovery_table.setRowCount(0)
        self.tcp_discovery_info.setText(
            f"Scanning {network}{f'–{range_end}' if range_end else ''} on TCP port {port}… "
            "no MOKE command will be sent."
        )
        self._tcp_discovery_worker = TcpDiscoveryWorker(
            network, port, range_end=range_end,
            allow_non_private=allow_non_private, parent=self
        )
        self._tcp_discovery_worker.completed.connect(self._tcp_scan_completed)
        self._tcp_discovery_worker.failed.connect(self._tcp_scan_failed)
        self._tcp_discovery_worker.finished.connect(
            lambda: self.tcp_scan_button.setEnabled(self._discovery_enabled)
        )
        self._tcp_discovery_worker.start()

    def _tcp_scan_completed(self, payload: object) -> None:
        results = tuple(payload) if isinstance(payload, tuple) else ()
        self.tcp_discovery_table.setRowCount(0)
        for endpoint in results:
            if not isinstance(endpoint, DiscoveredTcpEndpoint):
                continue
            row = self.tcp_discovery_table.rowCount()
            self.tcp_discovery_table.insertRow(row)
            self.tcp_discovery_table.setItem(row, 0, QTableWidgetItem(endpoint.endpoint))
            self.tcp_discovery_table.setItem(row, 1, QTableWidgetItem("TCP port open"))
        self.tcp_discovery_info.setText(
            f"TCP/IP scan complete: {self.tcp_discovery_table.rowCount()} host(s) accepted port {self.tcp_port.value()}."
        )
        self.status.emit(
            f"TCP/IP discovery completed: {self.tcp_discovery_table.rowCount()} host(s) accepted port {self.tcp_port.value()}"
        )

    def _tcp_scan_failed(self, error: str) -> None:
        self.tcp_discovery_info.setText(f"TCP/IP scan failed: {error}")
        self.status.emit(f"TCP/IP discovery failed: {error}")

    def _emit_assignments(self) -> None:
        assignments: dict[str, tuple[str, str, str]] = {}
        for row, result in enumerate(self._discovery_results):
            combo = self.discovery_table.cellWidget(row, 0)
            if not isinstance(combo, QComboBox) or combo.currentData() is None or result.idn is None:
                continue
            device = str(combo.currentData())
            if device in assignments:
                self.discovery_info.setText(f"Cannot save: more than one resource is assigned to {device.title()}.")
                return
            assignments[device] = (result.resource, result.backend, result.idn)
        if not assignments:
            self.discovery_info.setText("Select at least one responding instrument assignment.")
            return
        self.assignments_requested.emit(assignments)

    def _emit_single_assignment(self, result: DiscoveredInstrument, combo: QComboBox) -> None:
        device = combo.currentData()
        if device is None:
            self.discovery_info.setText("Select Rigol, Keithley or Anritsu before clicking Assign.")
            combo.setFocus()
            return
        if result.idn is None or result.resource == "—":
            self.discovery_info.setText("This VISA resource did not answer *IDN? and cannot be assigned.")
            return
        self.assignments_requested.emit(
            {str(device): (result.resource, result.backend, result.idn)}
        )

    def _card_assignment_requested(self, device: str, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 3:
            self.status.emit(f"VISA ASSIGN ERROR [{device}]: invalid resource payload from card")
            return
        resource, backend, idn = (str(value) for value in payload)
        self.status.emit(
            f"VISA ASSIGN CLICK [{device}]: resource={resource!r}, backend={backend!r}, IDN={idn!r}"
        )
        self.assignments_requested.emit({device: (resource, backend, idn)})

    def _refresh_card_resource_choices(self) -> None:
        if not hasattr(self, "cards"):
            return
        configured = {
            "rigol": self._settings.rigol.connection,
            "keithley": self._settings.keithley.connection,
            "anritsu": self._settings.anritsu.connection,
        }
        for device, card in self.cards.items():
            matches = tuple(
                result
                for result in self._discovery_results
                if result.device == device and result.idn is not None and result.resource != "—"
            )
            connection = configured[device]
            card.set_discovered_resources(
                matches,
                configured_resource=connection.resource,
                configured_backend=connection.visa_backend,
            )

    def mark_assignments_saved(self, assignments: dict[str, tuple[str, str, str]]) -> None:
        """Lock rows whose resource was successfully persisted by MainWindow."""

        for row, result in enumerate(self._discovery_results):
            for device, (resource, backend, _idn) in assignments.items():
                if result.resource == resource and result.backend == backend:
                    self._set_row_assigned(row, device)
        names = ", ".join(device.title() for device in sorted(assignments))
        self.discovery_info.setText(
            f"✓ Assignment saved for {names}. Connection cards above now use the selected VISA resources."
        )

    def _configured_device_for(self, result: DiscoveredInstrument) -> str | None:
        for name, device in (
            ("rigol", self._settings.rigol),
            ("keithley", self._settings.keithley),
            ("anritsu", self._settings.anritsu),
        ):
            if (
                device.connection.resource == result.resource
                and device.connection.visa_backend == result.backend
            ):
                return name
        return None

    def _set_row_assigned(self, row: int, device: str) -> None:
        badge = QLabel(f"✓ Assigned to {device.title()}")
        badge.setObjectName("assignmentConfirmed")
        badge.setAccessibleName(f"Assigned to {device.title()}")
        badge.setToolTip("This VISA resource is already saved. Change it in Settings or assign another resource.")
        self.discovery_table.setCellWidget(row, 0, badge)
        button = QPushButton("Assigned ✓")
        button.setObjectName("assignmentCompleteButton")
        button.setEnabled(False)
        button.setToolTip(f"Already assigned to {device.title()}; duplicate assignment is disabled.")
        self.discovery_table.setCellWidget(row, 1, button)
        status_item = self.discovery_table.item(row, 2)
        if status_item is not None:
            status_item.setText("Assigned")
