"""Application shell and lifecycle orchestration."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, QSignalBlocker, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QColor,
    QKeySequence,
    QPalette,
    QResizeEvent,
    QShortcut,
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
    qrouter,
)
from qfluentwidgets.common.style_sheet import styleSheetManager
from shiboken6 import isValid

from app.audit import AuditLogger
from app.bootstrap import StationComposition
from app.contracts import ExecutionTelemetryView
from app.devices.anritsu_ms2830a import (
    AdvancedSpectrumSnapshot,
    AnritsuConfigurationSnapshot,
)
from app.devices.keithley_2600.settings_defaults import (
    persist_keithley_default_snapshots,
    validate_keithley_default_snapshots,
)
from app.devices.simulators import simulated_station_settings
from app.domain.errors import AuthorizationError, ConfigurationError, SafetyViolation
from app.domain.manual_metadata import ManualMetadataValue
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_FREQUENCY,
    DIMENSION_POWER,
    DIMENSION_RESISTANCE,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    QuantityError,
    format_quantity_auto,
    parse_quantity,
)
from app.engine.estimation import PlanEstimator
from app.engine.runner import ExecutionMode
from app.inventory import ActiveSampleTarget, InventoryStore, SampleRunRecord
from app.platform.paths import default_catalogue_root
from app.safety.keithley_limit_reconciliation import (
    propose_keithley_limit_adjustments,
)
from app.security import AccessPolicy, Permission
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from app.settings.validation import format_settings_validation_error
from app.ui.dashboard import DeviceConnectionPanel, StationDashboardController
from app.ui.design_system import apply_application_theme, effective_theme
from app.ui.dialogs import StationMessageBox as QMessageBox
from app.ui.dialogs import SweepDeviceReadinessDialog
from app.ui.elab import ElabPage
from app.ui.execution import RunMonitorPage
from app.ui.inventory import SampleInventoryPage
from app.ui.moke_address_dialog import MokeAddressDialog
from app.ui.quick_controls import QuickControlCoordinator, QuickControlsWindow
from app.ui.recipes import DeviceParameterDialog, SweepGeneratorDialog  # noqa: F401
from app.ui.recipes.page import (  # noqa: F401
    AnritsuAcquisitionEditorDialog,
    CommentEditorDialog,
    FixedValueDialog,
    KeithleySweepBuilderDialog,
    RecipePage,
    SweepLibraryButton,
)
from app.ui.results import HeatmapResultsTab, ResultsPage
from app.ui.run_worker import RunController, serialize_settings_snapshot
from app.ui.settings_guidance import SettingsIssue, settings_issue_for_error
from app.ui.settings_page import SettingsPage
from app.ui.settings_workers import KeithleyDefaultsSaveWorker
from app.ui.shell.navigation import StationNavigationTreeWidget
from app.ui.shell.page_host import FluentPageHost
from app.ui.shell.safety_strip import StationSafetySnapshot, StationSafetyStrip
from app.ui.widgets import (
    KeithleyLimitProposalDialog,
    LimitEditDialog,
    LimitField,
    SpectrumPlotWidget,
    show_toast,
)
from app.ui.workers import DeviceController


class _BufferedEventLogEdit(PlainTextEdit):
    """PlainTextEdit that flushes batched UI log lines before synchronous reads."""

    def __init__(self, flush_callback: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._flush_callback = flush_callback

    def toPlainText(self) -> str:
        self._flush_callback()
        return super().toPlainText()


class _ClosedPageStub:
    """No-op placeholder returned for unrealized lazy pages after MainWindow closes."""

    def prepare_manual_archive_shutdown(self) -> bool:
        return True

    def shutdown_analysis(self) -> bool:
        return True

    def shutdown(self) -> bool:
        return True

    def confirm_close(self) -> bool:
        return True

    def cancel_preflight(self) -> bool:
        return True

    def __getattr__(self, _name: str) -> Any:
        return lambda *_args, **_kwargs: True


class _LazyDevicePages(Mapping[str, QWidget]):
    """Transparent mapping that realizes device pages only when accessed."""

    _KEYS = ("rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter")

    def __init__(self, window: MainWindow) -> None:
        self._window = window

    def __getitem__(self, key: str) -> QWidget:
        if key not in self._KEYS:
            raise KeyError(key)
        return self._window._ensure_device_page(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._KEYS)

    def __len__(self) -> int:
        return len(self._KEYS)


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
        self._closed = False
        self._simulation = simulation
        self._run_upload_to_elab_requested = False
        self._run_elab_upload_config: dict[str, Any] | None = None
        persisted = self._repository.load().settings
        self._settings = simulated_station_settings(persisted) if simulation else persisted
        raw_output_dir = Path(
            self._settings.storage.get("output_directory", "./measurements")
        ).expanduser()
        output_dir = (
            raw_output_dir
            if raw_output_dir.is_absolute()
            else (Path.cwd() / raw_output_dir).resolve()
        )
        legacy_db_path = output_dir / "inventory.db"
        raw_catalogue_dir = str(
            self._settings.storage.get("catalogue_directory", "") or ""
        ).strip()
        if raw_catalogue_dir:
            configured_path = Path(raw_catalogue_dir).expanduser()
            catalogue_root = (
                configured_path
                if configured_path.is_absolute()
                else (self._repository.path.parent / configured_path).resolve()
            )
        else:
            legacy_catalogue_root = InventoryStore.catalogue_root_from_database(
                legacy_db_path
            )
            if legacy_catalogue_root is not None:
                catalogue_root = legacy_catalogue_root
            elif raw_output_dir.is_absolute():
                # Isolated test/custom profiles remain isolated instead of
                # touching the user's default Documents/PyLab catalogue.
                catalogue_root = output_dir
            else:
                catalogue_root = default_catalogue_root()
        self.inventory_store = InventoryStore.open_for_catalogue_root(
            catalogue_root,
            legacy_db_path=legacy_db_path,
        )
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
        self._audit_health_timer = QTimer(self)
        self._audit_health_timer.setInterval(250)
        self._audit_health_timer.timeout.connect(self._refresh_audit_health)
        self._audit_health_timer.start()
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
        from app.ui.widgets.fluent_ownership import own_fluent_helpers

        own_fluent_helpers(self)
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
        # Below 1220 px the compact 48 px rail leaves measurement pages enough
        # horizontal width (e.g. 1024x700 keeps ~950 px content and a single-row
        # StationSafetyStrip) to preserve their control hierarchy.
        self._navigation_expand_threshold = 1_220
        self.navigationInterface.setMinimumExpandWidth(self._navigation_expand_threshold)

        registry = self._composition.registry
        self.dashboard = StationDashboardController(
            self._settings,
            registry,
            parent=self,
            discovery_enabled=not self._simulation,
        )
        self._realized_device_pages: dict[str, QWidget] = {}
        self._device_pages = _LazyDevicePages(self)
        self._keithley_characterization_page: Any = None
        self._recipe_page: Any = None
        self._run_monitor: Any = None
        self._prepared_execution: tuple[object, bool, str, str, str] | None = None
        self._results_page: Any = None
        self._inventory_page: Any = None
        self._elab_page: Any = None
        self._settings_page: Any = None
        self._last_device_capabilities: dict[str, object] = {}
        self._saved_anritsu_splitter: object = None
        self._last_selected_result_path: object = None
        self._building_routes: set[str] = set()
        self._execution_read_only_locked = False
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
        self._last_execution_readback: tuple[str, dict[str, object]] | None = None
        self._execution_output_status: dict[str, str] = {}
        self._execution_page_projection_cache: dict[
            str, tuple[dict[str, object], dict[str, str]]
        ] = {}
        self.stackedWidget.currentChanged.connect(
            lambda _index: QTimer.singleShot(0, self._refresh_visible_execution_readback)
        )
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
        self._resize_sync_timer = QTimer(self)
        self._resize_sync_timer.setSingleShot(True)
        self._resize_sync_timer.setInterval(0)
        self._resize_sync_timer.timeout.connect(self._flush_resize_layout)
        self.quick_control_coordinator = QuickControlCoordinator(
            self._controllers, self, settings=self._settings
        )
        self.quick_control_coordinator.set_hardware_request_builder(
            "rigol",
            lambda request, state: self.rigol_page.quick_control_hardware_request(
                request.target, request.text, state, live_only=request.live_only,
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
        self.dashboard.set_assignment_allowed(self._access.allows(Permission.ASSIGN_VISA))
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
        route_specs: tuple[tuple[str, str, QWidget | None], ...] = (
            ("overview", "Overview", self.dashboard.navigation_pages["overview"]),
            ("discovery", "Discovery", self.dashboard.navigation_pages["discovery"]),
            ("rigol", registry.get("rigol").display_name, None),
            ("keithley", registry.get("keithley").display_name, None),
            ("keithley_characterization", "Characterization", None),
            ("anritsu", registry.get("anritsu").display_name, None),
            ("moke_box", registry.get("moke_box").display_name, None),
            ("lakeshore_gaussmeter", registry.get("lakeshore_gaussmeter").display_name, None),
            ("sweeps", "Sweeps", None),
            ("execution", "Execution", None),
            ("results", "Results", None),
            ("inventory", "Samples", None),
            ("elab", "eLabFTW", None),
            ("settings", "Settings", None),
        )
        self.navigation_routes: dict[str, FluentPageHost] = {}
        self.connection_panels: dict[str, DeviceConnectionPanel] = {}
        self.apparatus_navigation_item = StationNavigationTreeWidget(
            FluentIcon.DEVELOPER_TOOLS, "Devices", False, self.navigationInterface.panel
        )
        self.navigationInterface.addWidget(
            routeKey="apparatusMenu",
            widget=self.apparatus_navigation_item,
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
        for route, display_name, eager_widget in route_specs:
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
            if eager_widget is not None:
                host = FluentPageHost(eager_widget, self)
            else:
                host = FluentPageHost(
                    content_factory=lambda target_route=route: self._build_route_content(target_route),
                    parent=self,
                )
            host.setObjectName(f"{route}PageHost")
            self.navigation_routes[route] = host
            if route in apparatus_routes:
                resource, backend = self._device_connection_details(self._settings, route)
                panel = DeviceConnectionPanel(registry.get(route).display_name, resource, parent=host)
                panel.update_resource(resource, backend)
                if route == "moke_box":
                    panel.test_button.setToolTip(
                        "Open one TCP session, validate the documented VOUT response, then disconnect. "
                        "No gain or VOUT-setting command is sent."
                    )
                panel.hide()
                self.connection_panels[route] = panel
            position = (
                NavigationItemPosition.BOTTOM if route == "settings" else NavigationItemPosition.TOP
            )
            if route == "keithley_characterization":
                parent_key = "keithleyPageHost"
            elif route in apparatus_routes:
                parent_key = "apparatusMenu"
            else:
                parent_key = None
            host.setProperty("isStackedTransparent", False)
            self.stackedWidget.addWidget(host)
            navigation_item = StationNavigationTreeWidget(
                route_icons[route], display_name, True, self.navigationInterface.panel
            )
            self.navigationInterface.addWidget(
                routeKey=host.objectName(),
                widget=navigation_item,
                onClick=lambda checked=False, page=host: (page.ensure_content(), self.switchTo(page)),
                position=position,
                tooltip=display_name,
                parentRouteKey=parent_key,
            )
            if self.stackedWidget.count() == 1:
                self.stackedWidget.currentChanged.connect(self._onCurrentInterfaceChanged)
                self.navigationInterface.setCurrentItem(host.objectName())
                qrouter.setDefaultRouteKey(self.stackedWidget, host.objectName())
            self._updateStackedBackground()
            if eager_widget is not None:
                eager_widget._scroll_area = host.scroll_area
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
        self._event_log_entries: deque[str] = deque(maxlen=2000)
        self._pending_log_ui_entries: list[str] = []
        self._log_ui_flush_timer = QTimer(self)
        self._log_ui_flush_timer.setInterval(50)
        self._log_ui_flush_timer.timeout.connect(self._flush_pending_log_ui)
        self.log = _BufferedEventLogEdit(self._flush_pending_log_ui)
        self.log.setObjectName("eventLogText")
        self.log.setProperty("stationSurface", "raised")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(50)
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
        self._run_controller.event.connect(self._run_event)
        self._run_controller.finished.connect(self._run_finished)
        self._run_controller.failed.connect(self._run_failed)
        self._run_controller.emergency_completed.connect(self._run_emergency_completed)
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

    @staticmethod
    def _peek_page(shell: Any, attr: str, device_key: str | None = None) -> Any:
        """Return a page only if already realized (or attached on a test stub)."""

        realized = getattr(shell, "_realized_device_pages", None)
        if isinstance(realized, dict):
            if device_key is not None:
                return realized.get(device_key)
            return getattr(shell, f"_{attr}", None)
        return getattr(shell, attr, None)

    def _ensure_route(self, route: str) -> QWidget:
        host = self.navigation_routes[route]
        host.ensure_content()
        return host.content

    def _ensure_device_page(self, key: str) -> Any:
        page = self._realized_device_pages.get(key)
        if page is not None:
            return page
        if getattr(self, "_closed", False):
            return _ClosedPageStub()
        self._ensure_route(key)
        return self._realized_device_pages[key]

    @property
    def rigol_page(self) -> Any:
        return self._ensure_device_page("rigol")

    @rigol_page.setter
    def rigol_page(self, value: Any) -> None:
        self._realized_device_pages["rigol"] = value

    @property
    def keithley_page(self) -> Any:
        return self._ensure_device_page("keithley")

    @keithley_page.setter
    def keithley_page(self, value: Any) -> None:
        self._realized_device_pages["keithley"] = value

    @property
    def keithley_characterization_page(self) -> Any:
        if self._keithley_characterization_page is None:
            if getattr(self, "_closed", False):
                return _ClosedPageStub()
            self._ensure_device_page("keithley")
        return self._keithley_characterization_page

    @keithley_characterization_page.setter
    def keithley_characterization_page(self, value: Any) -> None:
        self._keithley_characterization_page = value

    @property
    def anritsu_page(self) -> Any:
        return self._ensure_device_page("anritsu")

    @anritsu_page.setter
    def anritsu_page(self, value: Any) -> None:
        self._realized_device_pages["anritsu"] = value

    @property
    def moke_box_page(self) -> Any:
        return self._ensure_device_page("moke_box")

    @moke_box_page.setter
    def moke_box_page(self, value: Any) -> None:
        self._realized_device_pages["moke_box"] = value

    @property
    def lakeshore_gaussmeter_page(self) -> Any:
        return self._ensure_device_page("lakeshore_gaussmeter")

    @lakeshore_gaussmeter_page.setter
    def lakeshore_gaussmeter_page(self, value: Any) -> None:
        self._realized_device_pages["lakeshore_gaussmeter"] = value

    @property
    def recipe_page(self) -> Any:
        if self._recipe_page is None:
            if getattr(self, "_closed", False):
                return _ClosedPageStub()
            self._ensure_route("sweeps")
        return self._recipe_page

    @recipe_page.setter
    def recipe_page(self, value: Any) -> None:
        self._recipe_page = value

    @property
    def run_monitor(self) -> Any:
        if self._run_monitor is None:
            if getattr(self, "_closed", False):
                return _ClosedPageStub()
            self._ensure_route("execution")
        return self._run_monitor

    @run_monitor.setter
    def run_monitor(self, value: Any) -> None:
        self._run_monitor = value

    @property
    def results_page(self) -> Any:
        if self._results_page is None:
            if getattr(self, "_closed", False):
                return _ClosedPageStub()
            self._ensure_route("results")
        return self._results_page

    @results_page.setter
    def results_page(self, value: Any) -> None:
        self._results_page = value

    @property
    def inventory_page(self) -> Any:
        if self._inventory_page is None:
            if getattr(self, "_closed", False):
                return _ClosedPageStub()
            self._ensure_route("inventory")
        return self._inventory_page

    @inventory_page.setter
    def inventory_page(self, value: Any) -> None:
        self._inventory_page = value

    @property
    def elab_page(self) -> Any:
        if self._elab_page is None:
            if getattr(self, "_closed", False):
                return _ClosedPageStub()
            self._ensure_route("elab")
        return self._elab_page

    @elab_page.setter
    def elab_page(self, value: Any) -> None:
        self._elab_page = value

    @property
    def settings_page(self) -> Any:
        if self._settings_page is None:
            if getattr(self, "_closed", False):
                return _ClosedPageStub()
            self._ensure_route("settings")
        return self._settings_page

    @settings_page.setter
    def settings_page(self, value: Any) -> None:
        self._settings_page = value

    def _configure_limit_buttons_for(self, device_key: str, root: QWidget) -> None:
        roles = ", ".join(sorted(role.value for role in self._access.identity.roles)) or "none"
        display_map = {"rigol": "Rigol", "keithley": "Keithley", "anritsu": "Anritsu"}
        device_label = display_map.get(device_key, device_key)
        for field in root.findChildren(LimitField):
            fixed_instrument_range = (
                device_key == "keithley" and str(field.property("limitKey")) == "nplc"
            )
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
            field.edit_button.setAccessibleName(f"Edit {device_label} safety limits")
            field.edit_button.setAccessibleDescription(reason)
            if hasattr(field, "range_pill"):
                if not enabled or not field.edit_button.isVisible():
                    field.range_pill.setCursor(Qt.CursorShape.ArrowCursor)
                    field.range_pill.setToolTip(reason)
                else:
                    field.range_pill.setCursor(Qt.CursorShape.PointingHandCursor)
                    field.range_pill.setToolTip(
                        f"Configured {device_label} safety envelope. "
                        "Click to edit laboratory safety limits."
                    )
            if device_key != "keithley" or enabled:
                field.edit_requested.connect(
                    lambda field=field, dev=device_key: self._edit_device_limit(dev, field)
                )

    def _apply_theme_to_subtree(self, root: QWidget) -> None:
        theme = effective_theme(getattr(self, "_configured_theme_mode", "system"))
        for plot in root.findChildren(SpectrumPlotWidget):
            plot.apply_theme(theme)
        for heatmap in root.findChildren(HeatmapResultsTab):
            heatmap.apply_theme(theme)
        from app.ui.results.scalar_tab import ScalarResultsTab

        for scalars in root.findChildren(ScalarResultsTab):
            scalars.apply_theme(theme)

    def _build_route_content(self, route: str) -> QWidget:
        self._building_routes.add(route)
        try:
            if route in {"rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"}:
                widget = self._build_device_page(route)
            elif route == "keithley_characterization":
                if self._keithley_characterization_page is None:
                    self._ensure_device_page("keithley")
                widget = self._keithley_characterization_page
            elif route == "sweeps":
                widget = self._build_recipe_page()
            elif route == "execution":
                widget = self._build_run_monitor()
            elif route == "results":
                widget = self._build_results_page()
            elif route == "inventory":
                widget = self._build_inventory_page()
            elif route == "elab":
                widget = self._build_elab_page()
            elif route == "settings":
                widget = self._build_settings_page()
            else:
                raise KeyError(f"Unknown lazy route: {route!r}")
            widget._scroll_area = self.navigation_routes[route].scroll_area
            from app.ui.widgets.fluent_ownership import own_fluent_helpers

            own_fluent_helpers(widget)
            self._apply_accessibility_to(widget)
            self._apply_theme_to_subtree(widget)
            if self._execution_read_only_locked:
                if route in {
                    "rigol",
                    "keithley",
                    "keithley_characterization",
                    "anritsu",
                    "moke_box",
                    "lakeshore_gaussmeter",
                }:
                    self._lock_page_execution_read_only(widget)
                elif route == "sweeps":
                    widget.set_execution_controlled(True)
            return widget
        finally:
            self._building_routes.discard(route)

    def _build_device_page(self, key: str) -> QWidget:
        existing = self._realized_device_pages.get(key)
        if existing is not None:
            return existing
        page = self._composition.registry.get(key).create_page(
            self._controllers[key], self._settings
        )
        self._realized_device_pages[key] = page
        panel = self.connection_panels[key]
        # Keithley keeps its compact device title/live controls as the page
        # anchor; the single authoritative connection surface sits immediately
        # below it. Other device pages retain connection-first composition.
        page.layout().insertWidget(2 if key == "keithley" else 0, panel)
        panel.show()
        page.status.connect(self._log)

        if key == "rigol":
            page.output_state_changed.connect(
                lambda channel, state: self.quick_controls_window.set_output_state(
                    "rigol", channel, state
                )
            )
            page.quick_control_draft_changed.connect(
                lambda target, text: self.quick_control_coordinator.publish_draft(
                    target, text, source="device_card"
                )
            )
            self.quick_control_coordinator.draft_changed.connect(
                page.quick_control_draft_changed_from_coordinator
            )
            self.quick_control_coordinator.publish_draft_snapshot(
                page.quick_control_draft_snapshot(),
                source="device_card",
                mark_dirty=False,
            )
            page.quick_setpoint_requested.connect(
                lambda target, text: self.quick_control_coordinator.submit(
                    target, text, live_only=True
                )
            )
            self.quick_control_coordinator.state_changed.connect(
                page.quick_setpoint_state_changed
            )
            self.quick_control_coordinator.configuration_verified.connect(
                page.quick_control_configuration_verified
            )
            self.quick_control_coordinator.value_read.connect(page.quick_setpoint_value_read)
            page.quick_controls_requested.connect(self._show_quick_controls)
            self._configure_limit_buttons_for("rigol", page)
            if "rigol" in self._last_device_capabilities:
                page.set_capabilities(self._last_device_capabilities["rigol"])
        elif key == "keithley":
            card = page.characterization_card
            self._keithley_characterization_page = card
            page.characterization_requested.connect(
                lambda: self._navigate_to("keithley_characterization")
            )
            page.output_state_changed.connect(
                lambda channel, state: self.quick_controls_window.set_output_state(
                    "keithley", channel, state
                )
            )
            page.quick_control_draft_changed.connect(
                lambda target, text: self.quick_control_coordinator.publish_draft(
                    target, text, source="device_card"
                )
            )
            self.quick_control_coordinator.draft_changed.connect(
                page.quick_control_draft_changed_from_coordinator
            )
            self.quick_control_coordinator.publish_draft_snapshot(
                page.quick_control_draft_snapshot(),
                source="device_card",
                mark_dirty=False,
            )
            page.quick_setpoint_requested.connect(self.quick_control_coordinator.submit)
            self.quick_control_coordinator.state_changed.connect(
                page.quick_setpoint_state_changed
            )
            self.quick_control_coordinator.configuration_verified.connect(
                page.quick_control_configuration_verified
            )
            self.quick_control_coordinator.value_read.connect(page.quick_setpoint_value_read)
            page.quick_controls_requested.connect(self._show_quick_controls)
            page.settings_assignment_requested.connect(self._queue_keithley_assignment_save)
            page.settings_defaults_requested.connect(self._stage_keithley_defaults)
            card.set_inventory_store(self.inventory_store)
            card.set_active_sample_target(self.inventory_store.get_active_target())
            card.active_target_changed.connect(self._on_active_sample_target_changed)
            card.browse_samples_requested.connect(lambda: self._navigate_to("inventory"))
            card.measurement_saved.connect(self._refresh_inventory_if_realized)
            self._configure_limit_buttons_for("keithley", page)
            self._configure_limit_buttons_for("keithley", card)
            if "keithley_characterization" not in self._building_routes:
                self.navigation_routes["keithley_characterization"].ensure_content()
        elif key == "anritsu":
            page.set_manual_archive_context(
                metadata_provider=self._manual_spectrum_metadata_values,
                device_idn_provider=self._manual_spectrum_device_idn,
                settings_source_provider=lambda: serialize_settings_snapshot(
                    self._settings,
                    self._repository.path,
                    simulation=self._simulation,
                ),
                operator_context_provider=lambda: self._access.identity.as_context(),
                simulation=self._simulation,
            )
            page.set_manual_elab_context(
                configuration_provider=lambda: self.elab_page.manual_upload_configuration(),
                upload_callback=lambda *args, **kwargs: self.elab_page.queue_manual_upload(
                    *args, **kwargs
                ),
            )
            page.quick_controls_requested.connect(self._show_quick_controls)
            page.settings_readback_requested.connect(self._save_anritsu_readback_defaults)
            target = self.inventory_store.get_active_target()
            manual_directory = (
                self.inventory_store.measurement_directory_for(
                    target.sample_id,
                    "single_measurements",
                    "Anritsu_MS2830A",
                    row=target.row,
                    col=target.col,
                )
                if target.is_active
                else None
            )
            page.set_sample_measurement_context(manual_directory, target.as_dict())
            if self._saved_anritsu_splitter is not None:
                page.workspace_splitter.restoreState(self._saved_anritsu_splitter)
            self._configure_limit_buttons_for("anritsu", page)
            if "anritsu" in self._last_device_capabilities:
                page.set_capabilities(self._last_device_capabilities["anritsu"])
        elif key == "moke_box":
            def _pause_moke_and_lakeshore() -> None:
                page.stop_live("Live Hall paused for field control/calibration.")
                lakeshore = self._realized_device_pages.get("lakeshore_gaussmeter")
                if lakeshore is not None:
                    lakeshore.stop_live("Live reference paused for field calibration.")

            page.field_workflow.bind_reference(
                self._controllers["lakeshore_gaussmeter"],
                simulation=self._simulation,
                authorize=self._authorize_moke_workflow,
                pause_live=_pause_moke_and_lakeshore,
            )
            page.field_workflow.busy_changed.connect(
                lambda _busy: self._refresh_safety_strip()
            )
            self.quick_control_coordinator.bind_moke_workflow(page.field_workflow)
            ref_state = self._device_states.get("lakeshore_gaussmeter", "disconnected")
            if ref_state != "disconnected":
                self._controllers["lakeshore_gaussmeter"].state_changed.emit(ref_state)

        state = self._device_states.get(key, "disconnected")
        if state != "disconnected":
            self._controllers[key].state_changed.emit(state)
        return page

    def _build_recipe_page(self) -> RecipePage:
        if self._recipe_page is not None:
            return self._recipe_page
        page = RecipePage(
            self._settings,
            device_registry=self._composition.registry,
        )
        self._recipe_page = page
        page.set_keithley_snapshot_provider(
            lambda channel=None, mode=None: self.keithley_page.configuration_snapshot_for(channel, mode)
        )
        page.set_rigol_snapshot_provider(
            lambda channel: self.rigol_page.configuration_snapshot_for(channel),
            lambda channel: self.rigol_page.configuration_review_snapshot_for(channel),
        )
        page.parameter_snapshot_provider = (
            lambda: self.moke_box_page.field_workflow.quick_control_drafts()
        )
        page.set_device_comparison_extras(
            "moke_box",
            lambda channel: self.moke_box_page.field_workflow.configuration_comparison_values(
                channel
            ),
        )
        page.set_anritsu_snapshot_provider(
            lambda: self.anritsu_page.configuration_panel.configuration_snapshot(),
            lambda: self.anritsu_page.advanced_settings_snapshot(),
        )
        page.set_device_comparison_extras(
            "anritsu",
            lambda _channel: {
                "average_count": self.anritsu_page.average_count.value(),
                "reference_average_count": self.anritsu_page.reference_average_count.value(),
                "reference_operation": self.anritsu_page.reference_operation.currentData(),
            },
        )
        page.set_anritsu_sg_snapshot_provider(
            lambda: self.anritsu_page.signal_generator_snapshot()
        )
        page.set_elab_context(
            profile_provider=lambda: self.elab_page.current_profile(),
            credentials_provider=lambda: self.elab_page.current_credentials(),
            available_templates_provider=lambda: self.elab_page.available_templates(),
            refresh_templates_callback=lambda *args, **kwargs: self.elab_page.refresh_templates(
                *args, **kwargs
            ),
            navigate_to_elab=lambda: self._navigate_to("elab"),
        )
        page.execution_preview_requested.connect(self._prepare_execution)
        page.change_target_requested.connect(lambda: self._navigate_to("inventory"))
        page.settings_issue_requested.connect(self._open_settings_issue)
        page.plan_preflight_changed.connect(self.dashboard.update_plan_preflight)
        page.plan_preflight_changed.connect(self._invalidate_prepared_execution)
        page.output_directory.textChanged.connect(self._invalidate_prepared_execution)
        page.output_file_stem.textChanged.connect(self._invalidate_prepared_execution)
        page.save_to_elab_check.toggled.connect(self._invalidate_prepared_execution)
        page.status.connect(self._log)
        target = self.inventory_store.get_active_target()
        directory = (
            self.inventory_store.measurement_directory_for(
                target.sample_id, "sweeps", row=target.row, col=target.col
            )
            if target.is_active
            else None
        )
        page.set_active_sample_target(target, output_directory=directory)
        self._sync_elab_upload_controls()
        return page

    def _build_run_monitor(self) -> RunMonitorPage:
        if self._run_monitor is not None:
            return self._run_monitor
        page = RunMonitorPage()
        self._run_monitor = page
        page.launch_requested.connect(self._launch_prepared_execution)
        page.stop_requested.connect(self._run_controller.request_stop)
        page.pause_requested.connect(self._run_controller.request_pause)
        page.resume_requested.connect(self._run_controller.request_resume)
        page.manual_next_requested.connect(self._run_controller.advance_manual_step)
        return page

    def _build_results_page(self) -> ResultsPage:
        if self._results_page is not None:
            return self._results_page
        target = self.inventory_store.get_active_target()
        page = ResultsPage(
            str(
                self._results_directory_for_target(target)
                or self.inventory_store.catalogue_root
            ),
            catalogue_tree=True,
        )
        self._results_page = page
        page.file_browser.set_sample_context(
            self._results_directory_for_target(target),
            self.inventory_store.catalogue_root,
        )
        page.resume_requested.connect(self._resume_run)
        page.open_sweep_requested.connect(self._open_historical_thatec_sweep)
        page.result_selected.connect(self._on_result_selected)
        return page

    def _build_inventory_page(self) -> SampleInventoryPage:
        if self._inventory_page is not None:
            return self._inventory_page
        page = SampleInventoryPage(self.inventory_store, self)
        self._inventory_page = page
        page.catalogue_root_changed.connect(self._persist_catalogue_root)
        page.active_target_changed.connect(self._on_active_sample_target_changed)
        page.open_result_requested.connect(self._open_inventory_result)
        page.samples_updated.connect(self._on_inventory_samples_updated)
        page.status.connect(self._log)
        return page

    def _build_elab_page(self) -> ElabPage:
        if self._elab_page is not None:
            return self._elab_page
        page = ElabPage(
            self._repository,
            settings=self._settings,
            env_path=Path(".env"),
            simulation=self._simulation,
        )
        self._elab_page = page
        page.settings_saved.connect(self._settings_saved)
        page.configuration_changed.connect(self._on_elab_configuration_changed)
        page.upload_completed_record.connect(self._on_elab_upload_completed)
        page.status.connect(self._log)
        if self._last_selected_result_path is not None:
            page.set_selected_result(self._last_selected_result_path)
        return page

    def _build_settings_page(self) -> SettingsPage:
        if self._settings_page is not None:
            return self._settings_page
        page = SettingsPage(
            self._repository,
            read_only=self._simulation,
            access_policy=self._access,
        )
        self._settings_page = page
        page.settings_saved.connect(self._settings_saved)
        page.moke_address_change_requested.connect(self._change_moke_address)
        page.status.connect(self._log)
        return page

    def _on_device_capabilities_changed(self, device: str, capabilities: object) -> None:
        self._last_device_capabilities[device] = capabilities
        page = self._realized_device_pages.get(device)
        if page is not None and hasattr(page, "set_capabilities"):
            page.set_capabilities(capabilities)

    def _on_result_selected(self, path: object) -> None:
        self._last_selected_result_path = path
        if self._elab_page is not None:
            self._elab_page.set_selected_result(path)

    def _on_inventory_samples_updated(self) -> None:
        if self._keithley_characterization_page is not None:
            self._keithley_characterization_page.refresh_samples_list()
        if self._results_page is not None:
            self._refresh_results_catalogue()

    def _on_elab_configuration_changed(self) -> None:
        self._sync_elab_upload_controls()
        if self._recipe_page is not None:
            self._recipe_page.sync_elab_context()

    def _refresh_inventory_if_realized(self, _sample_id: object = None) -> None:
        if self._inventory_page is not None:
            self._inventory_page.refresh_samples()

    def _flush_resize_layout(self) -> None:
        if hasattr(self, "apparatus_navigation_item"):
            self._sync_apparatus_navigation_height()
        if hasattr(self, "event_log_panel"):
            self._sync_shell_splitter_layout()

    def _navigate_to(self, route: str) -> None:
        target = self.navigation_routes[route]
        target.ensure_content()
        nav_item = self.navigationInterface.widget(target.objectName())
        if (
            (not self.isVisible() or not self.navigationInterface.panel.isCollapsed())
            and nav_item is not None
            and getattr(nav_item, "treeParent", None) is not None
        ):
            p = nav_item.treeParent
            while p:
                if hasattr(p, "setExpanded") and not p.isExpanded:
                    p.setExpanded(True, ani=False)
                p = getattr(p, "treeParent", None)
        self.switchTo(target)
        self._sync_shell_splitter_layout()
        if self._last_execution_readback is not None:
            self._apply_runner_device_readback(*self._last_execution_readback)

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
        runner_outputs = getattr(self, "_execution_output_status", {})
        runner_devices = {endpoint.split(".", 1)[0] for endpoint in runner_outputs}
        active_outputs = sum(state == "on" for state in runner_outputs.values()) + sum(
            state == "output_on" for device, state in self._device_states.items()
            if device not in runner_devices)
        unknown_outputs = sum(
            state in {"unknown", "fault", "disconnected", "connecting"}
            for device, state in self._device_states.items()
            if device != "lakeshore_gaussmeter" and device not in runner_devices
        ) + sum(state == "unknown" for state in runner_outputs.values())
        # A valid DAC reply verifies communication, never Kepco power-off.
        moke_state = self._device_states.get("moke_box")
        moke_page = MainWindow._peek_page(self, "moke_box_page", "moke_box")
        moke_busy = bool(moke_page is not None and moke_page.field_workflow.busy)
        moke_power_uncertain = moke_state not in {None, "disconnected"} or moke_busy
        if moke_power_uncertain and moke_state not in {"unknown", "fault"} and "moke_box" not in runner_devices:
            unknown_outputs += 1
        self.safety_strip.update_snapshot(
            StationSafetySnapshot(
                ready=self.dashboard.evaluate_readiness(display_only=True).ready,
                active_outputs=active_outputs,
                unknown_outputs=unknown_outputs,
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

    def _apply_accessibility_to(self, root: QWidget) -> None:
        """Ensure controls inside ``root`` expose screen-reader metadata."""

        for button in root.findChildren(QPushButton):
            if not button.accessibleName():
                button.setAccessibleName(button.text().replace("&", ""))
            if not button.accessibleDescription() and button.toolTip():
                button.setAccessibleDescription(button.toolTip())
        for editor in root.findChildren(QLineEdit):
            if not editor.accessibleName():
                editor.setAccessibleName(
                    editor.placeholderText() or "Numeric or text parameter"
                )

    def _apply_accessibility(self) -> None:
        """Ensure controls expose text as well as colour and have screen-reader metadata."""

        self._apply_accessibility_to(self)
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
            except (QuantityError, ValueError, TypeError):
                continue
            return format_quantity_auto(max(abs(minimum_si), abs(maximum_si)), dimension)
        raise ValueError(f"Cannot synchronize max_abs={original_max_abs!r} with the edited range.")

    def _limit_edit_spec(self, device: str, field: LimitField) -> tuple[str, tuple[str, ...], bool]:
        key = str(field.property("limitKey"))
        if device == "rigol":
            channel = self.rigol_page.channel.currentText()
            path = ("devices", "rigol", "safety", "channels", channel, "lab_limits", key)
            if key == "combined_voltage_limit":
                return f"Rigol CH{channel} — voltage limits", path, True
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
        labels = {
            "source_current": "allowed source-current setting",
            "source_voltage": "allowed source-voltage setting",
            "current_compliance": "allowed current-compliance setting",
            "voltage_compliance": "allowed voltage-compliance setting",
            "point_settle_time": "allowed point settling time",
            "max_abs_power": "maximum absolute power",
        }
        path = ("devices", "keithley", "safety", "channels", channel, "lab_limits", mapped)
        return (
            f"Keithley CH{channel} — {labels[mapped]}",
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
            base_raw = (
                self._settings_page._raw
                if self._settings_page is not None and self._settings_page._dirty
                else loaded.raw
            )
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

        key = str(field.property("limitKey"))
        guidance: str | None = None
        max_label = "Maximum"
        if scalar_limit:
            if key == "combined_voltage_limit":
                value_label = "Minimum voltage"
                max_label = "Maximum voltage"
                maximum_enabled = True
                try:
                    current_si = parse_quantity(str(minimum), DIMENSION_VOLTAGE).si_value
                    minimum = format_quantity_auto(-abs(current_si), DIMENSION_VOLTAGE)
                    maximum = format_quantity_auto(abs(current_si), DIMENSION_VOLTAGE)
                except (QuantityError, ValueError, TypeError):
                    minimum = f"-{range_data}"
                    maximum = str(range_data)
                guidance = (
                    "Enter the symmetric voltage limits for this channel (for example: minimum -800 mV, maximum 800 mV). "
                    "The shared voltage budget (Vpp + |offset|) will not exceed the maximum absolute limit."
                )
            elif key == "max_abs_power":
                value_label = "Maximum power"
                guidance = (
                    "Enter the maximum absolute power limit for this Keithley channel (for example: 1000 uW, 10 mW). "
                    "The complete configuration is validated before it is saved."
                )
            else:
                value_label = "Limit value"
        else:
            value_label = "Minimum"

        dialog = LimitEditDialog(
            title,
            minimum,
            maximum,
            maximum_enabled=maximum_enabled,
            value_label=value_label,
            max_label=max_label,
            guidance=guidance,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            if scalar_limit:
                if key == "combined_voltage_limit":
                    min_text = dialog.minimum.text().strip()
                    max_text = dialog.maximum.text().strip()
                    if not min_text and not max_text:
                        raise ValueError("Voltage limit cannot be empty.")
                    min_v = parse_quantity(min_text, DIMENSION_VOLTAGE).si_value if min_text else None
                    max_v = parse_quantity(max_text, DIMENSION_VOLTAGE).si_value if max_text else None
                    if min_v is not None and max_v is not None:
                        budget_v = max(abs(min_v), abs(max_v))
                    elif min_v is not None:
                        budget_v = abs(min_v)
                    else:
                        assert max_v is not None
                        budget_v = abs(max_v)
                    if budget_v <= 0:
                        raise ValueError("Voltage limit must be greater than zero.")
                    replacement = format_quantity_auto(budget_v, DIMENSION_VOLTAGE)
                elif key == "max_abs_power":
                    replacement = self._coerce_limit_value(dialog.minimum.text(), minimum)
                    parsed = parse_quantity(str(replacement), DIMENSION_POWER)
                    if parsed.si_value <= 0:
                        raise ValueError("Maximum power limit must be positive.")
                else:
                    replacement = self._coerce_limit_value(dialog.minimum.text(), minimum)
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
            if key == "combined_voltage_limit":
                budget_qty = parse_quantity(str(replacement), DIMENSION_VOLTAGE)
                neg_bound = format_quantity_auto(-budget_qty.si_value, DIMENSION_VOLTAGE)
                field.set_limits(neg_bound, str(replacement))
            else:
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
        if device == "rigol":
            self.rigol_page.set_settings(settings)
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
            if not isValid(top_level) or getattr(top_level, "_closed", False):
                continue
            for plot in top_level.findChildren(SpectrumPlotWidget):
                plot.apply_theme(theme)
            for heatmap in top_level.findChildren(HeatmapResultsTab):
                heatmap.apply_theme(theme)
            from app.ui.results.scalar_tab import ScalarResultsTab
            for scalars in top_level.findChildren(ScalarResultsTab):
                scalars.apply_theme(theme)
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
        if self._settings_page is not None:
            if self._settings_page._dirty:
                self._settings_page.accept_external_snapshot(self._settings, loaded.raw)
            else:
                self._settings_page.reload()

    def _persist_catalogue_root(self, root: str) -> None:
        """Persist the catalogue locator outside the database it locates."""

        resolved = str(Path(root).expanduser().resolve())
        try:
            loaded = self._repository.load()
            loaded.raw.setdefault("storage", {})["catalogue_directory"] = resolved
            self._settings = self._repository.save_raw(loaded.raw)
        except ConfigurationError as exc:
            self._log(f"Catalogue root is active but could not be saved: {exc}")
            return
        if self._settings_page is not None:
            if self._settings_page._dirty:
                self._settings_page.accept_external_snapshot(self._settings, loaded.raw)
            else:
                self._settings_page.reload()
        self._log(f"Samples catalogue location saved: {resolved}")

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
        self._saved_anritsu_splitter = splitter
        if splitter is not None and "anritsu" in self._realized_device_pages:
            self._realized_device_pages["anritsu"].workspace_splitter.restoreState(splitter)
        route = str(settings.value("main_window/current_route", "overview"))
        if route not in self.navigation_routes:
            route = "overview"
        # Restore the initial page as layout state. Fluent's entrance transition
        # otherwise starts on the hidden host's provisional geometry and keeps
        # moving it while show/restoreGeometry are laying out the native window.
        animate = self.stackedWidget.isAnimationEnabled()
        self.stackedWidget.setAnimationEnabled(False)
        try:
            self._navigate_to(route)
        finally:
            self.stackedWidget.setAnimationEnabled(animate)

    def _save_workspace(self) -> None:
        settings = QSettings("LabControl", "LabControl")
        settings.setValue("main_window/geometry", self.saveGeometry())
        settings.setValue(
            "main_window/content_splitter",
            self.shell_splitter.saveState(),
        )
        settings.remove("main_window/navigation_expanded")
        settings.setValue("main_window/current_route", self._current_route())
        anritsu_page = self._realized_device_pages.get("anritsu")
        if anritsu_page is not None:
            settings.setValue("anritsu/splitter", anritsu_page.workspace_splitter.saveState())

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
            if name in {"rigol", "anritsu"}:
                controller.capabilities_changed.connect(
                    lambda capabilities, device=name: self._on_device_capabilities_changed(
                        device, capabilities
                    )
                )

    def _show_quick_controls(self) -> None:
        # Ensure quick-control sources are wired before populating rows/output pills.
        rigol_page = self.rigol_page
        keithley_page = self.keithley_page
        _ = self.moke_box_page
        if not self.quick_controls_window._selected:
            self.quick_controls_window.choose_controls()
        # Output controls remain available even when the operator has not
        # pinned a numeric quick-control row yet.
        self.quick_control_coordinator.refresh()
        for device, page in (
            ("rigol", rigol_page),
            ("keithley", keithley_page),
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
        """Collect last-confirmed numeric values from realized device pages."""

        values: list[ManualMetadataValue] = []
        for key in (
            "anritsu",
            "rigol",
            "keithley",
            "moke_box",
            "lakeshore_gaussmeter",
        ):
            page = self._realized_device_pages.get(key)
            if page is None:
                continue
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
                    if self._settings_page is not None:
                        self._settings_page.reload()
                except (ConfigurationError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
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
        if not self._run_controller.running:
            self._execution_output_status = {endpoint: value for endpoint, value in self._execution_output_status.items()
                if endpoint.split(".", 1)[0] != device}
        self._device_states[device] = state
        self.dashboard.update_device_state(device, state)
        self._refresh_safety_strip()

    def _guard_manual_operation(self, device: str, operation: str, payload: object) -> None:
        """Fail closed for new energy-producing operations after audit I/O failure."""

        # De-energising and disconnecting are never blocked by RBAC or audit
        # health. This invariant is stronger than any normal user permission.
        if operation in {
            "emergency_off",
            "recover_from_compliance",
            "set_compliance_policy",
            "ramp_to_zero",
            "abort_acquisition",
            "stop_live",
            "disconnect",
            "stop_vout",
            "disarm_voltage_plan",
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
        if device in self._leased_run_devices:
            raise ConfigurationError(
                f"{getattr(self._settings, device).display_name} is leased to the active recipe run."
            )
        self._refresh_audit_health()
        energizing_operations = {
            "arm_voltage_plan", "arm_field_calibration", "ramp_vout", "start_field_calibration",
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
            "arm_voltage_plan", "arm_field_calibration", "ramp_vout", "start_field_calibration",
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

    def _authorize_moke_workflow(self, operation: str, payload: object) -> None:
        self._guard_manual_operation("moke_box", operation, payload)
        self._audit_record(
            "MOKE field workflow authorized", category="device",
            event_type=operation, context={"device": "moke_box", "request": payload}, critical=True,
        )
        self._assert_audit_ready_for_run()

    def _assert_audit_ready_for_run(self) -> None:
        self._refresh_audit_health()
        if not self._audit_healthy:
            raise ConfigurationError(
                "The durable audit log is unavailable. A measurement run cannot start."
            )

    def _prepare_execution(
        self, plan: object, outputs_forced_off: bool, execution_mode: str,
        output_dir_override: str, file_stem_override: str,
    ) -> None:
        if self._run_controller.running:
            return
        try:
            estimate = PlanEstimator(self._settings).estimate(plan)
            tree = self.recipe_page.semantic_tree_snapshot(plan.recipe_source, plan, update_editor=False)
        except (ConfigurationError, ValueError) as exc:
            self._invalidate_prepared_execution()
            QMessageBox.warning(self, "Execution preview unavailable", str(exc))
            return
        rigol = self._realized_device_pages.get("rigol")
        self.run_monitor.preview_plan(
            plan, estimate, tree,
            execution_mode=execution_mode,
            output_preview=self.recipe_page.output_file_preview.text(),
            rigol_level_modes=rigol.voltage_entry_modes() if rigol is not None else None,
        )
        self._prepared_execution = (
            plan, outputs_forced_off, execution_mode, output_dir_override, file_stem_override,
        )
        self._navigate_to("execution")

    def _invalidate_prepared_execution(self, *_args: object) -> None:
        if self._prepared_execution is not None:
            self._prepared_execution = None
            self.run_monitor.invalidate_plan_preview()

    def _prepared_execution_is_current(self, request: tuple[object, bool, str, str, str]) -> bool:
        current = self.recipe_page.execution_request()
        return (
            self._prepared_execution is request
            and not self._run_controller.running
            and current is not None
            and current[0] is request[0]
            and current[1:] == request[1:]
        )

    def _launch_prepared_execution(self) -> None:
        request = self._prepared_execution
        if request is None:
            return
        if not self._prepared_execution_is_current(request):
            self._invalidate_prepared_execution()
            return
        self._start_run(*request, prepared_request=request)

    def _start_run(
        self,
        plan: object,
        outputs_forced_off: bool = False,
        execution_mode: str = "measurement",
        output_dir_override: str = "",
        file_stem_override: str = "",
        *,
        prepared_request: tuple[object, bool, str, str, str] | None = None,
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
            prepared_request=prepared_request,
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
        prepared_request: tuple[object, bool, str, str, str] | None = None,
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
                    prepared_request=prepared_request,
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
        # Verify missing sessions on the page controllers so the run can borrow
        # them and return them to the UI without closing the transport.
        self._connect_sweep_devices(dialog.connectable_devices)
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
        prepared_request: tuple[object, bool, str, str, str] | None = None,
    ) -> None:
        if prepared_request is not None and not self._prepared_execution_is_current(prepared_request):
            if self._prepared_execution is prepared_request:
                self._invalidate_prepared_execution()
            dialog.reject()
            return
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
            sample_target = self.inventory_store.get_active_target()
            if sample_target.is_active and sample_target.sample_id:
                output_dir_override = str(
                    self.inventory_store.measurement_directory_for(
                        sample_target.sample_id, "sweeps", row=sample_target.row, col=sample_target.col,
                    )
                )
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
                sample_target=sample_target,
            )
        except Exception as exc:  # noqa: BLE001 - UI run-start boundary displays any compile/engine error
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
            rigol_level_modes=self.rigol_page.voltage_entry_modes(),
            safe_shutdown_actions=plan.safe_shutdown_actions,
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
        except Exception as exc:  # noqa: BLE001 - UI reconstruction boundary displays any format error
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
        from app.ui.resume_preparation import ResumePreparation
        if not hasattr(self, "_resume_preparation"):
            self._resume_preparation = ResumePreparation(self)
            self._resume_preparation.ready.connect(self._resume_prepared)
            self._resume_preparation.failed.connect(self._resume_preparation_failed)
        self._log("Inspecting recovery archive and compiling its plan...")
        if not self._resume_preparation.start(path, self._settings, self._repository.path,
                                              self._simulation, self._composition.registry):
            QMessageBox.warning(self, "Resume unavailable", "Previous recovery inspection is still stopping. Try again when it finishes.")

    def _resume_preparation_failed(self, message):
        box = MessageBox("Resume unavailable", message, self)
        box.cancelButton.hide()
        box.exec()
        self._log(f"RUN RECOVERY REJECTED: {message}")

    def _resume_prepared(self, prepared):
        if self._run_controller.running:
            self._log("Recovery preparation discarded: another run is active.")
            return
        if self._settings != prepared.settings or self._simulation != prepared.simulation:
            self._resume_preparation_failed("Station settings changed during recovery inspection. Select the run again.")
            return
        try:
            self._require_permission(Permission.RUN_RECIPE, "resuming a measurement run", audit=True)
            self._assert_audit_ready_for_run()
        except (AuthorizationError, ConfigurationError) as exc:
            self._resume_preparation_failed(str(exc))
            return
        plan, checkpoint = prepared.plan, prepared.checkpoint
        stored_execution_mode = prepared.mode
        outputs_forced_off = stored_execution_mode is ExecutionMode.DRY_RUN
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
        if getattr(self, "_emergency_inhibit", False):
            QMessageBox.warning(
                self, "E-STOP Latched",
                "Emergency stop has been triggered and outputs are inhibited. "
                "Clear the emergency stop before resuming a measurement.",
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
        except Exception as exc:  # noqa: BLE001 - UI resume boundary displays any recovery/engine error
            QMessageBox.critical(self, "Resume not started", str(exc))
            return
        self._leased_run_devices = set(active_controllers)
        dialog.accept()
        remaining = len(plan.actions) - checkpoint.next_action_index
        remaining_fraction = remaining / max(1, len(plan.actions))
        self.run_monitor.run_started(
            remaining + len(checkpoint.prelude_actions),
            estimate.nominal_duration_s * remaining_fraction,
            timeline_action_offset=checkpoint.next_action_index - len(checkpoint.prelude_actions),
            rigol_level_modes=self.rigol_page.voltage_entry_modes(),
            safe_shutdown_actions=plan.safe_shutdown_actions,
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

    def _refresh_visible_execution_readback(self) -> None:
        if self._last_execution_readback is not None:
            self._apply_runner_device_readback(*self._last_execution_readback)

    def _run_event(self, name: str, data: object) -> None:
        payload = data if isinstance(data, dict) else {"data": data}
        if name == "spectrum_archive_ready":
            self.run_monitor.spectrogram_preview.set_archive(
                Path(str(payload["path"])), int(payload["stored_points"])
            )
            return
        if name == "point_stored" and "stored_points" in payload:
            self.run_monitor.spectrogram_preview.set_stored_points(int(payload["stored_points"]))
        if name == "run_started":
            self._execution_page_projection_cache.clear()
            self._last_execution_readback = None
        preview_event = name in {"spectrum_preview", "reference_preview"}
        # Heartbeats carry no confirmed state and previews are rendered at a
        # bounded cadence. Read-only device pages also use a latest-state
        # projection timer while the worker is active; the execution monitor
        # still receives every lifecycle event needed for progress and safety.
        if preview_event and self._run_controller.running:
            self._pending_execution_previews.pop(name, None)
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
                if name in {"compliance_detected", "sweep_compliance_limited", "run_aborted", "safe_finally_error"}
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
            "moke_ramp_progress",
            "action_finished",
            "recovery_prelude_started",
            "recovery_prelude_finished",
            "semantic_operation_started",
            "semantic_operation_applied",
            "semantic_operation_failed",
            "semantic_operation_skipped",
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
            "semantic_operation_skipped",
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

        self._last_execution_readback = (event_name, dict(payload))
        snapshot = payload.get("state_snapshot")
        statuses = snapshot.get("output_status") if isinstance(snapshot, dict) else None
        if isinstance(statuses, dict) and statuses:
            confirmed = {str(endpoint): state if isinstance(state, str) and state in {"on", "off", "unknown"} else "unknown"
                for endpoint, state in statuses.items()}
            if confirmed != self._execution_output_status:
                self._execution_output_status = confirmed
                self._refresh_safety_strip()

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
        realized = getattr(self, "_realized_device_pages", None)
        device_pages = realized if isinstance(realized, dict) else self._device_pages
        pages = (
            (("anritsu", device_pages["anritsu"]),)
            if event_name in {"spectrum_preview", "reference_preview"}
            and "anritsu" in device_pages
            else tuple(device_pages.items())
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
        self.run_monitor.complete(
            result, dialog_parent=self.navigation_routes["execution"].scroll_area.viewport()
        )
        result_path = None
        if isinstance(result, dict) and result.get("path"):
            result_path = Path(str(result["path"]))
        if result_path is not None and self._results_page is not None:
            self._results_page.offer_completed_result(result_path)
        run_result = result["result"]
        state = str(getattr(getattr(run_result, "state", None), "value", "unknown"))
        error = getattr(run_result, "error", None)
        elab_auto_enabled = bool(
            not self._simulation
            and getattr(getattr(self._settings, "elab", None), "enabled", False)
        )
        if result_path is not None and (
            self._run_upload_to_elab_requested
            or self._run_elab_upload_config is not None
            or self._elab_page is not None
            or elab_auto_enabled
        ):
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
        if (
            result_path is not None
            and sample_target is not None
            and hasattr(sample_target, "is_active")
            and sample_target.is_active
        ):
            point_count = (
                len(getattr(run_result, "points", ()))
                if hasattr(run_result, "points")
                else getattr(run_result, "committed_points", 0)
            )
            spectrum_count = (
                len(getattr(run_result, "spectra", ()))
                if hasattr(run_result, "spectra")
                else 0
            )
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
            if self._inventory_page is not None:
                self._inventory_page.refresh_samples()
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
        self._bind_sweep_sample_target(target)
        char_page = MainWindow._peek_page(self, "keithley_characterization_page")
        if char_page is not None:
            char_page.set_active_sample_target(target)
        self._refresh_safety_strip()

    def _bind_sweep_sample_target(self, target: ActiveSampleTarget) -> None:
        recipe_page = MainWindow._peek_page(self, "recipe_page")
        if recipe_page is not None:
            directory = (
                self.inventory_store.measurement_directory_for(
                    target.sample_id, "sweeps", row=target.row, col=target.col
                )
                if target.is_active
                else None
            )
            recipe_page.set_active_sample_target(target, output_directory=directory)
        anritsu_page = MainWindow._peek_page(self, "anritsu_page", "anritsu")
        if anritsu_page is not None:
            manual_directory = (
                self.inventory_store.measurement_directory_for(
                    target.sample_id,
                    "single_measurements",
                    "Anritsu_MS2830A",
                    row=target.row,
                    col=target.col,
                )
                if target.is_active
                else None
            )
            anritsu_page.set_sample_measurement_context(manual_directory, target.as_dict())
        self._set_results_sample_context(target)

    def _set_results_sample_context(self, target: ActiveSampleTarget) -> None:
        results_page = MainWindow._peek_page(self, "results_page")
        if results_page is None:
            return
        results_page.file_browser.set_sample_context(
            self._results_directory_for_target(target), self.inventory_store.catalogue_root
        )

    def _results_directory_for_target(self, target: ActiveSampleTarget) -> Path | None:
        active_directory = None
        if target.is_active:
            active_directory = (self.inventory_store.device_directory_for(target.sample_id, target.row, target.col)
                                if target.row and target.col else self.inventory_store.sample_directory(target.sample_id))
        return active_directory

    def _open_inventory_result(self, run_path: str) -> None:
        path = Path(run_path)
        if path.is_file():
            self.results_page.select_result_path(path)
            self._navigate_to("results")
        else:
            self._log(f"Measurement file not found: {run_path}")

    def _refresh_results_catalogue(self) -> None:
        """Update active-target context without resetting operator navigation."""

        results_page = MainWindow._peek_page(self, "results_page")
        if results_page is None:
            return
        previous_directory = results_page.file_browser.output_dir
        self._set_results_sample_context(self.inventory_store.get_active_target())
        # A changed directory has already started its own index job.
        if results_page.file_browser.output_dir == previous_directory:
            results_page.file_browser.refresh()

    def _on_elab_upload_completed(self, record: object) -> None:
        if hasattr(record, "run_path") and hasattr(record, "experiment_id"):
            self.inventory_store.update_run_elab_status(
                str(record.run_path),
                elab_experiment_id=record.experiment_id,
                elab_url=getattr(record, "experiment_url", None),
                elab_status="uploaded",
            )
            if self._inventory_page is not None:
                self._inventory_page.refresh_samples()

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
        anritsu_page = MainWindow._peek_page(self, "anritsu_page", "anritsu")
        if anritsu_page is not None:
            anritsu_page.prepare_acquisition_stop()
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
        for name, page in self._realized_device_pages.items():
            if changed_devices is None or name in changed_devices:
                page.set_settings(self._settings)
        if (
            self._keithley_characterization_page is not None
            and (changed_devices is None or "keithley" in changed_devices)
        ):
            self._keithley_characterization_page.set_settings(self._settings)
        for name, panel in self.connection_panels.items():
            if changed_devices is not None and name not in changed_devices:
                continue
            resource, backend = self._device_connection_details(self._settings, name)
            panel.update_resource(resource, backend)
        if self._recipe_page is not None:
            self._recipe_page.set_settings(self._settings)
        if self._elab_page is not None:
            self._elab_page.set_settings(self._settings)
        self._sync_elab_upload_controls()

    def _sync_elab_upload_controls(self) -> None:
        """Refresh per-run/per-save eLab controls from the central policy."""

        recipe_page = MainWindow._peek_page(self, "recipe_page")
        if recipe_page is None:
            return
        elab_page = MainWindow._peek_page(self, "elab_page")
        if elab_page is not None:
            available, default_enabled, hint = elab_page.upload_configuration()
        elif self._simulation:
            available, default_enabled, hint = (
                False,
                False,
                "Simulation mode disables external eLab writes.",
            )
        else:
            available, default_enabled, hint = self.elab_page.upload_configuration()
        recipe_page.set_elab_upload_state(available, default_enabled, hint)

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
        if self._settings_page is not None:
            self._settings_page.reload()
        self.anritsu_page.set_settings(self._settings)
        if self._recipe_page is not None:
            self._recipe_page.set_settings(self._settings)
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
                    "vbw_filter_mode": advanced.vbw_filter_mode,
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
                advanced = self.anritsu_page.advanced_settings_snapshot()
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
        if self._settings_page is not None:
            self._settings_page.reload()
        self.anritsu_page.set_settings(self._settings)
        if self._recipe_page is not None:
            self._recipe_page.set_settings(self._settings)
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
        if self._settings_page is not None:
            self._settings_page.reload()
        self.rigol_page.set_settings(self._settings)
        if self._recipe_page is not None:
            self._recipe_page.set_settings(self._settings)
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
        if self._settings_page is not None:
            self._settings_page.reload()
        self.lakeshore_gaussmeter_page.set_settings(self._settings)
        if self._recipe_page is not None:
            self._recipe_page.set_settings(self._settings)
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
        if self._settings_page is not None:
            self._settings_page.reload()
        self.moke_box_page.set_settings(self._settings)
        if self._recipe_page is not None:
            self._recipe_page.set_settings(self._settings)
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
        # Capture every realized device page before SettingsPage emits
        # settings_saved. Unrealized pages cannot hold unsaved edits.
        rigol_page = self._realized_device_pages.get("rigol")
        anritsu_page = self._realized_device_pages.get("anritsu")
        keithley_page = self._realized_device_pages.get("keithley")
        lakeshore_page = self._realized_device_pages.get("lakeshore_gaussmeter")
        moke_page = self._realized_device_pages.get("moke_box")
        rigol_defaults = None
        preview_defaults = None
        anritsu_defaults = None
        keithley_payload = None
        lakeshore_interval_ms = None
        moke_defaults = None
        try:
            if rigol_page is not None:
                rigol_defaults = rigol_page.settings_defaults()
            if anritsu_page is not None:
                preview_defaults = anritsu_page.preview_settings_snapshot()
                anritsu_defaults = (
                    anritsu_page.configuration_panel.configuration_snapshot(),
                    anritsu_page.advanced_settings_snapshot(),
                    anritsu_page.signal_generator_snapshot(),
                    anritsu_page.average_count.value(),
                    anritsu_page.refresh.value(),
                )
            keithley_snapshots = (
                {
                    channel: keithley_page.configuration_snapshot_for(channel)
                    for channel in ("A", "B")
                }
                if keithley_page is not None
                else None
            )
            if lakeshore_page is not None:
                lakeshore_interval_ms = int(lakeshore_page.sample_interval.currentData())
            if moke_page is not None:
                moke_defaults = (
                    int(moke_page.sample_interval.currentData()),
                    int(moke_page.refresh_interval.currentData()),
                    int(moke_page.history_window.currentData()),
                )
        except (ConfigurationError, SafetyViolation, TypeError, ValueError) as exc:
            QMessageBox.critical(self, "Settings not saved", str(exc))
            self._log(f"SAVE SETTINGS CAPTURE FAILED: {type(exc).__name__}: {exc}")
            return

        if keithley_snapshots is not None:
            try:
                keithley_payload = self._keithley_snapshot_payload(keithley_snapshots)
            except (ConfigurationError, SafetyViolation, KeyError, TypeError, ValueError) as exc:
                QMessageBox.critical(self, "Keithley settings not saved", str(exc))
                if keithley_page is not None:
                    keithley_page.banner.show_message(f"Keithley settings not saved: {exc}")
                self._log(f"SAVE SETTINGS REJECTED: {type(exc).__name__}: {exc}")
                return

        pending_keithley_defaults = self._pending_keithley_defaults

        def merge_device_forms(raw: dict[str, Any]) -> None:
            if rigol_defaults is not None:
                for channel, payload in rigol_defaults.items():
                    defaults = raw["devices"]["rigol"]["safety"]["channels"][str(channel)]["defaults"]
                    defaults.clear()
                    defaults.update(deepcopy(payload))

            if anritsu_defaults is not None:
                snapshot, advanced, signal_generator, average_count, refresh_ms = anritsu_defaults
                self._update_anritsu_defaults(raw, snapshot, advanced)
                generator = raw["devices"]["anritsu"]["signal_generator"]
                generator["default_frequency"] = format_quantity_auto(
                    signal_generator.frequency_hz, DIMENSION_FREQUENCY
                )
                generator["default_power"] = f"{signal_generator.power_dbm:.9g} dBm"
                acquisition = raw["devices"]["anritsu"]["acquisition"]
                acquisition["application_average_count"] = average_count
                raw["devices"]["anritsu"]["preview"] = preview_defaults
                acquisition["live_refresh_interval"] = format_quantity_auto(
                    refresh_ms / 1000, DIMENSION_TIME
                )

            if lakeshore_interval_ms is not None:
                raw["devices"]["lakeshore_gaussmeter"]["live_interval"] = format_quantity_auto(
                    lakeshore_interval_ms / 1000, DIMENSION_TIME
                )
            if moke_defaults is not None:
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
            if pending_keithley_defaults is None and keithley_payload is not None:
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

        if self._settings_page is not None:
            if not self._settings_page.save_draft(extra_transform=merge_device_forms):
                return
        elif not self._save_device_forms_without_settings_page(merge_device_forms):
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
        save_feedback = QTimer(self.safety_strip.save_settings)
        save_feedback.setSingleShot(True)
        save_feedback.timeout.connect(lambda: self.safety_strip.save_settings.setText("SAVE SETTINGS"))
        save_feedback.timeout.connect(save_feedback.deleteLater)
        save_feedback.start(1_500)

    def _save_device_forms_without_settings_page(
        self,
        extra_transform: Callable[[dict[str, Any]], None],
    ) -> bool:
        """Persist device-form updates directly without constructing lazy SettingsPage."""

        try:
            self._access.require(
                Permission.EDIT_SETTINGS,
                action="saving station settings",
            )

            def merge_latest(
                latest: dict[str, Any], _settings: StationSettings
            ) -> dict[str, Any]:
                draft = deepcopy(latest)
                extra_transform(draft)
                SettingsPage._sync_moke_vout_permissions(draft)
                self._repository.repair_known_issues(draft)
                for channel in ("A", "B"):
                    path = (
                        "devices",
                        "keithley",
                        "safety",
                        "channels",
                        channel,
                        "defaults",
                        "source_autorange",
                    )
                    if (
                        SettingsPage._get_path(draft, path[:-1]).get(
                            "source_autorange", False
                        )
                        is True
                        and SettingsPage._get_path(latest, path[:-1]).get(
                            "source_autorange", False
                        )
                        is not True
                    ):
                        raise AuthorizationError(
                            "Source AUTO requires explicit operator confirmation in Settings."
                        )
                return draft

            settings, _draft = self._repository.update_raw(merge_latest)
        except AuthorizationError as exc:
            QMessageBox.critical(self, "Changes not saved", str(exc))
            return False
        except (ConfigurationError, ValueError) as exc:
            QMessageBox.critical(
                self,
                "Changes not saved",
                format_settings_validation_error(exc),
            )
            return False
        self._settings_saved(settings)
        return True

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
        save_feedback = QTimer(self.safety_strip.save_settings)
        save_feedback.setSingleShot(True)
        save_feedback.timeout.connect(
            lambda: self.safety_strip.save_settings.setText("SAVE SETTINGS")
        )
        save_feedback.timeout.connect(save_feedback.deleteLater)
        save_feedback.start(1_500)
        if isinstance(persisted, StationSettings) and isinstance(raw, dict):
            self._settings = (
                simulated_station_settings(persisted) if self._simulation else persisted
            )
            if self._settings_page is not None:
                self._settings_page.accept_external_snapshot(persisted, raw)
            keithley_page = self._realized_device_pages.get("keithley")
            if keithley_page is not None:
                keithley_page.set_settings(self._settings)
            if self._recipe_page is not None:
                self._recipe_page.set_settings(self._settings)
            self.quick_control_coordinator.set_settings(self._settings)
            self._controllers["keithley"].call("refresh_station_context", self._settings)
            self._refresh_safety_strip()
        keithley_page = self._realized_device_pages.get("keithley")
        if keithley_page is not None:
            keithley_page.banner.show_message(
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
        except (ConfigurationError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            self._log(f"VISA ASSIGN FAILED: {type(exc).__name__}: {exc}")
            QMessageBox.critical(self, "VISA assignments not saved", str(exc))
            return
        self._log("VISA ASSIGN SAVED: settings.yml updated atomically")
        if self._settings_page is not None:
            self._settings_page.reload()
        self._settings_saved(settings)
        self.dashboard.mark_assignments_saved(assignments)
        for device, (resource, backend, _idn) in assignments.items():
            self._log(
                f"VISA ASSIGN SUCCESS [{device}]: card and worker now use {resource!r} via {backend!r}"
            )

    def _change_moke_address(self) -> None:
        if not self._access.allows(Permission.ASSIGN_VISA):
            return
        moke_page = self._realized_device_pages.get("moke_box")
        if self._run_controller.running or (
            moke_page is not None and moke_page.field_workflow.busy
        ):
            QMessageBox.warning(self, "MOKE Box is in use", "Stop the active workflow before testing a new address.")
            return
        if not self._simulation and self._device_states["moke_box"] in {"connected", "verified", "armed"}:
            QMessageBox.warning(self, "MOKE Box is connected", "Disconnect the current MOKE Box session first.")
            return
        dialog = MokeAddressDialog(
            self.settings_page.current_moke_address, self, discovery_available=not self._simulation,
        )
        try:
            if dialog.exec() and dialog.verified_endpoint:
                self._save_moke_assignment(dialog.verified_endpoint, confirmed=True)
        finally:
            dialog.deleteLater()

    def _save_moke_assignment(self, endpoint: str, *, confirmed: bool = False) -> None:
        try:
            self._require_permission(
                Permission.ASSIGN_VISA,
                "assigning a verified MOKE Box TCP endpoint",
                audit=True,
            )
        except AuthorizationError as exc:
            QMessageBox.warning(self, "Access denied", str(exc))
            return
        moke_page = self._realized_device_pages.get("moke_box")
        if self._run_controller.running or (
            moke_page is not None and moke_page.field_workflow.busy
        ):
            QMessageBox.warning(self, "MOKE Box is in use", "Stop the active workflow before changing its address.")
            return
        existing = self._settings.moke_box
        if existing.enabled and existing.protocol_qualified and existing.endpoint == endpoint:
            message = f"MOKE Box is already assigned to {endpoint}."
            self.dashboard.tcp_discovery_info.setText(message)
            if confirmed and self._settings_page is not None:
                show_toast(self._settings_page, message, severity="info", title="MOKE Box")
            return
        if not confirmed:
            answer = QMessageBox.question(
                self,
                "Assign MOKE Box",
                f"Save {endpoint} as the MOKE Box endpoint?\n\n"
                "The read-only VOUT response was verified. Your channel profiles and limits are kept. "
                "Review the wiring and enable voltage control in Settings → MOKE Box for the new address.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            def assign(raw: dict, _settings: StationSettings) -> dict:
                profile = raw["devices"]["moke_box"]
                if profile["endpoint"] != endpoint or not profile["protocol_qualified"]:
                    profile.update(allow_vout_control=False, allowed_vout_channels=[], test_vout_channels=[])
                profile.update(enabled=True, endpoint=endpoint, protocol_qualified=True)
                return raw

            settings, raw = self._repository.update_raw(assign)
        except (ConfigurationError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "MOKE Box assignment not saved", str(exc))
            return
        if self._settings_page is not None:
            self._settings_page.accept_external_snapshot(settings, raw)
        self._settings_saved(settings)
        output_status = ("VOUT control permitted." if settings.moke_box.allow_vout_control else
                         "Read-only access. Review VOUT permissions in Settings → MOKE Box.")
        self.dashboard.tcp_discovery_info.setText(f"MOKE Box assigned to {endpoint}. {output_status}")
        if confirmed and self._settings_page is not None:
            show_toast(self._settings_page, output_status, severity="success", title=f"Assigned {endpoint}")
        self._log(f"MOKE BOX ASSIGN SUCCESS: endpoint={endpoint!r}; {output_status}")

    def _set_run_ui_locked(self, locked: bool) -> None:
        self._execution_read_only_locked = bool(locked)
        self.quick_controls_window.setEnabled(not locked)
        if locked:
            self._prepared_execution = None
            self.quick_control_coordinator.cancel_all("Recipe run owns the instruments")
            moke_page = self._realized_device_pages.get("moke_box")
            if moke_page is not None:
                moke_page.stop_live(
                    "Live Hall readout paused while a recipe run owns the MOKE Box."
                )
            lakeshore_page = self._realized_device_pages.get("lakeshore_gaussmeter")
            if lakeshore_page is not None:
                lakeshore_page.stop_live(
                    "Live field readout paused while a recipe run owns the Lake Shore 475."
                )
        self._set_device_pages_execution_read_only(locked)
        if self._recipe_page is not None:
            self._recipe_page.set_execution_controlled(locked)
        # Device routes remain available during a run so the operator can
        # inspect their last verified values, output state and plots. Recipe
        # editing and station-profile changes remain unavailable.
        self._set_route_enabled("settings", not locked)
        # Sweeps is intentionally an inspectable route during execution.  The
        # page itself switches to an explicit read-only state while the
        # navigation item stays discoverable and clickable.
        self._set_route_enabled("sweeps", True)

    def _lock_page_execution_read_only(self, page: QWidget) -> None:
        controls: set[QWidget] = set()
        for widget_type in (
            QAbstractButton,
            QAbstractSpinBox,
            QComboBox,
            QLineEdit,
        ):
            controls.update(page.findChildren(widget_type))
        # Preserve the widget's own disabled flag before page handlers
        # or the lock disable its ancestors. isEnabled() includes
        # inherited state, which cannot be restored with setEnabled().
        enabled_states = {
            control: not control.testAttribute(Qt.WidgetAttribute.WA_ForceDisabled)
            for control in controls
        }
        if isinstance(page, ExecutionTelemetryView):
            page.set_execution_controlled(True)
        for control, enabled in enabled_states.items():
            if not isValid(control) or control.property("executionReadOnlyNavigation") is True:
                continue
            self._run_read_only_controls.setdefault(control, enabled)
            control.setEnabled(False)

    def _set_device_pages_execution_read_only(self, locked: bool) -> None:
        """Keep device pages inspectable while the Run Engine owns I/O.

        The execution worker uses independent sessions. Manual page actions
        must therefore be unavailable during the run, but disabling the entire
        route also hid output/compliance/plot evidence from the operator.
        Preserve each control's pre-run enabled state and disable only widgets
        that can initiate an edit or command.
        """

        realized = getattr(self, "_realized_device_pages", None)
        device_pages = realized if isinstance(realized, dict) else self._device_pages
        if locked:
            if self._run_read_only_controls:
                return
            pages_to_lock = list(device_pages.values())
            char_page = MainWindow._peek_page(self, "keithley_characterization_page")
            if char_page is not None:
                pages_to_lock.append(char_page)
            for page in pages_to_lock:
                MainWindow._lock_page_execution_read_only(self, page)
            return
        for control, enabled in self._run_read_only_controls.items():
            if control is not None and isValid(control):
                control.setEnabled(enabled)
        self._run_read_only_controls.clear()
        for page in device_pages.values():
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
            self._mark_audit_failed(exc)

    def _refresh_audit_health(self) -> None:
        try:
            self._audit.check_health()
        except (OSError, RuntimeError) as exc:
            self._mark_audit_failed(exc)

    def _mark_audit_failed(self, error: Exception) -> None:
        first_failure = self._audit_healthy
        self._audit_healthy = False
        if not first_failure:
            return
        if hasattr(self, "dashboard"):
            self.dashboard.update_audit_health(False)
        if hasattr(self, "_run_controller"):
            self._run_controller.request_stop()
        if hasattr(self, "log"):
            self.log.appendPlainText(
                "CRITICAL: durable audit logging failed; OUTPUT ON and new runs are locked: "
                + str(error)
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
            if critical or severity in {"error", "critical"}:
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
        panel = self.navigationInterface.panel
        # A native resize during geometry restoration can start a collapse.
        # expand(useAni=False) does not stop that animation in QFluent 1.11.2:
        # its next frame can overwrite the new width with the old 48 px target.
        panel.expandAni.stop()
        for item in panel.items.values():
            animation = getattr(item.widget, "expandAni", None)
            if animation is not None:
                animation.stop()
        expanded = (
            self._navigation_expanded_preference
            and self.width() >= self._navigation_expand_threshold
        )
        if expanded:
            self.navigationInterface.expand(useAni=False)
        else:
            # collapse() has no non-animated overload. Complete the native
            # Fluent transition before paint, including its compact item state.
            panel.collapse()
            panel.expandAni.setCurrentTime(panel.expandAni.duration())
        # Fluent recalculates a navigation tree's size hint while its parent
        # panel is laid out. Re-toggling here prevents child rows from keeping
        # their pre-layout geometry and overlapping the group heading. A compact
        # rail uses Fluent's device flyout, never expanded, clipped child rows.
        self.apparatus_navigation_item.setExpanded(False, ani=False)
        if expanded:
            self.apparatus_navigation_item.setExpanded(True, ani=False)
        if application is not None:
            self._apply_navigation_surface(applied_theme.tokens.surface)
        self.hBoxLayout.activate()
        QTimer.singleShot(0, self._capture_apparatus_required_height)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "event_log_panel"):
            self._sync_shell_splitter_layout()
        timer = getattr(self, "_resize_sync_timer", None)
        if timer is not None:
            if not timer.isActive():
                timer.start()
            return
        if hasattr(self, "apparatus_navigation_item"):
            QTimer.singleShot(0, self._sync_apparatus_navigation_height)

    def _sync_apparatus_navigation_height(self) -> None:
        """Prevent QFluent's expanded tree from overlapping sibling routes."""

        if not hasattr(self, "_apparatus_required_height"):
            return
        if self.navigationInterface.panel.isCollapsed():
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

    def nativeEvent(self, event_type, message) -> tuple[bool, int]:
        import sys
        if sys.platform == "win32":
            try:
                import win32con
                from qframelesswindow.windows import LPNCCALCSIZE_PARAMS, MSG, cast
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
            except (ImportError, OSError, AttributeError, ValueError, TypeError):
                return super().nativeEvent(event_type, message)
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event: QCloseEvent) -> None:
        if getattr(self, "_closed", False):
            event.accept()
            return
        # Phase 1: non-destructive confirmation and active-work checks. Any
        # rejected close returns before irreversible shell thread teardown.
        recipe_page = MainWindow._peek_page(self, "recipe_page")
        if recipe_page is not None and not recipe_page.confirm_close():
            event.ignore()
            return
        preparation = getattr(self, "_resume_preparation", None)
        if preparation is not None and not preparation.close():
            self._log("Application close is waiting for recovery archive inspection to finish.")
            event.ignore()
            return
        moke_page = MainWindow._peek_page(self, "moke_box_page", "moke_box")
        if moke_page is not None and not moke_page.field_workflow.prepare_application_shutdown():
            self._navigate_to("moke_box")
            moke_page.views.setCurrentIndex(3)
            event.ignore()
            return
        keithley_page = MainWindow._peek_page(self, "keithley_page", "keithley")
        if (
            keithley_page is not None
            and not keithley_page.characterization_card.prepare_application_shutdown()
        ):
            self._navigate_to("keithley_characterization")
            keithley_page.characterization_card.banner.show_message(
                "Application close is waiting for characterization shutdown, policy restoration or reporting. "
                "Complete any displayed recovery step, then close again.",
                severity="warning",
                timeout_ms=0,
            )
            event.ignore()
            return
        if getattr(self, "_keithley_defaults_in_flight", False):
            self._log("Application close is waiting for Keithley settings persistence to finish.")
            event.ignore()
            return
        self._audit_record(
            "Application safe shutdown started",
            category="application",
            event_type="shutdown_started",
            critical=True,
        )
        self._save_workspace()
        if not self.dashboard.shutdown():
            self._log("Application close is waiting for read-only discovery workers to finish.")
            event.ignore()
            return
        anritsu_page = MainWindow._peek_page(self, "anritsu_page", "anritsu")
        if anritsu_page is not None:
            if getattr(anritsu_page, "_background_assistant", None) is not None:
                anritsu_page._background_assistant.reject()
            correction_ws = getattr(anritsu_page, "_correction_workspace", None)
            if correction_ws is not None and not correction_ws.shutdown():
                self._log(
                    "Application close is waiting for spectrum correction checkpoints and processing to finish."
                )
                event.ignore()
                return
            if not anritsu_page.prepare_manual_archive_shutdown():
                self._log("Application close is waiting for manual spectrum archive I/O to finish.")
                event.ignore()
                return
            if not anritsu_page.shutdown_analysis():
                self._log("Application close is waiting for spectrum analysis threads to finish.")
                event.ignore()
                return
        if recipe_page is not None and not recipe_page.cancel_preflight():
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
        results_page = MainWindow._peek_page(self, "results_page")
        if results_page is not None and not results_page.shutdown():
            self._log("Application close is waiting for result readers to finish.")
            event.ignore()
            return
        elab_page = MainWindow._peek_page(self, "elab_page")
        if elab_page is not None and not elab_page.shutdown():
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
        self.quick_control_coordinator.cancel_all("Application closing")
        if anritsu_page is not None:
            anritsu_page._timer.stop()
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

        # Phase 2: irreversible teardown (all shutdown guards have succeeded).
        self._closed = True
        for timer_attr in (
            "_audit_health_timer",
            "_log_ui_flush_timer",
            "_resize_sync_timer",
            "_execution_preview_timer",
            "_execution_readback_timer",
            "_execution_event_timer",
            "_keithley_defaults_timer",
        ):
            timer = getattr(self, timer_attr, None)
            if timer is not None:
                timer.stop()
        self._pending_keithley_defaults = None
        self._keithley_defaults_thread.quit()
        self._keithley_defaults_thread.wait(5_000)
        self._close_owned_floating_windows()
        for plot in self.findChildren(SpectrumPlotWidget):
            plot.cleanup_plot()
        for managed_widget, _source in list(styleSheetManager.items()):
            if not isValid(managed_widget) or managed_widget is self or self.isAncestorOf(managed_widget):
                styleSheetManager.deregister(managed_widget)
        try:
            self._audit.close()
        except (OSError, RuntimeError):
            self._audit_healthy = False
        try:
            self.inventory_store.close()
        except (OSError, RuntimeError):
            pass
        event.accept()
