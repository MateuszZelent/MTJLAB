"""Station dashboard page independent of the application shell."""

from __future__ import annotations

import ipaddress

from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.contracts import DeviceModuleRegistry
from app.devices.discovery import (
    DiscoveredInstrument,
    DiscoveredTcpEndpoint,
    detect_local_ipv4_address,
    suggested_scan_cidr,
)
from app.devices.moke_box.protocol import readback_vout
from app.domain.readiness import ReadinessLevel, StationReadiness, evaluate_station_readiness
from app.engine.compiler import ExecutionPlan
from app.engine.estimation import PlanEstimate
from app.settings.models import StationSettings
from app.ui.dashboard.device_card import DeviceCard
from app.ui.discovery_worker import MokeIdentificationWorker, TcpDiscoveryWorker, VisaDiscoveryWorker


class _NoScrollTabBar(QObject):
    """Event filter that blocks wheel events on a QTabBar."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.Wheel:
            event.ignore()
            return True
        return super().eventFilter(watched, event)


class DashboardPage(QWidget):
    emergency_requested = Signal()
    assignments_requested = Signal(object)
    moke_assignment_requested = Signal(str)
    status = Signal(str)
    _DEVICE_KEYS = ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter")

    def __init__(
        self,
        settings: StationSettings,
        device_registry: DeviceModuleRegistry,
        parent: QWidget | None = None,
        *,
        discovery_enabled: bool = True,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._discovery_enabled = discovery_enabled
        self._discovery_worker: VisaDiscoveryWorker | None = None
        self._tcp_discovery_worker: TcpDiscoveryWorker | None = None
        self._moke_identification_worker: MokeIdentificationWorker | None = None
        self._identifying_host: str | None = None
        self._identifying_port: int | None = None
        self._tcp_rows_by_host: dict[str, int] = {}
        self._discovery_results: tuple[DiscoveredInstrument, ...] = ()
        self._device_states = {name: "disconnected" for name in self._DEVICE_KEYS}
        self._verified_resources: dict[str, str] = {}
        self._device_errors: dict[str, str] = {}
        self._audit_healthy = True
        self._assignment_allowed = True
        self._compiled_plan = None
        self._plan_estimate = None
        layout = QVBoxLayout(self)
        title = QLabel("Station overview")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Discover, identify and organize instruments from one calm workspace.")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        self.workspace = QTabWidget()
        self.workspace.setObjectName("dashboardWorkspace")
        self.workspace.setDocumentMode(True)
        self._ws_scroll_guard = _NoScrollTabBar(self.workspace)
        self.workspace.tabBar().installEventFilter(self._ws_scroll_guard)
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_intro = QLabel("Connected instruments")
        overview_intro.setObjectName("sectionTitle")
        overview_layout.addWidget(overview_intro)
        grid = QGridLayout()
        module_name = {
            module.key: module.display_name
            for module in device_registry.all_modules()
        }
        self.cards = {
            "rigol": DeviceCard(module_name["rigol"], settings.rigol.connection.resource),
            "keithley": DeviceCard(
                module_name["keithley"], settings.keithley.connection.resource
            ),
            "anritsu": DeviceCard(
                module_name["anritsu"], settings.anritsu.connection.resource
            ),
            "moke_box": DeviceCard(
                module_name["moke_box"], settings.moke_box.endpoint
            ),
            "lakeshore_gaussmeter": DeviceCard(
                module_name["lakeshore_gaussmeter"],
                settings.lakeshore_gaussmeter.resource,
            ),
        }
        for index, (device, card) in enumerate(self.cards.items()):
            grid.addWidget(card, index // 2, index % 2)
            if device == "moke_box":
                card.detected_resources.hide()
                card.assign_button.hide()
                card.assignment_hint.hide()
                card.resource.setToolTip("MOKE Box uses raw TCP/IP, not VISA.")
            else:
                card.assign_resource_requested.connect(
                    lambda payload, device=device: self._card_assignment_requested(device, payload)
                )
        overview_layout.addLayout(grid)
        self.checklist = QLabel()
        self.checklist.setObjectName("checklist")
        self.checklist.setWordWrap(True)
        overview_layout.addWidget(self.checklist)
        overview_layout.addStretch(1)
        self.workspace.addTab(overview, "Overview")

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
        self.discovery_table.verticalHeader().setDefaultSectionSize(40)
        header = self.discovery_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setMinimumSectionSize(130)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.discovery_table.setMinimumHeight(190)
        discovery_layout.addWidget(self.discovery_table, 1)
        self.workspace.addTab(discovery, "Find VISA")

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
        self.tcp_timeout_ms = QSpinBox()
        self.tcp_timeout_ms.setRange(50, 2_000)
        self.tcp_timeout_ms.setSingleStep(50)
        self.tcp_timeout_ms.setValue(150)
        self.tcp_timeout_ms.setSuffix(" ms")
        self.tcp_timeout_ms.setAccessibleName("TCP timeout per host")
        self.tcp_detect_button = QPushButton("Detect local IP")
        self.tcp_detect_button.setToolTip(
            "Detect the IPv4 address chosen by the active network route and prefill a /24 scan range."
        )
        self.tcp_identify_button = QPushButton("Test selected")
        self.tcp_identify_button.setEnabled(False)
        self.tcp_identify_button.setToolTip(
            "Test only the selected open endpoint and show the raw MOKE TX/RX exchange."
        )
        self.tcp_test_entered_button = QPushButton("Test entered IP")
        self.tcp_test_entered_button.setToolTip(
            "Test the single IPv4 address entered in CIDR / IP / from without scanning a subnet."
        )
        self.tcp_assign_moke_button = QPushButton("Assign MOKE Box")
        self.tcp_assign_moke_button.setEnabled(False)
        self.tcp_assign_moke_button.setToolTip(
            "Save the selected verified TCP endpoint as the read-only MOKE Box connection."
        )
        self.tcp_scan_button = QPushButton("Scan TCP/IP")
        self.tcp_scan_button.setToolTip(
            "Test one port on each host in the supplied private subnet. No MOKE command is sent."
        )
        self.tcp_stop_button = QPushButton("Stop scan")
        self.tcp_stop_button.setEnabled(False)
        self.tcp_stop_button.setToolTip("Stop scheduling further TCP connection attempts.")
        tcp_header.addWidget(QLabel("CIDR / IP / from:"))
        tcp_header.addWidget(self.tcp_network)
        tcp_header.addWidget(QLabel("to:"))
        tcp_header.addWidget(self.tcp_range_end)
        tcp_header.addWidget(QLabel("Port:"))
        tcp_header.addWidget(self.tcp_port)
        tcp_header.addWidget(QLabel("Timeout:"))
        tcp_header.addWidget(self.tcp_timeout_ms)
        tcp_header.addWidget(self.tcp_detect_button)
        tcp_header.addWidget(self.tcp_scan_button)
        tcp_header.addWidget(self.tcp_stop_button)
        tcp_header.addWidget(self.tcp_test_entered_button)
        tcp_header.addWidget(self.tcp_identify_button)
        tcp_header.addWidget(self.tcp_assign_moke_button)
        tcp_layout.addLayout(tcp_header)
        self.tcp_discovery_info = QLabel(
            "No TCP/IP scan performed. The reconstructed MOKE Box protocol uses TCP port 10001."
        )
        self.tcp_discovery_info.setObjectName("muted")
        self.tcp_discovery_info.setWordWrap(True)
        tcp_layout.addWidget(self.tcp_discovery_info)
        self.tcp_scan_progress = QProgressBar()
        self.tcp_scan_progress.setObjectName("tcpScanProgress")
        self.tcp_scan_progress.setFixedHeight(24)
        self.tcp_scan_progress.setTextVisible(True)
        self.tcp_scan_progress.setFormat("Ready")
        self.tcp_scan_progress.setValue(0)
        self.tcp_scan_progress.setStyleSheet(
            "QProgressBar#tcpScanProgress { min-height: 24px; color: #153a5b; "
            "background: #edf3f8; border: 1px solid #aac0d1; border-radius: 5px; "
            "text-align: center; font-weight: 600; } "
            "QProgressBar#tcpScanProgress::chunk { background: #2879b8; "
            "border-radius: 4px; margin: 1px; }"
        )
        self.tcp_scan_progress.hide()
        tcp_layout.addWidget(self.tcp_scan_progress)
        self.tcp_discovery_table = QTableWidget(0, 3)
        self.tcp_discovery_table.setHorizontalHeaderLabels(
            ["Open endpoint", "TCP result", "MOKE verification"]
        )
        self.tcp_discovery_table.setAlternatingRowColors(True)
        self.tcp_discovery_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tcp_discovery_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tcp_discovery_table.verticalHeader().setVisible(False)
        tcp_table_header = self.tcp_discovery_table.horizontalHeader()
        tcp_table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tcp_table_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        tcp_table_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tcp_discovery_table.setMinimumHeight(110)
        self.tcp_discovery_table.itemSelectionChanged.connect(self._update_tcp_identify_enabled)
        tcp_layout.addWidget(self.tcp_discovery_table)
        self.workspace.addTab(tcp_discovery, "Find TCP/IP")

        saved = QFrame()
        saved.setObjectName("discoveryCard")
        saved_layout = QVBoxLayout(saved)
        saved_title = QLabel("Saved instruments")
        saved_title.setObjectName("sectionTitle")
        saved_layout.addWidget(saved_title)
        saved_hint = QLabel("Configured resources are kept here for quick orientation. Connection controls live on each instrument page.")
        saved_hint.setObjectName("muted")
        saved_hint.setWordWrap(True)
        saved_layout.addWidget(saved_hint)
        self.saved_table = QTableWidget(0, 4)
        self.saved_table.setHorizontalHeaderLabels(["Instrument", "Resource", "Backend", "Status"])
        self.saved_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.saved_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.saved_table.verticalHeader().setVisible(False)
        self.saved_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        saved_layout.addWidget(self.saved_table)
        self.workspace.addTab(saved, "Saved")
        layout.addWidget(self.workspace, 1)
        emergency = QPushButton("E-STOP — disable all outputs")
        emergency.setObjectName("emergencyButton")
        emergency.setProperty("compact", True)
        emergency.setMaximumWidth(250)
        emergency_row = QHBoxLayout()
        emergency_row.addStretch(1)
        emergency_row.addWidget(emergency)
        layout.addLayout(emergency_row)
        emergency.clicked.connect(self.emergency_requested)
        self.scan_button.clicked.connect(self._scan_visa)
        self.tcp_detect_button.clicked.connect(self._detect_local_tcp_network)
        self.tcp_scan_button.clicked.connect(self._scan_tcp)
        self.tcp_stop_button.clicked.connect(self._stop_tcp_scan)
        self.tcp_identify_button.clicked.connect(self._identify_selected_moke)
        self.tcp_test_entered_button.clicked.connect(self._test_entered_moke_ip)
        self.tcp_assign_moke_button.clicked.connect(self._assign_selected_moke)
        self.save_assignments.clicked.connect(self._emit_assignments)
        if not discovery_enabled:
            self.scan_button.setEnabled(False)
            self.scan_button.setToolTip("VISA discovery is disabled in simulation mode.")
            self.tcp_scan_button.setEnabled(False)
            self.tcp_scan_button.setToolTip("TCP/IP discovery is disabled in simulation mode.")
            self.tcp_stop_button.setEnabled(False)
            self.tcp_identify_button.setEnabled(False)
            self.tcp_test_entered_button.setEnabled(False)
            self.tcp_assign_moke_button.setEnabled(False)
            self.tcp_detect_button.setEnabled(False)
        self.update_settings(settings)

    def update_settings(self, settings: StationSettings) -> None:
        previous_resources = {
            name: self._device_resource(self._settings, name)
            for name in self._DEVICE_KEYS
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
        self.cards["lakeshore_gaussmeter"].update_resource(
            settings.lakeshore_gaussmeter.resource,
            settings.lakeshore_gaussmeter.visa_backend,
        )
        if previous_resources.get("lakeshore_gaussmeter") != settings.lakeshore_gaussmeter.resource:
            self._verified_resources.pop("lakeshore_gaussmeter", None)
            self._device_errors.pop("lakeshore_gaussmeter", None)
        self.cards["moke_box"].update_resource(settings.moke_box.endpoint, "TCP/IP")
        if previous_resources.get("moke_box") != settings.moke_box.endpoint:
            self._verified_resources.pop("moke_box", None)
            self._device_errors.pop("moke_box", None)
        self._refresh_saved_devices()
        self._refresh_card_resource_choices()
        self._refresh_readiness()

    def _refresh_saved_devices(self) -> None:
        self.saved_table.setRowCount(0)
        for name in self._DEVICE_KEYS:
            device = getattr(self._settings, name)
            row = self.saved_table.rowCount()
            self.saved_table.insertRow(row)
            if name == "moke_box":
                resource = device.endpoint or "Not assigned"
                backend = "TCP/IP"
            else:
                if name == "lakeshore_gaussmeter":
                    resource = device.resource or "Not assigned"
                    backend = device.visa_backend
                else:
                    resource = device.connection.resource or "Not assigned"
                    backend = device.connection.visa_backend
            values = (
                device.display_name,
                resource,
                backend,
                self._device_states.get(name, "disconnected").replace("_", " ").title(),
            )
            for column, value in enumerate(values):
                self.saved_table.setItem(row, column, QTableWidgetItem(value))

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
        self._refresh_saved_devices()

    def mark_identity_verified(self, device: str) -> None:
        resource = self._device_resource(self._settings, device)
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
            assignment.setMinimumWidth(130)
            self.discovery_table.setCellWidget(row, 0, assignment)
            assign_button = QPushButton("Assign →")
            assign_button.setObjectName("assignButton")
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
        timeout_s = self.tcp_timeout_ms.value() / 1_000
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
        self.tcp_test_entered_button.setEnabled(False)
        self.tcp_stop_button.setEnabled(True)
        self.tcp_discovery_table.setRowCount(0)
        self._tcp_rows_by_host.clear()
        self.tcp_identify_button.setEnabled(False)
        self.tcp_assign_moke_button.setEnabled(False)
        total_hosts = self._tcp_scan_host_count(network, range_end)
        self.tcp_scan_progress.show()
        self.tcp_scan_progress.setRange(0, max(total_hosts or 1, 1))
        self.tcp_scan_progress.setValue(0)
        self.tcp_scan_progress.setFormat(
            "%v / %m hosts — %p%" if total_hosts else "Validating scan range…"
        )
        self.tcp_discovery_info.setText(
            f"Scanning {network}{f'–{range_end}' if range_end else ''} on TCP port {port}… "
            "no MOKE command will be sent. Select an open endpoint to identify it."
        )
        self._tcp_discovery_worker = TcpDiscoveryWorker(
            network, port, range_end=range_end,
            timeout_s=timeout_s, allow_non_private=allow_non_private,
            verify_moke=False, parent=self
        )
        self._tcp_discovery_worker.completed.connect(self._tcp_scan_completed)
        self._tcp_discovery_worker.failed.connect(self._tcp_scan_failed)
        self._tcp_discovery_worker.cancelled.connect(self._tcp_scan_cancelled)
        self._tcp_discovery_worker.progress.connect(self._tcp_scan_progressed)
        self._tcp_discovery_worker.host_activity.connect(self._tcp_host_activity)
        self._tcp_discovery_worker.finished.connect(self._tcp_scan_finished)
        self._tcp_discovery_worker.start()

    def _stop_tcp_scan(self) -> None:
        worker = self._tcp_discovery_worker
        if worker is None or not worker.isRunning():
            return
        worker.request_stop()
        self.tcp_stop_button.setEnabled(False)
        self.tcp_discovery_info.setText(
            "Stopping TCP/IP scan; active connection attempts will finish within the selected timeout."
        )

    def _tcp_scan_finished(self) -> None:
        self.tcp_scan_button.setEnabled(self._discovery_enabled)
        self.tcp_test_entered_button.setEnabled(self._discovery_enabled)
        self.tcp_stop_button.setEnabled(False)

    def _tcp_scan_progressed(self, completed: int, total: int, host: str) -> None:
        self.tcp_scan_progress.setRange(0, total)
        self.tcp_scan_progress.setValue(completed)
        self.tcp_scan_progress.setFormat("%v / %m hosts — %p%")

    @staticmethod
    def _tcp_scan_host_count(network: str, range_end: str | None) -> int | None:
        try:
            if range_end:
                return int(ipaddress.IPv4Address(range_end)) - int(ipaddress.IPv4Address(network)) + 1
            subnet = ipaddress.ip_network(network, strict=False)
            return (
                int(subnet.num_addresses)
                if subnet.prefixlen >= 31
                else int(subnet.num_addresses) - 2
            )
        except ValueError:
            return None

    def _tcp_host_activity(self, host: str, state: str, verification: str) -> None:
        row = self._tcp_rows_by_host.get(host)
        if row is None:
            row = self.tcp_discovery_table.rowCount()
            self._tcp_rows_by_host[host] = row
            self.tcp_discovery_table.insertRow(row)
            self.tcp_discovery_table.setItem(
                row, 0, QTableWidgetItem(f"{host}:{self.tcp_port.value()}")
            )
        label = {
            "scanning": "Scanning…",
            "closed": "Closed",
            "open": "TCP port open",
            "cancelled": "Cancelled",
        }[state]
        self.tcp_discovery_table.setItem(row, 1, QTableWidgetItem(label))
        self.tcp_discovery_table.setItem(
            row,
            2,
            QTableWidgetItem(verification or ("Pending…" if state == "scanning" else "—")),
        )

    def _update_tcp_identify_enabled(self) -> None:
        selected = self.tcp_discovery_table.selectedItems()
        enabled = False
        if selected:
            row = selected[0].row()
            state = self.tcp_discovery_table.item(row, 1)
            enabled = state is not None and state.text() in {"TCP port open", "Entered manually"}
        self.tcp_identify_button.setEnabled(enabled and self._discovery_enabled)
        verified = False
        if selected:
            row = selected[0].row()
            verification = self.tcp_discovery_table.item(row, 2)
            verified = verification is not None and verification.text() == "MOKE Box verified"
        self.tcp_assign_moke_button.setEnabled(verified and self._discovery_enabled)

    def _assign_selected_moke(self) -> None:
        selected = self.tcp_discovery_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        endpoint = self.tcp_discovery_table.item(row, 0)
        verification = self.tcp_discovery_table.item(row, 2)
        if (
            endpoint is None
            or verification is None
            or verification.text() != "MOKE Box verified"
        ):
            return
        self.moke_assignment_requested.emit(endpoint.text())

    def _identify_selected_moke(self) -> None:
        if (
            self._moke_identification_worker is not None
            and self._moke_identification_worker.isRunning()
        ):
            return
        selected = self.tcp_discovery_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        endpoint_item = self.tcp_discovery_table.item(row, 0)
        state_item = self.tcp_discovery_table.item(row, 1)
        if (
            endpoint_item is None
            or state_item is None
            or state_item.text() not in {"TCP port open", "Entered manually"}
        ):
            return
        host, separator, port_text = endpoint_item.text().rpartition(":")
        if not separator:
            return
        try:
            port = int(port_text)
        except ValueError:
            return
        answer = QMessageBox.warning(
            self,
            "Confirm MOKE read-only identification",
            "Identification sends only the documented Readback VOUT frame (18 00 00 18); "
            "it does not set or ramp any output. However, the reconstructed documentation "
            "requires a single TCP client. Ensure LabVIEW and every other MOKE controller "
            "are disconnected before continuing.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._identifying_host = host
        self._identifying_port = port
        self.tcp_identify_button.setEnabled(False)
        self.tcp_test_entered_button.setEnabled(False)
        self.tcp_discovery_table.setItem(row, 2, QTableWidgetItem("Verifying MOKE…"))
        self._moke_identification_worker = MokeIdentificationWorker(
            host, port, max(0.2, self.tcp_timeout_ms.value() / 1_000), self
        )
        self._moke_identification_worker.completed.connect(self._moke_identification_completed)
        self._moke_identification_worker.failed.connect(self._moke_identification_failed)
        self._moke_identification_worker.finished.connect(self._moke_identification_finished)
        self._moke_identification_worker.start()

    def _moke_identification_finished(self) -> None:
        self.tcp_test_entered_button.setEnabled(self._discovery_enabled)
        self._update_tcp_identify_enabled()

    def _test_entered_moke_ip(self) -> None:
        value = self.tcp_network.text().strip()
        try:
            if "/" in value:
                network = ipaddress.ip_network(value, strict=False)
                if network.prefixlen != 32:
                    raise ValueError("enter one IP address or a /32 network")
                host = str(network.network_address)
            else:
                host = str(ipaddress.IPv4Address(value))
        except ValueError as exc:
            self.tcp_discovery_info.setText(f"Cannot test entered MOKE IP: {exc}.")
            return
        row = self._tcp_rows_by_host.get(host)
        if row is None:
            row = self.tcp_discovery_table.rowCount()
            self._tcp_rows_by_host[host] = row
            self.tcp_discovery_table.insertRow(row)
        self.tcp_discovery_table.setItem(
            row, 0, QTableWidgetItem(f"{host}:{self.tcp_port.value()}")
        )
        self.tcp_discovery_table.setItem(row, 1, QTableWidgetItem("Entered manually"))
        self.tcp_discovery_table.setItem(row, 2, QTableWidgetItem("Pending test"))
        self.tcp_discovery_table.selectRow(row)
        self._identify_selected_moke()

    def _moke_identification_completed(self, payload: object) -> None:
        if not isinstance(payload, DiscoveredTcpEndpoint):
            return
        self._tcp_host_activity(
            payload.host,
            "open",
            "MOKE Box verified" if payload.moke_verified else payload.verification_detail or "Not MOKE",
        )
        self.status.emit(f"MOKE identification completed: {payload.endpoint}")
        self._update_tcp_identify_enabled()
        self._show_moke_test_trace(
            payload.endpoint,
            payload.moke_verified is True,
            payload.verification_detail or "No verification detail.",
            payload.tx_bytes,
            payload.rx_bytes,
        )

    def _moke_identification_failed(self, error: str) -> None:
        if self._identifying_host is not None:
            self._tcp_host_activity(self._identifying_host, "closed", f"Test failed: {error}")
        self.status.emit(f"MOKE identification failed: {error}")
        self._update_tcp_identify_enabled()
        endpoint = (
            f"{self._identifying_host}:{self._identifying_port}"
            if self._identifying_host is not None and self._identifying_port is not None
            else "unknown endpoint"
        )
        self._show_moke_test_trace(endpoint, False, error, readback_vout(), b"")

    @staticmethod
    def _format_protocol_bytes(payload: bytes) -> str:
        if not payload:
            return "<no bytes>"
        lines = []
        # MOKE commands and response records are four-byte frames, so one
        # frame per line makes captures directly comparable with the protocol.
        for offset in range(0, len(payload), 4):
            chunk = payload[offset:offset + 4]
            lines.append(f"{offset:04X}  " + " ".join(f"{value:02X}" for value in chunk))
        return "\n".join(lines)

    def _show_moke_test_trace(
        self,
        endpoint: str,
        verified: bool,
        detail: str,
        tx_bytes: bytes,
        rx_bytes: bytes,
    ) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"MOKE protocol test — {endpoint}")
        dialog.setMinimumSize(680, 460)
        layout = QVBoxLayout(dialog)

        result = QLabel("MOKE Box verified" if verified else "MOKE verification failed")
        result.setObjectName("sectionTitle")
        layout.addWidget(result)
        detail_label = QLabel(detail)
        detail_label.setWordWrap(True)
        layout.addWidget(detail_label)

        trace = QPlainTextEdit()
        trace.setReadOnly(True)
        trace.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        trace.setAccessibleName("MOKE TCP transmit and receive trace")
        trace.setStyleSheet(
            "QPlainTextEdit { font-family: Consolas, 'Courier New', monospace; "
            "font-size: 11pt; background: #101820; color: #e7f1f8; "
            "border: 1px solid #5d7485; border-radius: 5px; padding: 8px; }"
        )
        trace.setPlainText(
            f"Endpoint: {endpoint}\n"
            f"Result: {'VERIFIED' if verified else 'FAILED'}\n\n"
            f"TX → MOKE ({len(tx_bytes)} bytes)\n"
            f"{self._format_protocol_bytes(tx_bytes)}\n\n"
            f"RX ← MOKE ({len(rx_bytes)} bytes)\n"
            f"{self._format_protocol_bytes(rx_bytes)}"
        )
        layout.addWidget(trace, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _detect_local_tcp_network(self) -> None:
        try:
            address = detect_local_ipv4_address()
            cidr = suggested_scan_cidr(address)
        except (OSError, ValueError) as exc:
            self.tcp_discovery_info.setText(f"Could not detect local IPv4 address: {exc}")
            return
        self.tcp_network.setText(cidr)
        self.tcp_range_end.clear()
        self.tcp_discovery_info.setText(
            f"Detected local IPv4 address {address}; suggested scan network: {cidr}."
        )

    def _tcp_scan_completed(self, payload: object) -> None:
        results = tuple(payload) if isinstance(payload, tuple) else ()
        for endpoint in results:
            if not isinstance(endpoint, DiscoveredTcpEndpoint):
                continue
            self._tcp_host_activity(
                endpoint.host,
                "open",
                "MOKE Box verified"
                if endpoint.moke_verified
                else endpoint.verification_detail or "Not requested",
            )
        self.tcp_discovery_info.setText(
            f"TCP/IP scan complete: {len(results)} host(s) accepted port {self.tcp_port.value()}."
        )
        self.tcp_scan_progress.setFormat("Scan complete — %v / %m")
        self.status.emit(
            f"TCP/IP discovery completed: {self.tcp_discovery_table.rowCount()} host(s) accepted port {self.tcp_port.value()}"
        )

    def _tcp_scan_failed(self, error: str) -> None:
        self.tcp_discovery_info.setText(f"TCP/IP scan failed: {error}")
        self.tcp_scan_progress.setFormat("Scan failed")
        self.status.emit(f"TCP/IP discovery failed: {error}")

    def _tcp_scan_cancelled(self) -> None:
        self.tcp_discovery_info.setText("TCP/IP scan stopped by the user.")
        self.tcp_scan_progress.setFormat("Scan stopped — %v / %m")
        self.status.emit("TCP/IP discovery stopped by the user")

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
        for device in ("rigol", "keithley", "anritsu"):
            card = self.cards[device]
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

    @staticmethod
    def _device_resource(settings: StationSettings, device: str) -> str | None:
        if device == "moke_box":
            return settings.moke_box.endpoint
        if device == "lakeshore_gaussmeter":
            return settings.lakeshore_gaussmeter.resource
        return getattr(settings, device).connection.resource

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
