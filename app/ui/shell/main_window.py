"""Application shell and lifecycle orchestration."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, QSettings, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QToolBar,
    QWidget,
)

from app.audit import AuditLogger
from app.bootstrap import StationComposition
from app.domain.errors import AuthorizationError, ConfigurationError
from app.devices.simulators import simulated_station_settings
from app.engine.compiler import RecipeCompiler
from app.engine.estimation import PlanEstimator
from app.engine.recovery import RunRecoveryManager
from app.recipes import (
    parse_recipe_text,
)
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from app.security import AccessPolicy, Permission
from app.storage import Hdf5RunReader
from app.ui.settings_page import SettingsPage
from app.ui.run_worker import RunController, serialize_settings_snapshot
from app.ui.dashboard import DashboardPage, DeviceConnectionPanel
from app.ui.execution import RunMonitorPage
from app.ui.results import ResultsPage
from app.ui.design_system import effective_theme
from app.ui.recipes import DeviceParameterDialog, SweepGeneratorDialog  # noqa: F401
from app.ui.recipes.page import (  # noqa: F401
    AnritsuAcquisitionEditorDialog, CommentEditorDialog, FixedValueDialog,
    KeithleySweepBuilderDialog, RecipePage, RecipeTreeWidget, SweepLibraryButton,
)
from app.ui.widgets import LimitEditDialog, LimitField, SpectrumPlotWidget

class MainWindow(QMainWindow):
    """Local Qt client with manual control, live spectrum and safe settings."""

    theme_changed = Signal(str)

    def __init__(
        self,
        settings_path: str | Path = ".config/settings.yml",
        *,
        simulation: bool = False,
        authenticated_username: str | None = None,
    ) -> None:
        super().__init__()
        self._repository = SettingsRepository(settings_path)
        self._simulation = simulation
        persisted = self._repository.load().settings
        self._settings = simulated_station_settings(persisted) if simulation else persisted
        self._access = AccessPolicy.from_settings(
            persisted,
            username=authenticated_username,
            simulation=simulation,
        )
        configured_audit_directory = persisted.application.get("audit_log_directory", "logs")
        audit_directory = Path(str(configured_audit_directory))
        if not audit_directory.is_absolute():
            audit_directory = self._repository.path.parent / audit_directory
        self._audit = AuditLogger(
            audit_directory,
            profile_id=persisted.profile.id,
            simulation=simulation,
            actor=self._access.identity.username,
            actor_roles=tuple(sorted(role.value for role in self._access.identity.roles)),
        )
        self._audit_healthy = True
        self._run_correlation_id: str | None = None
        suffix = " — SIMULATION" if simulation else ""
        self.setWindowTitle("Lab Control — Rigol · Keithley · Anritsu · MOKE Box · Lake Shore 475" + suffix)
        self.resize(1360, 880)
        self._composition = StationComposition(self._settings, simulation=self._simulation)
        self._controllers = self._composition.create_controllers(
            ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"), self
        )
        for controller in self._controllers.values():
            controller.set_operation_guard(self._guard_manual_operation)
        self._device_states = {
            key: "disconnected" for key in ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter")
        }
        self._run_controller = RunController(self)
        self._build()
        self._apply_accessibility()
        self._connect_controllers()
        self._restore_workspace()
        self._audit.record(
            "Main window initialized",
            category="application",
            event_type="ui_ready",
            context={"settings_path": str(self._repository.path)},
        )

    def _build(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.setCentralWidget(self.tabs)
        self.dashboard = DashboardPage(self._settings, discovery_enabled=not self._simulation)
        self._device_pages = {
            key: self._composition.registry.get(key).create_page(
                self._controllers[key], self._settings
            )
            for key in ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter")
        }
        self.rigol_page = self._device_pages["rigol"]
        self.keithley_page = self._device_pages["keithley"]
        self.anritsu_page = self._device_pages["anritsu"]
        self.moke_box_page = self._device_pages["moke_box"]
        self.lakeshore_gaussmeter_page = self._device_pages["lakeshore_gaussmeter"]
        self.connection_panels: dict[str, DeviceConnectionPanel] = {}
        for key, page in self._device_pages.items():
            device = getattr(self._settings, key)
            resource, backend = self._device_connection_details(self._settings, key)
            panel = DeviceConnectionPanel(device.display_name, resource)
            panel.update_resource(resource, backend)
            if key == "moke_box":
                panel.test_button.setToolTip(
                    "Open one TCP session, validate the documented VOUT response, then disconnect. "
                    "No gain or VOUT-setting command is sent."
                )
            page.layout().insertWidget(0, panel)
            self.connection_panels[key] = panel
            # Compatibility facade for integrations that accessed connection
            # controls through the dashboard card. The buttons themselves are
            # parented and rendered only on the device page.
            card = self.dashboard.cards[key]
            card.connect_button = panel.connect_button
            card.disconnect_button = panel.disconnect_button
            card.test_button = panel.test_button
        self.recipe_page = RecipePage(
            self._settings,
            device_registry=self._composition.registry,
        )
        self.recipe_page.set_keithley_snapshot_provider(
            self.keithley_page.configuration_panel.snapshot
        )
        self.recipe_page.set_rigol_snapshot_provider(
            self.rigol_page.configuration_snapshot
        )
        self.recipe_page.set_anritsu_snapshot_provider(
            self.anritsu_page.configuration_panel.configuration_snapshot
        )
        self.recipe_page.set_anritsu_sg_snapshot_provider(
            self.anritsu_page.signal_generator_snapshot
        )
        self.run_monitor = RunMonitorPage()
        self.results_page = ResultsPage(str(self._settings.storage.get("output_directory", "./measurements")))
        self.settings_page = SettingsPage(
            self._repository,
            read_only=self._simulation,
            access_policy=self._access,
        )
        self.dashboard.set_assignment_allowed(
            self._access.allows(Permission.ASSIGN_VISA)
        )
        roles = ", ".join(
            sorted(role.value for role in self._access.identity.roles)
        ) or "none"

        def configure_limit_button(
            field: LimitField,
            *,
            device: str,
            fixed_instrument_range: bool = False,
        ) -> bool:
            if fixed_instrument_range:
                enabled = False
                reason = (
                    "This range is fixed by the instrument and is not an editable "
                    "laboratory safety limit."
                )
            elif self._simulation:
                enabled = False
                reason = (
                    "Safety-limit editing is disabled in simulation mode. "
                    "Open the hardware station profile to edit approved limits."
                )
            elif not self._access.allows(Permission.EDIT_SETTINGS):
                enabled = False
                reason = (
                    f"Access denied: current role(s) {roles} do not include "
                    "EDIT_SETTINGS. Use an engineer or service account."
                )
            else:
                enabled = True
                reason = (
                    "Edit this safety range. Saving revokes profile approval and "
                    "requires a new authorized approval."
                )
            field.edit_button.setEnabled(enabled)
            field.edit_button.setToolTip(reason)
            field.edit_button.setAccessibleName(
                f"Edit {device} safety limits"
            )
            field.edit_button.setAccessibleDescription(reason)
            return enabled

        for field in self.rigol_page.findChildren(LimitField):
            configure_limit_button(field, device="Rigol")
            field.edit_requested.connect(lambda field=field: self._edit_device_limit("rigol", field))
        for field in self.keithley_page.findChildren(LimitField):
            editable = configure_limit_button(
                field,
                device="Keithley",
                fixed_instrument_range=(
                    str(field.property("limitKey")) == "nplc"
                ),
            )
            if editable:
                field.edit_requested.connect(lambda field=field: self._edit_device_limit("keithley", field))
        for field in self.anritsu_page.findChildren(LimitField):
            configure_limit_button(field, device="Anritsu")
            field.edit_requested.connect(lambda field=field: self._edit_device_limit("anritsu", field))
        self._tab_indices: dict[str, int] = {}
        for widget, name in (
            (self.dashboard, "Dashboard"),
            (self.rigol_page, "Rigol"),
            (self.keithley_page, "Keithley"),
            (self.anritsu_page, "Anritsu"),
            (self.moke_box_page, "MOKE Box"),
            (self.lakeshore_gaussmeter_page, "Lake Shore 475"),
            (self.recipe_page, "Sweeps"),
            (self.run_monitor, "Execution"),
            (self.results_page, "Results"),
            (self.settings_page, "Settings"),
        ):
            self._tab_indices[name] = self.tabs.addTab(widget, name)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(50)
        self.setStatusBar(self.statusBar())
        self.event_log_dock = self._log_dock()
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.event_log_dock)
        self.resizeDocks([self.event_log_dock], [120], Qt.Orientation.Vertical)
        self.dashboard.emergency_requested.connect(self._emergency_off_all)
        self.dashboard.assignments_requested.connect(self._save_discovered_assignments)
        self.dashboard.moke_assignment_requested.connect(self._save_moke_assignment)
        self.dashboard.status.connect(self._log)
        self.settings_page.settings_saved.connect(self._settings_saved)
        self.recipe_page.run_requested.connect(self._start_run)
        self.recipe_page.plan_preflight_changed.connect(self.dashboard.update_plan_preflight)
        self.results_page.resume_requested.connect(self._resume_run)
        self.run_monitor.stop_requested.connect(self._run_controller.request_stop)
        self.run_monitor.pause_requested.connect(self._run_controller.request_pause)
        self.run_monitor.resume_requested.connect(self._run_controller.request_resume)
        self._run_controller.event.connect(self._run_event)
        self._run_controller.finished.connect(self._run_finished)
        self._run_controller.failed.connect(self._run_failed)
        self._run_controller.emergency_completed.connect(self._run_emergency_completed)
        for page in (
            self.rigol_page,
            self.keithley_page,
            self.anritsu_page,
            self.moke_box_page,
            self.lakeshore_gaussmeter_page,
            self.recipe_page,
            self.settings_page,
        ):
            page.status.connect(self._log)
        menu = self.menuBar().addMenu("Application")
        theme_menu = menu.addMenu("Theme")
        self.theme_group = QActionGroup(self)
        self.theme_group.setExclusive(True)
        self.theme_actions: dict[str, QAction] = {}
        configured_theme = str(self._settings.ui.get("theme", "system")).lower()
        if configured_theme not in {"light", "dark", "system"}:
            configured_theme = "system"
        for mode, label in (("system", "System"), ("light", "Light"), ("dark", "Dark")):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setData(mode)
            action.setChecked(mode == configured_theme)
            action.triggered.connect(lambda checked=False, mode=mode: checked and self._set_theme_mode(mode))
            self.theme_group.addAction(action)
            theme_menu.addAction(action)
            self.theme_actions[mode] = action
        # Compatibility alias for integrations that used the former action.
        self.theme_action = self.theme_actions["light"]
        menu.addAction(self.event_log_dock.toggleViewAction())
        menu.addSeparator()
        quit_action = QAction("Safe shutdown", self)
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)
        self.estop_shortcut = QShortcut(QKeySequence("Ctrl+Shift+E"), self)
        self.estop_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.estop_shortcut.activated.connect(self._emergency_off_all)
        self.scan_shortcut = QShortcut(QKeySequence("F5"), self)
        self.scan_shortcut.activated.connect(self.dashboard._scan_visa)
        self._build_top_chrome()

    def _build_top_chrome(self) -> None:
        """Build a compact menu status area and icon-based application ribbon."""

        self.tabs.tabBar().hide()
        ribbon = QToolBar("Application ribbon", self)
        ribbon.setObjectName("applicationRibbon")
        ribbon.setMovable(False)
        ribbon.setFloatable(False)
        ribbon.setIconSize(QSize(24, 24))
        ribbon.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.ribbon_group = QActionGroup(self)
        self.ribbon_group.setExclusive(True)
        self.ribbon_actions: list[QAction] = []
        icon_dir = Path(__file__).resolve().parents[1] / "assets" / "icons"
        icon_files = {
            "Dashboard": "dashboard.svg",
            "Rigol": "rigol.svg",
            "Keithley": "keithley.svg",
            "Anritsu": "anritsu.svg",
            "MOKE Box": "moke_box.svg",
            "Lake Shore 475": "lakeshore.svg",
            "Sweeps": "recipes.svg",
            "Execution": "execution.svg",
            "Results": "results.svg",
            "Settings": "settings.svg",
        }
        labels = tuple(self.tabs.tabText(index) for index in range(self.tabs.count()))
        for index, label in enumerate(labels):
            if label in {"Sweeps", "Results"}:
                ribbon.addSeparator()
            action = QAction(QIcon(str(icon_dir / icon_files[label])), label, self)
            action.setCheckable(True)
            action.setChecked(index == self.tabs.currentIndex())
            action.setToolTip(f"Open {label}")
            action.triggered.connect(lambda checked=False, index=index: checked and self.tabs.setCurrentIndex(index))
            self.ribbon_group.addAction(action)
            ribbon.addAction(action)
            self.ribbon_actions.append(action)
        self.tabs.currentChanged.connect(
            lambda index: 0 <= index < len(self.ribbon_actions) and self.ribbon_actions[index].setChecked(True)
        )
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, ribbon)
        self.ribbon = ribbon

        corner = QWidget()
        corner.setObjectName("menuStatusArea")
        status_layout = QHBoxLayout(corner)
        status_layout.setContentsMargins(6, 1, 6, 1)
        status_layout.setSpacing(8)
        profile_state = "LOCKED" if self._settings.outputs_locked else "APPROVED"
        self.profile_status = QLabel(f"Profile: {profile_state}")
        self.profile_status.setObjectName("profileLocked" if self._settings.outputs_locked else "profileApproved")
        status_layout.addWidget(self.profile_status)
        self.identity_status = QLabel(f"User: {self._access.identity.display_name}")
        self.identity_status.setObjectName("compactIdentityStatus")
        self.identity_status.setToolTip(
            "Authenticated operating-system identity and effective Lab Control role(s)."
        )
        status_layout.addWidget(self.identity_status)
        self.toolbar_device_status: dict[str, QLabel] = {}
        for device in ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"):
            display = {"moke_box": "MOKE", "lakeshore_gaussmeter": "Lake Shore"}.get(device, device.title())
            label = QLabel(f"● {display}: OFFLINE")
            label.setObjectName("compactDeviceStatus")
            label.setAccessibleName(f"{display} connection and output state")
            status_layout.addWidget(label)
            self.toolbar_device_status[device] = label
        stop = QPushButton("E-STOP")
        stop.setObjectName("compactEmergencyButton")
        stop.setMaximumWidth(74)
        stop.setMaximumHeight(24)
        stop.setToolTip("Confirm and disable every output and abort acquisition.")
        stop.clicked.connect(self._emergency_off_all)
        status_layout.addWidget(stop)
        self.menuBar().setCornerWidget(corner, Qt.Corner.TopRightCorner)
        self.menu_status_area = corner

    def _apply_accessibility(self) -> None:
        """Ensure controls expose text as well as colour and have screen-reader metadata."""

        for button in self.findChildren(QPushButton):
            if not button.accessibleName():
                button.setAccessibleName(button.text().replace("&", ""))
            if not button.accessibleDescription() and button.toolTip():
                button.setAccessibleDescription(button.toolTip())
        for editor in self.findChildren(QLineEdit):
            if not editor.accessibleName():
                editor.setAccessibleName(editor.placeholderText() or "Numeric or text parameter")
        self.tabs.setAccessibleName("Application workspace")
        self.log.setAccessibleName("Event log")
        self.statusBar().setAccessibleName("Current application status")

    @staticmethod
    def _nested_value(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
        value: Any = payload
        for part in path:
            value = value[part]
        return value

    @staticmethod
    def _device_connection_details(
        settings: StationSettings, device: str
    ) -> tuple[str | None, str]:
        if device == "moke_box":
            return settings.moke_box.endpoint, "TCP/IP"
        if device == "lakeshore_gaussmeter":
            return settings.lakeshore_gaussmeter.resource, settings.lakeshore_gaussmeter.visa_backend
        connection = getattr(settings, device).connection
        return connection.resource, connection.visa_backend

    @staticmethod
    def _coerce_limit_value(text: str, original: object) -> object:
        stripped = text.strip()
        if original is None:
            return None if not stripped else stripped
        if isinstance(original, int) and not isinstance(original, bool):
            return int(stripped)
        if isinstance(original, float):
            return float(stripped)
        return stripped

    def _limit_edit_spec(self, device: str, field: LimitField) -> tuple[str, tuple[str, ...], bool]:
        key = str(field.property("limitKey"))
        if device == "rigol":
            channel = self.rigol_page.channel.currentText()
            path = ("devices", "rigol", "safety", "channels", channel, "lab_limits", key)
            return f"Rigol CH{channel} — {key.replace('_', ' ')}", path, key != "declared_dut_impedance"
        if device == "anritsu":
            path = ("devices", "anritsu", "safety", key)
            return f"Anritsu — {key.replace('_', ' ')}", path, True

        channel = self.keithley_page.channel.currentText()
        mode = self.keithley_page.mode.currentText()
        if mode == "measure_only" and key in {"level", "compliance", "source_range"}:
            raise ConfigurationError("Source limits are not applicable while Keithley is in measure-only mode.")
        mappings = {
            "level": "source_current" if mode == "current" else "source_voltage",
            "compliance": "voltage_compliance" if mode == "current" else "current_compliance",
            "source_range": "source_current" if mode == "current" else "source_voltage",
            "measure_voltage_range": "measured_voltage_trip",
            "measure_current_range": "measured_current_trip",
            "settle": "point_settle_time",
        }
        mapped = mappings[key]
        path = ("devices", "keithley", "safety", "channels", channel, "lab_limits", mapped)
        return f"Keithley CH{channel} — {mapped.replace('_', ' ')}", path, True

    def _edit_device_limit(self, device: str, field: LimitField) -> None:
        try:
            self._require_permission(
                Permission.EDIT_SETTINGS,
                f"editing {device} safety limits",
                audit=True,
            )
        except AuthorizationError as exc:
            QMessageBox.warning(self, "Access denied", str(exc))
            return
        try:
            loaded = self._repository.load()
            raw = deepcopy(loaded.raw)
            title, path, maximum_enabled = self._limit_edit_spec(device, field)
            range_data = self._nested_value(raw, path)
            if range_data is None:
                range_data = {"min": "", "max": ""}
            minimum = range_data.get("min")
            maximum = range_data.get("max") if maximum_enabled else None
        except (ConfigurationError, KeyError, TypeError) as exc:
            QMessageBox.critical(self, "Cannot edit limits", str(exc))
            return

        dialog = LimitEditDialog(title, minimum, maximum, maximum_enabled=maximum_enabled, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            replacement = dict(range_data)
            replacement["min"] = self._coerce_limit_value(dialog.minimum.text(), minimum)
            if maximum_enabled:
                replacement["max"] = self._coerce_limit_value(dialog.maximum.text(), maximum)
            container: Any = raw
            for part in path[:-1]:
                container = container[part]
            container[path[-1]] = replacement
            settings = self._repository.save_raw(raw)
        except (ConfigurationError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.critical(self, "Invalid safety limits", str(exc))
            return

        self.settings_page.reload()
        self._settings_saved(settings)
        self.statusBar().showMessage(f"Saved {title}; safety profile approval is now required.", 12_000)
        self._log(f"Safety limits saved: {title}")

    def _set_theme_mode(self, mode: str, *, persist: bool = True) -> None:
        theme = effective_theme(mode)
        application = QApplication.instance()
        if application is not None:
            application.setProperty("activeTheme", theme)
        for plot in self.findChildren(SpectrumPlotWidget):
            plot.apply_theme(theme)
        self.theme_changed.emit(theme)
        if persist:
            self._persist_theme(mode)
        self._log(f"Theme changed to {mode} ({theme})" + (" and saved" if persist else ""))

    def refresh_system_theme(self) -> None:
        if self.theme_actions["system"].isChecked():
            self._set_theme_mode("system", persist=False)

    def _persist_theme(self, theme: str) -> None:
        if self.settings_page._dirty:
            self.settings_page._autosave_timer.stop()
            if not self.settings_page.save_draft(silent=True):
                self._log("Theme was applied but not saved because Settings contains invalid values")
                return
        try:
            loaded = self._repository.load()
            loaded.raw.setdefault("ui", {})["theme"] = theme
            self._settings = self._repository.save_raw(loaded.raw)
        except ConfigurationError as exc:
            self._log(f"Theme was applied but could not be saved: {exc}")
            return
        self.settings_page.reload()

    def _log_dock(self):
        from PySide6.QtWidgets import QDockWidget

        dock = QDockWidget("Event log", self)
        dock.setObjectName("eventLogDock")
        dock.setWidget(self.log)
        return dock

    def _restore_workspace(self) -> None:
        settings = QSettings("LabControl", "LabControl")
        geometry = settings.value("main_window/geometry")
        state = settings.value("main_window/state")
        splitter = settings.value("anritsu/splitter")
        if geometry is not None:
            self.restoreGeometry(geometry)
        if state is not None:
            self.restoreState(state, 1)
        if splitter is not None:
            self.anritsu_page.workspace_splitter.restoreState(splitter)
        self.tabs.setCurrentIndex(int(settings.value("main_window/current_tab", 0)))

    def _save_workspace(self) -> None:
        settings = QSettings("LabControl", "LabControl")
        settings.setValue("main_window/geometry", self.saveGeometry())
        settings.setValue("main_window/state", self.saveState(1))
        settings.setValue("main_window/current_tab", self.tabs.currentIndex())
        settings.setValue("anritsu/splitter", self.anritsu_page.workspace_splitter.saveState())

    def _connect_controllers(self) -> None:
        for name, controller in self._controllers.items():
            card = self.dashboard.cards[name]
            panel = self.connection_panels[name]
            panel.connect_requested.connect(
                lambda current=controller, current_panel=panel: (
                    current_panel.set_connecting(True),
                    current.call("connect"),
                )
            )
            panel.disconnect_requested.connect(lambda current=controller: current.call("disconnect"))
            panel.test_requested.connect(
                lambda current=controller, current_card=panel: (
                    current_card.set_testing(True),
                    current.call("test_communication"),
                )
            )
            controller.state_changed.connect(card.update_state)
            controller.state_changed.connect(panel.update_state)
            controller.state_changed.connect(lambda state, device=name: self._set_device_state(device, state))
            controller.result.connect(
                lambda operation, result, device=name, current=panel: self._device_result(
                    device, current, operation, result
                )
            )
            controller.error.connect(lambda operation, error, device=name: self._device_error(device, operation, error))
            controller.traffic.connect(
                lambda message, device=name: self._log(
                    f"{device.upper()} {'TCP' if device == 'moke_box' else 'VISA'} {message}"
                )
            )
            if name == "rigol":
                controller.capabilities_changed.connect(self.rigol_page.set_capabilities)
            elif name == "anritsu":
                controller.capabilities_changed.connect(self.anritsu_page.set_capabilities)

    def _device_result(self, device: str, card: DeviceConnectionPanel, operation: str, result: object) -> None:
        if operation == "connect":
            card.set_connecting(False)
            card.update_identity(result)
            self.dashboard.cards[device].update_identity(result)
            self.dashboard.mark_identity_verified(device)
            self._log(f"Connected: {getattr(result, 'idn', result)}")
        elif operation == "disconnect":
            self._log("Instrument disconnected")
        elif operation == "replace_adapter":
            card.set_reconfiguring(False)
            card.update_state("disconnected")
            card.identity.setText("IDN: not connected")
            self.dashboard.cards[device].set_reconfiguring(False)
            self.dashboard.cards[device].update_state("disconnected")
            self.dashboard.cards[device].identity.setText("IDN: not connected")
            self._log(f"VISA ADAPTER REPLACE COMPLETE: {card.summary.text()}")
        elif operation == "test_communication" and isinstance(result, dict):
            card.set_testing(False)
            card.update_state("verified")
            features = ", ".join(str(item) for item in result.get("features", ())) or "basic VISA"
            options = ", ".join(str(item) for item in result.get("hardware_options", ())) or "not reported"
            card.identity.setText(
                f"TEST PASS: {result.get('vendor', '')} {result.get('model', '')} • "
                f"SN {result.get('serial', '—')} • FW {result.get('firmware', '—')}\n"
                f"Protocols/features: {features} • Options: {options}"
            )
            self.dashboard.cards[device].identity.setText(card.identity.text())
            self.dashboard.mark_identity_verified(device)
            self._log(
                f"Communication test passed: {result.get('idn', '')}; "
                f"features={features}; options={options}"
            )

    def _device_error(self, device: str, operation: str, error: str) -> None:
        if operation == "replace_adapter":
            self.dashboard.cards[device].set_reconfiguring(False)
        elif operation in {"connect", "test_communication"}:
            card = self.connection_panels[device]
            action = "Connection" if operation == "connect" else "Communication test"
            card.show_error(action, error)
            self.dashboard.cards[device].identity.setText(f"{action.upper()} FAILED: {error}")
            QMessageBox.warning(
                self,
                f"{getattr(self._settings, device).display_name} — {action} failed",
                error,
            )
        self._log(f"{device}/{operation}: {error}")
        self.dashboard.record_device_error(device, error)

    def _set_device_state(self, device: str, state: str) -> None:
        self._device_states[device] = state
        self.dashboard.update_device_state(device, state)
        label = self.toolbar_device_status.get(device)
        if label is not None:
            display = {"moke_box": "MOKE", "lakeshore_gaussmeter": "Lake Shore"}.get(device, device.title())
            label.setText(f"● {display}: {state.replace('_', ' ').upper()}")
            label.setProperty("deviceState", state)
            label.style().unpolish(label)
            label.style().polish(label)

    def _guard_manual_operation(self, operation: str, payload: object) -> None:
        """Fail closed for new energy-producing operations after audit I/O failure."""

        # De-energising and disconnecting are never blocked by RBAC or audit
        # health. This invariant is stronger than any normal user permission.
        if operation in {"emergency_off", "ramp_to_zero", "stop_live", "disconnect"}:
            return
        if operation == "set_signal_generator_output" and not bool(payload):
            return
        if operation == "set_output":
            try:
                _channel, enabled = payload  # type: ignore[misc]
            except (TypeError, ValueError):
                enabled = True
            if not bool(enabled):
                return
        energizing_operations = {
            "arm",
            "configure",
            "configure_output",
            "configure_modulation",
            "configure_sweep",
            "configure_burst",
            "synchronize_phases",
            "set_output",
            "trigger_sweep",
            "trigger_burst",
            "ramp_to_level",
            "configure_signal_generator",
            "configure_advanced_spectrum",
            "arm_signal_generator",
            "set_signal_generator_output",
        }
        permission = (
            Permission.OPERATE_OUTPUT
            if operation in energizing_operations
            else Permission.CONNECT
            if operation in {"connect", "test_communication", "replace_adapter"}
            else Permission.PASSIVE_MEASURE
        )
        self._require_permission(
            permission,
            f"manual instrument operation {operation}",
            audit=operation in energizing_operations,
        )

        if self._audit_healthy:
            return
        energizing = operation in {
            "arm",
            "arm_signal_generator",
            "trigger_sweep",
            "trigger_burst",
            "ramp_to_level",
        }
        if operation == "set_output":
            try:
                _channel, enabled = payload  # type: ignore[misc]
            except (TypeError, ValueError):
                energizing = True
            else:
                energizing = bool(enabled)
        if operation == "set_signal_generator_output":
            energizing = bool(payload)
        if energizing:
            raise ConfigurationError(
                "The durable audit log is unavailable. ARM and OUTPUT ON are locked; "
                "OUTPUT OFF and E-STOP remain available."
            )

    def _assert_audit_ready_for_run(self) -> None:
        if not self._audit_healthy:
            raise ConfigurationError(
                "The durable audit log is unavailable. A measurement run cannot start."
            )

    def _start_run(self, plan: object) -> None:
        try:
            self._require_permission(
                Permission.RUN_RECIPE,
                "starting a measurement run",
                audit=True,
            )
            self._assert_audit_ready_for_run()
        except (AuthorizationError, ConfigurationError) as exc:
            QMessageBox.critical(self, "Run not started", str(exc))
            return
        if self._settings.outputs_locked:
            QMessageBox.warning(
                self,
                "Unverified profile",
                "Approve the profile in Settings before running a recipe.",
            )
            return
        connected = [name for name, state in self._device_states.items() if state != "disconnected"]
        if connected:
            QMessageBox.warning(
                self,
                "Disconnect manual control",
                "Run Engine opens its own VISA sessions. Disconnect first: " + ", ".join(connected) + ".",
            )
            return
        try:
            estimate = PlanEstimator(self._settings).estimate(plan)  # type: ignore[arg-type]
            readiness = self.dashboard.evaluate_readiness(plan, estimate)
            if readiness.blocking_items:
                details = "\n".join(
                    f"• {item.label}: {item.detail}" for item in readiness.blocking_items
                )
                raise ConfigurationError("Station preflight is blocked:\n" + details)
            self._run_controller.start(
                self._settings,
                self._repository.path,
                plan,  # type: ignore[arg-type]
                simulation=self._simulation,
                operator_context=self._access.identity.as_context(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "Run not started", str(exc))
            return
        self.run_monitor.run_started(
            len(plan.actions),  # type: ignore[union-attr]
            estimate.nominal_duration_s,
        )
        self._set_run_ui_locked(True)
        self.tabs.setCurrentWidget(self.run_monitor)
        self._log("Run Engine started")

    def _resume_run(self, selected: object) -> None:
        try:
            self._require_permission(
                Permission.RUN_RECIPE,
                "resuming a measurement run",
                audit=True,
            )
            self._assert_audit_ready_for_run()
        except (AuthorizationError, ConfigurationError) as exc:
            QMessageBox.critical(self, "Resume not started", str(exc))
            return
        path = Path(selected) if isinstance(selected, (str, Path)) else None
        if path is None:
            QMessageBox.warning(self, "Resume run", "No valid run file was selected.")
            return
        if self._settings.outputs_locked:
            QMessageBox.warning(
                self,
                "Unverified profile",
                "Approve the current safety profile before resuming a run.",
            )
            return
        connected = [
            name for name, state in self._device_states.items() if state != "disconnected"
        ]
        if connected:
            QMessageBox.warning(
                self,
                "Disconnect manual control",
                "Run Engine opens its own VISA sessions. Disconnect first: "
                + ", ".join(connected)
                + ".",
            )
            return
        try:
            detail = Hdf5RunReader.detail(path)
            current_settings_source = serialize_settings_snapshot(
                self._settings,
                self._repository.path,
                simulation=self._simulation,
            )
            if current_settings_source != detail.settings_yaml:
                raise ConfigurationError(
                    "The current settings differ from the immutable run snapshot. "
                    "Restore and approve the exact profile before resuming."
                )
            recipe = parse_recipe_text(detail.recipe_yaml, origin=str(path))
            plan = RecipeCompiler(self._settings).compile(recipe)
            checkpoint = RunRecoveryManager().inspect(path, plan)
            if (
                checkpoint.stored_points >= plan.total_points
                and checkpoint.next_action_index >= len(plan.actions)
            ):
                raise ConfigurationError("The selected run has no remaining actions.")
        except Exception as exc:
            QMessageBox.warning(self, "Resume unavailable", str(exc))
            self._log(f"RUN RECOVERY REJECTED: {exc}")
            return
        discarded = checkpoint.committed_points_found - checkpoint.stored_points
        answer = QMessageBox.question(
            self,
            "Resume measurement",
            "Resume only from the last confirmed safe boundary?\n\n"
            f"Preserved checkpoints: {checkpoint.stored_points}\n"
            f"Unsafe tail to discard and remeasure: {discarded}\n"
            f"Remaining action index: {checkpoint.next_action_index}/{len(plan.actions)}\n"
            f"Configuration prelude actions: {len(checkpoint.prelude_actions)}\n\n"
            "All required instruments will connect with outputs OFF. The stored recipe, "
            "plan hash and exact settings snapshot have been verified.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            estimate = PlanEstimator(self._settings).estimate(plan)  # type: ignore[arg-type]
            self._run_controller.start(
                self._settings,
                self._repository.path,
                plan,
                simulation=self._simulation,
                recovery=checkpoint,
                operator_context=self._access.identity.as_context(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "Resume not started", str(exc))
            return
        remaining = len(plan.actions) - checkpoint.next_action_index
        remaining_fraction = remaining / max(1, len(plan.actions))
        self.run_monitor.run_started(
            remaining + len(checkpoint.prelude_actions),
            estimate.nominal_duration_s * remaining_fraction,
        )
        self._set_run_ui_locked(True)
        self.tabs.setCurrentWidget(self.run_monitor)
        self._log(
            f"Run recovery started at safe checkpoint {checkpoint.stored_points}; "
            f"discarding {discarded} unsafe tail point(s)"
        )

    def _run_event(self, name: str, data: object) -> None:
        payload = data if isinstance(data, dict) else {"data": data}
        if name == "run_started":
            value = payload.get("correlation_id") or payload.get("hash")
            self._run_correlation_id = str(value) if value else None
        severity = "error" if name in {"run_fault", "shutdown_error", "watchdog_timeout"} else (
            "warning" if name in {"compliance_detected", "run_aborted", "safe_finally_error"} else "info"
        )
        if name != "spectrum_preview":
            self._audit_record(
                name.replace("_", " "),
                severity=severity,
                category="run",
                event_type=name,
                context=payload,
                correlation_id=self._run_correlation_id,
                critical=severity in {"error", "warning"},
            )
        if name == "runner_heartbeat":
            self.run_monitor.update_heartbeat(payload)
        elif name == "spectrum_preview":
            self.run_monitor.update_spectrum_preview(payload)
        else:
            self.run_monitor.append_event(name, payload)
        if name in {"run_completed", "run_aborted", "run_fault"}:
            self._run_correlation_id = None

    def _run_finished(self, result: object) -> None:
        self._set_run_ui_locked(False)
        self.run_monitor.complete(result)
        self.results_page.refresh()
        self._log("Run Engine completed the measurement")

    def _run_failed(self, error: str) -> None:
        self._set_run_ui_locked(False)
        self.run_monitor.failed(error)
        self._log(f"Run Engine: {error}")
        QMessageBox.critical(self, "Run Engine", error)

    def _run_emergency_completed(self, errors: object) -> None:
        if errors:
            self._log("E-STOP Run Engine: " + "; ".join(str(item) for item in errors))
        else:
            self._log("E-STOP Run Engine: OFF/ABORT sent through emergency sessions")

    def _emergency_off_all(self) -> None:
        answer = QMessageBox.warning(
            self,
            "E-STOP",
            "Disable every instrument output and abort acquisition?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            # Always use independent short-lived VISA sessions. A manual
            # instrument worker can be blocked just as a recipe worker can;
            # queueing OFF behind that operation is not an emergency path.
            self._run_controller.request_emergency_stop(
                self._settings,
                simulation=self._simulation,
            )
            for controller in self._controllers.values():
                controller.call("emergency_off")
            self._log(
                "E-STOP sent through independent emergency sessions and "
                "queued on all instrument workers"
            )

    def _settings_saved(self, settings: StationSettings) -> None:
        self._settings = simulated_station_settings(settings) if self._simulation else settings
        self.dashboard.update_settings(self._settings)
        self.rigol_page.set_settings(self._settings)
        self.keithley_page.set_settings(self._settings)
        self.anritsu_page.set_settings(self._settings)
        self.moke_box_page.set_settings(self._settings)
        self.lakeshore_gaussmeter_page.set_settings(self._settings)
        for name, panel in self.connection_panels.items():
            resource, backend = self._device_connection_details(self._settings, name)
            panel.update_resource(resource, backend)
        self.recipe_page.set_settings(self._settings)
        profile_state = "LOCKED" if self._settings.outputs_locked else "APPROVED"
        self.profile_status.setText(f"Profile: {profile_state}")
        self.profile_status.setObjectName("profileLocked" if self._settings.outputs_locked else "profileApproved")
        self.profile_status.style().unpolish(self.profile_status)
        self.profile_status.style().polish(self.profile_status)
        for name, controller in self._controllers.items():
            self.dashboard.cards[name].set_reconfiguring(True)
            self.connection_panels[name].set_reconfiguring(True)
            resource, backend = self._device_connection_details(self._settings, name)
            self._log(
                f"DEVICE ADAPTER REPLACE QUEUED [{name}]: resource={resource!r}, "
                f"backend={backend!r}"
            )
            controller.reconfigure(self._composition.create_adapter(name, settings=self._settings))
            self._device_states[name] = "disconnected"
            self.dashboard.cards[name].update_state("disconnected")
        self._log("Profile changed. VISA sessions were safely switched OFF and disconnected; new limits apply on the next connection.")

    def _save_discovered_assignments(self, payload: object) -> None:
        self._log(f"VISA ASSIGN RECEIVED: payload={payload!r}")
        try:
            self._require_permission(
                Permission.ASSIGN_VISA,
                "changing VISA assignments",
                audit=True,
            )
        except AuthorizationError as exc:
            self._log(f"VISA ASSIGN REJECTED: {exc}")
            QMessageBox.warning(self, "Access denied", str(exc))
            return
        if self._simulation:
            self._log("VISA ASSIGN ERROR: assignment is disabled in simulation mode")
            return
        if not isinstance(payload, dict):
            self._log(f"VISA ASSIGN ERROR: expected mapping, received {type(payload).__name__}")
            return
        assignments = {
            str(device): value
            for device, value in payload.items()
            if device in {"rigol", "keithley", "anritsu"}
            and isinstance(value, tuple)
            and len(value) == 3
        }
        if not assignments:
            self._log("VISA ASSIGN ERROR: payload contains no supported device assignment")
            return
        for device, (resource, backend, idn) in assignments.items():
            self._log(
                f"VISA ASSIGN VALIDATED [{device}]: resource={resource!r}, backend={backend!r}, IDN={idn!r}"
            )
        summary = "\n".join(
            f"• {device.title()}: {value[0]} ({value[1]})\n  {value[2]}"
            for device, value in sorted(assignments.items())
        )
        answer = QMessageBox.question(
            self,
            "Save VISA assignments",
            "Save these discovered resources?\n\n"
            f"{summary}\n\n"
            "Changing a connection revokes profile approval and safely disconnects current sessions. "
            "Existing serial-number requirements are not changed automatically.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._log("VISA ASSIGN CANCELLED: operator did not confirm configuration change")
            return
        self._log("VISA ASSIGN CONFIRMED: loading settings.yml for atomic update")
        try:
            loaded = self._repository.load()
            raw = deepcopy(loaded.raw)
            for device, (resource, backend, _idn) in assignments.items():
                old = raw["devices"][device]["connection"]
                self._log(
                    f"VISA ASSIGN WRITE [{device}]: {old.get('resource')!r}/{old.get('visa_backend')!r} "
                    f"-> {resource!r}/{backend!r}"
                )
                connection = raw["devices"][device]["connection"]
                connection["resource"] = resource
                connection["visa_backend"] = backend
            settings = self._repository.save_raw(raw)
        except Exception as exc:
            self._log(f"VISA ASSIGN FAILED: {type(exc).__name__}: {exc}")
            QMessageBox.critical(self, "VISA assignments not saved", str(exc))
            return
        self._log(
            f"VISA ASSIGN SAVED: settings.yml updated atomically; profile state={settings.profile.state}"
        )
        self.settings_page.reload()
        self._settings_saved(settings)
        self.dashboard.mark_assignments_saved(assignments)
        for device, (resource, backend, _idn) in assignments.items():
            self._log(
                f"VISA ASSIGN SUCCESS [{device}]: card and worker now use {resource!r} via {backend!r}"
            )

    def _save_moke_assignment(self, endpoint: str) -> None:
        try:
            self._require_permission(
                Permission.ASSIGN_VISA,
                "assigning a verified MOKE Box TCP endpoint",
                audit=True,
            )
        except AuthorizationError as exc:
            QMessageBox.warning(self, "Access denied", str(exc))
            return
        answer = QMessageBox.question(
            self,
            "Assign MOKE Box",
            f"Save {endpoint} as the MOKE Box endpoint?\n\n"
            "The read-only VOUT response was verified. VOUT control will remain disabled. "
            "Changing the hardware profile revokes its approval.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            loaded = self._repository.load()
            raw = deepcopy(loaded.raw)
            profile = raw["devices"]["moke_box"]
            profile["enabled"] = True
            profile["endpoint"] = endpoint
            profile["protocol_qualified"] = True
            profile["allow_vout_control"] = False
            profile["allowed_vout_channels"] = []
            settings = self._repository.save_raw(raw)
        except Exception as exc:
            QMessageBox.critical(self, "MOKE Box assignment not saved", str(exc))
            return
        self.settings_page.reload()
        self._settings_saved(settings)
        self.dashboard.tcp_discovery_info.setText(
            f"MOKE Box assigned to {endpoint}. Read-only control is ready; profile approval was revoked."
        )
        self._log(f"MOKE BOX ASSIGN SUCCESS: endpoint={endpoint!r}; VOUT control remains disabled")

    def _set_run_ui_locked(self, locked: bool) -> None:
        if locked:
            self.moke_box_page.stop_live(
                "Live Hall readout paused while a recipe run owns the MOKE Box."
            )
            self.lakeshore_gaussmeter_page.stop_live(
                "Live field readout paused while a recipe run owns the Lake Shore 475."
            )
        for label in ("Rigol", "Keithley", "Anritsu", "MOKE Box", "Lake Shore 475", "Sweeps", "Settings"):
            index = self._tab_indices[label]
            self.tabs.setTabEnabled(index, not locked)
            self.ribbon_actions[index].setEnabled(not locked)

    def _require_permission(
        self,
        permission: Permission,
        action: str,
        *,
        audit: bool,
    ) -> None:
        try:
            self._access.require(permission, action=action)
        except AuthorizationError as exc:
            if audit:
                self._audit_record(
                    str(exc),
                    severity="warning",
                    category="authorization",
                    event_type="access_denied",
                    context={
                        **self._access.identity.as_context(),
                        "permission": permission.value,
                        "action": action,
                    },
                    critical=True,
                )
            raise
        if audit:
            self._audit_record(
                f"Access granted for {action}",
                category="authorization",
                event_type="access_granted",
                context={
                    **self._access.identity.as_context(),
                    "permission": permission.value,
                    "action": action,
                },
            )

    def _audit_record(
        self,
        message: str,
        *,
        severity: str = "info",
        category: str = "ui",
        event_type: str = "message",
        context: dict[str, object] | None = None,
        correlation_id: str | None = None,
        critical: bool = False,
    ) -> None:
        try:
            self._audit.record(
                message,
                severity=severity,
                category=category,
                event_type=event_type,
                context=context,
                correlation_id=correlation_id,
                critical=critical,
            )
        except (OSError, RuntimeError) as exc:
            first_failure = self._audit_healthy
            self._audit_healthy = False
            if hasattr(self, "dashboard"):
                self.dashboard.update_audit_health(False)
            if first_failure and hasattr(self, "log"):
                self.log.appendPlainText(
                    "CRITICAL: durable audit logging failed; ARM, OUTPUT ON and new runs are locked: "
                    + str(exc)
                )

    @staticmethod
    def _log_classification(message: str) -> tuple[str, str, bool]:
        upper = message.upper()
        if "E-STOP" in upper or "COMPLIANCE" in upper:
            return "safety", "critical", True
        if any(marker in upper for marker in ("FAILED", "FAULT", " ERROR", "REJECTED")):
            return "application", "error", True
        if "VISA" in upper or "CONNECTED" in upper or "DISCONNECTED" in upper:
            return "visa", "info", False
        if "PROFILE" in upper or "SETTINGS" in upper or "LIMIT" in upper:
            return "configuration", "info", False
        if "RUN " in upper or upper.startswith("RUN"):
            return "run", "info", False
        return "ui", "info", False

    def _log(self, message: str) -> None:
        category, severity, critical = self._log_classification(message)
        self._audit_record(
            message,
            severity=severity,
            category=category,
            critical=critical,
            correlation_id=self._run_correlation_id,
        )
        self.log.appendPlainText(message)
        self.statusBar().showMessage(message, 8_000)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._audit_record(
            "Application safe shutdown started",
            category="application",
            event_type="shutdown_started",
            critical=True,
        )
        self._save_workspace()
        self.recipe_page.cancel_preflight()
        self.anritsu_page._timer.stop()
        self._run_controller.close()
        for controller in self._controllers.values():
            controller.close()
        try:
            self._audit.close()
        except (OSError, RuntimeError):
            self._audit_healthy = False
        event.accept()
