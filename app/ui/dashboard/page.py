"""Station dashboard page independent of the application shell."""

from __future__ import annotations

import ipaddress

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
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
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    CaptionLabel,
    FluentIcon,
    IconWidget,
    IndeterminateProgressBar,
    InfoBar,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SegmentedWidget,
    SimpleCardWidget,
    SpinBox,
    StrongBodyLabel,
    SubtitleLabel,
    TitleLabel,
)

from app.ui.dashboard.device_card import DeviceCard
from app.ui.dashboard.discovery_surfaces import SavedInstrumentsView, TcpDiscoveryResultsView
from app.ui.dashboard.visa_results import VisaResultState, VisaResultsView
from app.ui.discovery_worker import MokeIdentificationWorker, TcpDiscoveryWorker, VisaDiscoveryWorker
from app.ui.dialogs import StationDialog


class DashboardPage(QWidget):
    emergency_requested = Signal()
    assignments_requested = Signal(object)
    moke_assignment_requested = Signal(str)
    readiness_changed = Signal(bool)
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
        self._discovery_results: tuple[DiscoveredInstrument, ...] = ()
        self._device_states = {name: "disconnected" for name in self._DEVICE_KEYS}
        self._verified_resources: dict[str, str] = {}
        self._device_errors: dict[str, str] = {}
        self._audit_healthy = True
        self._assignment_allowed = True
        self._compiled_plan = None
        self._plan_estimate = None
        self.overview_page = QWidget(self)
        self.overview_page.setProperty("stationSurface", "page")
        overview_layout = QVBoxLayout(self.overview_page)
        overview_layout.setContentsMargins(0, 0, 0, 0)
        overview_layout.setSpacing(12)
        overview_layout.addWidget(SubtitleLabel("Station overview", self.overview_page))
        overview_subtitle = BodyLabel(
            "Discover, identify and organize instruments from one calm workspace.",
            self.overview_page,
        )
        overview_subtitle.setWordWrap(True)
        overview_layout.addWidget(overview_subtitle)
        overview_intro = SubtitleLabel("Connected instruments", self.overview_page)
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
        self.discovery_page = QWidget(self)
        self.discovery_page.setProperty("stationSurface", "page")
        discovery_layout = QVBoxLayout(self.discovery_page)
        discovery_layout.setContentsMargins(24, 20, 24, 24)
        discovery_layout.setSpacing(16)
        discovery_header = QHBoxLayout()
        discovery_header.setSpacing(16)
        discovery_heading = QVBoxLayout()
        discovery_heading.setSpacing(4)
        discovery_title = TitleLabel("Instrument discovery", self.discovery_page)
        discovery_title.setObjectName("pageTitle")
        discovery_heading.addWidget(discovery_title)
        discovery_subtitle = BodyLabel(
            "Find, identify and assign station instruments from one controlled workspace.",
            self.discovery_page,
        )
        discovery_subtitle.setWordWrap(True)
        discovery_heading.addWidget(discovery_subtitle)
        discovery_header.addLayout(discovery_heading, 1)
        discovery_layout.addLayout(discovery_header)

        self.discovery_safety_card = SimpleCardWidget(self.discovery_page)
        self.discovery_safety_card.setProperty("stationSurface", "raised")
        safety_layout = QHBoxLayout(self.discovery_safety_card)
        safety_layout.setContentsMargins(16, 12, 16, 12)
        safety_layout.setSpacing(12)
        safety_icon = IconWidget(FluentIcon.INFO, self.discovery_safety_card)
        safety_icon.setFixedSize(22, 22)
        safety_layout.addWidget(safety_icon, 0, Qt.AlignmentFlag.AlignTop)
        safety_copy = QVBoxLayout()
        safety_copy.setSpacing(2)
        safety_copy.addWidget(StrongBodyLabel("Read-only discovery", self.discovery_safety_card))
        safety_hint = CaptionLabel(
            "Scans never enable outputs. Saving an assignment is a separate, deliberate action and revokes safety-profile approval.",
            self.discovery_safety_card,
        )
        safety_hint.setWordWrap(True)
        safety_copy.addWidget(safety_hint)
        safety_layout.addLayout(safety_copy, 1)
        discovery_layout.addWidget(self.discovery_safety_card)

        self.discovery_workspace = SimpleCardWidget(self.discovery_page)
        self.discovery_workspace.setProperty("stationSurface", "surface")
        workspace_layout = QVBoxLayout(self.discovery_workspace)
        workspace_layout.setContentsMargins(20, 16, 20, 20)
        workspace_layout.setSpacing(16)
        self.discovery_pivot = SegmentedWidget(self.discovery_workspace)
        self.discovery_pivot.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        workspace_layout.addWidget(self.discovery_pivot, 0, Qt.AlignmentFlag.AlignLeft)
        self.discovery_stack = QStackedWidget(self.discovery_page)
        self.discovery_stack.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Expanding,
        )
        workspace_layout.addWidget(self.discovery_stack, 1)
        discovery_layout.addWidget(self.discovery_workspace, 1)

        self.visa_discovery_page = QWidget(self.discovery_stack)
        visa_layout = QVBoxLayout(self.visa_discovery_page)
        visa_layout.setContentsMargins(0, 0, 0, 0)
        visa_layout.setSpacing(12)
        discovery_header = QHBoxLayout()
        discovery_title = StrongBodyLabel("VISA instruments", self.visa_discovery_page)
        discovery_header.addWidget(discovery_title)
        discovery_header.addStretch(1)
        self.scan_button = PrimaryPushButton("Scan VISA", self.visa_discovery_page)
        self.scan_button.setToolTip(
            "Enumerate VISA resources and send only *IDN? with a short timeout. No output is enabled."
        )
        self.scan_button.setAccessibleName("Scan VISA instruments")
        self.save_assignments = PushButton("Save assignments", self.visa_discovery_page)
        self.save_assignments.setEnabled(False)
        self.save_assignments.setToolTip(
            "Persist selected VISA addresses. This changes the safety profile and revokes its approval."
        )
        discovery_header.addWidget(self.scan_button)
        discovery_header.addWidget(self.save_assignments)
        visa_layout.addLayout(discovery_header)
        self.discovery_info = CaptionLabel(
            "No scan performed. USB/GPIB resources are normally discoverable; LAN discovery depends on the VISA backend.",
            self.visa_discovery_page,
        )
        self.discovery_info.setWordWrap(True)
        visa_layout.addWidget(self.discovery_info)
        self.visa_progress = IndeterminateProgressBar(self.visa_discovery_page)
        self.visa_progress.setFixedHeight(4)
        self.visa_progress.hide()
        visa_layout.addWidget(self.visa_progress)
        self.visa_results = VisaResultsView(
            assignment_allowed=self._assignment_allowed,
            parent=self.visa_discovery_page,
        )
        self.visa_results.setMinimumHeight(220)
        visa_layout.addWidget(self.visa_results, 1)
        self.visa_state = "empty"
        self.discovery_stack.addWidget(self.visa_discovery_page)

        self.tcp_discovery_page = QWidget(self.discovery_stack)
        self.tcp_discovery_page.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Expanding,
        )
        tcp_layout = QVBoxLayout(self.tcp_discovery_page)
        tcp_layout.setContentsMargins(0, 0, 0, 0)
        tcp_header = QHBoxLayout()
        tcp_title = StrongBodyLabel("TCP/IP endpoints", self.tcp_discovery_page)
        tcp_header.addWidget(tcp_title)
        tcp_header.addStretch(1)
        self.tcp_network = LineEdit(self.tcp_discovery_page)
        self.tcp_network.setText(self._moke_network_default(settings))
        self.tcp_network.setPlaceholderText("192.168.1.0/24 or start IP")
        self.tcp_network.setAccessibleName("Network CIDR or first IP for TCP port scan")
        self.tcp_network.setMaximumWidth(170)
        self.tcp_range_end = LineEdit(self.tcp_discovery_page)
        self.tcp_range_end.setPlaceholderText("optional end IP")
        self.tcp_range_end.setAccessibleName("Last IP for TCP port scan range")
        self.tcp_range_end.setMaximumWidth(135)
        self.tcp_port = SpinBox(self.tcp_discovery_page)
        self.tcp_port.setRange(1, 65_535)
        self.tcp_port.setValue(10_001)
        self.tcp_port.setAccessibleName("TCP port for MOKE Box discovery")
        self.tcp_timeout_ms = SpinBox(self.tcp_discovery_page)
        self.tcp_timeout_ms.setRange(50, 2_000)
        self.tcp_timeout_ms.setSingleStep(50)
        self.tcp_timeout_ms.setValue(150)
        self.tcp_timeout_ms.setSuffix(" ms")
        self.tcp_timeout_ms.setAccessibleName("TCP timeout per host")
        self.tcp_detect_button = PushButton("Detect local IP")
        self.tcp_detect_button.setToolTip(
            "Detect the IPv4 address chosen by the active network route and prefill a /24 scan range."
        )
        self.tcp_identify_button = PushButton("Test selected")
        self.tcp_identify_button.setEnabled(False)
        self.tcp_identify_button.setToolTip(
            "Test only the selected open endpoint and show the raw MOKE TX/RX exchange."
        )
        self.tcp_test_entered_button = PushButton("Test entered IP")
        self.tcp_test_entered_button.setToolTip(
            "Test the single IPv4 address entered in CIDR / IP / from without scanning a subnet."
        )
        self.tcp_assign_moke_button = PrimaryPushButton("Assign MOKE Box")
        self.tcp_assign_moke_button.setEnabled(False)
        self.tcp_assign_moke_button.setToolTip(
            "Save the selected verified TCP endpoint as the read-only MOKE Box connection."
        )
        self.tcp_scan_button = PrimaryPushButton("Scan TCP/IP")
        self.tcp_scan_button.setToolTip(
            "Test one port on each host in the supplied private subnet. No MOKE command is sent."
        )
        self.tcp_stop_button = PushButton("Stop scan")
        self.tcp_stop_button.setEnabled(False)
        self.tcp_stop_button.setToolTip("Stop scheduling further TCP connection attempts.")
        tcp_layout.addLayout(tcp_header)
        self.tcp_controls = QGridLayout()
        self.tcp_controls.setHorizontalSpacing(8)
        self.tcp_controls.setVerticalSpacing(8)
        self.tcp_network_label = BodyLabel("CIDR / IP / from", self.tcp_discovery_page)
        self.tcp_range_label = BodyLabel("To", self.tcp_discovery_page)
        self.tcp_port_label = BodyLabel("Port", self.tcp_discovery_page)
        self.tcp_timeout_label = BodyLabel("Timeout", self.tcp_discovery_page)
        tcp_layout.addLayout(self.tcp_controls)
        self._tcp_controls_compact: bool | None = None
        # Start from the narrow-safe arrangement so the wide grid cannot set a
        # minimum page width before the Fluent host receives its geometry.
        self._layout_tcp_controls(compact=True)
        self.tcp_discovery_info = CaptionLabel(
            "No TCP/IP scan performed. The reconstructed MOKE Box protocol uses TCP port 10001.",
            self.tcp_discovery_page,
        )
        self.tcp_discovery_info.setWordWrap(True)
        tcp_layout.addWidget(self.tcp_discovery_info)
        self.tcp_scan_progress = ProgressBar(self.tcp_discovery_page)
        self.tcp_scan_progress.setFixedHeight(6)
        self.tcp_scan_progress.setFormat("Ready")
        self.tcp_scan_progress.setValue(0)
        self.tcp_scan_progress.hide()
        tcp_layout.addWidget(self.tcp_scan_progress)
        self.tcp_results = TcpDiscoveryResultsView(self.tcp_discovery_page)
        self.tcp_results.setMinimumHeight(180)
        self.tcp_results.selection_changed.connect(self._update_tcp_identify_enabled)
        tcp_layout.addWidget(self.tcp_results, 1)
        self.discovery_stack.addWidget(self.tcp_discovery_page)

        self.saved_page = QWidget(self.discovery_stack)
        saved_layout = QVBoxLayout(self.saved_page)
        saved_layout.setContentsMargins(0, 0, 0, 0)
        saved_title = StrongBodyLabel("Saved instruments", self.saved_page)
        saved_layout.addWidget(saved_title)
        saved_hint = BodyLabel(
            "Configured resources are kept here for quick orientation. Connection controls live on each instrument page.",
            self.saved_page,
        )
        saved_hint.setWordWrap(True)
        saved_layout.addWidget(saved_hint)
        self.saved_instruments = SavedInstrumentsView(self.saved_page)
        self.saved_instruments.setMinimumHeight(180)
        saved_layout.addWidget(self.saved_instruments, 1)
        self.discovery_stack.addWidget(self.saved_page)
        self.discovery_pivot.addItem("visa", "Find VISA", lambda: self._show_discovery_page("visa"))
        self.discovery_pivot.addItem("tcp", "Find TCP/IP", lambda: self._show_discovery_page("tcp"))
        self.discovery_pivot.addItem("saved", "Saved", lambda: self._show_discovery_page("saved"))
        self._show_discovery_page("visa")
        self.navigation_pages = {
            "overview": self.overview_page,
            "discovery": self.discovery_page,
        }
        # The persistent Fluent safety strip owns the emergency action.
        self.scan_button.clicked.connect(self._scan_visa)
        self.visa_results.assignment_requested.connect(self.assignments_requested)
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

    def _show_discovery_page(self, route: str) -> None:
        pages = {
            "visa": self.visa_discovery_page,
            "tcp": self.tcp_discovery_page,
            "saved": self.saved_page,
        }
        self.discovery_stack.setCurrentWidget(pages[route])
        self.discovery_pivot.setCurrentItem(route)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._layout_tcp_controls(compact=event.size().width() < 980)

    def _layout_tcp_controls(self, *, compact: bool) -> None:
        if getattr(self, "_tcp_controls_compact", None) == compact:
            return
        self._tcp_controls_compact = compact
        controls = (
            self.tcp_network_label,
            self.tcp_network,
            self.tcp_range_label,
            self.tcp_range_end,
            self.tcp_port_label,
            self.tcp_port,
            self.tcp_timeout_label,
            self.tcp_timeout_ms,
            self.tcp_detect_button,
            self.tcp_scan_button,
            self.tcp_stop_button,
            self.tcp_test_entered_button,
            self.tcp_identify_button,
            self.tcp_assign_moke_button,
        )
        for control in controls:
            self.tcp_controls.removeWidget(control)
        if compact:
            fields = (
                (self.tcp_network_label, self.tcp_network),
                (self.tcp_range_label, self.tcp_range_end),
                (self.tcp_port_label, self.tcp_port),
                (self.tcp_timeout_label, self.tcp_timeout_ms),
            )
            for row, (label, field) in enumerate(fields):
                self.tcp_controls.addWidget(label, row, 0)
                self.tcp_controls.addWidget(field, row, 1, 1, 3)
            for field in (self.tcp_network, self.tcp_range_end):
                field.setMaximumWidth(16777215)
            for index, button in enumerate((
                self.tcp_detect_button,
                self.tcp_scan_button,
                self.tcp_stop_button,
                self.tcp_test_entered_button,
                self.tcp_identify_button,
                self.tcp_assign_moke_button,
            )):
                self.tcp_controls.addWidget(button, 4 + index // 2, index % 2 * 2, 1, 2)
            self.tcp_controls.setColumnStretch(1, 1)
            self.tcp_controls.setColumnStretch(3, 1)
        else:
            for column, (label, field) in enumerate((
                (self.tcp_network_label, self.tcp_network),
                (self.tcp_range_label, self.tcp_range_end),
                (self.tcp_port_label, self.tcp_port),
                (self.tcp_timeout_label, self.tcp_timeout_ms),
            )):
                self.tcp_controls.addWidget(label, 0, column * 2)
                self.tcp_controls.addWidget(field, 0, column * 2 + 1)
            self.tcp_network.setMaximumWidth(170)
            self.tcp_range_end.setMaximumWidth(135)
            self.tcp_controls.addWidget(self.tcp_detect_button, 1, 0, 1, 2)
            self.tcp_controls.addWidget(self.tcp_scan_button, 1, 2, 1, 2)
            self.tcp_controls.addWidget(self.tcp_stop_button, 1, 4)
            self.tcp_controls.addWidget(self.tcp_test_entered_button, 1, 5)
            self.tcp_controls.addWidget(self.tcp_identify_button, 1, 6)
            self.tcp_controls.addWidget(self.tcp_assign_moke_button, 1, 7)
            self.tcp_controls.setColumnStretch(1, 3)
            self.tcp_controls.setColumnStretch(3, 2)

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
        self._refresh_visa_results()
        self._refresh_readiness()

    def _refresh_saved_devices(self) -> None:
        saved_devices: list[tuple[str, str, str, str]] = []
        for name in self._DEVICE_KEYS:
            device = getattr(self._settings, name)
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
            saved_devices.append((
                device.display_name,
                resource,
                backend,
                self._device_states.get(name, "disconnected").replace("_", " ").title(),
            ))
        self.saved_instruments.set_instruments(saved_devices)

    def set_assignment_allowed(self, allowed: bool) -> None:
        self._assignment_allowed = allowed
        self.visa_results.set_assignment_allowed(allowed)
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
        self.readiness_changed.emit(readiness.ready)

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
        self.visa_state = "scanning"
        self.visa_progress.show()
        self.visa_progress.start()
        self.discovery_info.setText("Scanning VISA resources… only *IDN? will be sent.")
        self._discovery_worker = VisaDiscoveryWorker(
            backends,
            self,
            preferred_lakeshore_baud=self._settings.lakeshore_gaussmeter.baud_rate,
        )
        self._discovery_worker.completed.connect(self._scan_completed)
        self._discovery_worker.failed.connect(self._scan_failed)
        self._discovery_worker.finished.connect(lambda: self.scan_button.setEnabled(self._discovery_enabled))
        self._discovery_worker.start()

    def _scan_completed(self, payload: object) -> None:
        self._discovery_results = tuple(payload) if isinstance(payload, tuple) else ()
        states = tuple(
            VisaResultState.from_result(
                result,
                configured_device=self._configured_device_for(result),
            )
            for result in self._discovery_results
        )
        self.visa_results.set_results(states)
        self.visa_state = "success" if states else "empty"
        self.visa_progress.stop()
        self.visa_progress.hide()
        usable = sum(result.idn is not None for result in self._discovery_results)
        assignable = sum(state.status in {"recognized", "unknown"} for state in states)
        self.save_assignments.setEnabled(self._assignment_allowed and assignable > 0)
        self.discovery_info.setText(
            f"Scan complete: {usable} responding instrument(s), {assignable} available for assignment."
        )
        self.status.emit(f"VISA discovery completed: {usable} instrument(s) responded to *IDN?")
        self._refresh_card_resource_choices()

    def _scan_failed(self, error: str) -> None:
        self.visa_state = "failed"
        self.visa_progress.stop()
        self.visa_progress.hide()
        self.discovery_info.setText(f"VISA scan failed: {error}")
        InfoBar.error(
            title="VISA scan failed",
            content=error,
            parent=self.visa_discovery_page,
            duration=4_000,
        )
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
        self.tcp_results.clear()
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
        self.tcp_results.upsert_endpoint(
            host=host,
            endpoint=f"{host}:{self.tcp_port.value()}",
            state=state,
            verification=verification or ("Pending…" if state == "scanning" else "—"),
        )

    def _update_tcp_identify_enabled(self) -> None:
        state = self.tcp_results.selected_state
        enabled = state in {"open", "entered"}
        self.tcp_identify_button.setEnabled(enabled and self._discovery_enabled)
        verified = self.tcp_results.selected_verification == "MOKE Box verified"
        self.tcp_assign_moke_button.setEnabled(verified and self._discovery_enabled)

    def _assign_selected_moke(self) -> None:
        endpoint = self.tcp_results.selected_endpoint
        if endpoint is None or self.tcp_results.selected_verification != "MOKE Box verified":
            return
        self.moke_assignment_requested.emit(endpoint)

    def _identify_selected_moke(self) -> None:
        if (
            self._moke_identification_worker is not None
            and self._moke_identification_worker.isRunning()
        ):
            return
        endpoint = self.tcp_results.selected_endpoint
        if endpoint is None or self.tcp_results.selected_state not in {"open", "entered"}:
            return
        host, separator, port_text = endpoint.rpartition(":")
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
        self.tcp_results.upsert_endpoint(
            host=host,
            endpoint=endpoint,
            state=self.tcp_results.selected_state or "open",
            verification="Verifying MOKE…",
        )
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
        self.tcp_results.upsert_endpoint(
            host=host,
            endpoint=f"{host}:{self.tcp_port.value()}",
            state="entered",
            verification="Pending test",
        )
        self.tcp_results.select_host(host)
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
        dialog = StationDialog(self)
        dialog.setObjectName("mokeProtocolTraceDialog")
        dialog.setWindowTitle(f"MOKE protocol test — {endpoint}")
        dialog.setMinimumSize(680, 460)
        layout = QVBoxLayout(dialog)

        result = QLabel("MOKE Box verified" if verified else "MOKE verification failed")
        result.setObjectName("sectionTitle")
        layout.addWidget(result)
        detail_label = QLabel(detail)
        detail_label.setWordWrap(True)
        layout.addWidget(detail_label)

        trace_card = CardWidget(dialog)
        trace_card.setObjectName("mokeProtocolTraceCard")
        trace_layout = QVBoxLayout(trace_card)
        trace_layout.setContentsMargins(12, 12, 12, 12)
        trace = PlainTextEdit(trace_card)
        trace.setObjectName("mokeProtocolTrace")
        trace.setReadOnly(True)
        trace.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)
        trace.setAccessibleName("MOKE TCP transmit and receive trace")
        trace.setPlainText(
            f"Endpoint: {endpoint}\n"
            f"Result: {'VERIFIED' if verified else 'FAILED'}\n\n"
            f"TX → MOKE ({len(tx_bytes)} bytes)\n"
            f"{self._format_protocol_bytes(tx_bytes)}\n\n"
            f"RX ← MOKE ({len(rx_bytes)} bytes)\n"
            f"{self._format_protocol_bytes(rx_bytes)}"
        )
        trace_layout.addWidget(trace)
        layout.addWidget(trace_card, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        close_button = PushButton("Close", dialog)
        close_button.clicked.connect(dialog.accept)
        actions.addWidget(close_button)
        layout.addLayout(actions)
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
            f"TCP/IP discovery completed: {self.tcp_results.row_count} host(s) accepted port {self.tcp_port.value()}"
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
        for row in self.visa_results.rows:
            result = row.state.result
            device = row.assignment.currentData()
            if not isinstance(device, str) or result.idn is None or not row.assignment.isEnabled():
                continue
            if device in assignments:
                self.discovery_info.setText(f"Cannot save: more than one resource is assigned to {device.title()}.")
                return
            assignments[device] = (result.resource, result.backend, result.idn)
        if not assignments:
            self.discovery_info.setText("Select at least one responding instrument assignment.")
            return
        self.assignments_requested.emit(assignments)

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
            "lakeshore_gaussmeter": self._settings.lakeshore_gaussmeter,
        }
        for device in ("rigol", "keithley", "anritsu", "lakeshore_gaussmeter"):
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

        self._refresh_visa_results()
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
        lake_shore = self._settings.lakeshore_gaussmeter
        if (
            lake_shore.resource == result.resource
            and lake_shore.visa_backend == result.backend
        ):
            return "lakeshore_gaussmeter"
        return None

    def discovered_serial_baud(self, resource: str, backend: str) -> int | None:
        """Return the baud that produced IDN for one current scan result."""

        for result in self._discovery_results:
            if result.resource == resource and result.backend == backend:
                return result.serial_baud
        return None

    def _refresh_visa_results(self) -> None:
        states = tuple(
            VisaResultState.from_result(
                result,
                configured_device=self._configured_device_for(result),
            )
            for result in self._discovery_results
        )
        self.visa_results.set_results(states)
        self.visa_results.set_assignment_allowed(self._assignment_allowed)
