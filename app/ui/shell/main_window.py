"""Application shell and lifecycle orchestration."""

from __future__ import annotations

from copy import deepcopy
from collections import deque
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QSettings, QSignalBlocker, QThread, QTimer, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QColor,
    QKeySequence,
    QPalette,
    QShortcut,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    FluentIcon,
    FluentWindow,
    MessageBox,
    NavigationItemPosition,
    PlainTextEdit,
    PushButton,
    RoundMenu,
    SimpleCardWidget,
    TransparentDropDownToolButton,
)
from qfluentwidgets.common.style_sheet import styleSheetManager

from app.audit import AuditLogger
from app.bootstrap import StationComposition
from app.contracts import ExecutionTelemetryView
from app.domain.errors import AuthorizationError, ConfigurationError, SafetyViolation
from app.domain.manual_metadata import ManualMetadataValue
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_FREQUENCY,
    DIMENSION_POWER,
    DIMENSION_RESISTANCE,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    format_quantity_auto,
    parse_quantity,
)
from app.devices.anritsu_ms2830a import (
    AdvancedSpectrumSnapshot,
    AnritsuConfigurationSnapshot,
)
from app.devices.keithley_2600.settings_defaults import (
    persist_keithley_default_snapshots,
    validate_keithley_default_snapshots,
)
from app.devices.simulators import simulated_station_settings
from app.engine.compiler import RecipeCompiler
from app.engine.estimation import PlanEstimator
from app.engine.recovery import RunRecoveryManager
from app.engine.runner import ExecutionMode
from app.recipes import parse_recipe_text
from app.inventory import ActiveSampleTarget, InventoryStore, SampleRunRecord
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from app.settings.validation import format_settings_validation_error
from app.security import AccessPolicy, Permission
from app.safety.keithley_limit_reconciliation import (
    propose_keithley_limit_adjustments,
)
from app.storage import Hdf5RunReader
from app.ui.settings_page import SettingsPage
from app.ui.settings_guidance import SettingsIssue, settings_issue_for_error
from app.ui.dialogs import SweepDeviceReadinessDialog, StationMessageBox as QMessageBox
from app.ui.settings_workers import KeithleyDefaultsSaveWorker
from app.ui.run_worker import RunController, serialize_settings_snapshot
from app.ui.dashboard import DashboardPage, DeviceConnectionPanel
from app.ui.elab import ElabPage
from app.ui.inventory import SampleInventoryPage
from app.ui.execution import RunMonitorPage
from app.ui.results import HeatmapResultsTab, ResultsPage
from app.ui.design_system import apply_application_theme, effective_theme
from app.ui.recipes import DeviceParameterDialog, SweepGeneratorDialog  # noqa: F401
from app.ui.recipes.page import (  # noqa: F401
    AnritsuAcquisitionEditorDialog,
    CommentEditorDialog,
    FixedValueDialog,
    KeithleySweepBuilderDialog,
    RecipePage,
    SweepLibraryButton,
)
from app.ui.shell.page_host import FluentPageHost
from app.ui.shell.safety_strip import StationSafetySnapshot, StationSafetyStrip
from app.ui.widgets import (
    KeithleyLimitProposalDialog,
    LimitEditDialog,
    LimitField,
    SpectrumPlotWidget,
)
from app.ui.quick_controls import QuickControlCoordinator, QuickControlsWindow
from app.ui.workers import DeviceController


class MainWindow(FluentWindow):
    """Local Qt client with manual control, live spectrum and safe settings."""

    theme_changed = Signal(str)
    _keithley_defaults_write_requested = Signal(int, object)

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
        self._run_upload_to_elab_requested = False
        self._run_elab_upload_config: dict[str, Any] | None = None
        persisted = self._repository.load().settings
        self._settings = simulated_station_settings(persisted) if simulation else persisted
        output_dir = Path(self._settings.storage.get("output_directory", "./measurements"))
        self.inventory_store = InventoryStore(output_dir / "inventory.db")
        # Establish the global Fluent theme before constructing the large page
        # tree.  QFluent's synchronous stylesheet refresh walks every existing
        # widget; doing it after ``_build()`` made the first ``show()`` block
        # for tens of seconds on Windows.  Newly-created controls inherit the
        # selected Fluent theme and the station event filter styles them when
        # they become visible.  ``showEvent`` retains an idempotent fallback
        # for embedders that construct a window without this composition path.
        application = QApplication.instance()
        if application is not None:
            applied_theme = apply_application_theme(
                application, str(self._settings.ui.get("theme", "system"))
            )
            application.setProperty("activeTheme", applied_theme.name)
        self._keithley_defaults_generation = 0
        self._keithley_defaults_in_flight = False
        self._pending_keithley_defaults: tuple[int, dict[str, dict[str, Any]]] | None = None
        self._active_keithley_defaults: tuple[int, dict[str, dict[str, Any]]] | None = None
        self._keithley_defaults_timer = QTimer(self)
        self._keithley_defaults_timer.setSingleShot(True)
        self._keithley_defaults_timer.setInterval(2_000)
        self._keithley_defaults_thread = QThread(self)
        self._keithley_defaults_worker = KeithleyDefaultsSaveWorker(self._repository.path)
        self._keithley_defaults_worker.moveToThread(self._keithley_defaults_thread)
        self._keithley_defaults_write_requested.connect(
            self._keithley_defaults_worker.save,
            Qt.ConnectionType.QueuedConnection,
        )
        self._keithley_defaults_worker.succeeded.connect(self._keithley_defaults_saved)
        self._keithley_defaults_worker.failed.connect(self._keithley_defaults_save_failed)
        self._keithley_defaults_thread.finished.connect(self._keithley_defaults_worker.deleteLater)
        self._keithley_defaults_thread.start()
        self._pending_limit_rollbacks: dict[str, StationSettings] = {}
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
        self.setMinimumSize(820, 560)
        self.resize(1360, 880)
        self._composition = StationComposition(self._settings, simulation=self._simulation)
        self.setWindowTitle("PyLab")
        self._controllers = self._composition.create_controllers(
            ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"), self
        )
        for device, controller in self._controllers.items():
            controller.set_operation_guard(
                lambda operation, payload, device=device: self._guard_manual_operation(
                    device, operation, payload
                )
            )
        self._device_states = {
            key: "disconnected"
            for key in ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter")
        }
        self._manual_device_idn: dict[str, str] = {}
        self._run_controller = RunController(self)
        self._leased_run_devices: set[str] = set()
        self._sweep_readiness_dialog: SweepDeviceReadinessDialog | None = None
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
        self.fluent_content = QWidget(self)
        self.fluent_content.setObjectName("fluentShellContent")
        content_layout = QVBoxLayout(self.fluent_content)
        content_layout.setContentsMargins(12, 8, 12, 8)
        content_layout.setSpacing(8)
        self.safety_strip = StationSafetyStrip(self.fluent_content)
        self.safety_strip.save_settings.setFixedHeight(28)
        self.safety_strip.save_settings.setMinimumWidth(100)
        content_layout.addWidget(self.safety_strip)
        self.shell_splitter = QSplitter(
            Qt.Orientation.Vertical,
            self.fluent_content,
        )
        self.shell_splitter.setObjectName("fluentShellSplitter")
        self.shell_splitter.setStyleSheet(
            "QSplitter#fluentShellSplitter::handle {"
            "background: transparent; border-top: 1px solid palette(mid);"
            "}"
            "QSplitter#fluentShellSplitter::handle:hover {"
            "border-top-color: palette(highlight);"
            "}"
        )
        self.shell_splitter.setChildrenCollapsible(False)
        self.shell_splitter.setHandleWidth(5)
        self.widgetLayout.removeWidget(self.stackedWidget)
        # The stock Fluent window stack draws a framed, rounded content pane.
        # Our station shell already owns the page hierarchy and its spacing;
        # transparent mode prevents that stock edge from becoming a bright
        # outline around dark pages.
        self.stackedWidget.setObjectName("fluentApplicationStack")
        stack_qss = (
            "QStackedWidget#fluentApplicationStack {"
            "border: none; border-radius: 0; background: transparent;"
            "}"
        )
        styleSheetManager.deregister(self.stackedWidget)
        self.stackedWidget.setStyleSheet(stack_qss)
        self.stackedWidget.setProperty("isTransparent", True)
        self.shell_splitter.addWidget(self.stackedWidget)
        content_layout.addWidget(self.shell_splitter, 1)
        self.widgetLayout.addWidget(self.fluent_content)
        self.navigationInterface.setExpandWidth(248)
        # Below the standard Fluent breakpoint the compact 48 px rail leaves
        # measurement pages enough width to preserve their control hierarchy.
        # The full panel becomes a temporary MENU overlay only when the operator
        # explicitly opens it; it must not consume 248 px at the 820 px minimum.
        self._navigation_expand_threshold = 1_008
        self.navigationInterface.setMinimumExpandWidth(self._navigation_expand_threshold)

        registry = self._composition.registry
        self.dashboard = DashboardPage(
            self._settings,
            registry,
            discovery_enabled=not self._simulation,
        )
        self._device_pages = {
            key: self._composition.registry.get(key).create_page(
                self._controllers[key], self._settings
            )
            for key in ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter")
        }
        self.rigol_page = self._device_pages["rigol"]
        self.keithley_page = self._device_pages["keithley"]
        self.keithley_characterization_page = self.keithley_page.characterization_card
        self.keithley_page.characterization_requested.connect(
            lambda: self._navigate_to("keithley_characterization")
        )
        self.anritsu_page = self._device_pages["anritsu"]
        self.moke_box_page = self._device_pages["moke_box"]
        self.lakeshore_gaussmeter_page = self._device_pages["lakeshore_gaussmeter"]
        self.anritsu_page.set_manual_archive_context(
            metadata_provider=self._manual_spectrum_metadata_values,
            device_idn_provider=self._manual_spectrum_device_idn,
            settings_source_provider=lambda: serialize_settings_snapshot(
                self._settings,
                self._repository.path,
                simulation=self._simulation,
            ),
            operator_context_provider=lambda: self._access.identity.as_context(),
        )
        self._run_read_only_controls: dict[QWidget, bool] = {}
        self._pending_execution_previews: dict[str, dict[str, object]] = {}
        self._execution_preview_timer = QTimer(self)
        self._execution_preview_timer.setSingleShot(True)
        self._execution_preview_timer.setInterval(100)
        self._execution_preview_timer.timeout.connect(
            self._flush_execution_previews
        )
        # Device pages are read-only projections of the runner state. Keep the
        # latest confirmed snapshot and repaint those pages at a bounded rate;
        # instrument I/O and safety decisions remain entirely in RunWorker.
        self._pending_execution_readback: tuple[str, dict[str, object]] | None = None
        self._execution_page_projection_cache: dict[
            str, tuple[dict[str, object], dict[str, str]]
        ] = {}
        self._execution_readback_timer = QTimer(self)
        self._execution_readback_timer.setSingleShot(True)
        self._execution_readback_timer.setInterval(100)
        self._execution_readback_timer.timeout.connect(
            self._flush_execution_readback
        )
        # Action lifecycle signals are deliberately drained in small GUI
        # batches.  The worker remains free to run and persist every event,
        # while Qt gets regular opportunities to paint, process input and
        # service the safety controls during a long sweep.
        self._pending_execution_events: deque[tuple[str, dict[str, object]]] = deque()
        # A checkpoint is a latest-state projection.  Keeping only the newest
        # one prevents a fast acquisition from making Qt replay hundreds of
        # stale QTreeWidget updates after the worker has already advanced.
        self._pending_execution_checkpoint: tuple[str, dict[str, object]] | None = None
        self._execution_event_drain_batch = 8
        self._execution_event_timer = QTimer(self)
        self._execution_event_timer.setSingleShot(True)
        self._execution_event_timer.setInterval(20)
        self._execution_event_timer.timeout.connect(
            lambda: self._drain_execution_events(limit=self._execution_event_drain_batch)
        )
        self.quick_control_coordinator = QuickControlCoordinator(
            self._controllers, self, settings=self._settings
        )
        self.quick_control_coordinator.set_hardware_request_builder(
            "rigol",
            lambda request, state: self.rigol_page.quick_control_hardware_request(
                request.target, request.text, state
            ),
        )
        self.quick_control_coordinator.set_hardware_request_builder(
            "keithley",
            lambda request, state: self.keithley_page.quick_control_hardware_request(
                request.target, request.text, state
            ),
        )
        self.quick_controls_window = QuickControlsWindow(self.quick_control_coordinator, self)
        self.quick_controls_window.restore_workspace()
        self.quick_controls_window.output_requested.connect(self._request_quick_control_output)
        self.quick_controls_window.output_group_requested.connect(
            self._request_quick_control_output_group
        )
        self.rigol_page.output_state_changed.connect(
            lambda channel, state: self.quick_controls_window.set_output_state(
                "rigol", channel, state
            )
        )
        self.keithley_page.output_state_changed.connect(
            lambda channel, state: self.quick_controls_window.set_output_state(
                "keithley", channel, state
            )
        )

        self.rigol_page.quick_control_draft_changed.connect(
            lambda target, text: self.quick_control_coordinator.publish_draft(
                target, text, source="device_card"
            )
        )
        self.keithley_page.quick_control_draft_changed.connect(
            lambda target, text: self.quick_control_coordinator.publish_draft(
                target, text, source="device_card"
            )
        )
        self.quick_control_coordinator.draft_changed.connect(
            self.rigol_page.quick_control_draft_changed_from_coordinator
        )
        self.quick_control_coordinator.draft_changed.connect(
            self.keithley_page.quick_control_draft_changed_from_coordinator
        )
        self.quick_control_coordinator.publish_draft_snapshot(
            self.rigol_page.quick_control_draft_snapshot(),
            source="device_card",
            mark_dirty=False,
        )
        self.quick_control_coordinator.publish_draft_snapshot(
            self.keithley_page.quick_control_draft_snapshot(),
            source="device_card",
            mark_dirty=False,
        )

        self.rigol_page.quick_setpoint_requested.connect(self.quick_control_coordinator.submit)
        self.keithley_page.quick_setpoint_requested.connect(self.quick_control_coordinator.submit)
        self.quick_control_coordinator.state_changed.connect(
            self.rigol_page.quick_setpoint_state_changed
        )
        self.quick_control_coordinator.state_changed.connect(
            self.keithley_page.quick_setpoint_state_changed
        )
        self.quick_control_coordinator.configuration_verified.connect(
            self.rigol_page.quick_control_configuration_verified
        )
        self.quick_control_coordinator.configuration_verified.connect(
            self.keithley_page.quick_control_configuration_verified
        )
        self.quick_control_coordinator.value_read.connect(self.rigol_page.quick_setpoint_value_read)
        self.quick_control_coordinator.value_read.connect(
            self.keithley_page.quick_setpoint_value_read
        )
        self.anritsu_page.quick_controls_requested.connect(self._show_quick_controls)
        self.rigol_page.quick_controls_requested.connect(self._show_quick_controls)
        self.keithley_page.quick_controls_requested.connect(self._show_quick_controls)
        self.connection_panels: dict[str, DeviceConnectionPanel] = {}
        for key, page in self._device_pages.items():
            resource, backend = self._device_connection_details(self._settings, key)
            panel = DeviceConnectionPanel(registry.get(key).display_name, resource)
            panel.update_resource(resource, backend)
            if key == "moke_box":
                panel.test_button.setToolTip(
                    "Open one TCP session, validate the documented VOUT response, then disconnect. "
                    "No gain or VOUT-setting command is sent."
                )
            # Keithley keeps its compact device title/live controls as the
            # page anchor; the single authoritative connection surface sits
            # immediately below it. Other device pages retain their current
            # connection-first composition.
            page.layout().insertWidget(2 if key == "keithley" else 0, panel)
            self.connection_panels[key] = panel
        self.recipe_page = RecipePage(
            self._settings,
            device_registry=self._composition.registry,
        )
        self.recipe_page.set_keithley_snapshot_provider(
            self.keithley_page.configuration_snapshot_for
        )
        self.recipe_page.set_rigol_snapshot_provider(self.rigol_page.configuration_snapshot_for)
        self.recipe_page.set_anritsu_snapshot_provider(
            self.anritsu_page.configuration_panel.configuration_snapshot
        )
        self.recipe_page.set_anritsu_sg_snapshot_provider(
            self.anritsu_page.signal_generator_snapshot
        )
        self.run_monitor = RunMonitorPage()
        self.results_page = ResultsPage(
            str(self._settings.storage.get("output_directory", "./measurements"))
        )
        self.elab_page = ElabPage(
            self._repository,
            settings=self._settings,
            env_path=Path(".env"),
            simulation=self._simulation,
        )
        self.inventory_page = SampleInventoryPage(self.inventory_store, self)
        self.inventory_page.active_target_changed.connect(self._on_active_sample_target_changed)
        self.inventory_page.open_result_requested.connect(self._open_inventory_result)
        self.recipe_page.change_target_requested.connect(lambda: self._navigate_to("inventory"))
        self.recipe_page.set_active_sample_target(self.inventory_store.get_active_target())
        self.keithley_characterization_page.set_inventory_store(self.inventory_store)
        self.inventory_page.active_target_changed.connect(
            self.keithley_characterization_page.set_active_sample_target
        )
        self.inventory_page.samples_updated.connect(
            self.keithley_characterization_page.refresh_samples_list
        )
        self.keithley_characterization_page.active_target_changed.connect(
            self._on_active_sample_target_changed
        )
        self.keithley_characterization_page.browse_samples_requested.connect(
            lambda: self._navigate_to("inventory")
        )
        self.elab_page.upload_completed_record.connect(self._on_elab_upload_completed)
        self.recipe_page.set_elab_context(
            profile_provider=self.elab_page.current_profile,
            credentials_provider=self.elab_page.current_credentials,
            available_templates_provider=self.elab_page.available_templates,
            refresh_templates_callback=self.elab_page.refresh_templates,
            navigate_to_elab=lambda: self._navigate_to("elab"),
        )
        self.elab_page.configuration_changed.connect(self.recipe_page.sync_elab_context)
        self.anritsu_page.set_manual_elab_context(
            configuration_provider=self.elab_page.manual_upload_configuration,
            upload_callback=self.elab_page.queue_manual_upload,
        )
        self._sync_elab_upload_controls()
        self.settings_page = SettingsPage(
            self._repository,
            read_only=self._simulation,
            access_policy=self._access,
        )
        self.dashboard.set_assignment_allowed(self._access.allows(Permission.ASSIGN_VISA))
        roles = ", ".join(sorted(role.value for role in self._access.identity.roles)) or "none"

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
                    "Open the hardware station settings to edit configured limits."
                )
            elif not self._access.allows(Permission.EDIT_SETTINGS):
                enabled = False
                reason = (
                    f"Access denied: current role(s) {roles} do not include "
                    "EDIT_SETTINGS. Use an engineer or service account."
                )
            else:
                enabled = True
                reason = "Edit this safety range. Values are validated before saving."
            field.edit_button.setEnabled(enabled)
            field.edit_button.setToolTip(reason)
            field.edit_button.setAccessibleName(f"Edit {device} safety limits")
            field.edit_button.setAccessibleDescription(reason)
            if hasattr(field, "range_pill"):
                if not enabled or not field.edit_button.isVisible():
                    field.range_pill.setCursor(Qt.CursorShape.ArrowCursor)
                    field.range_pill.setToolTip(reason)
                else:
                    field.range_pill.setCursor(Qt.CursorShape.PointingHandCursor)
                    field.range_pill.setToolTip(
                        f"Configured {device} safety envelope. Click to edit laboratory safety limits."
                    )
            return enabled

        for field in self.rigol_page.findChildren(LimitField):
            configure_limit_button(field, device="Rigol")
            field.edit_requested.connect(
                lambda field=field: self._edit_device_limit("rigol", field)
            )
        keithley_fields = set(self.keithley_page.findChildren(LimitField))
        if hasattr(self, "keithley_characterization_page") and self.keithley_characterization_page is not None:
            keithley_fields.update(self.keithley_characterization_page.findChildren(LimitField))
        for field in keithley_fields:
            editable = configure_limit_button(
                field,
                device="Keithley",
                fixed_instrument_range=(str(field.property("limitKey")) == "nplc"),
            )
            if editable:
                field.edit_requested.connect(
                    lambda field=field: self._edit_device_limit("keithley", field)
                )
        for field in self.anritsu_page.findChildren(LimitField):
            configure_limit_button(field, device="Anritsu")
            field.edit_requested.connect(
                lambda field=field: self._edit_device_limit("anritsu", field)
            )
        route_icons = {
            "overview": FluentIcon.HOME,
            "discovery": FluentIcon.SEARCH,
            "rigol": FluentIcon.MEDIA,
            "keithley": FluentIcon.POWER_BUTTON,
            "keithley_characterization": FluentIcon.SPEED_HIGH,
            "anritsu": FluentIcon.PROJECTOR,
            "moke_box": FluentIcon.IOT,
            "lakeshore_gaussmeter": FluentIcon.PIN,
            "sweeps": FluentIcon.DOCUMENT,
            "execution": FluentIcon.PLAY,
            "results": FluentIcon.FOLDER,
            "inventory": FluentIcon.TILES,
            "elab": FluentIcon.GLOBE,
            "settings": FluentIcon.SETTING,
        }
        route_specs = (
            (self.dashboard.navigation_pages["overview"], "overview", "Overview"),
            (self.dashboard.navigation_pages["discovery"], "discovery", "Discovery"),
            (
                self.rigol_page,
                "rigol",
                registry.get("rigol").display_name,
            ),
            (
                self.keithley_page,
                "keithley",
                registry.get("keithley").display_name,
            ),
            (
                self.keithley_characterization_page,
                "keithley_characterization",
                "Characterization",
            ),
            (
                self.anritsu_page,
                "anritsu",
                registry.get("anritsu").display_name,
            ),
            (
                self.moke_box_page,
                "moke_box",
                registry.get("moke_box").display_name,
            ),
            (
                self.lakeshore_gaussmeter_page,
                "lakeshore_gaussmeter",
                registry.get("lakeshore_gaussmeter").display_name,
            ),
            (self.recipe_page, "sweeps", "Sweeps"),
            (self.run_monitor, "execution", "Execution"),
            (self.results_page, "results", "Results"),
            (self.inventory_page, "inventory", "Samples"),
            (self.elab_page, "elab", "eLabFTW"),
            (self.settings_page, "settings", "Settings"),
        )
        self.navigation_routes: dict[str, FluentPageHost] = {}
        self.apparatus_navigation_item = self.navigationInterface.addItem(
            routeKey="apparatusMenu",
            icon=FluentIcon.DEVELOPER_TOOLS,
            text="Devices",
            selectable=False,
            position=NavigationItemPosition.TOP,
            tooltip="Connected measurement devices",
        )
        apparatus_routes = {
            "rigol",
            "keithley",
            "anritsu",
            "moke_box",
            "lakeshore_gaussmeter",
        }
        for widget, route, display_name in route_specs:
            if route == "settings":
                self.event_log_navigation_item = self.navigationInterface.addItem(
                    routeKey="eventLogToggle",
                    icon=FluentIcon.COMMAND_PROMPT,
                    text="Event log",
                    onClick=self._toggle_event_log_requested,
                    selectable=False,
                    position=NavigationItemPosition.BOTTOM,
                    tooltip="Show event log",
                )
                self.theme_navigation_item = self.navigationInterface.addItem(
                    routeKey="themeMenu",
                    icon=FluentIcon.BRUSH,
                    text="Theme",
                    onClick=self._open_theme_navigation_menu,
                    selectable=False,
                    position=NavigationItemPosition.BOTTOM,
                    tooltip="Choose application theme",
                )
            host = FluentPageHost(widget, self)
            host.setObjectName(f"{route}PageHost")
            self.navigation_routes[route] = host
            position = (
                NavigationItemPosition.BOTTOM if route == "settings" else NavigationItemPosition.TOP
            )
            if route == "keithley_characterization":
                parent_key = "keithleyPageHost"
            elif route in apparatus_routes:
                parent_key = "apparatusMenu"
            else:
                parent_key = None
            self.addSubInterface(
                host,
                route_icons[route],
                display_name,
                position=position,
                parent=parent_key,
            )
            widget._scroll_area = host.scroll_area
        # Keep the equipment group open on launch: device controls remain a
        # single click away while the navigation still communicates hierarchy.
        self.apparatus_navigation_item.setExpanded(True, ani=False)
        keithley_nav_item = self.navigationInterface.widget("keithleyPageHost")
        if keithley_nav_item is not None:
            if hasattr(keithley_nav_item, "expanded"):
                keithley_nav_item.expanded.connect(
                    lambda: QTimer.singleShot(0, self._sync_apparatus_navigation_height)
                )
            if hasattr(keithley_nav_item, "clicked"):
                keithley_nav_item.clicked.connect(
                    lambda: QTimer.singleShot(0, self._sync_apparatus_navigation_height)
                )
        self._apparatus_auto_collapsed = False
        self._apparatus_required_height = (
            self.navigationInterface.panel.vBoxLayout.minimumSize().height() + 1
        )
        self.apparatus_navigation_item.clicked.connect(
            lambda: QTimer.singleShot(0, self._sync_apparatus_navigation_height)
        )
        self.log = PlainTextEdit()
        self.log.setObjectName("eventLogText")
        self.log.setProperty("stationSurface", "raised")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(50)
        self._event_log_entries: deque[str] = deque(maxlen=2000)
        self._pending_log_ui_entries: list[str] = []
        self._log_ui_flush_timer = QTimer(self)
        self._log_ui_flush_timer.setInterval(50)
        self._log_ui_flush_timer.timeout.connect(self._flush_pending_log_ui)
        self.event_log_panel = SimpleCardWidget(self.fluent_content)
        self.event_log_panel.setObjectName("eventLogPanel")
        self.event_log_panel.setProperty("stationSurface", "surface")
        # The global log is a useful cross-page diagnostic surface, but it is
        # normally hidden by default so workspace pages have full vertical space.
        # It can be toggled on demand via the sidebar button or application menu.
        self._event_log_requested_visible = False
        self._event_log_compact_override = False
        event_log_layout = QVBoxLayout(self.event_log_panel)
        event_log_layout.setContentsMargins(12, 8, 12, 8)
        event_log_layout.setSpacing(6)
        event_log_header = QHBoxLayout()
        event_log_header.setContentsMargins(0, 0, 0, 0)
        event_log_header.addWidget(CaptionLabel("Event log", self.event_log_panel))
        event_log_header.addStretch(1)
        self.traffic_only_button = PushButton("TX/RX only", self.event_log_panel)
        self.traffic_only_button.setCheckable(True)
        self.traffic_only_button.setAccessibleName("Show VISA TX and RX traffic only")
        self.traffic_only_button.setToolTip(
            "Filter the visible log to exact instrument commands, responses and transport errors."
        )
        self.copy_traffic_button = PushButton("Copy TX/RX", self.event_log_panel)
        self.copy_traffic_button.setToolTip(
            "Copy all recorded instrument transport messages for diagnostics."
        )
        self.clear_log_button = PushButton("Clear", self.event_log_panel)
        self.clear_log_button.setToolTip("Clear the visible in-memory event log.")
        event_log_header.addWidget(self.traffic_only_button)
        event_log_header.addWidget(self.copy_traffic_button)
        event_log_header.addWidget(self.clear_log_button)
        event_log_layout.addLayout(event_log_header)
        event_log_layout.addWidget(self.log)
        self.traffic_only_button.toggled.connect(self._refresh_event_log_view)
        self.copy_traffic_button.clicked.connect(self._copy_traffic_log)
        self.clear_log_button.clicked.connect(self._clear_event_log)
        self.shell_splitter.addWidget(self.event_log_panel)
        self.shell_splitter.setStretchFactor(0, 1)
        self.shell_splitter.setStretchFactor(1, 0)
        self.event_log_panel.setVisible(False)
        self.shell_splitter.setSizes([800, 0])
        self.dashboard.emergency_requested.connect(self._emergency_off_all)
        self.dashboard.readiness_changed.connect(lambda _ready: self._refresh_safety_strip())
        self.dashboard.assignments_requested.connect(self._save_discovered_assignments)
        self.dashboard.moke_assignment_requested.connect(self._save_moke_assignment)
        self.dashboard.status.connect(self._log)
        self.settings_page.settings_saved.connect(self._settings_saved)
        self.elab_page.settings_saved.connect(self._settings_saved)
        self.elab_page.configuration_changed.connect(self._sync_elab_upload_controls)
        self.anritsu_page.settings_readback_requested.connect(self._save_anritsu_readback_defaults)
        self.keithley_page.settings_assignment_requested.connect(
            self._queue_keithley_assignment_save
        )
        self.keithley_page.settings_defaults_requested.connect(self._stage_keithley_defaults)
        self.recipe_page.run_requested.connect(self._start_run)
        self.recipe_page.change_target_requested.connect(lambda: self._navigate_to("inventory"))
        self.recipe_page.settings_issue_requested.connect(self._open_settings_issue)
        self.recipe_page.plan_preflight_changed.connect(self.dashboard.update_plan_preflight)
        self.inventory_page.active_target_changed.connect(self._on_active_sample_target_changed)
        self.inventory_page.open_result_requested.connect(self._open_inventory_result)
        self.elab_page.upload_completed_record.connect(self._on_elab_upload_completed)
        self.recipe_page.set_active_sample_target(self.inventory_store.get_active_target())
        self.results_page.resume_requested.connect(self._resume_run)
        self.results_page.open_sweep_requested.connect(self._open_historical_thatec_sweep)
        self.results_page.result_selected.connect(self.elab_page.set_selected_result)
        self.run_monitor.stop_requested.connect(self._run_controller.request_stop)
        self.run_monitor.pause_requested.connect(self._run_controller.request_pause)
        self.run_monitor.resume_requested.connect(self._run_controller.request_resume)
        self.run_monitor.manual_next_requested.connect(self._run_controller.advance_manual_step)
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
            self.inventory_page,
            self.elab_page,
            self.settings_page,
        ):
            page.status.connect(self._log)
        self.theme_group = QActionGroup(self)
        self.theme_group.setExclusive(True)
        self.theme_actions: dict[str, QAction] = {}
        configured_theme = str(self._settings.ui.get("theme", "system")).lower()
        if configured_theme not in {"light", "dark", "system"}:
            configured_theme = "system"
        self._configured_theme_mode = configured_theme
        for mode, label in (("system", "System"), ("light", "Light"), ("dark", "Dark")):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setData(mode)
            action.setChecked(mode == configured_theme)
            action.triggered.connect(
                lambda checked=False, mode=mode: checked and self._activate_theme_mode(mode)
            )
            self.theme_group.addAction(action)
            self.addAction(action)
            self.theme_actions[mode] = action
        self.theme_navigation_menu = RoundMenu("Theme", self)
        self.theme_navigation_menu.addActions(tuple(self.theme_actions.values()))
        self.event_log_action = QAction("Event log", self)
        self.event_log_action.setCheckable(True)
        self.event_log_action.setChecked(False)
        self.event_log_action.toggled.connect(self._set_event_log_requested_visible)
        self.addAction(self.event_log_action)
        quit_action = QAction("Safe shutdown", self)
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)
        application_menu = RoundMenu("Application", self)
        application_menu.addAction(self.event_log_action)
        application_menu.addAction(quit_action)
        self.application_menu_button = TransparentDropDownToolButton(
            FluentIcon.MENU,
            self.titleBar,
        )
        self.application_menu_button.setAccessibleName("Application menu")
        self.application_menu_button.setToolTip("Application menu")
        self.application_menu_button.setMenu(application_menu)
        self.titleBar.hBoxLayout.insertWidget(
            self.titleBar.hBoxLayout.count() - 1,
            self.application_menu_button,
            0,
            Qt.AlignmentFlag.AlignVCenter,
        )
        self.estop_shortcut = QShortcut(QKeySequence("Ctrl+Shift+E"), self)
        self.estop_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.estop_shortcut.activated.connect(self._emergency_off_all)
        self.scan_shortcut = QShortcut(QKeySequence("F5"), self)
        self.scan_shortcut.activated.connect(self.dashboard._scan_visa)
        self.safety_strip.estop_requested.connect(self._emergency_off_all)
        self.safety_strip.save_settings_requested.connect(self._save_all_settings)
        self._refresh_safety_strip()
        self._sync_event_log_navigation_state(False)

    def _navigate_to(self, route: str) -> None:
        target = self.navigation_routes[route]
        nav_item = self.navigationInterface.widget(target.objectName())
        if nav_item is not None and getattr(nav_item, "treeParent", None) is not None:
            p = nav_item.treeParent
            while p:
                if hasattr(p, "setExpanded") and not p.isExpanded:
                    p.setExpanded(True, ani=False)
                p = getattr(p, "treeParent", None)
        self.switchTo(target)
        self._sync_shell_splitter_layout()

    def _toggle_event_log_requested(self) -> None:
        """Toggle global event log panel visibility from the navigation bar."""

        self._set_event_log_requested_visible(not self._event_log_requested_visible)

    def _sync_event_log_navigation_state(self, show_log: bool | None = None) -> None:
        """Synchronize the sidebar navigation button appearance with the event log state."""

        nav_item = getattr(self, "event_log_navigation_item", None)
        if nav_item is None:
            return
        if show_log is None:
            compact_execution = (
                hasattr(self, "navigation_routes")
                and self._current_route() == "execution"
                and self.width() < 1_200
                and not getattr(self, "_event_log_compact_override", False)
            )
            show_log = (
                getattr(self, "_event_log_requested_visible", False) and not compact_execution
            )

        nav_item.isSelected = show_log
        if hasattr(nav_item, "itemWidget"):
            nav_item.itemWidget.isSelected = show_log
            if show_log:
                nav_item.setIcon(FluentIcon.COMMAND_PROMPT)
                nav_item.itemWidget.setIcon(FluentIcon.COMMAND_PROMPT)
                nav_item.itemWidget.setLightTextColor(QColor(0, 0, 0))
                nav_item.itemWidget.setDarkTextColor(QColor(255, 255, 255))
            else:
                muted_icon = FluentIcon.COMMAND_PROMPT.colored(
                    QColor(140, 140, 140), QColor(140, 140, 140)
                )
                nav_item.setIcon(muted_icon)
                nav_item.itemWidget.setIcon(muted_icon)
                nav_item.itemWidget.setLightTextColor(QColor(140, 140, 140))
                nav_item.itemWidget.setDarkTextColor(QColor(140, 140, 140))
            nav_item.itemWidget.update()
        nav_item.setToolTip("Hide event log" if show_log else "Show event log")
        nav_item.update()

    def _set_event_log_requested_visible(self, visible: bool) -> None:
        """Apply the operator's log preference to the responsive shell."""

        self._event_log_requested_visible = bool(visible)
        # A checked action while compact is an explicit request to keep the
        # diagnostic drawer open, overriding the automatic Execution collapse.
        self._event_log_compact_override = bool(visible)
        self._sync_shell_splitter_layout()

    def _sync_shell_splitter_layout(self) -> None:
        """Keep the compact Execution viewport focused on the live procedure."""

        if not hasattr(self, "event_log_panel") or not hasattr(
            self, "navigation_routes"
        ):
            return
        compact_execution = (
            self._current_route() == "execution"
            and self.width() < 1_200
            and not self._event_log_compact_override
        )
        show_log = self._event_log_requested_visible and not compact_execution
        if self.event_log_panel.isVisible() != show_log:
            self.event_log_panel.setVisible(show_log)
        action = getattr(self, "event_log_action", None)
        if action is not None and compact_execution:
            # Reflect an automatic collapse in the menu without changing the
            # saved operator preference or invoking the toggle handler.
            if action.isChecked():
                with QSignalBlocker(action):
                    action.setChecked(False)
        elif action is not None and not compact_execution:
            desired = bool(self._event_log_requested_visible)
            if action.isChecked() != desired:
                with QSignalBlocker(action):
                    action.setChecked(desired)
        self._sync_event_log_navigation_state(show_log)
        current_sizes = self.shell_splitter.sizes()
        if show_log:
            total = max(0, self.shell_splitter.height())
            target_sizes = [max(0, total - 125), 125]
            if len(current_sizes) < 2 or current_sizes[1] != 125:
                self.shell_splitter.setSizes(target_sizes)
        else:
            # Hidden QSplitter children do not consume space, but setting an
            # explicit size makes the expansion deterministic after resize.
            if len(current_sizes) < 2 or current_sizes[1] != 0:
                self.shell_splitter.setSizes([max(0, self.shell_splitter.height()), 0])

    def _open_settings_issue(self, issue: SettingsIssue) -> None:
        """Navigate only; correcting a safety profile never changes live outputs."""

        self._navigate_to("settings")
        self.settings_page.reveal_settings_issues(issue.paths, issue.message)

    def _current_route(self) -> str:
        current = self.stackedWidget.currentWidget()
        return next(
            (route for route, host in self.navigation_routes.items() if host is current),
            "overview",
        )

    def _set_route_enabled(self, route: str, enabled: bool) -> None:
        host = self.navigation_routes[route]
        host.setEnabled(enabled)
        item = self.navigationInterface.widget(host.objectName())
        if item is not None:
            item.setEnabled(enabled)

    def _refresh_safety_strip(self) -> None:
        active_outputs = sum(state == "output_on" for state in self._device_states.values())
        self.safety_strip.update_snapshot(
            StationSafetySnapshot(
                ready=self.dashboard.evaluate_readiness().ready,
                active_outputs=active_outputs,
                simulation=self._simulation,
                actor=self._access.identity.username,
                roles=tuple(sorted(role.value for role in self._access.identity.roles)),
            )
        )

    def _open_theme_navigation_menu(self) -> None:
        position = self.theme_navigation_item.mapToGlobal(
            self.theme_navigation_item.rect().topRight()
        )
        self.theme_navigation_menu.exec(position)

    def _activate_theme_mode(self, mode: str) -> None:
        self.theme_actions[mode].setChecked(True)
        self._set_theme_mode(mode)

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
        self.stackedWidget.setAccessibleName("Application workspace")
        self.log.setAccessibleName("Event log")

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
            return (
                settings.lakeshore_gaussmeter.resource,
                settings.lakeshore_gaussmeter.visa_backend,
            )
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

    @staticmethod
    def _synchronised_max_abs(
        minimum: object,
        maximum: object,
        original_max_abs: object,
    ) -> object:
        """Keep a hidden max_abs bound aligned with the edited MIN/MAX pair."""

        if not isinstance(original_max_abs, str):
            return original_max_abs
        for dimension in (
            DIMENSION_CURRENT,
            DIMENSION_VOLTAGE,
            DIMENSION_POWER,
            DIMENSION_FREQUENCY,
            DIMENSION_RESISTANCE,
            DIMENSION_TIME,
        ):
            try:
                parse_quantity(original_max_abs, dimension)
                minimum_si = parse_quantity(str(minimum), dimension).si_value
                maximum_si = parse_quantity(str(maximum), dimension).si_value
            except Exception:
                continue
            return format_quantity_auto(max(abs(minimum_si), abs(maximum_si)), dimension)
        raise ValueError(f"Cannot synchronize max_abs={original_max_abs!r} with the edited range.")

    def _limit_edit_spec(self, device: str, field: LimitField) -> tuple[str, tuple[str, ...], bool]:
        key = str(field.property("limitKey"))
        if device == "rigol":
            channel = self.rigol_page.channel.currentText()
            path = ("devices", "rigol", "safety", "channels", channel, "lab_limits", key)
            return f"Rigol CH{channel} — {key.replace('_', ' ')}", path, True
        if device == "anritsu":
            path = ("devices", "anritsu", "safety", key)
            return f"Anritsu — {key.replace('_', ' ')}", path, True

        if (
            field.property("characterizationField")
            and hasattr(self, "keithley_characterization_page")
            and self.keithley_characterization_page is not None
        ):
            channel = self.keithley_characterization_page._selected_channel()
            mode = "current" if self.keithley_characterization_page._is_current_mode() else "voltage"
        else:
            channel = self.keithley_page.channel.currentText()
            mode = self.keithley_page.mode.currentText()
        if mode == "measure_only" and key in {"level", "compliance", "source_range"}:
            raise ConfigurationError(
                "Source limits are not applicable while Keithley is in measure-only mode."
            )
        mappings = {
            "level": "source_current" if mode == "current" else "source_voltage",
            "compliance": "voltage_compliance" if mode == "current" else "current_compliance",
            "settle": "point_settle_time",
            "max_abs_power": "max_abs_power",
        }
        if key in {"source_range", "measure_voltage_range", "measure_current_range"}:
            raise ConfigurationError(
                "Instrument ranges are edited directly in the Keithley form after "
                "disabling autorange. Their hardware limits are fixed and are not "
                "DUT measured-value trip thresholds."
            )
        mapped = mappings[key]
        path = ("devices", "keithley", "safety", "channels", channel, "lab_limits", mapped)
        return (
            f"Keithley CH{channel} — {mapped.replace('_', ' ')}",
            path,
            key != "max_abs_power",
        )

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
            base_raw = self.settings_page._raw if self.settings_page._dirty else loaded.raw
            raw = deepcopy(base_raw)
            title, path, maximum_enabled = self._limit_edit_spec(device, field)
            range_data = self._nested_value(raw, path)
            if range_data is None:
                range_data = {"min": "", "max": ""}
            scalar_limit = not isinstance(range_data, dict)
            minimum = range_data if scalar_limit else range_data.get("min")
            maximum = range_data.get("max") if maximum_enabled and not scalar_limit else None
        except (ConfigurationError, KeyError, TypeError) as exc:
            QMessageBox.critical(self, "Cannot edit limits", str(exc))
            return

        dialog = LimitEditDialog(
            title,
            minimum,
            maximum,
            maximum_enabled=maximum_enabled,
            value_label="Maximum power" if scalar_limit else "Minimum",
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            if scalar_limit:
                replacement = self._coerce_limit_value(dialog.minimum.text(), minimum)
                parse_quantity(str(replacement), DIMENSION_POWER)
            else:
                replacement = dict(range_data)
                replacement["min"] = self._coerce_limit_value(dialog.minimum.text(), minimum)
                if maximum_enabled:
                    replacement["max"] = self._coerce_limit_value(dialog.maximum.text(), maximum)
                if "max_abs" in replacement and maximum_enabled:
                    replacement["max_abs"] = self._synchronised_max_abs(
                        replacement["min"],
                        replacement["max"],
                        replacement["max_abs"],
                    )
            container: Any = raw
            for part in path[:-1]:
                container = container[part]
            container[path[-1]] = replacement
            if device == "keithley":
                channel_limits = self._nested_value(raw, path[: path.index("lab_limits") + 1])
                if not isinstance(channel_limits, dict):
                    raise ConfigurationError("Keithley channel limits are not configured.")
                if scalar_limit:
                    primary_path = ("max_abs_power",)
                    primary_value = str(replacement)
                else:
                    changed_boundaries = [
                        boundary
                        for boundary in ("min", "max")
                        if maximum_enabled or boundary == "min"
                        if replacement.get(boundary) != range_data.get(boundary)
                    ]
                    primary_boundary = "max" if "max" in changed_boundaries else "min"
                    primary_path = (str(path[-1]), primary_boundary)
                    primary_value = str(replacement[primary_boundary])
                proposal = propose_keithley_limit_adjustments(
                    channel_limits, primary_path, primary_value
                )
                if proposal.adjustments:
                    confirmation = KeithleyLimitProposalDialog(proposal, self)
                    if confirmation.exec() != QDialog.DialogCode.Accepted:
                        return
                    for adjustment in proposal.adjustments:
                        adjustment_target: Any = channel_limits
                        for part in adjustment.path[:-1]:
                            adjustment_target = adjustment_target[part]
                        adjustment_target[adjustment.path[-1]] = adjustment.proposed
            settings = StationSettings.model_validate(raw)
        except (ConfigurationError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.critical(
                self,
                "Invalid safety limits",
                format_settings_validation_error(exc),
            )
            return

        changed_paths = SettingsPage._changed_leaf_paths(base_raw, raw)
        if self.settings_page._can_stage_limit_snapshot(changed_paths):
            self.settings_page.stage_limit_snapshot(settings, raw, changed_paths)
        else:
            self.settings_page.stage_external_snapshot(settings, raw)
        if scalar_limit:
            field.editor.setText(str(replacement))
        else:
            fields = (field,)
            if device == "anritsu":
                # Start and Stop are two editors governed by the same
                # frequency safety range. Never show two different limits
                # while the shared change is staged for SAVE SETTINGS.
                fields = tuple(
                    candidate
                    for candidate in self.anritsu_page.findChildren(LimitField)
                    if candidate.property("limitKey") == field.property("limitKey")
                )
            for candidate in fields:
                candidate.set_limits(replacement["min"], replacement.get("max", "N/A"))
        if device == "keithley":
            self.keithley_page.set_settings(settings)
            if hasattr(self, "keithley_characterization_page") and self.keithley_characterization_page is not None:
                self.keithley_characterization_page.set_settings(settings)
        if device == "anritsu":
            self.anritsu_page.banner.show_message(
                "Anritsu safety limits are staged. Press SAVE SETTINGS before "
                "applying a spectrum outside the previously saved range.",
                severity="warning",
                timeout_ms=0,
            )
        self._log(f"Safety limits staged: {title}; press SAVE SETTINGS")

    def _set_theme_mode(self, mode: str, *, persist: bool = True) -> None:
        theme = effective_theme(mode)
        application = QApplication.instance()
        changed = application is None or application.property("stationAppliedTheme") != theme
        if application is not None:
            applied_theme = apply_application_theme(application, mode)
            self._apply_navigation_surface(applied_theme.tokens.surface)
            application.setProperty("activeTheme", theme)
        # Plot canvases maintain their own brushes and may be hidden when a
        # global theme is selected. Reapply them across all top-level surfaces
        # even when the application property already names this theme.
        for top_level in QApplication.topLevelWidgets():
            for plot in top_level.findChildren(SpectrumPlotWidget):
                plot.apply_theme(theme)
            for heatmap in top_level.findChildren(HeatmapResultsTab):
                heatmap.apply_theme(theme)
        if changed:
            self.theme_changed.emit(theme)
        self._configured_theme_mode = mode
        self._sync_event_log_navigation_state()
        if persist:
            self._persist_theme(mode)
        self._log(f"Theme changed to {mode} ({theme})" + (" and saved" if persist else ""))

    def _apply_navigation_surface(self, color: str) -> None:
        """Keep Fluent's translucent navigation panel opaque across theme swaps."""

        panel = self.navigationInterface.panel
        palette = panel.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(color))
        panel.setPalette(palette)
        panel.setAutoFillBackground(True)
        panel.update()

    def refresh_system_theme(self) -> None:
        if self.theme_actions["system"].isChecked():
            self._set_theme_mode("system", persist=False)

    def _persist_theme(self, theme: str) -> None:
        try:
            loaded = self._repository.load()
            loaded.raw.setdefault("ui", {})["theme"] = theme
            self._settings = self._repository.save_raw(loaded.raw)
        except ConfigurationError as exc:
            self._log(f"Theme was applied but could not be saved: {exc}")
            return
        if self.settings_page._dirty:
            self.settings_page.accept_external_snapshot(self._settings, loaded.raw)
        else:
            self.settings_page.reload()

    def _restore_workspace(self) -> None:
        settings = QSettings("LabControl", "LabControl")
        geometry = settings.value("main_window/geometry")
        content_splitter = settings.value("main_window/content_splitter")
        splitter = settings.value("anritsu/splitter")
        if geometry is not None:
            self.restoreGeometry(geometry)
        # Navigation starts expanded whenever the window has enough room.
        # Narrow windows start with the compact rail so page content remains
        # operable; expansion remains an operator's current-session preference.
        self._navigation_expanded_preference = True
        self._navigation_layout_initialized = False
        if content_splitter is not None:
            self.shell_splitter.restoreState(content_splitter)
        if splitter is not None:
            self.anritsu_page.workspace_splitter.restoreState(splitter)
        route = str(settings.value("main_window/current_route", "overview"))
        if route not in self.navigation_routes:
            route = "overview"
        self._navigate_to(route)

    def _save_workspace(self) -> None:
        settings = QSettings("LabControl", "LabControl")
        settings.setValue("main_window/geometry", self.saveGeometry())
        settings.setValue(
            "main_window/content_splitter",
            self.shell_splitter.saveState(),
        )
        settings.remove("main_window/navigation_expanded")
        settings.setValue("main_window/current_route", self._current_route())
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
            panel.disconnect_requested.connect(
                lambda current=controller: current.call("disconnect")
            )
            panel.test_requested.connect(
                lambda current=controller, current_card=panel: (
                    current_card.set_testing(True),
                    current.call("test_communication"),
                )
            )
            controller.state_changed.connect(card.update_state)
            controller.state_changed.connect(panel.update_state)
            controller.state_changed.connect(
                lambda state, device=name: self._set_device_state(device, state)
            )
            controller.result.connect(
                lambda operation, result, device=name, current=panel: self._device_result(
                    device, current, operation, result
                )
            )
            controller.error.connect(
                lambda operation, error, device=name: self._device_error(device, operation, error)
            )
            controller.traffic.connect(
                lambda message, device=name: self._log(
                    f"{device.upper()} {'TCP' if device == 'moke_box' else 'VISA'} {message}"
                )
            )
            if name == "rigol":
                controller.capabilities_changed.connect(self.rigol_page.set_capabilities)
            elif name == "anritsu":
                controller.capabilities_changed.connect(self.anritsu_page.set_capabilities)

    def _show_quick_controls(self) -> None:
        if not self.quick_controls_window._selected:
            self.quick_controls_window.choose_controls()
        # Output controls remain available even when the operator has not
        # pinned a numeric quick-control row yet.
        self.quick_control_coordinator.refresh()
        for device, page in (
            ("rigol", self.rigol_page),
            ("keithley", self.keithley_page),
        ):
            for channel, state in page.output_state_snapshot().items():
                self.quick_controls_window.set_output_state(device, channel, state)
        self.quick_controls_window.show()
        self.quick_controls_window.raise_()
        self.quick_controls_window.activateWindow()

    def _request_quick_control_output(self, device: str, channel: str, enabled: bool) -> None:
        """Send floating-window output requests through the owning device page."""

        if device == "rigol":
            if channel not in {"1", "2"}:
                self._log(f"Rejected Quick controls Rigol channel {channel!r}")
                return
            self.rigol_page.channel.setCurrentText(channel)
            self.rigol_page.request_output(enabled)
            return
        if device == "keithley":
            if channel not in {"A", "B"}:
                self._log(f"Rejected Quick controls Keithley channel {channel!r}")
                return
            self.keithley_page.request_channel_output(channel, enabled)
            return
        self._log(f"Rejected Quick controls output request for {device!r}")

    def _request_quick_control_output_group(self, device: str, enabled: bool) -> None:
        """Route an explicit group request through the device safety boundary."""

        if device == "keithley":
            self.keithley_page.request_output_group(("A", "B"), enabled)
            return
        self._log(f"Rejected Quick controls output group request for {device!r}")

    def _manual_spectrum_metadata_values(self) -> tuple[ManualMetadataValue, ...]:
        """Collect last-confirmed numeric values from the visible device pages."""

        values: list[ManualMetadataValue] = []
        for page in (
            self.anritsu_page,
            self.rigol_page,
            self.keithley_page,
            self.moke_box_page,
            self.lakeshore_gaussmeter_page,
        ):
            provider = getattr(page, "manual_metadata_values", None)
            if callable(provider):
                values.extend(provider())
        unique: dict[str, ManualMetadataValue] = {}
        for value in values:
            key = value.key
            if key:
                unique.setdefault(key, value)
        return tuple(unique.values())

    def _manual_spectrum_device_idn(self) -> dict[str, str]:
        """Return identities already verified by manual station sessions."""

        return dict(self._manual_device_idn)

    def _device_result(
        self, device: str, card: DeviceConnectionPanel, operation: str, result: object
    ) -> None:
        if operation == "connect":
            identity = str(getattr(result, "idn", "") or "")
            if identity:
                self._manual_device_idn[device] = identity
            card.set_connecting(False)
            card.update_identity(result)
            self.dashboard.cards[device].update_identity(result)
            self.dashboard.mark_identity_verified(device)
            self._log(f"Connected: {getattr(result, 'idn', result)}")
        elif operation == "disconnect":
            self._manual_device_idn.pop(device, None)
            card.set_connecting(False)
            card.identity.setText("IDN: not connected")
            self.dashboard.cards[device].identity.setText("IDN: not connected")
            self.dashboard.clear_identity_verified(device)
            self._log("Instrument disconnected")
        elif operation == "replace_adapter":
            self._manual_device_idn.pop(device, None)
            card.set_reconfiguring(False)
            card.update_state("disconnected")
            card.identity.setText("IDN: not connected")
            self.dashboard.cards[device].set_reconfiguring(False)
            self.dashboard.cards[device].update_state("disconnected")
            self.dashboard.cards[device].identity.setText("IDN: not connected")
            self.dashboard.clear_identity_verified(device)
            self._log(f"VISA ADAPTER REPLACE COMPLETE: {card.summary.text()}")
        elif operation == "test_communication" and isinstance(result, dict):
            self._manual_device_idn.pop(device, None)
            card.set_testing(False)
            # Communication test uses a temporary session and disconnects it;
            # a successful identity check must not look like an active link.
            card.update_state("disconnected")
            features = ", ".join(str(item) for item in result.get("features", ())) or "basic VISA"
            options = (
                ", ".join(str(item) for item in result.get("hardware_options", ()))
                or "not reported"
            )
            card.identity.setText(
                f"TEST PASS: {result.get('vendor', '')} {result.get('model', '')} • "
                f"SN {result.get('serial', '—')} • FW {result.get('firmware', '—')}\n"
                f"Protocols/features: {features} • Options: {options}"
            )
            self.dashboard.cards[device].identity.setText(card.identity.text())
            self.dashboard.clear_identity_verified(device)
            self._log(
                f"Communication test passed: {result.get('idn', '')}; "
                f"features={features}; options={options}"
            )
        elif operation == "apply_limit_settings":
            self._pending_limit_rollbacks.pop(device, None)
            self._log(f"Safety limits applied immediately: {device}")

    def _device_error(self, device: str, operation: str, error: str) -> None:
        if operation == "replace_adapter":
            self.dashboard.cards[device].set_reconfiguring(False)
        elif operation == "apply_limit_settings":
            previous = self._pending_limit_rollbacks.pop(device, None)
            rollback_error: str | None = None
            if previous is not None:
                try:
                    raw = self._repository.load().raw
                    raw["devices"][device] = getattr(previous.devices, device).model_dump(
                        mode="python"
                    )
                    restored = self._repository.save_raw(raw)
                    self._apply_settings_to_ui(restored)
                    self.settings_page.reload()
                except Exception as exc:
                    rollback_error = str(exc)
            detail = error
            if previous is not None and rollback_error is None:
                detail += "\n\nThe saved limits were rolled back and the UI was restored."
            elif rollback_error is not None:
                detail += f"\n\nAutomatic settings rollback also failed: {rollback_error}"
            QMessageBox.critical(
                self,
                f"{getattr(self._settings, device).display_name} — limits not applied",
                detail,
            )
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
        self._refresh_safety_strip()

    def _guard_manual_operation(self, device: str, operation: str, payload: object) -> None:
        """Fail closed for new energy-producing operations after audit I/O failure."""

        if device in self._leased_run_devices and operation not in {
            "emergency_off",
            "recover_from_compliance",
            "set_compliance_policy",
        }:
            raise ConfigurationError(
                f"{getattr(self._settings, device).display_name} is leased to the active recipe run."
            )

        # De-energising and disconnecting are never blocked by RBAC or audit
        # health. This invariant is stronger than any normal user permission.
        if operation in {
            "emergency_off",
            "recover_from_compliance",
            "set_compliance_policy",
            "ramp_to_zero",
            "stop_live",
            "disconnect",
        }:
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
        if operation == "set_output_group":
            try:
                _channels, enabled = payload  # type: ignore[misc]
            except (TypeError, ValueError):
                enabled = True
            if not bool(enabled):
                return
        energizing_operations = {
            "configure",
            "configure_output",
            "configure_modulation",
            "configure_sweep",
            "configure_burst",
            "synchronize_phases",
            "set_output",
            "set_output_group",
            "trigger_sweep",
            "trigger_burst",
            "ramp_to_level",
            "quick_setpoint",
            "quick_configure",
            "configure_signal_generator",
            "configure_advanced_spectrum",
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
            "trigger_sweep",
            "trigger_burst",
            "ramp_to_level",
            "quick_setpoint",
        }
        if operation == "set_output":
            try:
                _channel, enabled = payload  # type: ignore[misc]
            except (TypeError, ValueError):
                energizing = True
            else:
                energizing = bool(enabled)
        if operation == "set_output_group":
            try:
                _channels, enabled = payload  # type: ignore[misc]
            except (TypeError, ValueError):
                energizing = True
            else:
                energizing = bool(enabled)
        if operation == "set_signal_generator_output":
            energizing = bool(payload)
        if energizing:
            raise ConfigurationError(
                "The durable audit log is unavailable. OUTPUT ON is locked; "
                "OUTPUT OFF and E-STOP remain available."
            )

    def _assert_audit_ready_for_run(self) -> None:
        if not self._audit_healthy:
            raise ConfigurationError(
                "The durable audit log is unavailable. A measurement run cannot start."
            )

    def _start_run(
        self,
        plan: object,
        outputs_forced_off: bool = False,
        execution_mode: str = "measurement",
        output_dir_override: str = "",
        file_stem_override: str = "",
    ) -> None:
        selected_execution_mode = ExecutionMode.coerce(execution_mode)
        if outputs_forced_off:
            selected_execution_mode = ExecutionMode.DRY_RUN
        outputs_forced_off = selected_execution_mode is ExecutionMode.DRY_RUN
        if getattr(self, "_emergency_inhibit", False):
            QMessageBox.warning(
                self,
                "E-STOP Latched",
                "Emergency stop has been triggered and outputs are inhibited. "
                "Clear the emergency stop before starting a measurement.",
            )
            return
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
        has_recipe_elab = getattr(plan, "elab_upload_config", None) is not None
        self._run_upload_to_elab_requested = (
            has_recipe_elab or self.recipe_page.save_to_elab_check.isChecked()
        )
        self._run_elab_upload_config = getattr(plan, "elab_upload_config", None)
        self._open_sweep_readiness(
            plan,
            outputs_forced_off=outputs_forced_off,
            execution_mode=selected_execution_mode.value,
            output_dir_override=output_dir_override,
            file_stem_override=file_stem_override,
        )

    def _open_sweep_readiness(
        self,
        plan: object,
        *,
        outputs_forced_off: bool = False,
        execution_mode: str = "measurement",
        output_dir_override: str = "",
        file_stem_override: str = "",
        on_start: Callable[[SweepDeviceReadinessDialog], None] | None = None,
    ) -> SweepDeviceReadinessDialog:
        """Show current connection evidence for every device the plan needs."""

        required = tuple(sorted(set(getattr(plan, "required_devices", ()) or ())))
        display_names = {
            "rigol": "Rigol DG1032Z",
            "keithley": "Keithley 2600",
            "anritsu": "Anritsu MS2830A",
            "moke_box": "MOKE Box",
            "lakeshore_gaussmeter": "Lake Shore 475",
        }
        additional_safety_devices = ()
        if execution_mode != ExecutionMode.DRY_RUN.value:
            additional_safety_devices = tuple(
                device
                for device in ("rigol", "keithley", "anritsu")
                if device not in required
            )
        previous = self._sweep_readiness_dialog
        if previous is not None:
            previous.close()
        dialog = SweepDeviceReadinessDialog(
            required,
            display_names,
            self,
            additional_safety_devices=additional_safety_devices,
        )
        self._sweep_readiness_dialog = dialog
        cleanup_slots: list[tuple[object, object]] = []
        for device in required:
            self._update_sweep_readiness_device(dialog, device)
            def slot_state(_state: object, dev: str = device, cur: object = dialog) -> None:
                self._update_sweep_readiness_device(cur, dev)

            def slot_res(_operation: object, _result: object, dev: str = device, cur: object = dialog) -> None:
                self._update_sweep_readiness_device(cur, dev)

            def slot_err(_operation: object, error: object, dev: str = device, cur: object = dialog) -> None:
                self._update_sweep_readiness_device(cur, dev, error)

            controller = self._controllers[device]
            controller.state_changed.connect(slot_state)
            controller.result.connect(slot_res)
            controller.error.connect(slot_err)
            cleanup_slots.extend([
                (controller.state_changed, slot_state),
                (controller.result, slot_res),
                (controller.error, slot_err),
            ])
        dialog.connect_missing_requested.connect(self._connect_sweep_devices)
        if on_start is not None:
            dialog.start_requested.connect(lambda current=dialog: on_start(current))
        else:
            dialog.start_requested.connect(
                lambda current=dialog: self._start_run_from_ready_dialog(
                    current,
                    plan,
                    outputs_forced_off=outputs_forced_off,
                    execution_mode=execution_mode,
                    output_dir_override=output_dir_override,
                    file_stem_override=file_stem_override,
                )
            )
        def _on_dialog_finished(_result: int = 0, current: SweepDeviceReadinessDialog = dialog) -> None:
            for sig, slot in cleanup_slots:
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            self._clear_sweep_readiness_dialog(current)
        dialog.finished.connect(_on_dialog_finished)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def _clear_sweep_readiness_dialog(self, dialog: SweepDeviceReadinessDialog) -> None:
        if self._sweep_readiness_dialog is dialog:
            self._sweep_readiness_dialog = None

    def _update_sweep_readiness_device(
        self,
        dialog: SweepDeviceReadinessDialog,
        device: str,
        error: str | None = None,
    ) -> None:
        if self._sweep_readiness_dialog is not dialog:
            return
        dialog.update_device(
            device,
            self._device_states.get(device, "disconnected"),
            bool(self._manual_device_idn.get(device)),
            error,
        )

    def _connect_sweep_devices(self, devices: tuple[str, ...]) -> None:
        for device in devices:
            if self._device_states.get(device, "disconnected") != "disconnected":
                continue
            panel = self.connection_panels[device]
            panel.set_connecting(True)
            self._controllers[device].call("connect")

    def _active_device_controllers(self) -> dict[str, object]:
        return {
            device: controller
            for device, controller in self._controllers.items()
            if self._device_states.get(device) != "disconnected"
        }

    def _start_run_from_ready_dialog(
        self,
        dialog: SweepDeviceReadinessDialog,
        plan: object,
        *,
        outputs_forced_off: bool,
        execution_mode: str,
        output_dir_override: str,
        file_stem_override: str,
    ) -> None:
        if dialog.missing_devices:
            return
        if getattr(self, "_emergency_inhibit", False):
            QMessageBox.warning(
                self,
                "E-STOP Latched",
                "Emergency stop has been triggered and outputs are inhibited. "
                "Clear the emergency stop before starting a measurement.",
            )
            return
        selected_execution_mode = ExecutionMode.coerce(execution_mode)
        if outputs_forced_off:
            selected_execution_mode = ExecutionMode.DRY_RUN
        outputs_forced_off = selected_execution_mode is ExecutionMode.DRY_RUN
        try:
            estimate = PlanEstimator(self._settings).estimate(plan)  # type: ignore[arg-type]
            readiness = self.dashboard.evaluate_readiness(plan, estimate)
            if readiness.blocking_items:
                details = "\n".join(
                    f"• {item.label}: {item.detail}" for item in readiness.blocking_items
                )
                raise ConfigurationError("Station preflight is blocked:\n" + details)
            semantic_tree = self.recipe_page.semantic_tree_snapshot(
                plan.recipe_source,
                plan,  # type: ignore[union-attr]
            )
            active_controllers = self._active_device_controllers()
            self._run_controller.start(
                self._settings,
                self._repository.path,
                plan,  # type: ignore[arg-type]
                simulation=self._simulation,
                operator_context=self._access.identity.as_context(),
                outputs_forced_off=outputs_forced_off,
                execution_mode=selected_execution_mode.value,
                output_dir_override=output_dir_override,
                file_stem_override=file_stem_override,
                device_controllers=active_controllers,
                sample_target=self.inventory_store.get_active_target(),
            )
        except Exception as exc:
            issue = settings_issue_for_error(exc)
            if issue is not None and QMessageBox.settings_guidance(
                self, "Run not started", str(exc)
            ):
                self._open_settings_issue(issue)
            elif issue is None:
                QMessageBox.critical(self, "Run not started", str(exc))
            return
        self._leased_run_devices = set(active_controllers)
        dialog.accept()
        self.run_monitor.run_started(
            len(plan.actions),  # type: ignore[union-attr]
            estimate.nominal_duration_s,
            plan_actions=plan.actions,  # type: ignore[union-attr]
            recipe_source=plan.recipe_source,  # type: ignore[union-attr]
            semantic_tree=semantic_tree,
            execution_mode=selected_execution_mode.value,
        )
        self._set_run_ui_locked(True)
        self._navigate_to("execution")
        self._log(
            "Run Engine started in DRY RUN mode; all source outputs are forced OFF"
            if outputs_forced_off
            else "Run Engine started"
        )

    def _open_historical_thatec_sweep(self, run: object, tree: object) -> None:
        """Switch from Results to the immutable Sweep reconstructed from THATEC."""
        if not hasattr(run, "rows") or not isinstance(tree, tuple):
            self._log("THATEC Sweep reconstruction rejected: invalid public data")
            return
        try:
            self.recipe_page.load_reconstructed_thatec_sweep(run, tree)
        except Exception as exc:
            QMessageBox.warning(self, "THATEC Sweep", f"Cannot reconstruct Sweep tree: {exc}")
            return
        self._navigate_to("sweeps")
        self._log("Historical THATEC Sweep reconstructed in Sweeps")

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
        try:
            detail = Hdf5RunReader.detail(path)
            outputs_forced_off = bool(detail.simulation_metadata.get("outputs_forced_off", False))
            stored_execution_mode = ExecutionMode.coerce(
                str(detail.simulation_metadata.get("execution_mode", "measurement"))
            )
            if outputs_forced_off:
                stored_execution_mode = ExecutionMode.DRY_RUN
            outputs_forced_off = stored_execution_mode is ExecutionMode.DRY_RUN
            current_settings_source = serialize_settings_snapshot(
                self._settings,
                self._repository.path,
                simulation=self._simulation,
            )
            if current_settings_source != detail.settings_yaml:
                raise ConfigurationError(
                    "The current settings differ from the immutable run snapshot. "
                    "Restore the exact station configuration before resuming."
                )
            recipe = parse_recipe_text(detail.recipe_yaml, origin=str(path))
            plan = RecipeCompiler(
                self._settings,
                outputs_forced_off=outputs_forced_off,
                device_registry=self._composition.registry,
            ).compile(
                recipe
            )
            checkpoint = RunRecoveryManager().inspect(path, plan)
            if (
                checkpoint.stored_points >= plan.total_points
                and checkpoint.next_action_index >= len(plan.actions)
            ):
                raise ConfigurationError("The selected run has no remaining actions.")
        except Exception as exc:
            box = MessageBox("Resume unavailable", str(exc), self)
            box.cancelButton.hide()
            box.exec()
            self._log(f"RUN RECOVERY REJECTED: {exc}")
            return
        discarded = checkpoint.committed_points_found - checkpoint.stored_points
        msg = (
            "Resume only from the last confirmed safe boundary?\n\n"
            f"Preserved checkpoints: {checkpoint.stored_points}\n"
            f"Unsafe tail to discard and remeasure: {discarded}\n"
            f"Remaining action index: {checkpoint.next_action_index}/{len(plan.actions)}\n"
            f"Configuration prelude actions: {len(checkpoint.prelude_actions)}\n\n"
            "All required instruments will connect with outputs OFF. The stored recipe, "
            "plan hash and exact settings snapshot have been verified."
        )
        box = MessageBox("Resume measurement", msg, self)
        box.yesButton.setText("Resume")
        box.cancelButton.setText("Cancel")
        if not box.exec():
            return
        self._open_sweep_readiness(
            plan,
            outputs_forced_off=outputs_forced_off,
            execution_mode=stored_execution_mode.value,
            on_start=lambda dialog: self._start_resume_from_ready_dialog(
                dialog,
                plan,
                checkpoint,
                estimate_execution_mode=stored_execution_mode,
                outputs_forced_off=outputs_forced_off,
                discarded=discarded,
            ),
        )

    def _start_resume_from_ready_dialog(
        self,
        dialog: SweepDeviceReadinessDialog,
        plan: object,
        checkpoint: object,
        *,
        estimate_execution_mode: ExecutionMode,
        outputs_forced_off: bool,
        discarded: int,
    ) -> None:
        if dialog.missing_devices:
            return
        try:
            estimate = PlanEstimator(self._settings).estimate(plan)  # type: ignore[arg-type]
            semantic_tree = self.recipe_page.semantic_tree_snapshot(plan.recipe_source, plan)
            active_controllers = self._active_device_controllers()
            self._run_controller.start(
                self._settings,
                self._repository.path,
                plan,
                simulation=self._simulation,
                recovery=checkpoint,
                operator_context=self._access.identity.as_context(),
                outputs_forced_off=outputs_forced_off,
                execution_mode=estimate_execution_mode.value,
                device_controllers=active_controllers,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Resume not started", str(exc))
            return
        self._leased_run_devices = set(active_controllers)
        dialog.accept()
        remaining = len(plan.actions) - checkpoint.next_action_index
        remaining_fraction = remaining / max(1, len(plan.actions))
        self.run_monitor.run_started(
            remaining + len(checkpoint.prelude_actions),
            estimate.nominal_duration_s * remaining_fraction,
            plan_actions=(
                *checkpoint.prelude_actions,
                *plan.actions[checkpoint.next_action_index :],
            ),
            recipe_source=plan.recipe_source,
            semantic_tree=semantic_tree,
            execution_mode=(estimate_execution_mode.value),
        )
        self._set_run_ui_locked(True)
        self._navigate_to("execution")
        self._log(
            f"Run recovery started at safe checkpoint {checkpoint.stored_points}; "
            f"discarding {discarded} unsafe tail point(s); "
            + (
                "DRY RUN outputs remain forced OFF"
                if outputs_forced_off
                else "measurement output policy restored"
            )
        )

    def _run_event(self, name: str, data: object) -> None:
        payload = data if isinstance(data, dict) else {"data": data}
        if name == "run_started":
            self._execution_page_projection_cache.clear()
        preview_event = name in {"spectrum_preview", "reference_preview"}
        # Heartbeats carry no confirmed state and previews are rendered at a
        # bounded cadence. Read-only device pages also use a latest-state
        # projection timer while the worker is active; the execution monitor
        # still receives every lifecycle event needed for progress and safety.
        if preview_event and self._run_controller.running:
            self._pending_execution_previews[name] = dict(payload)
            if not self._execution_preview_timer.isActive():
                self._execution_preview_timer.start()
        elif name != "runner_heartbeat":
            self._queue_runner_device_readback(name, payload)
        if name == "run_started":
            self._execution_preview_timer.stop()
            self._pending_execution_previews.clear()
            self._execution_readback_timer.stop()
            self._pending_execution_readback = None
            self._execution_event_timer.stop()
            self._pending_execution_events.clear()
            self._pending_execution_checkpoint = None
            value = payload.get("correlation_id") or payload.get("hash")
            self._run_correlation_id = str(value) if value else None
        severity = (
            "error"
            if name
            in {
                "action_failed",
                "run_fault",
                "shutdown_error",
                "watchdog_timeout",
                "worker_cleanup_warning",
            }
            else (
                "warning"
                if name in {"compliance_detected", "run_aborted", "safe_finally_error"}
                else "info"
            )
        )
        # The Run Engine already persists every action event in the immutable
        # HDF5 event stream. Re-recording thousands of loop action boundaries
        # through AuditLogger here would synchronously open/flush a JSONL file
        # on the GUI thread and can starve painting for the duration of a
        # sweep. Audit the lifecycle/fault boundaries here; action-level
        # provenance remains durable in the engine event stream.
        if name not in {
            "runner_heartbeat",
            "point_stored",
            "safe_resume_boundary",
            "spectrum_preview",
            "reference_preview",
            "action_started",
            "action_finished",
            "recovery_prelude_started",
            "recovery_prelude_finished",
            "semantic_operation_started",
            "semantic_operation_applied",
            "semantic_operation_failed",
        }:
            self._audit_record(
                name.replace("_", " "),
                severity=severity,
                category="run",
                event_type=name,
                context=payload,
                correlation_id=self._run_correlation_id,
                critical=severity in {"error", "warning"},
            )
        if name == "safe_resume_boundary":
            # Recovery boundaries are persisted by the Run Engine and are not
            # operator-facing actions.  Replaying one log row per checkpoint
            # would turn a long sweep into a GUI document/layout workload.
            return
        if name in {
            "semantic_operation_started",
            "semantic_operation_applied",
            "semantic_operation_failed",
        }:
            immediate = (
                name == "semantic_operation_failed"
                or not self._run_controller.running
            )
            if immediate:
                self.run_monitor.append_event(name, payload)
            else:
                self.run_monitor.queue_semantic_event(name, payload)
            return
        if name in {"action_started", "action_finished"} and payload.get("semantic_id"):
            # The typed semantic lifecycle is the sole presentation stream for
            # rows represented in the shared model. Technical action events
            # remain durable in HDF5 but do not trigger a second widget pass.
            return
        if name == "runner_heartbeat":
            self.run_monitor.update_heartbeat(payload)
        elif preview_event:
            if self._run_controller.running:
                self.run_monitor.queue_spectrum_preview(payload)
            else:
                # Direct calls used by diagnostics/tests and the final queued
                # event remain immediately visible when no worker is active.
                self.run_monitor.update_spectrum_preview(payload)
        elif (
            name in {
                "action_started",
                "action_finished",
                "recovery_prelude_started",
                "recovery_prelude_finished",
                "point_stored",
            }
            and not payload.get("semantic_id")
            and self._run_controller.running
        ):
            if name == "point_stored":
                self._pending_execution_checkpoint = (name, dict(payload))
            else:
                self._pending_execution_events.append((name, dict(payload)))
            if not self._execution_event_timer.isActive():
                self._execution_event_timer.start()
        else:
            # Preserve ordering at lifecycle boundaries. Normal action events
            # are cheap in the worker but may trigger several Qt widget
            # layouts, so they are drained before completion; safety events
            # instead preempt stale presentation work below.
            if name in {
                "run_started",
                "manual_stage_waiting",
                "run_completed",
            }:
                self._drain_execution_events()
                if name == "run_completed":
                    self.run_monitor.flush_semantic_states()
            elif name in {
                "action_failed",
                "compliance_detected",
                "run_aborting",
                "run_aborted",
                "run_fault",
                "watchdog_timeout",
                "safe_finally_started",
                "safe_finally_finished",
                "safe_finally_error",
                "shutdown_action_started",
                "shutdown_action_finished",
                "shutdown_error",
                "worker_cleanup_warning",
            }:
                # A safety boundary must not wait behind stale presentation
                # work. The complete event stream is already durable in HDF5;
                # discard only queued GUI projections before showing the
                # confirmed fault/shutdown state immediately.
                self._execution_event_timer.stop()
                self._pending_execution_events.clear()
                self._pending_execution_checkpoint = None
                self.run_monitor.discard_pending_semantic()
            self.run_monitor.append_event(name, payload)
        if name in {"run_completed", "run_aborted", "run_fault"}:
            self._run_correlation_id = None

    def _drain_execution_events(self, *, limit: int | None = None) -> None:
        """Render a bounded action-event batch and yield back to Qt.

        ``RecipeRunner`` remains the authoritative event stream and persists
        every action in HDF5.  This queue only controls the presentation
        cadence; limiting work per timer tick prevents a burst of lifecycle
        signals from starving paints and user input on Windows.
        """

        if limit is None:
            limit = len(self._pending_execution_events)
        for _ in range(max(0, min(limit, len(self._pending_execution_events)))):
            name, payload = self._pending_execution_events.popleft()
            self.run_monitor.append_event(name, payload)
        checkpoint = self._pending_execution_checkpoint
        self._pending_execution_checkpoint = None
        if checkpoint is not None:
            self.run_monitor.append_event(*checkpoint)
        if (
            self._pending_execution_events
            or self._pending_execution_checkpoint is not None
        ) and not self._execution_event_timer.isActive():
            self._execution_event_timer.start()

    def _flush_execution_previews(self) -> None:
        pending = tuple(self._pending_execution_previews.items())
        self._pending_execution_previews.clear()
        for name, payload in pending:
            self._apply_runner_device_readback(name, payload)

    _IMMEDIATE_READBACK_EVENTS = frozenset(
        {
            "run_started",
            "action_failed",
            "compliance_detected",
            "run_aborting",
            "run_aborted",
            "run_fault",
            "watchdog_timeout",
            "safe_finally_started",
            "safe_finally_finished",
            "safe_finally_error",
            "shutdown_action_started",
            "shutdown_action_finished",
            "shutdown_error",
            "worker_cleanup_warning",
        }
    )

    def _queue_runner_device_readback(
        self, event_name: str, payload: dict[str, object]
    ) -> None:
        """Schedule one latest-state repaint for read-only device pages."""

        if (
            not self._run_controller.running
            or event_name in self._IMMEDIATE_READBACK_EVENTS
        ):
            if event_name in self._IMMEDIATE_READBACK_EVENTS:
                self._execution_readback_timer.stop()
                self._pending_execution_readback = None
            self._apply_runner_device_readback(event_name, payload)
            return
        self._pending_execution_readback = (event_name, dict(payload))
        if not self._execution_readback_timer.isActive():
            self._execution_readback_timer.start()

    def _flush_execution_readback(self) -> None:
        pending = self._pending_execution_readback
        self._pending_execution_readback = None
        if pending is not None:
            self._apply_runner_device_readback(*pending)

    def _apply_runner_device_readback(self, event_name: str, payload: dict[str, object]) -> None:
        """Dispatch one confirmed runner event through every device module.

        Pages receive immutable event/snapshot mappings and may only render
        them.  Instrument I/O remains exclusively owned by the Run Engine.
        """
        snapshot = payload.get("state_snapshot")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        raw_device_states = snapshot.get("device_states")
        device_states = raw_device_states if isinstance(raw_device_states, dict) else {}
        raw_statuses = snapshot.get("output_status")
        output_status = raw_statuses if isinstance(raw_statuses, dict) else {}
        safe_statuses = {
            str(endpoint): state
            for endpoint, state in output_status.items()
            if isinstance(state, str) and state in {"on", "off", "unknown"}
        }
        pages = (
            (("anritsu", self._device_pages["anritsu"]),)
            if event_name in {"spectrum_preview", "reference_preview"}
            and "anritsu" in self._device_pages
            else tuple(self._device_pages.items())
        )
        kind = str(payload.get("kind", "")).casefold()
        relevant_module = next(
            (
                module_key
                for module_key, markers in (
                    ("keithley", ("keithley",)),
                    ("rigol", ("rigol",)),
                    ("anritsu", ("anritsu", "spectrum", "reference")),
                    ("moke_box", ("moke",)),
                    ("lakeshore_gaussmeter", ("lakeshore",)),
                )
                if any(marker in kind for marker in markers)
            ),
            None,
        )
        for module_key, page in pages:
            if not isinstance(page, ExecutionTelemetryView):
                continue
            if (
                self._run_controller.running
                and event_name not in self._IMMEDIATE_READBACK_EVENTS
                and not page.isVisibleTo(self)
            ):
                # The Execution page owns the live plot and semantic state.
                # Mutating hidden, form-heavy instrument pages during every
                # point creates needless layouts and can starve Qt. If the
                # operator navigates to a device mid-run, the next coalesced
                # confirmed snapshot refreshes that now-visible page.
                continue
            module = self._composition.registry.get(module_key)
            raw_state = device_states.get(module.runner_state_key)
            device_state = raw_state if isinstance(raw_state, dict) else {}
            cached = self._execution_page_projection_cache.get(module_key)
            current_projection = (device_state, safe_statuses)
            event_requires_refresh = (
                event_name in self._IMMEDIATE_READBACK_EVENTS
                or event_name in {"spectrum_preview", "reference_preview"}
                or relevant_module == module_key
            )
            if cached == current_projection and not event_requires_refresh:
                continue
            page.apply_execution_event(
                event_name,
                payload,
                device_state,
                safe_statuses,
            )
            self._execution_page_projection_cache[module_key] = (
                deepcopy(device_state),
                dict(safe_statuses),
            )
        # Rendering helpers may normally refresh manual action availability.
        # A runner-owned session must remain inspect-only after every event.
        for control in self._run_read_only_controls:
            if control is not None and control.isEnabled():
                control.setEnabled(False)

    def _run_finished(self, result: object) -> None:
        self._execution_event_timer.stop()
        self._drain_execution_events()
        self.run_monitor.flush_semantic_states()
        self._execution_preview_timer.stop()
        self._flush_execution_previews()
        self._execution_readback_timer.stop()
        self._flush_execution_readback()
        self._leased_run_devices.clear()
        self._set_run_ui_locked(False)
        self.run_monitor.complete(result)
        result_path = None
        if isinstance(result, dict) and result.get("path"):
            result_path = Path(str(result["path"]))
        if result_path is not None:
            self.results_page.set_output_directory(result_path.parent)
        self.results_page.refresh()
        run_result = result["result"]
        state = str(getattr(getattr(run_result, "state", None), "value", "unknown"))
        error = getattr(run_result, "error", None)
        if result_path is not None:
            profile_override = None
            if self._run_elab_upload_config is not None:
                base_profile = self.elab_page.current_profile()
                cfg = self._run_elab_upload_config
                profile_override = base_profile.with_overrides(
                    enabled=True,
                    template_id=cfg.get("template_id"),
                    template_name=cfg.get("template_name") or None,
                    title_pattern=cfg.get("title_pattern") or None,
                    tags=cfg.get("tags") or None,
                    upload_hdf5=cfg.get("attach_hdf5"),
                    upload_csv=cfg.get("attach_csv"),
                )
            self.elab_page.queue_automatic_upload(
                result_path,
                run_state=state,
                error=error,
                requested=self._run_upload_to_elab_requested,
                profile_override=profile_override,
            )
        self._run_upload_to_elab_requested = False
        self._run_elab_upload_config = None
        sample_target = result.get("sample_target") if isinstance(result, dict) else None
        if result_path is not None and sample_target is not None:
            if hasattr(sample_target, "is_active") and sample_target.is_active:
                point_count = len(getattr(run_result, "points", ())) if hasattr(run_result, "points") else getattr(run_result, "committed_points", 0)
                spectrum_count = len(getattr(run_result, "spectra", ())) if hasattr(run_result, "spectra") else 0
                recipe_name = ""
                if hasattr(self._run_controller, "_plan") and self._run_controller._plan is not None:
                    recipe_name = getattr(self._run_controller._plan, "recipe_name", "")
                self.inventory_store.record_run(
                    SampleRunRecord(
                        sample_id=sample_target.sample_id,
                        sample_name=sample_target.sample_name or sample_target.sample_id,
                        row=str(sample_target.row or ""),
                        col=str(sample_target.col or ""),
                        device_label=str(sample_target.device_label or ""),
                        run_path=str(result_path),
                        run_sha256="",
                        created_at_utc="",
                        status=state,
                        point_count=point_count,
                        spectrum_count=spectrum_count,
                        recipe_name=recipe_name,
                    )
                )
                self.inventory_page.refresh_samples()
                self._log(
                    f"Sample inventory recorded sweep {result_path.name} under {sample_target.display_text()}"
                )
        if state == "fault":
            message = str(error or "The measurement finished in a fault state.")
            self._log(f"Run Engine finished in FAULT: {message}")
            QMessageBox.critical(self, "Run fault", message)
        elif error:
            self._log(f"Run Engine stopped safely: {error}")
        else:
            self._log("Run Engine completed the measurement")

    def _on_active_sample_target_changed(self, target: ActiveSampleTarget) -> None:
        self.recipe_page.set_active_sample_target(target)
        self.keithley_characterization_page.set_active_sample_target(target)
        self._refresh_safety_strip()

    def _open_inventory_result(self, run_path: str) -> None:
        path = Path(run_path)
        if path.is_file():
            self.results_page.set_output_directory(path.parent)
            self.results_page.select_result_path(path)
            self._navigate_to("results")
        else:
            self._log(f"Measurement file not found: {run_path}")

    def _on_elab_upload_completed(self, record: object) -> None:
        if hasattr(record, "run_path") and hasattr(record, "experiment_id"):
            self.inventory_store.update_run_elab_status(
                str(record.run_path),
                elab_experiment_id=record.experiment_id,
                elab_url=getattr(record, "experiment_url", None),
                elab_status="uploaded",
            )
            self.inventory_page.refresh_samples()

    def _run_failed(self, error: str) -> None:
        self._execution_event_timer.stop()
        self._drain_execution_events()
        self.run_monitor.flush_semantic_states()
        self._execution_preview_timer.stop()
        self._flush_execution_previews()
        self._execution_readback_timer.stop()
        self._flush_execution_readback()
        self._leased_run_devices.clear()
        self._set_run_ui_locked(False)
        self.run_monitor.failed(error)
        self._run_upload_to_elab_requested = False
        self._run_elab_upload_config = None
        self._log(f"Run Engine: {error}")
        QMessageBox.critical(self, "Run Engine", error)

    def _run_emergency_completed(self, errors: object) -> None:
        if errors:
            self._log("E-STOP Run Engine: " + "; ".join(str(item) for item in errors))
        else:
            self._log("E-STOP Run Engine: OFF/ABORT sent through emergency sessions")

    def _emergency_off_all(self) -> None:
        self._emergency_inhibit = True
        self.quick_control_coordinator.cancel_all("E-STOP requested")
        # Always use independent short-lived VISA sessions. A manual
        # instrument worker can be blocked just as a recipe worker can;
        # queueing OFF behind that operation is not an emergency path.
        self._run_controller.request_emergency_stop(
            self._settings,
            simulation=self._simulation,
        )
        for controller in self._controllers.values():
            controller.call("emergency_off")
        self._audit_record(
            "E-STOP executed: emergency shutdown sent to all instruments",
            severity="critical",
            category="safety",
            event_type="emergency_stop",
            critical=True,
        )
        self._log(
            "E-STOP executed: shutdown sent through independent emergency sessions and "
            "queued on all instrument workers"
        )
        self._refresh_safety_strip()

    def reset_emergency_inhibit(self) -> None:
        """Clear the latched E-STOP inhibit after verified safe state."""
        self._emergency_inhibit = False
        self._run_controller.reset_emergency_latch()
        self._log("E-STOP latch cleared by operator")
        self._refresh_safety_strip()

    def _settings_saved(self, settings: StationSettings) -> None:
        previous_settings = self._settings
        updated_settings = simulated_station_settings(settings) if self._simulation else settings
        changes_by_device = {
            name: self._setting_changes(
                getattr(previous_settings.devices, name).model_dump(mode="python"),
                getattr(updated_settings.devices, name).model_dump(mode="python"),
            )
            for name in self._controllers
        }
        changed_devices = {name for name, changes in changes_by_device.items() if changes}
        self._apply_settings_to_ui(settings, changed_devices=changed_devices)

        for name, controller in self._controllers.items():
            changes = changes_by_device[name]
            if not changes:
                controller.call("refresh_station_context", self._settings)
                continue
            operational_changes = {
                path
                for path in changes
                if not self._is_non_operational_device_preference(name, path)
            }
            if not operational_changes:
                controller.call("refresh_station_context", self._settings)
                continue
            if all(path and path[-1] in {"min", "max", "max_abs"} for path in operational_changes):
                if self._device_states.get(name) in {
                    None,
                    "disconnected",
                    "unknown",
                    "fault",
                }:
                    controller.call("refresh_station_context", self._settings)
                    self._log(
                        f"DEVICE LIMITS SAVED [{name}]: device is disconnected; "
                        "the new limits will govern the next connection."
                    )
                    continue
                self._pending_limit_rollbacks.setdefault(name, previous_settings)
                controller.call("apply_limit_settings", self._settings)
                self._log(
                    f"DEVICE LIMITS HOT-APPLY QUEUED [{name}]: session remains connected; "
                    "output-capable hardware must confirm OUTPUT OFF."
                )
                continue
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
        self._refresh_safety_strip()
        self._log(
            "Settings changed. Limit-only edits were applied without reconnecting; "
            "only connection, identity, driver or other operational changes replaced adapters."
        )

    @staticmethod
    def _is_non_operational_device_preference(device: str, path: tuple[str, ...]) -> bool:
        if device in {"rigol", "keithley"} and "defaults" in path:
            return True
        if device == "anritsu":
            if path[:2] == ("safety", "defaults"):
                return True
            if path in {
                ("signal_generator", "default_frequency"),
                ("signal_generator", "default_power"),
                ("acquisition", "application_average_count"),
                ("acquisition", "live_refresh_interval"),
            }:
                return True
        return (
            device == "moke_box"
            and path
            and path[0] in {"live_interval", "plot_refresh_interval", "history_window"}
        ) or (device == "lakeshore_gaussmeter" and path == ("live_interval",))

    def _apply_settings_to_ui(
        self,
        settings: StationSettings,
        *,
        changed_devices: set[str] | None = None,
    ) -> None:
        """Update every visible settings consumer without dispatching device work."""

        self._settings = simulated_station_settings(settings) if self._simulation else settings
        self.quick_control_coordinator.set_settings(self._settings)
        self.dashboard.update_settings(self._settings)
        pages = {
            "rigol": self.rigol_page,
            "keithley": self.keithley_page,
            "anritsu": self.anritsu_page,
            "moke_box": self.moke_box_page,
            "lakeshore_gaussmeter": self.lakeshore_gaussmeter_page,
        }
        for name, page in pages.items():
            if changed_devices is None or name in changed_devices:
                page.set_settings(self._settings)
        if hasattr(self, "keithley_characterization_page") and self.keithley_characterization_page is not None:
            if changed_devices is None or "keithley" in changed_devices:
                self.keithley_characterization_page.set_settings(self._settings)
        for name, panel in self.connection_panels.items():
            if changed_devices is not None and name not in changed_devices:
                continue
            resource, backend = self._device_connection_details(self._settings, name)
            panel.update_resource(resource, backend)
        self.recipe_page.set_settings(self._settings)
        self.elab_page.set_settings(self._settings)
        self._sync_elab_upload_controls()

    def _sync_elab_upload_controls(self) -> None:
        """Refresh per-run/per-save eLab controls from the central policy."""

        if not hasattr(self, "elab_page") or not hasattr(self, "recipe_page"):
            return
        available, default_enabled, hint = self.elab_page.upload_configuration()
        self.recipe_page.set_elab_upload_state(available, default_enabled, hint)

    @classmethod
    def _setting_changes(
        cls,
        previous: object,
        updated: object,
        prefix: tuple[str, ...] = (),
    ) -> set[tuple[str, ...]]:
        if isinstance(previous, dict) and isinstance(updated, dict):
            changes: set[tuple[str, ...]] = set()
            for key in previous.keys() | updated.keys():
                path = (*prefix, str(key))
                if key not in previous or key not in updated:
                    changes.add(path)
                else:
                    changes.update(cls._setting_changes(previous[key], updated[key], path))
            return changes
        return set() if previous == updated else {prefix}

    def _save_anritsu_readback_defaults(self, basic: object, advanced: object) -> None:
        """Persist query-only analyser readback without replacing the live adapter."""

        if not isinstance(basic, AnritsuConfigurationSnapshot):
            self._log("ANRITSU SETTINGS IMPORT FAILED: invalid basic readback")
            return
        try:
            self._access.require(
                Permission.EDIT_SETTINGS,
                action="saving Anritsu instrument readback to settings.yml",
            )
            loaded = self._repository.load()
            raw = loaded.raw
            self._update_anritsu_defaults(raw, basic, advanced)
            persisted = self._repository.save_raw(raw)
        except (AuthorizationError, ConfigurationError, KeyError, ValueError) as exc:
            QMessageBox.critical(self, "Anritsu settings not saved", str(exc))
            self._log(f"ANRITSU SETTINGS IMPORT FAILED: {type(exc).__name__}: {exc}")
            return

        self._settings = simulated_station_settings(persisted) if self._simulation else persisted
        self.settings_page.reload()
        self.anritsu_page.set_settings(self._settings)
        self.recipe_page.set_settings(self._settings)
        self._controllers["anritsu"].call("refresh_station_context", self._settings)
        self._refresh_safety_strip()
        self.anritsu_page.banner.show_message(
            "Instrument readback saved to settings.yml as acquisition defaults. "
            "The analyser, safety limits and live VISA session were not changed.",
            severity="success",
        )
        self._log("ANRITSU SETTINGS IMPORT SAVED: query-only readback persisted; adapter unchanged")

    @staticmethod
    def _update_anritsu_defaults(
        raw: dict[str, Any],
        basic: AnritsuConfigurationSnapshot,
        advanced: object = None,
    ) -> None:
        if basic.start_hz <= 0 or basic.stop_hz <= basic.start_hz:
            raise ConfigurationError(
                "Anritsu defaults require a frequency range satisfying 0 < start < stop."
            )
        defaults = raw["devices"]["anritsu"]["safety"]["defaults"]
        defaults.update(
            {
                "application": basic.instrument_mode or "SPECT",
                "start_frequency": format_quantity_auto(basic.start_hz, DIMENSION_FREQUENCY),
                "stop_frequency": format_quantity_auto(basic.stop_hz, DIMENSION_FREQUENCY),
                "center_frequency": format_quantity_auto(
                    (basic.start_hz + basic.stop_hz) / 2,
                    DIMENSION_FREQUENCY,
                ),
                "span": format_quantity_auto(basic.stop_hz - basic.start_hz, DIMENSION_FREQUENCY),
                "reference_level": f"{basic.reference_level_dbm:.9g} dBm",
                "sweep_points": basic.points,
            }
        )
        if isinstance(advanced, AdvancedSpectrumSnapshot):
            defaults.update(
                {
                    "rbw": format_quantity_auto(advanced.rbw_hz, DIMENSION_FREQUENCY),
                    "rbw_auto": advanced.rbw_auto,
                    "vbw": (
                        None
                        if advanced.vbw_hz is None
                        else format_quantity_auto(advanced.vbw_hz, DIMENSION_FREQUENCY)
                    ),
                    "vbw_mode": advanced.vbw_mode,
                    "detector": advanced.detector,
                    "attenuation": f"{advanced.attenuation_db:.9g} dB",
                    "attenuation_auto": advanced.attenuation_auto,
                    "preamplifier_enabled": advanced.preamplifier_enabled,
                    "sweep_time": format_quantity_auto(advanced.sweep_time_s, DIMENSION_TIME),
                    "sweep_time_auto": advanced.sweep_time_auto,
                }
            )

    def _save_anritsu_form_defaults(
        self,
        captured: tuple[AnritsuConfigurationSnapshot, AdvancedSpectrumSnapshot, object, int, int]
        | None = None,
    ) -> bool:
        """Persist the visible basic Spectrum form without touching hardware."""

        try:
            if captured is None:
                snapshot = self.anritsu_page.configuration_panel.configuration_snapshot()
                advanced = self.anritsu_page.advanced_configuration_panel.settings_snapshot()
                signal_generator = self.anritsu_page.signal_generator_snapshot()
                average_count = self.anritsu_page.average_count.value()
                refresh_ms = self.anritsu_page.refresh.value()
            else:
                snapshot, advanced, signal_generator, average_count, refresh_ms = captured
            self._access.require(
                Permission.EDIT_SETTINGS,
                action="saving Anritsu form defaults to settings.yml",
            )
            loaded = self._repository.load()
            raw = loaded.raw
            before = deepcopy(raw["devices"]["anritsu"]["safety"]["defaults"])
            before_generator = deepcopy(raw["devices"]["anritsu"]["signal_generator"])
            before_acquisition = deepcopy(raw["devices"]["anritsu"]["acquisition"])
            self._update_anritsu_defaults(raw, snapshot, advanced)
            generator = raw["devices"]["anritsu"]["signal_generator"]
            generator["default_frequency"] = format_quantity_auto(
                signal_generator.frequency_hz, DIMENSION_FREQUENCY
            )
            generator["default_power"] = f"{signal_generator.power_dbm:.9g} dBm"
            acquisition = raw["devices"]["anritsu"]["acquisition"]
            acquisition["application_average_count"] = average_count
            acquisition["live_refresh_interval"] = format_quantity_auto(
                refresh_ms / 1000,
                DIMENSION_TIME,
            )
            after = raw["devices"]["anritsu"]["safety"]["defaults"]
            if (
                before == after
                and before_generator == generator
                and before_acquisition == acquisition
            ):
                return True
            persisted = self._repository.save_raw(raw)
        except (AuthorizationError, ConfigurationError, KeyError, ValueError) as exc:
            QMessageBox.critical(self, "Anritsu settings not saved", str(exc))
            self.anritsu_page.banner.show_message(
                f"Anritsu defaults were not saved: {exc}", severity="error"
            )
            self._log(f"ANRITSU FORM SAVE FAILED: {type(exc).__name__}: {exc}")
            return False

        self._settings = simulated_station_settings(persisted) if self._simulation else persisted
        self.settings_page.reload()
        self.anritsu_page.set_settings(self._settings)
        self.recipe_page.set_settings(self._settings)
        self._controllers["anritsu"].call("refresh_station_context", self._settings)
        self._refresh_safety_strip()
        self.anritsu_page.banner.show_message(
            "Current Spectrum form saved to settings.yml as acquisition defaults. "
            "No command was sent to the analyser.",
            severity="success",
        )
        self._log("ANRITSU FORM DEFAULTS SAVED: settings.yml updated; hardware unchanged")
        return True

    def _save_rigol_form_defaults(
        self, channel_defaults: dict[int, dict[str, object]] | None = None
    ) -> bool:
        """Persist both visible/cached Rigol channel forms without hardware I/O."""

        try:
            if channel_defaults is None:
                channel_defaults = self.rigol_page.settings_defaults()
            self._access.require(
                Permission.EDIT_SETTINGS,
                action="saving Rigol form defaults to settings.yml",
            )
            loaded = self._repository.load()
            raw = loaded.raw
            changed = False
            for channel, payload in channel_defaults.items():
                defaults = raw["devices"]["rigol"]["safety"]["channels"][str(channel)]["defaults"]
                if defaults != payload:
                    defaults.clear()
                    defaults.update(payload)
                    changed = True
            if not changed:
                return True
            persisted = self._repository.save_raw(raw)
        except (AuthorizationError, ConfigurationError, KeyError, ValueError) as exc:
            QMessageBox.critical(self, "Rigol settings not saved", str(exc))
            self.rigol_page.banner.show_message(
                f"Rigol defaults were not saved: {exc}", severity="error"
            )
            self._log(f"RIGOL FORM SAVE FAILED: {type(exc).__name__}: {exc}")
            return False

        self._settings = simulated_station_settings(persisted) if self._simulation else persisted
        self.settings_page.reload()
        self.rigol_page.set_settings(self._settings)
        self.recipe_page.set_settings(self._settings)
        self._controllers["rigol"].call("refresh_station_context", self._settings)
        self._refresh_safety_strip()
        self.rigol_page.banner.show_message(
            "Both Rigol channel forms were saved to settings.yml as defaults. "
            "Outputs remain OFF and no command was sent to the generator.",
            severity="success",
        )
        self._log("RIGOL FORM DEFAULTS SAVED: settings.yml updated; hardware unchanged")
        return True

    def _save_lakeshore_form_defaults(self, interval_ms: int | None = None) -> bool:
        """Persist the read-only gaussmeter polling preference."""

        try:
            if interval_ms is None:
                interval_ms = int(self.lakeshore_gaussmeter_page.sample_interval.currentData())
            self._access.require(
                Permission.EDIT_SETTINGS,
                action="saving Lake Shore display defaults to settings.yml",
            )
            loaded = self._repository.load()
            raw = loaded.raw
            interval = format_quantity_auto(interval_ms / 1000, DIMENSION_TIME)
            profile = raw["devices"]["lakeshore_gaussmeter"]
            if profile.get("live_interval") == interval:
                return True
            profile["live_interval"] = interval
            persisted = self._repository.save_raw(raw)
        except (AuthorizationError, ConfigurationError, KeyError, ValueError) as exc:
            QMessageBox.critical(self, "Lake Shore settings not saved", str(exc))
            self._log(f"LAKESHORE FORM SAVE FAILED: {type(exc).__name__}: {exc}")
            return False
        self._settings = simulated_station_settings(persisted) if self._simulation else persisted
        self.settings_page.reload()
        self.lakeshore_gaussmeter_page.set_settings(self._settings)
        self.recipe_page.set_settings(self._settings)
        self._refresh_safety_strip()
        self._log("LAKESHORE DISPLAY DEFAULTS SAVED: settings.yml updated")
        return True

    def _save_moke_form_defaults(self, captured: tuple[int, int, int] | None = None) -> bool:
        """Persist MOKE read-only sampling and plot preferences."""

        try:
            if captured is None:
                captured = (
                    int(self.moke_box_page.sample_interval.currentData()),
                    int(self.moke_box_page.refresh_interval.currentData()),
                    int(self.moke_box_page.history_window.currentData()),
                )
            live_ms, refresh_ms, history_seconds = captured
            self._access.require(
                Permission.EDIT_SETTINGS,
                action="saving MOKE display defaults to settings.yml",
            )
            loaded = self._repository.load()
            raw = loaded.raw
            profile = raw["devices"]["moke_box"]
            updates = {
                "live_interval": format_quantity_auto(live_ms / 1000, DIMENSION_TIME),
                "plot_refresh_interval": format_quantity_auto(refresh_ms / 1000, DIMENSION_TIME),
                "history_window": format_quantity_auto(history_seconds, DIMENSION_TIME),
            }
            if all(profile.get(key) == value for key, value in updates.items()):
                return True
            profile.update(updates)
            persisted = self._repository.save_raw(raw)
        except (AuthorizationError, ConfigurationError, KeyError, ValueError) as exc:
            QMessageBox.critical(self, "MOKE settings not saved", str(exc))
            self._log(f"MOKE FORM SAVE FAILED: {type(exc).__name__}: {exc}")
            return False
        self._settings = simulated_station_settings(persisted) if self._simulation else persisted
        self.settings_page.reload()
        self.moke_box_page.set_settings(self._settings)
        self.recipe_page.set_settings(self._settings)
        self._refresh_safety_strip()
        self._log("MOKE DISPLAY DEFAULTS SAVED: settings.yml updated")
        return True

    @staticmethod
    def _keithley_snapshot_payload(
        snapshots: object,
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(snapshots, dict) or set(snapshots) != {"A", "B"}:
            raise ConfigurationError("Incomplete Keithley A/B form configuration.")
        payload: dict[str, dict[str, Any]] = {}
        for channel in ("A", "B"):
            snapshot = snapshots[channel]
            if is_dataclass(snapshot) and not isinstance(snapshot, type):
                values = asdict(snapshot)
            elif isinstance(snapshot, dict):
                values = deepcopy(snapshot)
            else:
                raise ConfigurationError(f"Channel {channel}: invalid Keithley form snapshot.")
            payload[channel] = values
        return payload

    def _stage_keithley_defaults(self, snapshots: object) -> None:
        self._stage_keithley_defaults_save(snapshots, assignment=False)

    def _queue_keithley_assignment_save(self, snapshots: object) -> None:
        self._stage_keithley_defaults_save(snapshots, assignment=True)

    def _stage_keithley_defaults_save(self, snapshots: object, *, assignment: bool) -> None:
        try:
            self._access.require(
                Permission.EDIT_SETTINGS,
                action="saving Keithley defaults to settings.yml",
            )
            payload = self._keithley_snapshot_payload(snapshots)
        except (
            AuthorizationError,
            ConfigurationError,
            SafetyViolation,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            self.keithley_page.readback_assignment_completed(False)
            QMessageBox.critical(self, "Keithley settings not saved", str(exc))
            self.keithley_page.banner.show_message(f"Keithley settings were not queued: {exc}")
            self._log(f"KEITHLEY SETTINGS QUEUE FAILED: {type(exc).__name__}: {exc}")
            return

        self._keithley_defaults_generation += 1
        generation = self._keithley_defaults_generation
        self._pending_keithley_defaults = (generation, payload)
        if assignment:
            self.keithley_page.readback_assignment_completed(True)
        self.keithley_page.banner.show_message(
            "Keithley settings changed in memory. Press SAVE SETTINGS to validate "
            "and write them to settings.yml.",
            timeout_ms=6_000,
        )
        self._log(f"KEITHLEY SETTINGS STAGED: generation {generation}; file unchanged")

    def _stage_current_keithley_forms_if_changed(self, snapshots: object = None) -> bool:
        """Include ordinary manual A/B edits in the global settings save."""

        try:
            if snapshots is None:
                snapshots = {
                    channel: self.keithley_page.configuration_snapshot_for(channel)
                    for channel in ("A", "B")
                }
            payload = self._keithley_snapshot_payload(snapshots)
            updates = validate_keithley_default_snapshots(self._settings, payload)
            changed = any(
                any(
                    self._settings.keithley.safety.channels[channel].defaults.get(key) != value
                    for key, value in values.items()
                )
                for channel, values in updates.items()
            )
        except (
            ConfigurationError,
            SafetyViolation,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            QMessageBox.critical(self, "Keithley settings not saved", str(exc))
            self.keithley_page.banner.show_message(f"Keithley settings were not saved: {exc}")
            self._log(f"KEITHLEY FORM SAVE FAILED: {type(exc).__name__}: {exc}")
            return False
        if changed:
            self._keithley_defaults_generation += 1
            self._pending_keithley_defaults = (
                self._keithley_defaults_generation,
                payload,
            )
        return True

    def _save_all_settings(self) -> None:
        """Persist station settings while keeping live form values transient.

        Keithley has two deliberately different persistence paths.  Values
        copied from a verified hardware readback are staged in
        ``_pending_keithley_defaults`` and are written by the dedicated worker
        after the operator presses SAVE SETTINGS.  The level/compliance fields
        in the normal manual form describe the next operation, so they must
        not be copied into ``settings.yml`` by an unrelated global save.  The
        remaining station defaults (mode, ranges, NPLC, settling time, ...)
        still participate in the global transaction.
        """

        if self._keithley_defaults_in_flight:
            self._log("SAVE SETTINGS ignored: a settings write is already running")
            return
        # Capture every device page before SettingsPage emits settings_saved.
        # That signal intentionally reloads all pages and would otherwise erase
        # unsaved form edits before their device-specific persistence runs.
        try:
            rigol_defaults = self.rigol_page.settings_defaults()
            anritsu_defaults = (
                self.anritsu_page.configuration_panel.configuration_snapshot(),
                self.anritsu_page.advanced_configuration_panel.settings_snapshot(),
                self.anritsu_page.signal_generator_snapshot(),
                self.anritsu_page.average_count.value(),
                self.anritsu_page.refresh.value(),
            )
            keithley_snapshots = {
                channel: self.keithley_page.configuration_snapshot_for(channel)
                for channel in ("A", "B")
            }
            lakeshore_interval_ms = int(
                self.lakeshore_gaussmeter_page.sample_interval.currentData()
            )
            moke_defaults = (
                int(self.moke_box_page.sample_interval.currentData()),
                int(self.moke_box_page.refresh_interval.currentData()),
                int(self.moke_box_page.history_window.currentData()),
            )
        except (ConfigurationError, SafetyViolation, TypeError, ValueError) as exc:
            QMessageBox.critical(self, "Settings not saved", str(exc))
            self._log(f"SAVE SETTINGS CAPTURE FAILED: {type(exc).__name__}: {exc}")
            return

        try:
            keithley_payload = self._keithley_snapshot_payload(keithley_snapshots)
        except (ConfigurationError, SafetyViolation, KeyError, TypeError, ValueError) as exc:
            QMessageBox.critical(self, "Keithley settings not saved", str(exc))
            self.keithley_page.banner.show_message(f"Keithley settings not saved: {exc}")
            self._log(f"SAVE SETTINGS REJECTED: {type(exc).__name__}: {exc}")
            return

        pending_keithley_defaults = self._pending_keithley_defaults

        def merge_device_forms(raw: dict[str, Any]) -> None:
            for channel, payload in rigol_defaults.items():
                defaults = raw["devices"]["rigol"]["safety"]["channels"][str(channel)]["defaults"]
                defaults.clear()
                defaults.update(deepcopy(payload))

            snapshot, advanced, signal_generator, average_count, refresh_ms = anritsu_defaults
            self._update_anritsu_defaults(raw, snapshot, advanced)
            generator = raw["devices"]["anritsu"]["signal_generator"]
            generator["default_frequency"] = format_quantity_auto(
                signal_generator.frequency_hz, DIMENSION_FREQUENCY
            )
            generator["default_power"] = f"{signal_generator.power_dbm:.9g} dBm"
            acquisition = raw["devices"]["anritsu"]["acquisition"]
            acquisition["application_average_count"] = average_count
            acquisition["live_refresh_interval"] = format_quantity_auto(
                refresh_ms / 1000, DIMENSION_TIME
            )

            raw["devices"]["lakeshore_gaussmeter"]["live_interval"] = format_quantity_auto(
                lakeshore_interval_ms / 1000, DIMENSION_TIME
            )
            live_ms, refresh_plot_ms, history_seconds = moke_defaults
            raw["devices"]["moke_box"].update(
                {
                    "live_interval": format_quantity_auto(live_ms / 1000, DIMENSION_TIME),
                    "plot_refresh_interval": format_quantity_auto(
                        refresh_plot_ms / 1000, DIMENSION_TIME
                    ),
                    "history_window": format_quantity_auto(history_seconds, DIMENSION_TIME),
                }
            )

            # A pending readback assignment owns all Keithley default fields
            # and is persisted by the background worker below.  Do not let a
            # stale manual form overwrite that snapshot in this transaction.
            if pending_keithley_defaults is None:
                candidate = StationSettings.model_validate(raw)
                validation_snapshots = deepcopy(keithley_payload)
                channels = raw["devices"]["keithley"]["safety"]["channels"]
                for channel, snapshot in validation_snapshots.items():
                    defaults = channels[channel].get("defaults", {})
                    mode = str(snapshot.get("source_mode", "")).strip().lower()
                    if mode == "current":
                        snapshot["source_level"] = defaults.get(
                            "source_current", snapshot["source_level"]
                        )
                        snapshot["compliance"] = defaults.get(
                            "voltage_compliance", snapshot["compliance"]
                        )
                    elif mode == "voltage":
                        snapshot["source_level"] = defaults.get(
                            "source_voltage", snapshot["source_level"]
                        )
                        snapshot["compliance"] = defaults.get(
                            "current_compliance", snapshot["compliance"]
                        )
                    else:
                        snapshot["source_level"] = "0 V"
                        snapshot["compliance"] = "0 A"
                updates = validate_keithley_default_snapshots(
                    candidate, validation_snapshots
                )
                # Source level and compliance are working setpoints.  Keep
                # their persisted defaults unchanged during a global save.
                transient_keys = {
                    "source_current",
                    "source_voltage",
                    "voltage_compliance",
                    "current_compliance",
                }
                for channel, values in updates.items():
                    channels[channel]["defaults"].update(
                        {
                            key: value
                            for key, value in values.items()
                            if key not in transient_keys
                        }
                    )

        if not self.settings_page.save_draft(extra_transform=merge_device_forms):
            return
        if pending_keithley_defaults is not None:
            # The station-profile transaction has completed; now hand the
            # explicitly staged readback snapshot to the non-GUI worker.  The
            # worker reloads the latest YAML under the repository lock, so the
            # two commits cannot lose each other's fields.
            self._start_keithley_defaults_save()
        else:
            self._pending_keithley_defaults = None
            self._active_keithley_defaults = None
        self._log("SAVE SETTINGS: station profile transaction completed")
        self.safety_strip.save_settings.setText("SAVED")
        QTimer.singleShot(
            1_500,
            lambda: self.safety_strip.save_settings.setText("SAVE SETTINGS"),
        )

    def _start_keithley_defaults_save(self) -> None:
        if self._keithley_defaults_in_flight:
            return
        pending = self._pending_keithley_defaults
        if pending is None:
            return
        self._pending_keithley_defaults = None
        self._active_keithley_defaults = pending
        self._keithley_defaults_in_flight = True
        self.safety_strip.save_settings.setEnabled(False)
        self.safety_strip.save_settings.setText("SAVING...")
        generation, payload = pending
        self._keithley_defaults_write_requested.emit(generation, payload)
        self._log(f"KEITHLEY SETTINGS SAVE STARTED: generation {generation}")

    def _keithley_defaults_saved(self, generation: int, persisted: object, raw: object) -> None:
        self._keithley_defaults_in_flight = False
        self._active_keithley_defaults = None
        self.safety_strip.save_settings.setEnabled(True)
        self.safety_strip.save_settings.setText("SAVED")
        QTimer.singleShot(
            1_500,
            lambda: self.safety_strip.save_settings.setText("SAVE SETTINGS"),
        )
        if isinstance(persisted, StationSettings) and isinstance(raw, dict):
            self._settings = (
                simulated_station_settings(persisted) if self._simulation else persisted
            )
            self.settings_page.accept_external_snapshot(persisted, raw)
            self.keithley_page.set_settings(self._settings)
            self.recipe_page.set_settings(self._settings)
            self.quick_control_coordinator.set_settings(self._settings)
            self._controllers["keithley"].call("refresh_station_context", self._settings)
            self._refresh_safety_strip()
        self.keithley_page.banner.show_message(
            "Keithley defaults saved after explicit SAVE SETTINGS. The live instrument and "
            "OUTPUT state were not changed.",
            severity="success",
            timeout_ms=4_000,
        )
        self._log(f"KEITHLEY SETTINGS SAVE COMPLETED: generation {generation}")

    def _keithley_defaults_save_failed(self, generation: int, error: str) -> None:
        self._keithley_defaults_in_flight = False
        if self._pending_keithley_defaults is None:
            self._pending_keithley_defaults = self._active_keithley_defaults
        self._active_keithley_defaults = None
        self.safety_strip.save_settings.setEnabled(True)
        self.safety_strip.save_settings.setText("SAVE SETTINGS")
        self.keithley_page.readback_assignment_completed(False)
        QMessageBox.critical(self, "Keithley settings not saved", error)
        self.keithley_page.banner.show_message(f"Keithley background save failed: {error}")
        self._log(f"KEITHLEY SETTINGS SAVE FAILED: generation {generation}: {error}")

    def _save_keithley_readback_defaults(self, snapshots: object) -> None:
        """Synchronous compatibility entry point used by tests and shutdown."""

        try:
            self._access.require(
                Permission.EDIT_SETTINGS,
                action="saving Keithley defaults to settings.yml",
            )
            payload = self._keithley_snapshot_payload(snapshots)
            persisted, raw = persist_keithley_default_snapshots(self._repository.path, payload)
        except (
            AuthorizationError,
            ConfigurationError,
            SafetyViolation,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            self.keithley_page.readback_assignment_completed(False)
            QMessageBox.critical(self, "Keithley settings not saved", str(exc))
            self.keithley_page.banner.show_message(f"Keithley settings were not saved: {exc}")
            self._log(f"KEITHLEY SETTINGS IMPORT FAILED: {type(exc).__name__}: {exc}")
            return
        self._keithley_defaults_saved(0, persisted, raw)
        self.keithley_page.readback_assignment_completed(True)

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
            if device in {"rigol", "keithley", "anritsu", "lakeshore_gaussmeter"}
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
            "Changing a connection safely disconnects current sessions. "
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
                profile = raw["devices"][device]
                old = profile if device == "lakeshore_gaussmeter" else profile["connection"]
                self._log(
                    f"VISA ASSIGN WRITE [{device}]: {old.get('resource')!r}/{old.get('visa_backend')!r} "
                    f"-> {resource!r}/{backend!r}"
                )
                connection = old
                connection["resource"] = resource
                connection["visa_backend"] = backend
                if device == "lakeshore_gaussmeter":
                    connection["enabled"] = True
                    detected_baud = self.dashboard.discovered_serial_baud(resource, backend)
                    if detected_baud is not None:
                        connection["baud_rate"] = detected_baud
                        self._log(
                            "VISA ASSIGN LAKE SHORE SERIAL: "
                            f"persisting detected baud_rate={detected_baud}"
                        )
            settings = self._repository.save_raw(raw)
        except Exception as exc:
            self._log(f"VISA ASSIGN FAILED: {type(exc).__name__}: {exc}")
            QMessageBox.critical(self, "VISA assignments not saved", str(exc))
            return
        self._log("VISA ASSIGN SAVED: settings.yml updated atomically")
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
            "The read-only VOUT response was verified. VOUT control will remain disabled.",
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
            f"MOKE Box assigned to {endpoint}. Read-only control is ready."
        )
        self._log(f"MOKE BOX ASSIGN SUCCESS: endpoint={endpoint!r}; VOUT control remains disabled")

    def _set_run_ui_locked(self, locked: bool) -> None:
        self.quick_controls_window.setEnabled(not locked)
        if locked:
            self.quick_control_coordinator.cancel_all("Recipe run owns the instruments")
        if locked:
            self.moke_box_page.stop_live(
                "Live Hall readout paused while a recipe run owns the MOKE Box."
            )
            self.lakeshore_gaussmeter_page.stop_live(
                "Live field readout paused while a recipe run owns the Lake Shore 475."
            )
        self._set_device_pages_execution_read_only(locked)
        self.recipe_page.set_execution_controlled(locked)
        # Device routes remain available during a run so the operator can
        # inspect their last verified values, output state and plots. Recipe
        # editing and station-profile changes remain unavailable.
        self._set_route_enabled("settings", not locked)
        # Sweeps is intentionally an inspectable route during execution.  The
        # page itself switches to an explicit read-only state while the
        # navigation item stays discoverable and clickable.
        self._set_route_enabled("sweeps", True)

    def _set_device_pages_execution_read_only(self, locked: bool) -> None:
        """Keep device pages inspectable while the Run Engine owns I/O.

        The execution worker uses independent sessions. Manual page actions
        must therefore be unavailable during the run, but disabling the entire
        route also hid output/compliance/plot evidence from the operator.
        Preserve each control's pre-run enabled state and disable only widgets
        that can initiate an edit or command.
        """

        if locked:
            if self._run_read_only_controls:
                return
            pages_to_lock = list(self._device_pages.values())
            if hasattr(self, "keithley_characterization_page") and self.keithley_characterization_page is not None:
                pages_to_lock.append(self.keithley_characterization_page)
            for page in pages_to_lock:
                if isinstance(page, ExecutionTelemetryView):
                    page.set_execution_controlled(True)
                controls: set[QWidget] = set()
                for widget_type in (
                    QAbstractButton,
                    QAbstractSpinBox,
                    QComboBox,
                    QLineEdit,
                ):
                    controls.update(page.findChildren(widget_type))
                for control in controls:
                    self._run_read_only_controls[control] = control.isEnabled()
                    control.setEnabled(False)
            return
        for control, enabled in self._run_read_only_controls.items():
            if control is not None:
                control.setEnabled(enabled)
        self._run_read_only_controls.clear()
        for page in self._device_pages.values():
            if isinstance(page, ExecutionTelemetryView):
                page.set_execution_controlled(False)

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
                    "CRITICAL: durable audit logging failed; OUTPUT ON and new runs are locked: "
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

    def _flush_pending_log_ui(self) -> None:
        if not self._pending_log_ui_entries:
            if hasattr(self, "_log_ui_flush_timer"):
                self._log_ui_flush_timer.stop()
            return
        batch, self._pending_log_ui_entries = self._pending_log_ui_entries, []
        if hasattr(self, "_log_ui_flush_timer"):
            self._log_ui_flush_timer.stop()
        self.log.appendPlainText("\n".join(batch))

    def _log(self, message: str) -> None:
        category, severity, critical = self._log_classification(message)
        self._audit_record(
            message,
            severity=severity,
            category=category,
            critical=critical,
            correlation_id=self._run_correlation_id,
        )
        self._event_log_entries.append(message)
        if not self.traffic_only_button.isChecked() or self._is_transport_log(message):
            if getattr(self, "_simulation", False) or critical or severity in {"error", "critical"}:
                self._flush_pending_log_ui()
                self.log.appendPlainText(message)
            else:
                self._pending_log_ui_entries.append(message)
                if hasattr(self, "_log_ui_flush_timer") and not self._log_ui_flush_timer.isActive():
                    self._log_ui_flush_timer.start()

    @staticmethod
    def _is_transport_log(message: str) -> bool:
        upper = f" {message.upper()} "
        return any(
            marker in upper
            for marker in (
                " VISA TX ",
                " VISA RX ",
                " VISA OPEN ",
                " VISA CONFIG ",
                " TCP TX ",
                " TCP RX ",
                " TCP OPEN ",
                " TCP CONFIG ",
            )
        )

    def _refresh_event_log_view(self, _checked: bool = False) -> None:
        self._flush_pending_log_ui()
        entries = list(self._event_log_entries)
        if self.traffic_only_button.isChecked():
            entries = [message for message in entries if self._is_transport_log(message)]
        self.log.setPlainText("\n".join(entries[-500:]))
        scrollbar = self.log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _copy_traffic_log(self) -> None:
        traffic = [
            message for message in self._event_log_entries if self._is_transport_log(message)
        ]
        QApplication.clipboard().setText("\n".join(traffic))

    def _clear_event_log(self) -> None:
        self._pending_log_ui_entries.clear()
        if hasattr(self, "_log_ui_flush_timer"):
            self._log_ui_flush_timer.stop()
        self._event_log_entries.clear()
        self.log.clear()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._navigation_layout_initialized:
            return
        self._navigation_layout_initialized = True
        application = QApplication.instance()
        if application is not None:
            applied_theme = apply_application_theme(application, self._configured_theme_mode)
            self._apply_navigation_surface(applied_theme.tokens.surface)
        if (
            self._navigation_expanded_preference
            and self.width() >= self._navigation_expand_threshold
        ):
            self.navigationInterface.expand(useAni=False)
        # Fluent recalculates a navigation tree's size hint while its parent
        # panel is laid out. Re-toggling here prevents child rows from keeping
        # their pre-layout geometry and overlapping the group heading.
        self.apparatus_navigation_item.setExpanded(False, ani=False)
        self.apparatus_navigation_item.setExpanded(True, ani=False)
        QTimer.singleShot(0, self._capture_apparatus_required_height)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "apparatus_navigation_item"):
            QTimer.singleShot(0, self._sync_apparatus_navigation_height)
        if hasattr(self, "event_log_panel"):
            QTimer.singleShot(0, self._sync_shell_splitter_layout)

    def _sync_apparatus_navigation_height(self) -> None:
        """Prevent QFluent's expanded tree from overlapping sibling routes."""

        if not hasattr(self, "_apparatus_required_height"):
            return
        short = self.navigationInterface.panel.height() < self._apparatus_required_height
        if short and self.apparatus_navigation_item.isExpanded:
            self.apparatus_navigation_item.setExpanded(False, ani=False)
            self._apparatus_auto_collapsed = True
        elif (
            not short
            and self._apparatus_auto_collapsed
            and not self.apparatus_navigation_item.isExpanded
        ):
            self.apparatus_navigation_item.setExpanded(True, ani=False)
            self._apparatus_auto_collapsed = False
        panel = self.navigationInterface.panel
        panel.topLayout.invalidate()
        panel.vBoxLayout.invalidate()
        panel.layout().activate()

    def _capture_apparatus_required_height(self) -> None:
        """Capture QFluent's settled expanded-tree height after first layout."""

        panel = self.navigationInterface.panel
        self._apparatus_required_height = max(
            self._apparatus_required_height,
            panel.vBoxLayout.minimumSize().height() + 1,
        )
        self._sync_apparatus_navigation_height()

    def _owned_floating_windows(self) -> tuple[QWidget, ...]:
        """Return visible top-level widgets owned by this application shell.

        A top-level QWidget can still have a QWidget parent. Closing the parent
        does not reliably close such a native child window, so shutdown must
        enumerate the ownership chain explicitly.
        """

        owned: list[QWidget] = []
        for widget in QApplication.topLevelWidgets():
            if widget is self or not widget.isVisible():
                continue
            owner = widget.parentWidget()
            while owner is not None:
                if owner is self:
                    owned.append(widget)
                    break
                owner = owner.parentWidget()
        return tuple(owned)

    def _close_owned_floating_windows(self) -> None:
        for widget in self._owned_floating_windows():
            widget.close()

    def nativeEvent(self, event_type, message) -> tuple[bool, int]:  # noqa: N802 - Qt override
        import sys
        if sys.platform == "win32":
            try:
                import win32con
                from qframelesswindow.windows import MSG, LPNCCALCSIZE_PARAMS, cast
                msg = MSG.from_address(message.__int__())
                if msg.message == win32con.WM_NCCALCSIZE:
                    handled, result = super().nativeEvent(event_type, message)
                    if msg.wParam and handled:
                        params = cast(msg.lParam, LPNCCALCSIZE_PARAMS).contents
                        new_w = params.rgrc[0].right - params.rgrc[0].left
                        new_h = params.rgrc[0].bottom - params.rgrc[0].top
                        old_w = params.rgrc[1].right - params.rgrc[1].left
                        old_h = params.rgrc[1].bottom - params.rgrc[1].top
                        if new_w == old_w and new_h == old_h:
                            return True, 0
                    return handled, result
            except Exception:
                pass
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event: QCloseEvent) -> None:
        # RecipePage is hosted inside the Fluent stack, so its own closeEvent
        # is not delivered when the application window closes. Ask it before
        # persisting workspace state or beginning the shutdown sequence.
        if not self.recipe_page.confirm_close():
            event.ignore()
            return
        self._audit_record(
            "Application safe shutdown started",
            category="application",
            event_type="shutdown_started",
            critical=True,
        )
        self._save_workspace()
        if not self.recipe_page.cancel_preflight():
            self._audit_record(
                "Application close blocked: recipe validation is still stopping",
                severity="error",
                category="application",
                event_type="shutdown_blocked",
                critical=True,
            )
            QMessageBox.warning(
                self,
                "Validation still stopping",
                "Recipe validation is still active. Wait for cancellation to finish, "
                "then close the application again.",
            )
            event.ignore()
            return
        if not self._run_controller.close():
            self._audit_record(
                "Application close blocked: measurement workers are still stopping",
                severity="error",
                category="application",
                event_type="shutdown_blocked",
                critical=True,
            )
            QMessageBox.warning(
                self,
                "Measurement still stopping",
                "The application cannot close while a measurement or emergency-OFF "
                "worker is still active. Outputs were sent an emergency-OFF request. "
                "Wait for the stop to finish, then close the application again.",
            )
            event.ignore()
            return
        if not self.elab_page.shutdown():
            self._audit_record(
                "Application close blocked: eLab network work is still stopping",
                severity="error",
                category="application",
                event_type="shutdown_blocked",
                critical=True,
            )
            QMessageBox.warning(
                self,
                "eLab upload still active",
                "The application cannot close while an eLab connection or upload is active. "
                "Wait for it to finish, then close the application again.",
            )
            event.ignore()
            return
        self._keithley_defaults_timer.stop()
        self._pending_keithley_defaults = None
        self._keithley_defaults_thread.quit()
        self._keithley_defaults_thread.wait(5_000)
        self._close_owned_floating_windows()
        self.quick_control_coordinator.cancel_all("Application closing")
        self.anritsu_page._timer.stop()
        try:
            self.anritsu_page.close_manual_archive_session()
        except Exception as exc:
            self._log(f"Manual spectrum archive close warning: {exc}")
        self.anritsu_page._analysis_controller.close()
        if not DeviceController.close_all(self._controllers.values()):
            self._audit_record(
                "Application close delayed: background device threads did not terminate cleanly within timeout",
                severity="error",
                category="application",
                event_type="shutdown_blocked",
                critical=True,
            )
            QMessageBox.warning(
                self,
                "Device worker still stopping",
                "One or more instrument controller threads did not terminate within the safe timeout. "
                "Wait a moment and try closing the application again.",
            )
            event.ignore()
            return
        try:
            self._audit.close()
        except (OSError, RuntimeError):
            self._audit_healthy = False
        event.accept()
