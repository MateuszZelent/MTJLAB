"""Main PySide6 application window and manual-control pages."""

from __future__ import annotations

import math
import json
import time
from copy import deepcopy
from dataclasses import astuple, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pyqtgraph as pg

from PySide6.QtCore import QMimeData, QSize, QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QColor, QDrag, QIcon, QKeySequence, QPainter, QPalette, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSpinBox,
    QStyledItemDelegate,
    QTabWidget,
    QStyle,
    QToolBar,
    QToolButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.audit import AuditLogger
from app.devices.anritsu import (
    ANRITSU_PREAMPLIFIER_OPTIONS,
    AdvancedSpectrumConfig,
    AdvancedSpectrumSnapshot,
    AnritsuAdapter,
    AnritsuConfigurationSnapshot,
    ReferenceSpectrum,
    SignalGeneratorConfig,
    SignalGeneratorSnapshot,
    SpectrumConfig,
    SpectrumTrace,
    frequency_option_for,
)
from app.devices.discovery import DiscoveredInstrument
from app.devices.keithley import (
    KeithleyAdapter,
    KeithleyRampRequest,
    KeithleySourceRequest,
    build_keithley_ramp_levels,
)
from app.devices.rigol import (
    RigolAdapter,
    RigolBurstConfig,
    RigolChannelConfig,
    RigolFrequencySweepConfig,
    RigolModulationConfig,
    RigolOutputConfig,
)
from app.domain.errors import AuthorizationError, ConfigurationError
from app.domain.readiness import ReadinessLevel, StationReadiness, evaluate_station_readiness
from app.devices.simulators import SimulatedVisaFactory, simulated_station_settings
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_RESISTANCE,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    format_quantity_auto,
    parse_quantity,
)
from app.engine.compiler import ExecutionPlan, RecipeCompiler
from app.engine.estimation import PlanEstimate, PlanEstimator
from app.engine.recovery import RunRecoveryManager
from app.recipes import (
    RecipeNode,
    RecipeRepository,
    add_recipe_node,
    delete_recipe_node,
    estimate_sweep_point_count,
    generate_sweep_stage_points,
    generate_sweep_points,
    move_recipe_node,
    parse_recipe_text,
    replace_recipe_node,
)
from app.recipes.parameter_registry import (
    SWEEP_DIMENSIONS,
    legacy_ui_parameter_definitions,
)
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from app.security import AccessPolicy, Permission
from app.safety.rigol_current import validate_rigol_frequency_sweep, validate_rigol_waveform
from app.safety.anritsu import ANRITSU_SWEEP_POINT_COUNTS
from app.safety.keithley import validate_keithley_source
from app.storage import Hdf5RunReader, ReferenceHdf5Store, RunDetail, StoredPoint
from app.spectrum import (
    LinearPowerAverager,
    apply_reference_operation,
    frequency_grids_match,
)
from app.ui.settings_page import SettingsPage
from app.ui.run_worker import RunController, serialize_settings_snapshot
from app.ui.workers import DeviceController, RecipePreflightWorker
from app.ui.discovery_worker import VisaDiscoveryWorker
from app.ui.design_system import effective_theme
from app.ui.widgets import NotificationBanner, SpectrumPlotWidget


def _line(value: str, width: int = 14) -> QLineEdit:
    edit = QLineEdit(value)
    edit.setMinimumWidth(width * 8)
    return edit


def _human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, remaining = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)} min {remaining:.0f} s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)} h {int(minutes)} min"


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


class LimitField(QWidget):
    """A value editor with an always-visible approved MIN/MAX range."""

    edit_requested = Signal()

    def __init__(self, editor: QWidget, minimum: object = None, maximum: object = None) -> None:
        super().__init__()
        self.editor = editor
        self._minimum_value = minimum
        self._maximum_value = maximum
        self._last_valid = editor.text() if isinstance(editor, QLineEdit) else None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(editor, 1)
        self.minimum = QLabel()
        self.maximum = QLabel()
        for label in (self.minimum, self.maximum):
            label.setObjectName("limitBadge")
            label.setMinimumWidth(88)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.minimum)
        row.addWidget(self.maximum)
        self.edit_button = QPushButton("Edit…")
        self.edit_button.setObjectName("limitEditButton")
        self.edit_button.setToolTip(
            "Edit this safety range in a popup window. Saving revokes profile approval."
        )
        self.edit_button.clicked.connect(self.edit_requested)
        row.addWidget(self.edit_button)
        layout.addLayout(row)
        self.validation_warning = QLabel()
        self.validation_warning.setObjectName("inlineValidationWarning")
        self.validation_warning.setStyleSheet("color: #d84343; font-weight: 600;")
        self.validation_warning.setWordWrap(True)
        self.validation_warning.hide()
        layout.addWidget(self.validation_warning)
        self.set_limits(minimum, maximum)
        editing_finished = getattr(editor, "editingFinished", None)
        if editing_finished is not None:
            editing_finished.connect(self.validate_and_clamp)

    def set_limits(self, minimum: object, maximum: object) -> None:
        self._minimum_value = minimum
        self._maximum_value = maximum
        minimum_text = "NOT SET" if minimum is None else str(minimum)
        maximum_text = "NOT SET" if maximum is None else str(maximum)
        self.minimum.setText(f"MIN  {minimum_text}")
        self.maximum.setText(f"MAX  {maximum_text}")
        incomplete = minimum is None or maximum is None
        state = "undefined" if incomplete else "defined"
        self.setProperty("limitState", state)
        for label in (self.minimum, self.maximum):
            label.setProperty("limitState", state)
            label.style().unpolish(label)
            label.style().polish(label)
        self.setToolTip(
            "Approved laboratory range. The operation is rejected before SCPI is sent "
            "when a value or sweep endpoint is outside this range."
        )

    def _show_validation_warning(self, message: str) -> None:
        self.validation_warning.setText(f"Warning: {message}")
        self.validation_warning.show()
        self.editor.setStyleSheet("border: 2px solid #d84343;")

    def _clear_validation_warning(self) -> None:
        self.validation_warning.clear()
        self.validation_warning.hide()
        self.editor.setStyleSheet("")

    def _quantity_values(self) -> tuple[float, float | None, float | None, str] | None:
        if not isinstance(self.editor, QLineEdit):
            return None
        boundaries = [value for value in (self._minimum_value, self._maximum_value) if value is not None]
        if not boundaries or any(not isinstance(value, str) for value in boundaries):
            return None
        dimensions = (
            DIMENSION_VOLTAGE,
            DIMENSION_CURRENT,
            DIMENSION_FREQUENCY,
            DIMENSION_RESISTANCE,
            DIMENSION_TIME,
            DIMENSION_DBM,
        )
        for dimension in dimensions:
            try:
                parsed_bounds = [parse_quantity(value, dimension).si_value for value in boundaries]
                current = parse_quantity(self.editor.text(), dimension, require_unit=False).si_value
            except Exception:
                continue
            minimum = parsed_bounds[0] if self._minimum_value is not None else None
            maximum = parsed_bounds[-1] if self._maximum_value is not None else None
            return current, minimum, maximum, dimension
        return None

    def validate_and_clamp(self) -> bool:
        """Clamp a field on focus loss, while final safety validation remains authoritative."""

        if isinstance(self.editor, QSpinBox):
            minimum = self._minimum_value if isinstance(self._minimum_value, int) else None
            maximum = self._maximum_value if isinstance(self._maximum_value, int) else None
            value = self.editor.value()
        else:
            textual_bounds = [
                str(value).lower()
                for value in (self._minimum_value, self._maximum_value)
                if value is not None
            ]
            if any(value.startswith(">") or "n/a" in value or "no profile" in value for value in textual_bounds):
                return True
            parsed = self._quantity_values()
            if parsed is None:
                if isinstance(self.editor, QLineEdit) and self._last_valid is not None:
                    current = self.editor.text().strip()
                    if current and current.upper() != "AUTO":
                        self.editor.setText(self._last_valid)
                        self._show_validation_warning(
                            f"Invalid value or unit. Restored the previous value: {self._last_valid}."
                        )
                        return False
                return True
            value, minimum, maximum, dimension = parsed

        if minimum is not None and value < minimum:
            replacement = str(self._minimum_value)
            self._set_editor_value(self._minimum_value)
            self._show_validation_warning(f"Value was below MIN and has been changed to {replacement}.")
            return False
        if maximum is not None and value > maximum:
            replacement = str(self._maximum_value)
            self._set_editor_value(self._maximum_value)
            self._show_validation_warning(f"Value exceeded MAX and has been changed to {replacement}.")
            return False
        if isinstance(self.editor, QLineEdit):
            normalized = format_quantity_auto(value, dimension)
            self.editor.setText(normalized)
            self._last_valid = normalized
        self._clear_validation_warning()
        return True

    def _set_editor_value(self, value: object) -> None:
        if isinstance(self.editor, QSpinBox):
            self.editor.setValue(int(value))
        elif isinstance(self.editor, QLineEdit):
            self.editor.setText(str(value))
            self._last_valid = str(value)


class LimitEditDialog(QDialog):
    """Small focused editor for one safety range."""

    def __init__(
        self,
        title: str,
        minimum: object,
        maximum: object,
        *,
        maximum_enabled: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit limits — {title}")
        self.setModal(True)
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        note = QLabel(
            "Enter explicit units where applicable (for example: 10 mA, 67 mV, 1 MHz). "
            "The complete configuration is validated before it is saved."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.minimum = QLineEdit("" if minimum is None else str(minimum))
        self.maximum = QLineEdit("" if maximum is None else str(maximum))
        self.maximum.setEnabled(maximum_enabled)
        if not maximum_enabled:
            self.maximum.setPlaceholderText("Not applicable")
        form.addRow("Minimum", self.minimum)
        form.addRow("Maximum", self.maximum)
        layout.addLayout(form)
        warning = QLabel("Saving a safety limit change sets the safety profile to UNVERIFIED.")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


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
        controls = QHBoxLayout()
        self.connect_button = QPushButton("Connect")
        self.disconnect_button = QPushButton("Disconnect")
        self.test_button = QPushButton("Test")
        self.test_button.setToolTip(
            "Open a temporary session, validate IDN/model/serial and protocol probes, force safe OFF, then disconnect."
        )
        controls.addWidget(self.connect_button)
        controls.addWidget(self.disconnect_button)
        controls.addWidget(self.test_button)
        layout.addWidget(name)
        layout.addWidget(self.state)
        layout.addWidget(self.resource)
        layout.addWidget(self.identity)
        layout.addStretch(1)
        layout.addLayout(assignment_row)
        layout.addWidget(self.assignment_hint)
        layout.addLayout(controls)
        self.connect_button.clicked.connect(self.connect_requested)
        self.disconnect_button.clicked.connect(self.disconnect_requested)
        self.test_button.clicked.connect(self.test_requested)
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
        self.connect_button.setEnabled(not active)
        self.disconnect_button.setEnabled(not active)
        self.test_button.setEnabled(not active)
        if active:
            self.state.setText("APPLYING NEW VISA ADDRESS…")

    def set_testing(self, active: bool) -> None:
        self.connect_button.setEnabled(not active)
        self.disconnect_button.setEnabled(not active)
        self.test_button.setEnabled(not active)
        self.test_button.setText("Testing…" if active else "Test")
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
            self.connect_button.setEnabled(True)
            self.test_button.setEnabled(True)
        elif assigned_index >= 0:
            self.detected_resources.setCurrentIndex(assigned_index)
            self.detected_resources.setEnabled(False)
            self.assign_button.setEnabled(False)
            self.assign_button.setText("Assigned ✓")
            self.assignment_hint.hide()
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
        self.connect_button.setEnabled(False)
        self.test_button.setEnabled(False)
        self.connect_button.setToolTip("Assign the selected VISA resource first; this prevents using the old address.")
        self.test_button.setToolTip("Assign the selected VISA resource first; Test never uses an unconfirmed selection.")


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
        self.save_assignments.clicked.connect(self._emit_assignments)
        if not discovery_enabled:
            self.scan_button.setEnabled(False)
            self.scan_button.setToolTip("VISA discovery is disabled in simulation mode.")
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


@dataclass(frozen=True, slots=True)
class RigolConfigurationSnapshot:
    """Qt-independent carrier configuration shared by manual and plan editors."""

    channel: int = 1
    waveform: str = "SIN"
    frequency: str = "1 kHz"
    high_level: str = "1 mV"
    low_level: str = "-1 mV"
    output_load: str = "HIGHZ"
    phase_deg: str = "0"
    square_duty_percent: str = "50"
    ramp_symmetry_percent: str = "50"
    pulse_width: str = "100 us"
    pulse_leading: str = "10 ns"
    pulse_trailing: str = "10 ns"
    dut_min_impedance: str = "50 ohm"
    output_polarity: str = "NORM"
    output_mode: str = "NORM"
    gate_polarity: str = "NORM"
    sync_enabled: bool = False
    sync_polarity: str = "NORM"
    sync_delay: str = "0 s"


class RigolPage(QWidget):
    status = Signal(str)

    def __init__(self, controller: DeviceController, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._station_settings = settings
        self._limit_fields: dict[QWidget, LimitField] = {}
        self._pending_output_enable = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        header = QFrame()
        header.setObjectName("rigolHero")
        header_layout = QHBoxLayout(header)
        heading = QVBoxLayout()
        title = QLabel("Rigol DG1032Z")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Function generator · channel control and safe output activation")
        subtitle.setObjectName("muted")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header_layout.addLayout(heading, 1)

        self.device_led = QLabel("●")
        self.device_led.setObjectName("rigolLed")
        self.device_state = QLabel("DISCONNECTED")
        self.device_state.setObjectName("rigolState")
        state_box = QVBoxLayout()
        state_line = QHBoxLayout()
        state_line.addWidget(self.device_led)
        state_line.addWidget(self.device_state)
        state_box.addLayout(state_line)
        self.capability_badge = QLabel("Capabilities: awaiting identification")
        self.capability_badge.setObjectName("rigolBadge")
        state_box.addWidget(self.capability_badge)
        header_layout.addLayout(state_box)
        layout.addWidget(header)
        self.banner = NotificationBanner()
        layout.addWidget(self.banner)

        self.channel = QComboBox()
        self.channel.addItems(["1", "2"])
        self.waveform = QComboBox()
        self.waveform.addItems(["SIN", "SQU", "RAMP", "PULS", "NOIS", "DC"])
        self.waveform.setCurrentText("SIN")
        self.time_mode = QComboBox()
        self.time_mode.addItems(["Frequency", "Period"])
        self.frequency = _line("1 kHz")
        self.period = _line("1 ms")
        self.level_mode = QComboBox()
        self.level_mode.addItems(["HighL / LowL", "Amplitude / Offset"])
        self.level_mode.setCurrentText("Amplitude / Offset")
        self.high_level = _line("1 mV")
        self.low_level = _line("-1 mV")
        self.vpp = _line("2 mV")
        self.offset = _line("0 V")
        self.load = _line("HIGHZ")
        self.output_polarity = QComboBox()
        self.output_polarity.addItems(["NORM", "INV"])
        self.output_mode = QComboBox()
        self.output_mode.addItems(["NORM", "GAT"])
        self.gate_polarity = QComboBox()
        self.gate_polarity.addItems(["NORM", "INV"])
        self.sync_enabled = QCheckBox("SYNC enabled")
        self.sync_polarity = QComboBox()
        self.sync_polarity.addItems(["NORM", "INV"])
        self.sync_delay = _line("0 s")
        self.dut_impedance = _line("50 ohm")
        self.phase = _line("0")
        self.duty = _line("50")
        self.ramp_symmetry = _line("50")
        self.pulse_width = _line("100 us")
        self.pulse_leading = _line("10 ns")
        self.pulse_trailing = _line("10 ns")
        self._level_syncing = False
        self._time_syncing = False

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.control_tabs = QTabWidget()
        self.control_tabs.setObjectName("rigolControlTabs")

        configure = QPushButton("Validate and apply waveform")
        configure.setObjectName("primaryButton")
        self.basic_scroll = self._form_page(
            "Basic parameters",
            "For a standard sine wave, change only Frequency and Amplitude. Other fields already contain safe defaults.",
            (
                ("Channel", self.channel),
                ("Waveform", self.waveform),
                ("Time representation", self.time_mode),
                ("Frequency", self._bounded(self.frequency, "frequency")),
                ("Period", self.period),
                ("Level representation", self.level_mode),
                ("HighL", self._bounded(self.high_level, "high_level")),
                ("LowL", self._bounded(self.low_level, "low_level")),
                ("Amplitude (Vpp)", self._bounded(self.vpp, "amplitude_vpp")),
                ("Offset / DC level", self._bounded(self.offset, "offset")),
                ("Minimum DUT impedance", self._bounded(self.dut_impedance, "declared_dut_impedance")),
                ("Phase [deg]", self.phase),
            ),
            (configure,),
        )
        self.basic_form = self.basic_scroll.widget().findChild(QFormLayout)
        shape_apply = QPushButton("Apply shape parameters")
        shape_apply.setObjectName("primaryButton")
        self.shape_scroll = self._form_page(
            "Waveform shape",
            "Only parameters applicable to the selected waveform are shown.",
            (
                ("Duty [%] · SQU", self.duty),
                ("Symmetry [%] · RAMP", self.ramp_symmetry),
                ("Pulse width · PULS", self.pulse_width),
                ("Pulse leading edge · PULS", self.pulse_leading),
                ("Pulse trailing edge · PULS", self.pulse_trailing),
            ),
            (shape_apply,),
        )
        self.shape_form = self.shape_scroll.widget().findChild(QFormLayout)

        configure_output = QPushButton("Apply output path")
        configure_output.setObjectName("primaryButton")
        self.sync_phases_button = QPushButton("Synchronize CH1/CH2 phases")
        self.sync_phases_button.setEnabled(False)
        self.output_on = QPushButton("OUTPUT ON")
        self.output_on.setObjectName("outputOnButton")
        self.output_off = QPushButton("OUTPUT OFF")
        self.output_off.setObjectName("outputOffButton")
        self.output_scroll = self._form_page(
            "Output path and SYNC",
            "OUTPUT ON validates the visible channel settings, performs an internal "
            "short-lived safety arm and enables the selected output.",
            (
                ("Generator load setting", self.load),
                ("Output polarity", self.output_polarity),
                ("Output mode", self.output_mode),
                ("Gate polarity", self.gate_polarity),
                ("", self.sync_enabled),
                ("SYNC polarity", self.sync_polarity),
                ("SYNC delay", self.sync_delay),
            ),
            (
                configure_output,
                self.sync_phases_button,
                self.output_on,
                self.output_off,
            ),
        )

        self.advanced = QTabWidget()
        self.advanced.setObjectName("rigolAdvancedTabs")
        self.advanced.addTab(self._modulation_tab(), "Modulation")
        self.advanced.addTab(self._sweep_tab(), "Sweep")
        self.advanced.addTab(self._burst_tab(), "Burst")

        self.control_tabs.addTab(self.basic_scroll, "Basic")
        self.control_tabs.addTab(self.shape_scroll, "Shape")
        self.control_tabs.addTab(self.output_scroll, "Output")
        self.control_tabs.addTab(self.advanced, "Advanced")
        splitter.addWidget(self.control_tabs)

        insight = QWidget()
        insight_layout = QVBoxLayout(insight)
        insight_layout.setContentsMargins(10, 0, 0, 0)
        preview_title = QLabel("Waveform preview")
        preview_title.setObjectName("sectionTitle")
        insight_layout.addWidget(preview_title)
        self.preview_plot = SpectrumPlotWidget(legend=False)
        self.preview_plot.set_labels(x="Normalized period", x_unit="", y="Voltage", y_unit="V")
        self.preview_plot.setMinimumHeight(260)
        insight_layout.addWidget(self.preview_plot, 1)

        safety = QFrame()
        safety.setObjectName("rigolSafetyCard")
        safety_layout = QVBoxLayout(safety)
        safety_title = QLabel("Load safety")
        safety_title.setObjectName("sectionTitle")
        safety_layout.addWidget(safety_title)
        self.estimate = QLabel("Estimated current: —")
        self.estimate.setObjectName("muted")
        self.estimate.setWordWrap(True)
        safety_layout.addWidget(self.estimate)
        warning = QLabel("⚠ This estimate is not a measurement. Verify DUT impedance and profile limits before ARM.")
        warning.setObjectName("rigolWarning")
        warning.setWordWrap(True)
        safety_layout.addWidget(warning)
        insight_layout.addWidget(safety)
        splitter.addWidget(insight)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([620, 500])
        layout.addWidget(splitter, 1)

        configure.clicked.connect(self.configure)
        shape_apply.clicked.connect(self.configure)
        configure_output.clicked.connect(self.configure_output)
        self.sync_phases_button.clicked.connect(lambda: self._controller.call("synchronize_phases"))
        self.high_level.editingFinished.connect(self._sync_vpp_offset_from_levels)
        self.low_level.editingFinished.connect(self._sync_vpp_offset_from_levels)
        self.vpp.editingFinished.connect(self._sync_levels_from_vpp_offset)
        self.offset.editingFinished.connect(self._sync_levels_from_vpp_offset)
        self.frequency.editingFinished.connect(self._sync_period_from_frequency)
        self.period.editingFinished.connect(self._sync_frequency_from_period)
        self.output_on.clicked.connect(lambda: self.request_output(True))
        self.output_off.clicked.connect(lambda: self.request_output(False))
        controller.result.connect(self._result)
        controller.error.connect(self._error)
        controller.state_changed.connect(self._device_state_changed)
        self.waveform.currentTextChanged.connect(self._update_dynamic_controls)
        self.level_mode.currentTextChanged.connect(self._update_dynamic_controls)
        self.time_mode.currentTextChanged.connect(self._update_dynamic_controls)
        self.waveform.currentTextChanged.connect(self._update_preview)
        self.channel.currentTextChanged.connect(self._update_preview)
        self.channel.currentTextChanged.connect(self._refresh_rigol_limits)
        for field in (self.frequency, self.period, self.high_level, self.low_level, self.vpp, self.offset, self.duty, self.ramp_symmetry, self.pulse_width):
            field.textChanged.connect(self._update_preview)
        self._sync_vpp_offset_from_levels()
        self._sync_period_from_frequency()
        self._update_dynamic_controls()
        self._update_preview()
        self._install_rigol_help(
            configure=configure,
            shape_apply=shape_apply,
            configure_output=configure_output,
            output_on=self.output_on,
            output_off=self.output_off,
        )

    @staticmethod
    def _set_help(widget: QWidget, title: str, text: str) -> None:
        help_text = f"<b>{title}</b><br>{text}"
        widget.setToolTip(help_text)
        widget.setToolTipDuration(20_000)
        widget.setWhatsThis(help_text)
        widget.setAccessibleDescription(f"{title}. {text}")

    def _install_rigol_help(
        self,
        *,
        configure: QPushButton,
        shape_apply: QPushButton,
        configure_output: QPushButton,
        output_on: QPushButton,
        output_off: QPushButton,
    ) -> None:
        help_items = {
            self.channel: ("Channel", "Selects physical output CH1 or CH2. Each channel has independent settings and safety limits."),
            self.waveform: ("Waveform", "SIN is sine, SQU square, RAMP triangular/ramp, PULS pulse, NOIS noise and DC a constant voltage."),
            self.time_mode: ("Time representation", "Choose whether the same repetition rate is entered as frequency or period. The application converts one into the other."),
            self.frequency: ("Frequency", "Number of waveform cycles per second. For a standard sine wave this and Amplitude are normally the only values you change."),
            self.period: ("Period", "Duration of one complete waveform cycle. Period equals 1/frequency."),
            self.level_mode: ("Level representation", "HighL/LowL defines the upper and lower levels directly. Amplitude/Offset defines Vpp and the vertical center; both describe the same signal."),
            self.high_level: ("HighL", "Highest programmed waveform voltage. This is a generator setting/read-back, not a measured DUT voltage."),
            self.low_level: ("LowL", "Lowest programmed waveform voltage. Vpp = HighL − LowL."),
            self.vpp: ("Amplitude (Vpp)", "Peak-to-peak voltage: the difference between maximum and minimum level. A 2 mVpp sine at 0 V offset spans −1 mV to +1 mV."),
            self.offset: ("Offset / DC level", "Moves the waveform vertically around zero. In DC mode this is the constant programmed output voltage."),
            self.dut_impedance: ("Minimum DUT impedance", "Safety declaration used to estimate worst-case current. The Rigol does not measure DUT impedance; enter the lowest credible load impedance."),
            self.phase: ("Phase", "Starting angular position of the waveform in degrees. The safe default is 0°. It matters mainly when comparing or synchronizing channels."),
            self.duty: ("Square duty cycle", "Percentage of each period for which a square wave remains at HighL. 50% gives equal high and low durations."),
            self.ramp_symmetry: ("Ramp symmetry", "Percentage of the period spent on the rising part of a ramp. 50% produces a symmetric triangle."),
            self.pulse_width: ("Pulse width", "Time for which a pulse remains at its active/high level. It must fit within the selected period."),
            self.pulse_leading: ("Leading-edge time", "Programmed transition time from LowL to HighL for a pulse."),
            self.pulse_trailing: ("Trailing-edge time", "Programmed transition time from HighL to LowL for a pulse."),
            self.load: ("Load setting", "Expected external load used by the generator to calculate displayed voltage. HIGHZ means a high-impedance load. This does not measure the real DUT impedance."),
            self.output_polarity: ("Output polarity", "NORM preserves the waveform; INV inverts it around the configured offset."),
            self.output_mode: ("Output mode", "NORM continuously follows the selected waveform. GAT makes output behavior depend on an external gate signal."),
            self.gate_polarity: ("Gate polarity", "Selects which external gate level is considered active when gated output mode is used."),
            self.sync_enabled: ("SYNC output", "Enables the rear-panel synchronization signal associated with this channel. It is a timing reference, not the analog waveform output."),
            self.sync_polarity: ("SYNC polarity", "Selects normal or inverted polarity for the SYNC timing signal."),
            self.sync_delay: ("SYNC delay", "Time shift applied between the waveform timing and its SYNC output."),
            self.mod_enabled: ("Modulation", "Modulation varies a carrier parameter using another signal. Leave disabled for an ordinary sine, square, ramp or pulse."),
            self.mod_type: ("Modulation type", "AM varies amplitude, FM frequency, PM phase; ASK/FSK/PSK switch between discrete states; PWM varies pulse width."),
            self.mod_source: ("Modulation source", "INT uses the generator's internal modulating waveform. EXT uses a signal connected to the rear Mod/Trig connector."),
            self.mod_rate: ("Modulation rate", "Repetition frequency of the internal modulating signal or digital state changes."),
            self.mod_parameter: ("Modulation parameter", "Type-dependent amount, such as AM depth, FM deviation or PM deviation. Its meaning changes with Modulation type."),
            self.mod_shape: ("Internal modulation shape", "Waveform used internally to vary the carrier when Source is INT."),
            self.mod_polarity: ("Modulation polarity", "Defines the logical polarity for supported digital modulation types such as ASK/FSK/PSK."),
            self.sweep_enabled: ("Frequency sweep", "Automatically changes frequency from Start to Stop. Leave disabled for a fixed-frequency signal."),
            self.sweep_start: ("Sweep start", "Frequency at the beginning of the sweep."),
            self.sweep_stop: ("Sweep stop", "Frequency at the end of the sweep. It may be above or below Start."),
            self.sweep_duration: ("Sweep time", "Time used to traverse from the start frequency to the stop frequency."),
            self.sweep_start_hold: ("Start hold", "Time spent at the start frequency before the sweep begins."),
            self.sweep_stop_hold: ("Stop hold", "Time spent at the stop frequency after the sweep reaches it."),
            self.sweep_return_time: ("Return time", "Time used to return from Stop to Start before the next sweep cycle."),
            self.sweep_spacing: ("Sweep spacing", "LIN changes frequency linearly, LOG logarithmically, and STEP advances through discrete frequency points."),
            self.sweep_steps: ("Sweep steps", "Number of discrete points used by STEP sweep mode."),
            self.sweep_trigger: ("Sweep trigger source", "INT starts sweeps internally, EXT waits for the rear trigger input, and MAN waits for the Trigger sweep button."),
            self.sweep_trigger_slope: ("Trigger slope", "Selects rising/positive or falling/negative edge of an external trigger."),
            self.sweep_trigger_out: ("Trigger output", "Emits a timing signal so another instrument can synchronize with the sweep."),
            self.burst_enabled: ("Burst", "Outputs a limited group of cycles after a trigger, or follows an external gate. Leave disabled for continuous output."),
            self.burst_mode: ("Burst mode", "TRIG outputs the configured number of cycles after a trigger. GAT outputs while the external gate has the active level."),
            self.burst_cycles: ("Burst cycles", "Number of complete carrier cycles emitted for each trigger in TRIG mode."),
            self.burst_phase: ("Burst start phase", "Carrier phase at which each triggered burst begins."),
            self.burst_period: ("Burst period", "Interval between internally triggered bursts. It is not the carrier waveform period."),
            self.burst_delay: ("Burst delay", "Delay from the accepted trigger to the start of the burst."),
            self.burst_trigger: ("Burst trigger source", "INT generates triggers internally, EXT uses the rear input, and MAN uses the Trigger burst button."),
            self.burst_trigger_slope: ("Burst trigger slope", "Selects the active edge of the external burst trigger."),
            self.burst_trigger_out: ("Burst trigger output", "Provides a trigger timing signal for synchronizing other instruments."),
            self.burst_gate_polarity: ("Burst gate polarity", "Selects which level at the external gate input allows waveform output in GAT mode."),
            self.burst_idle: ("Burst idle level", "Determines the output level between bursts: first point, top, center or bottom of the waveform."),
            self.sync_phases_button: ("Synchronize phases", "Aligns the phase reference of CH1 and CH2. It does not enable either output."),
            configure: ("Apply waveform safely", "Validates limits, forces the selected output OFF, writes only parameters relevant to the selected waveform and verifies read-back."),
            shape_apply: ("Apply shape", "Applies the waveform together with its duty, symmetry or pulse-edge parameters while OUTPUT remains OFF."),
            configure_output: ("Apply output path", "Configures load, polarity, gate and SYNC settings while OUTPUT remains OFF."),
            output_on: (
                "OUTPUT ON",
                "Validates and applies the visible channel settings, performs an "
                "internal short-lived safety arm and energizes the physical BNC output.",
            ),
            output_off: ("OUTPUT OFF", "Immediately requests the selected physical output to be disabled. No ARM is required."),
        }
        for widget, (title, description) in help_items.items():
            self._set_help(widget, title, description)

        tab_help = {
            0: "Basic waveform selection, frequency/period and voltage levels. Start here for ordinary signals.",
            1: "Parameters specific to square, ramp and pulse shapes. This tab appears only when relevant.",
            2: "Physical output path, load model, polarity, SYNC and protected OUTPUT controls.",
            3: "Optional modulation, frequency sweep and burst functions. Leave these disabled for normal continuous output.",
        }
        for index, description in tab_help.items():
            self.control_tabs.setTabToolTip(index, description)
        self.advanced.setTabToolTip(0, "Vary carrier amplitude, frequency, phase or digital state with an internal or external signal.")
        self.advanced.setTabToolTip(1, "Automatically move carrier frequency between Start and Stop.")
        self.advanced.setTabToolTip(2, "Generate finite cycle groups or externally gated waveform segments.")

    @staticmethod
    def _form_page(
        title: str,
        description: str,
        rows: tuple[tuple[str, QWidget], ...],
        actions: tuple[QPushButton, ...],
    ) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(18, 18, 18, 24)
        content_layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        content_layout.addWidget(heading)
        help_text = QLabel(description)
        help_text.setObjectName("muted")
        help_text.setWordWrap(True)
        content_layout.addWidget(help_text)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setVerticalSpacing(12)
        for label, widget in rows:
            form.addRow(label, widget)
        content_layout.addLayout(form)
        action_grid = QGridLayout()
        for index, button in enumerate(actions):
            action_grid.addWidget(button, index // 2, index % 2)
        content_layout.addLayout(action_grid)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        return scroll

    def _rigol_limit_values(self, key: str) -> tuple[object, object]:
        limits = self._station_settings.rigol.safety.channels[self.channel.currentText()].lab_limits
        value = getattr(limits, key)
        if key == "declared_dut_impedance":
            return value.min, "no profile maximum"
        return value.min, value.max

    def _bounded(self, editor: QWidget, limit_key: str) -> LimitField:
        minimum, maximum = self._rigol_limit_values(limit_key)
        field = LimitField(editor, minimum, maximum)
        field.setProperty("limitKey", limit_key)
        self._limit_fields[editor] = field
        return field

    def _row_widget(self, editor: QWidget) -> QWidget:
        return self._limit_fields.get(editor, editor)

    def _refresh_rigol_limits(self, *_args: object) -> None:
        for field in self._limit_fields.values():
            key = str(field.property("limitKey"))
            field.set_limits(*self._rigol_limit_values(key))

    def set_settings(self, settings: StationSettings) -> None:
        self._station_settings = settings
        self._refresh_rigol_limits()

    def configuration_snapshot(self) -> RigolConfigurationSnapshot:
        """Return the visible carrier state without communicating with hardware."""

        high, low = self._effective_levels()
        return RigolConfigurationSnapshot(
            channel=int(self.channel.currentText()),
            waveform=self.waveform.currentText(),
            frequency=self.frequency.text().strip(),
            high_level=self._format_voltage(high),
            low_level=self._format_voltage(low),
            output_load=self.load.text().strip(),
            phase_deg=self.phase.text().strip(),
            square_duty_percent=self.duty.text().strip(),
            ramp_symmetry_percent=self.ramp_symmetry.text().strip(),
            pulse_width=self.pulse_width.text().strip(),
            pulse_leading=self.pulse_leading.text().strip(),
            pulse_trailing=self.pulse_trailing.text().strip(),
            dut_min_impedance=self.dut_impedance.text().strip(),
            output_polarity=self.output_polarity.currentText(),
            output_mode=self.output_mode.currentText(),
            gate_polarity=self.gate_polarity.currentText(),
            sync_enabled=self.sync_enabled.isChecked(),
            sync_polarity=self.sync_polarity.currentText(),
            sync_delay=self.sync_delay.text().strip(),
        )

    @staticmethod
    def _format_voltage(value_v: float) -> str:
        if 0 < abs(value_v) < 1:
            return f"{value_v * 1e3:.12g} mV"
        return f"{value_v:.12g} V"

    def set_capabilities(self, capabilities: object) -> None:
        supports = getattr(capabilities, "supports", lambda _feature: False)
        features = (
            ("modulation", "MOD"),
            ("frequency_sweep", "SWEEP"),
            ("burst", "BURST"),
            ("phase_sync", "PHASE SYNC"),
        )
        supported = [label for feature, label in features if supports(feature)]
        # Advanced pages remain openable before a capability probe.  Actions
        # themselves still verify capabilities before talking to the device.
        for index in range(self.advanced.count()):
            self.advanced.setTabEnabled(index, True)
        self.sync_phases_button.setEnabled(bool(supports("phase_sync")))
        self.capability_badge.setText("Capabilities: " + (" · ".join(supported) if supported else "no extensions"))

    def _device_state_changed(self, state: str) -> None:
        normalized = str(state).strip().lower()
        colors = {
            "verified": "#38d996",
            "output_off": "#38d996",
            "output_on": "#ffcc66",
            "connecting": "#66b3ff",
            "fault": "#ff657a",
            "unknown": "#ff657a",
            "disconnected": "#91a0b2",
        }
        self.device_state.setText(normalized.replace("_", " ").upper())
        self.device_led.setStyleSheet(f"color: {colors.get(normalized, '#91a0b2')};")

    def _update_dynamic_controls(self, *_args: object) -> None:
        waveform = self.waveform.currentText()
        is_dc = waveform == "DC"
        has_time = waveform not in {"DC", "NOIS"}
        high_low_mode = self.level_mode.currentText() == "HighL / LowL"

        visibility = {
            self.time_mode: has_time,
            self.frequency: has_time and self.time_mode.currentText() == "Frequency",
            self.period: has_time and self.time_mode.currentText() == "Period",
            self.level_mode: not is_dc,
            self.high_level: not is_dc and high_low_mode,
            self.low_level: not is_dc and high_low_mode,
            self.vpp: not is_dc and not high_low_mode,
            self.offset: is_dc or not high_low_mode,
            self.phase: waveform not in {"DC", "NOIS"},
        }
        for widget, visible in visibility.items():
            self.basic_form.setRowVisible(self._row_widget(widget), visible)

        shape_visibility = {
            self.duty: waveform == "SQU",
            self.ramp_symmetry: waveform == "RAMP",
            self.pulse_width: waveform == "PULS",
            self.pulse_leading: waveform == "PULS",
            self.pulse_trailing: waveform == "PULS",
        }
        for widget, visible in shape_visibility.items():
            self.shape_form.setRowVisible(widget, visible)
        self.control_tabs.setTabVisible(1, any(shape_visibility.values()))
        self.control_tabs.setTabVisible(3, not is_dc)
        if is_dc and self.control_tabs.currentIndex() in {1, 3}:
            self.control_tabs.setCurrentIndex(0)
        self._update_preview()

    def _effective_levels(self) -> tuple[float, float]:
        if self.waveform.currentText() == "DC":
            value = parse_quantity(self.offset.text(), DIMENSION_VOLTAGE).si_value
            return value, value
        if self.level_mode.currentText() == "Amplitude / Offset":
            vpp = parse_quantity(self.vpp.text(), DIMENSION_VOLTAGE).si_value
            offset = parse_quantity(self.offset.text(), DIMENSION_VOLTAGE).si_value
            return offset + vpp / 2, offset - vpp / 2
        return (
            parse_quantity(self.high_level.text(), DIMENSION_VOLTAGE).si_value,
            parse_quantity(self.low_level.text(), DIMENSION_VOLTAGE).si_value,
        )

    def _update_preview(self, *_args: object) -> None:
        try:
            high, low = self._effective_levels()
        except Exception:
            high, low = 1e-3, -1e-3
        if high < low:
            high, low = low, high
        amplitude = (high - low) / 2
        center = (high + low) / 2
        waveform = self.waveform.currentText()
        duty = self._bounded_number(self.duty.text(), 50.0, 0.01, 99.99) / 100
        symmetry = self._bounded_number(self.ramp_symmetry.text(), 50.0, 0.01, 99.99) / 100
        try:
            frequency = parse_quantity(self.frequency.text(), DIMENSION_FREQUENCY).si_value
            width = parse_quantity(self.pulse_width.text(), DIMENSION_TIME).si_value
            pulse_duty = min(max(frequency * width, 0.001), 0.999)
        except Exception:
            pulse_duty = 0.2
        x_values: list[float] = []
        y_values: list[float] = []
        for index in range(241):
            x = index / 240
            if waveform == "SIN":
                value = center + amplitude * math.sin(2 * math.pi * x)
            elif waveform == "SQU":
                value = high if x % 1 < duty else low
            elif waveform == "RAMP":
                if x <= symmetry:
                    value = low + (high - low) * x / symmetry
                else:
                    value = high - (high - low) * (x - symmetry) / (1 - symmetry)
            elif waveform == "PULS":
                value = high if x % 1 < pulse_duty else low
            elif waveform == "NOIS":
                noise = 0.58 * math.sin(2 * math.pi * 37 * x) + 0.28 * math.sin(2 * math.pi * 83 * x + 0.7)
                value = center + amplitude * max(-1.0, min(1.0, noise))
            elif waveform == "DC":
                value = high
            else:
                value = center
            x_values.append(x)
            y_values.append(value)
        self.preview_plot.set_trace("Waveform", x_values, y_values, color="#2196f3", primary=True)
        if waveform == "DC":
            title = f"CH{self.channel.currentText()} · DC · Offset {high:.6g} V"
        else:
            title = f"CH{self.channel.currentText()} · {waveform} · HighL {high:.6g} V · LowL {low:.6g} V"
        self.preview_plot.set_title(title)

    @staticmethod
    def _bounded_number(text: str, fallback: float, minimum: float, maximum: float) -> float:
        try:
            value = float(text.replace(",", "."))
        except ValueError:
            value = fallback
        return min(max(value, minimum), maximum)

    def _sync_vpp_offset_from_levels(self) -> None:
        if self._level_syncing:
            return
        try:
            high = parse_quantity(self.high_level.text(), DIMENSION_VOLTAGE).si_value
            low = parse_quantity(self.low_level.text(), DIMENSION_VOLTAGE).si_value
        except Exception:
            return
        self._level_syncing = True
        try:
            self.vpp.setText(self._format_voltage(high - low))
            self.offset.setText(self._format_voltage((high + low) / 2))
        finally:
            self._level_syncing = False
        self._update_preview()

    def _sync_levels_from_vpp_offset(self) -> None:
        if self._level_syncing:
            return
        try:
            offset = parse_quantity(self.offset.text(), DIMENSION_VOLTAGE).si_value
            vpp = (
                0.0
                if self.waveform.currentText() == "DC"
                else parse_quantity(self.vpp.text(), DIMENSION_VOLTAGE).si_value
            )
        except Exception:
            return
        self._level_syncing = True
        try:
            self.high_level.setText(self._format_voltage(offset + vpp / 2))
            self.low_level.setText(self._format_voltage(offset - vpp / 2))
        finally:
            self._level_syncing = False
        self._update_preview()

    def _sync_period_from_frequency(self) -> None:
        if self._time_syncing:
            return
        try:
            frequency = parse_quantity(self.frequency.text(), DIMENSION_FREQUENCY).si_value
            if frequency <= 0:
                return
        except Exception:
            return
        self._time_syncing = True
        try:
            period = 1 / frequency
            if period < 1e-6:
                self.period.setText(f"{period * 1e9:.12g} ns")
            elif period < 1e-3:
                self.period.setText(f"{period * 1e6:.12g} us")
            elif period < 1:
                self.period.setText(f"{period * 1e3:.12g} ms")
            else:
                self.period.setText(f"{period:.12g} s")
        finally:
            self._time_syncing = False

    def _sync_frequency_from_period(self) -> None:
        if self._time_syncing:
            return
        try:
            period = parse_quantity(self.period.text(), DIMENSION_TIME).si_value
            if period <= 0:
                return
        except Exception:
            return
        self._time_syncing = True
        try:
            frequency = 1 / period
            if frequency >= 1e6:
                self.frequency.setText(f"{frequency / 1e6:.12g} MHz")
            elif frequency >= 1e3:
                self.frequency.setText(f"{frequency / 1e3:.12g} kHz")
            else:
                self.frequency.setText(f"{frequency:.12g} Hz")
        finally:
            self._time_syncing = False

    def _modulation_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.mod_enabled = QCheckBox("Modulation enabled")
        self.mod_type = QComboBox()
        self.mod_type.addItems(["AM", "FM", "PM", "ASK", "FSK", "PSK", "PWM"])
        self.mod_source = QComboBox()
        self.mod_source.addItems(["INT", "EXT"])
        self.mod_rate = _line("1 kHz")
        self.mod_parameter = _line("50")
        self.mod_shape = QComboBox()
        self.mod_shape.addItems(["SIN", "SQU", "RAMP", "NOIS", "ARB"])
        self.mod_polarity = QComboBox()
        self.mod_polarity.addItems(["POS", "NEG"])
        apply = QPushButton("Apply modulation while OUTPUT is OFF")
        for label, widget in (
            ("State", self.mod_enabled),
            ("Typ", self.mod_type),
            ("Source", self.mod_source),
                ("Rate / freq.", self._bounded(self.mod_rate, "modulation_rate")),
            ("Type parameter", self.mod_parameter),
            ("Internal shape", self.mod_shape),
            ("Polarity", self.mod_polarity),
            ("", apply),
        ):
            form.addRow(label, widget)
        apply.clicked.connect(self.configure_modulation)
        self._set_help(apply, "Apply modulation", "Validates and applies modulation settings while the physical output remains OFF.")
        return self._scroll_widget(tab)

    def _sweep_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.sweep_enabled = QCheckBox("Sweep enabled")
        self.sweep_start = _line("100 Hz")
        self.sweep_stop = _line("1 kHz")
        self.sweep_duration = _line("1 s")
        self.sweep_start_hold = _line("0 s")
        self.sweep_stop_hold = _line("0 s")
        self.sweep_return_time = _line("0 s")
        self.sweep_spacing = QComboBox()
        self.sweep_spacing.addItems(["LIN", "LOG", "STEP"])
        self.sweep_steps = QSpinBox()
        self.sweep_steps.setRange(2, 1_000_000)
        self.sweep_steps.setValue(10)
        self.sweep_trigger = QComboBox()
        self.sweep_trigger.addItems(["INT", "EXT", "MAN"])
        self.sweep_trigger_slope = QComboBox()
        self.sweep_trigger_slope.addItems(["POS", "NEG"])
        self.sweep_trigger_out = QCheckBox("Trigger output")
        apply = QPushButton("Apply sweep while OUTPUT is OFF")
        trigger = QPushButton("Trigger sweep")
        for label, widget in (
            ("State", self.sweep_enabled),
            ("Start", self._bounded(self.sweep_start, "frequency")),
            ("Stop", self._bounded(self.sweep_stop, "frequency")),
            ("Time", self._bounded(self.sweep_duration, "sweep_duration")),
            ("Hold start", self.sweep_start_hold),
            ("Hold stop", self.sweep_stop_hold),
            ("Return time", self.sweep_return_time),
            ("Spacing", self.sweep_spacing),
            ("Steps", self._bounded(self.sweep_steps, "sweep_steps")),
            ("Trigger source", self.sweep_trigger),
            ("Trigger slope", self.sweep_trigger_slope),
            ("", self.sweep_trigger_out),
            ("", apply),
            ("", trigger),
        ):
            form.addRow(label, widget)
        apply.clicked.connect(self.configure_sweep)
        trigger.clicked.connect(lambda: self._controller.call("trigger_sweep", int(self.channel.currentText())))
        self._set_help(apply, "Apply sweep", "Validates and programs the sweep while the physical output remains OFF.")
        self._set_help(trigger, "Manual sweep trigger", "Starts one sweep when Trigger source is MAN. It does not bypass output safety interlocks.")
        return self._scroll_widget(tab)

    def _burst_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.burst_enabled = QCheckBox("Burst enabled")
        self.burst_mode = QComboBox()
        self.burst_mode.addItems(["TRIG", "GAT"])
        self.burst_cycles = QSpinBox()
        self.burst_cycles.setRange(1, 1_000_000)
        self.burst_cycles.setValue(1)
        self.burst_phase = _line("0")
        self.burst_period = _line("1 ms")
        self.burst_delay = _line("0 s")
        self.burst_trigger = QComboBox()
        self.burst_trigger.addItems(["INT", "EXT", "MAN"])
        self.burst_trigger_slope = QComboBox()
        self.burst_trigger_slope.addItems(["POS", "NEG"])
        self.burst_trigger_out = QCheckBox("Trigger output")
        self.burst_gate_polarity = QComboBox()
        self.burst_gate_polarity.addItems(["POS", "NEG"])
        self.burst_idle = QComboBox()
        self.burst_idle.addItems(["FPT", "TOP", "CENT", "BOT"])
        apply = QPushButton("Apply burst while OUTPUT is OFF")
        trigger = QPushButton("Trigger burst")
        for label, widget in (
            ("State", self.burst_enabled),
            ("Mode", self.burst_mode),
            ("Cycles", self._bounded(self.burst_cycles, "burst_cycles")),
            ("Phase [deg]", self.burst_phase),
            ("Period", self._bounded(self.burst_period, "burst_period")),
            ("Delay", self.burst_delay),
            ("Trigger source", self.burst_trigger),
            ("Trigger slope", self.burst_trigger_slope),
            ("", self.burst_trigger_out),
            ("Gate polarity", self.burst_gate_polarity),
            ("Idle", self.burst_idle),
            ("", apply),
            ("", trigger),
        ):
            form.addRow(label, widget)
        apply.clicked.connect(self.configure_burst)
        trigger.clicked.connect(lambda: self._controller.call("trigger_burst", int(self.channel.currentText())))
        self._set_help(apply, "Apply burst", "Validates and programs burst settings while the physical output remains OFF.")
        self._set_help(trigger, "Manual burst trigger", "Emits one configured burst when Trigger source is MAN.")
        return self._scroll_widget(tab)

    @staticmethod
    def _scroll_widget(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        return scroll

    def configure(self) -> None:
        try:
            high_level, low_level = self._effective_levels()
            frequency_hz = (
                1.0
                if self.waveform.currentText() in {"DC", "NOIS"}
                else parse_quantity(self.frequency.text(), DIMENSION_FREQUENCY).si_value
            )
            config = RigolChannelConfig(
                channel=int(self.channel.currentText()),
                waveform=self.waveform.currentText(),
                frequency_hz=frequency_hz,
                high_level_v=high_level,
                low_level_v=low_level,
                output_load=self.load.text().strip(),
                phase_deg=float(self.phase.text().replace(",", ".")),
                square_duty_percent=float(self.duty.text().replace(",", ".")) if self.waveform.currentText() == "SQU" else None,
                ramp_symmetry_percent=float(self.ramp_symmetry.text().replace(",", ".")) if self.waveform.currentText() == "RAMP" else None,
                pulse_width_s=parse_quantity(self.pulse_width.text(), DIMENSION_TIME).si_value if self.waveform.currentText() == "PULS" else None,
                pulse_leading_s=parse_quantity(self.pulse_leading.text(), DIMENSION_TIME).si_value if self.waveform.currentText() == "PULS" else None,
                pulse_trailing_s=parse_quantity(self.pulse_trailing.text(), DIMENSION_TIME).si_value if self.waveform.currentText() == "PULS" else None,
                dut_min_impedance_ohm=parse_quantity(self.dut_impedance.text(), DIMENSION_RESISTANCE).si_value,
            )
            validate_rigol_waveform(
                channel=self._station_settings.rigol.safety.channels[self.channel.currentText()],
                safety=self._station_settings.rigol.safety,
                waveform=config.waveform,
                frequency=config.frequency_hz,
                high_level=config.high_level_v,
                low_level=config.low_level_v,
                output_load=config.output_load,
                dut_min_impedance=config.dut_min_impedance_ohm,
            )
        except Exception as exc:
            self.banner.show_message(f"Invalid waveform settings: {exc}")
            return
        self._controller.call("configure", config)

    def configure_output(self) -> None:
        try:
            config = RigolOutputConfig(
                channel=int(self.channel.currentText()),
                output_load=self.load.text().strip(),
                polarity=self.output_polarity.currentText(),  # type: ignore[arg-type]
                mode=self.output_mode.currentText(),  # type: ignore[arg-type]
                gate_polarity=self.gate_polarity.currentText(),  # type: ignore[arg-type]
                sync_enabled=self.sync_enabled.isChecked(),
                sync_polarity=self.sync_polarity.currentText(),  # type: ignore[arg-type]
                sync_delay_s=parse_quantity(self.sync_delay.text(), DIMENSION_TIME).si_value,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Output Rigol", str(exc))
            return
        self._controller.call("configure_output", config)

    def configure_modulation(self) -> None:
        try:
            config = RigolModulationConfig(
                channel=int(self.channel.currentText()),
                enabled=self.mod_enabled.isChecked(),
                modulation_type=self.mod_type.currentText(),  # type: ignore[arg-type]
                source=self.mod_source.currentText(),  # type: ignore[arg-type]
                rate_hz=parse_quantity(self.mod_rate.text(), DIMENSION_FREQUENCY).si_value,
                parameter=float(self.mod_parameter.text().replace(",", ".")),
                internal_shape=self.mod_shape.currentText(),  # type: ignore[arg-type]
                polarity=self.mod_polarity.currentText(),  # type: ignore[arg-type]
            )
        except Exception as exc:
            QMessageBox.warning(self, "Rigol modulation", str(exc))
            return
        self._controller.call("configure_modulation", config)

    def configure_sweep(self) -> None:
        try:
            config = RigolFrequencySweepConfig(
                channel=int(self.channel.currentText()),
                enabled=self.sweep_enabled.isChecked(),
                start_hz=parse_quantity(self.sweep_start.text(), DIMENSION_FREQUENCY).si_value,
                stop_hz=parse_quantity(self.sweep_stop.text(), DIMENSION_FREQUENCY).si_value,
                duration_s=parse_quantity(self.sweep_duration.text(), "time").si_value,
                spacing=self.sweep_spacing.currentText(),  # type: ignore[arg-type]
                steps=self.sweep_steps.value(),
                start_hold_s=parse_quantity(self.sweep_start_hold.text(), DIMENSION_TIME).si_value,
                stop_hold_s=parse_quantity(self.sweep_stop_hold.text(), DIMENSION_TIME).si_value,
                return_time_s=parse_quantity(self.sweep_return_time.text(), DIMENSION_TIME).si_value,
                trigger_source=self.sweep_trigger.currentText(),  # type: ignore[arg-type]
                trigger_slope=self.sweep_trigger_slope.currentText(),  # type: ignore[arg-type]
                trigger_output=self.sweep_trigger_out.isChecked(),
            )
            validate_rigol_frequency_sweep(
                channel=self._station_settings.rigol.safety.channels[self.channel.currentText()],
                start_hz=config.start_hz,
                stop_hz=config.stop_hz,
                duration_s=config.duration_s,
                steps=config.steps,
                start_hold_s=config.start_hold_s,
                stop_hold_s=config.stop_hold_s,
                return_time_s=config.return_time_s,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Sweep Rigol", str(exc))
            return
        self._controller.call("configure_sweep", config)

    def configure_burst(self) -> None:
        try:
            config = RigolBurstConfig(
                channel=int(self.channel.currentText()),
                enabled=self.burst_enabled.isChecked(),
                mode=self.burst_mode.currentText(),  # type: ignore[arg-type]
                cycles=self.burst_cycles.value(),
                phase_deg=float(self.burst_phase.text().replace(",", ".")),
                period_s=parse_quantity(self.burst_period.text(), "time").si_value,
                delay_s=parse_quantity(self.burst_delay.text(), "time").si_value,
                trigger_source=self.burst_trigger.currentText(),  # type: ignore[arg-type]
                trigger_slope=self.burst_trigger_slope.currentText(),  # type: ignore[arg-type]
                trigger_output=self.burst_trigger_out.isChecked(),
                gate_polarity=self.burst_gate_polarity.currentText(),  # type: ignore[arg-type]
                idle=self.burst_idle.currentText(),  # type: ignore[arg-type]
            )
        except Exception as exc:
            QMessageBox.warning(self, "Burst Rigol", str(exc))
            return
        self._controller.call("configure_burst", config)

    def arm_output(self) -> None:
        channel = self.channel.currentText()
        answer = QMessageBox.question(
            self,
            "ARM Rigol",
            f"Arm Rigol CH{channel} for 30 seconds? This does not enable the output yet.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._controller.call("arm", int(channel))

    def request_output(self, enabled: bool) -> None:
        channel = int(self.channel.currentText())
        if not enabled:
            self._pending_output_enable = False
            self._controller.call("set_output", (channel, False))
            return
        if self._pending_output_enable:
            return
        self._pending_output_enable = True
        self.output_on.setEnabled(False)
        self.status.emit(
            f"Rigol CH{channel}: validating visible settings before OUTPUT ON"
        )
        self.configure()

    def _result(self, operation: str, result: object) -> None:
        if operation == "configure" and hasattr(result, "peak_absolute_current_a"):
            estimate = result
            self.estimate.setText(
                "Estimated load current (not measured): "
                f"{estimate.peak_absolute_current_a * 1e3:.6g} mA; "
                f"estimated DUT power: {estimate.peak_estimated_dut_power_w * 1e3:.6g} mW; "
                f"Vth High/Low: {estimate.open_circuit_high_v:.6g} / {estimate.open_circuit_low_v:.6g} V"
            )
            if self._pending_output_enable:
                channel = int(self.channel.currentText())
                self.status.emit(
                    f"Rigol CH{channel}: settings valid; applying internal safety arm"
                )
                self._controller.call("arm", channel)
            else:
                self.status.emit("Rigol configured while OUTPUT is OFF")
        elif operation in {"configure_modulation", "configure_sweep", "configure_burst"}:
            self.status.emit(f"Rigol: {operation} configured while OUTPUT is OFF")
        elif operation == "configure_output":
            self.status.emit("Rigol: output path confirmed while OUTPUT is OFF")
        elif operation == "arm":
            if self._pending_output_enable:
                channel = int(self.channel.currentText())
                self.status.emit(f"Rigol CH{channel}: enabling OUTPUT")
                self._controller.call("set_output", (channel, True))
            else:
                self.status.emit("Rigol armed for 30 seconds")
        elif operation == "set_output":
            self._pending_output_enable = False
            self.output_on.setEnabled(True)
            self.status.emit(
                f"Rigol CH{self.channel.currentText()} OUTPUT "
                f"{'ON' if bool(result) else 'OFF'}"
            )
        elif operation == "synchronize_phases":
            self.status.emit("Rigol: CH1/CH2 phases synchronized after capability confirmation")

    def _error(self, operation: str, error: str) -> None:
        if operation in {
            "configure",
            "set_output",
            "arm",
            "configure_modulation",
            "configure_output",
            "configure_sweep",
            "configure_burst",
            "trigger_sweep",
            "trigger_burst",
            "synchronize_phases",
        }:
            if operation in {"configure", "arm", "set_output"}:
                self._pending_output_enable = False
                self.output_on.setEnabled(True)
            QMessageBox.warning(self, "Rigol", error)


@dataclass(frozen=True, slots=True)
class KeithleyConfigurationSnapshot:
    """Qt-independent configuration shared by manual and planned Keithley hosts."""

    channel: str = "B"
    source_mode: str = "current"
    source_level: str = "1 mA"
    compliance: str = "67 mV"
    nplc: str = "1"
    settling_time: str = "100 ms"
    sense_mode: str = "2wire"
    source_autorange: bool = True
    source_range: str = "AUTO"
    measure_voltage_autorange: bool = True
    measure_voltage_range: str = "AUTO"
    measure_current_autorange: bool = True
    measure_current_range: str = "AUTO"
    output_policy: str = "enable_for_run"


def _keithley_roi_definition(
    snapshot: KeithleyConfigurationSnapshot,
    parameter_id: str,
) -> dict[str, str]:
    mode = snapshot.source_mode
    definitions = {
        "source.level": {
            "label": (
                f"Channel {snapshot.channel} · source "
                f"{'current' if mode == 'current' else 'voltage'}"
            ),
            "target": f"keithley.{snapshot.channel}.{mode}",
            "dimension": (
                DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
            ),
        },
        "source.compliance": {
            "label": f"Channel {snapshot.channel} · compliance",
            "target": (
                f"keithley.{snapshot.channel}.compliance_voltage"
                if mode == "current"
                else f"keithley.{snapshot.channel}.compliance_current"
            ),
            "dimension": (
                DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT
            ),
        },
        "measurement.settling_time": {
            "label": f"Channel {snapshot.channel} · settling time",
            "target": f"keithley.{snapshot.channel}.settling_time",
            "dimension": DIMENSION_TIME,
        },
    }
    if parameter_id not in definitions:
        raise ConfigurationError(
            f"Keithley parameter {parameter_id!r} does not support ROI sweeps."
        )
    return {"device": "Keithley", **definitions[parameter_id]}


class KeithleyConfigurationPanel(QFrame):
    """Reusable source/measurement form with no hardware side effects."""

    def __init__(
        self,
        settings: StationSettings,
        parent: QWidget | None = None,
        *,
        plan_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("keithleyConfigurationPanel")
        self._settings = settings
        self.plan_mode = plan_mode
        self.limit_fields: dict[str, LimitField] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        title = QLabel("Source and measurement configuration")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.form = QFormLayout()
        self.form.setVerticalSpacing(4)
        self.form.setHorizontalSpacing(8)
        self.channel = QComboBox()
        self.channel.addItems(["A", "B"])
        self.channel.setCurrentText("B")
        self.mode = QComboBox()
        self.mode.addItems(["current", "voltage", "measure_only"])
        self.level = _line("1 mA")
        self.compliance = _line("67 mV")
        self.nplc = _line("1")
        self.settle = _line("100 ms")
        self.sense_mode = QComboBox()
        self.sense_mode.addItems(["2wire", "4wire"])
        self.source_autorange = QCheckBox("Source autorange")
        self.source_autorange.setChecked(True)
        self.source_range = _line("AUTO")
        self.measure_voltage_autorange = QCheckBox("Measure V autorange")
        self.measure_voltage_autorange.setChecked(True)
        self.measure_voltage_range = _line("AUTO")
        self.measure_current_autorange = QCheckBox("Measure I autorange")
        self.measure_current_autorange.setChecked(True)
        self.measure_current_range = _line("AUTO")
        self.level_field = self._bounded("level", self.level)
        self.compliance_field = self._bounded("compliance", self.compliance)
        self.source_range_field = self._bounded("source_range", self.source_range)
        for label, widget in (
            ("Channel", self.channel),
            ("Source mode", self.mode),
            ("Source current", self.level_field),
            ("Voltage compliance (safety limit)", self.compliance_field),
            ("NPLC", self._bounded("nplc", self.nplc)),
            ("Settling time", self._bounded("settle", self.settle)),
            ("Sense mode", self.sense_mode),
            ("", self.source_autorange),
            ("Current source range", self.source_range_field),
            ("", self.measure_voltage_autorange),
            (
                "Measure V range (AUTO or value with unit)",
                self._bounded("measure_voltage_range", self.measure_voltage_range),
            ),
            ("", self.measure_current_autorange),
            (
                "Measure I range (AUTO or value with unit)",
                self._bounded("measure_current_range", self.measure_current_range),
            ),
        ):
            self.form.addRow(label, widget)
        layout.addLayout(self.form)
        if plan_mode:
            note = QLabel(
                "Plan editing is offline. Applying these values changes only the sweep document; "
                "it never communicates with the instrument or enables OUTPUT."
            )
            note.setObjectName("recipeHint")
            note.setWordWrap(True)
            layout.addWidget(note)
            for field in self.limit_fields.values():
                field.edit_button.setVisible(False)
        self.channel.currentTextChanged.connect(self.refresh_limits)
        self.mode.currentTextChanged.connect(self._update_mode_ui)
        self._update_mode_ui()

    def _bounded(self, key: str, editor: QWidget) -> LimitField:
        field = LimitField(editor, *self.limit_values(key))
        field.setProperty("limitKey", key)
        for badge in (field.minimum, field.maximum):
            badge.setMinimumWidth(68)
            badge.setProperty("keithleyCompact", True)
        field.edit_button.setFixedSize(48, 28)
        field.edit_button.setText("Edit")
        self.limit_fields[key] = field
        return field

    def limit_values(self, key: str) -> tuple[object, object]:
        limits = self._settings.keithley.safety.channels[
            self.channel.currentText()
        ].lab_limits
        mode = self.mode.currentText()
        if key == "nplc":
            return 0.001, 25
        if key == "settle":
            return (
                (limits.point_settle_time.min, limits.point_settle_time.max)
                if limits.point_settle_time
                else ("0 s", "no profile maximum")
            )
        if mode == "measure_only" and key in {"level", "compliance", "source_range"}:
            return "N/A", "N/A"
        if key == "level":
            value = limits.source_current if mode == "current" else limits.source_voltage
            return value.min, value.max
        if key == "compliance":
            value = (
                limits.voltage_compliance
                if mode == "current"
                else limits.current_compliance
            )
            return value.min, value.max
        if key == "source_range":
            value = limits.source_current if mode == "current" else limits.source_voltage
            return "> 0", value.max_abs or value.max
        if key == "measure_voltage_range":
            values = limits.measured_voltage_trip
            return "> 0", values.max_abs or values.max
        if key == "measure_current_range":
            values = limits.measured_current_trip
            return "> 0", values.max_abs or values.max
        return "NOT SET", "NOT SET"

    def refresh_limits(self, *_args: object) -> None:
        for key, field in self.limit_fields.items():
            field.set_limits(*self.limit_values(key))

    def _update_mode_ui(self, *_args: object) -> None:
        mode = self.mode.currentText()
        source_visible = mode != "measure_only"
        for widget in (
            self.level_field,
            self.compliance_field,
            self.source_autorange,
            self.source_range_field,
        ):
            self.form.setRowVisible(widget, source_visible)
        if mode == "current":
            self.form.labelForField(self.level_field).setText("Source current")
            self.form.labelForField(self.compliance_field).setText(
                "Voltage compliance (safety limit)"
            )
            self.form.labelForField(self.source_range_field).setText("Current source range")
        elif mode == "voltage":
            self.form.labelForField(self.level_field).setText("Source voltage")
            self.form.labelForField(self.compliance_field).setText(
                "Current compliance (safety limit)"
            )
            self.form.labelForField(self.source_range_field).setText("Voltage source range")
        self.refresh_limits()

    def snapshot(self) -> KeithleyConfigurationSnapshot:
        return KeithleyConfigurationSnapshot(
            channel=self.channel.currentText(),
            source_mode=self.mode.currentText(),
            source_level=self.level.text().strip(),
            compliance=self.compliance.text().strip(),
            nplc=self.nplc.text().strip(),
            settling_time=self.settle.text().strip(),
            sense_mode=self.sense_mode.currentText(),
            source_autorange=self.source_autorange.isChecked(),
            source_range=self.source_range.text().strip(),
            measure_voltage_autorange=self.measure_voltage_autorange.isChecked(),
            measure_voltage_range=self.measure_voltage_range.text().strip(),
            measure_current_autorange=self.measure_current_autorange.isChecked(),
            measure_current_range=self.measure_current_range.text().strip(),
        )

    def load_snapshot(self, snapshot: KeithleyConfigurationSnapshot) -> None:
        self.channel.setCurrentText(snapshot.channel)
        self.mode.setCurrentText(snapshot.source_mode)
        self.level.setText(snapshot.source_level)
        self.compliance.setText(snapshot.compliance)
        self.nplc.setText(snapshot.nplc)
        self.settle.setText(snapshot.settling_time)
        self.sense_mode.setCurrentText(snapshot.sense_mode)
        self.source_autorange.setChecked(snapshot.source_autorange)
        self.source_range.setText(snapshot.source_range)
        self.measure_voltage_autorange.setChecked(snapshot.measure_voltage_autorange)
        self.measure_voltage_range.setText(snapshot.measure_voltage_range)
        self.measure_current_autorange.setChecked(snapshot.measure_current_autorange)
        self.measure_current_range.setText(snapshot.measure_current_range)
        self._update_mode_ui()

    def set_settings(self, settings: StationSettings) -> None:
        self._settings = settings
        self.refresh_limits()


class KeithleyNodeEditorDialog(QDialog):
    """Offline host for the shared Keithley configuration component."""

    hardware_actions_enabled = False

    def __init__(
        self,
        settings: StationSettings,
        parent: QWidget | None = None,
        *,
        snapshot: KeithleyConfigurationSnapshot | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._loaded_segments_by_parameter: dict[
            str, list[dict[str, object]]
        ] = {}
        self.setWindowTitle("Keithley 2600 — configure sweep node")
        self.resize(1120, 720)
        layout = QVBoxLayout(self)
        heading = QLabel("Keithley 2600")
        heading.setObjectName("recipePageTitle")
        layout.addWidget(heading)
        self.configuration_panel = KeithleyConfigurationPanel(
            settings, self, plan_mode=True
        )
        workspace = QSplitter(Qt.Orientation.Horizontal)
        configuration_scroll = QScrollArea()
        configuration_scroll.setWidgetResizable(True)
        configuration_scroll.setFrameShape(QFrame.Shape.NoFrame)
        configuration_scroll.setWidget(self.configuration_panel)
        workspace.addWidget(configuration_scroll)
        self.channel = self.configuration_panel.channel
        self.mode = self.configuration_panel.mode
        self.level = self.configuration_panel.level
        self.compliance = self.configuration_panel.compliance
        self.nplc = self.configuration_panel.nplc
        self.settle = self.configuration_panel.settle
        self.sense_mode = self.configuration_panel.sense_mode
        parameter_card = QFrame()
        parameter_card.setObjectName("recipeEditorParameters")
        parameter_layout = QGridLayout(parameter_card)
        parameter_layout.setContentsMargins(10, 10, 10, 10)
        selection_title = QLabel("Select what this node controls")
        selection_title.setObjectName("sectionTitle")
        parameter_layout.addWidget(selection_title, 0, 0, 1, 2)
        parameter_layout.addWidget(QLabel("Parameter"), 1, 0)
        parameter_layout.addWidget(QLabel("Action"), 1, 1)
        self.parameter_selectors: dict[str, QComboBox] = {}
        definitions = (
            ("Source value", "source.level", True),
            ("Compliance", "source.compliance", True),
            ("NPLC", "measurement.nplc", False),
            ("Settling time", "measurement.settling_time", True),
            ("Sense mode", "measurement.sense_mode", False),
            ("Source range", "source.range", False),
            ("Voltage measurement range", "measurement.voltage_range", False),
            ("Current measurement range", "measurement.current_range", False),
        )
        for row, (label, parameter_id, sweepable) in enumerate(definitions, start=2):
            parameter_layout.addWidget(QLabel(label), row, 0)
            selector = QComboBox()
            selector.setProperty("parameterId", parameter_id)
            selector.addItem("Bez zmian", "unchanged")
            selector.addItem("Ustaw", "set")
            if sweepable:
                selector.addItem("Sweep — ROI wymagane", "sweep")
            parameter_layout.addWidget(selector, row, 1)
            self.parameter_selectors[parameter_id] = selector
        output_row = 2 + len(definitions)
        parameter_layout.addWidget(QLabel("Output state"), output_row, 0)
        self.output_policy = QComboBox()
        self.output_policy.addItem("Bez zmian", "unchanged")
        self.output_policy.addItem("OUTPUT ON na początku", "on")
        self.output_policy.addItem("OUTPUT OFF", "off")
        parameter_layout.addWidget(self.output_policy, output_row, 1)
        self.open_roi_button = QPushButton("Przejdź do ROI…")
        self.open_roi_button.setObjectName("primaryButton")
        self.open_roi_button.setEnabled(False)
        self.open_roi_button.setToolTip(
            "Open the interval and point editor for the single parameter marked Sweep."
        )
        parameter_layout.addWidget(self.open_roi_button, output_row + 1, 0, 1, 2)
        self.roi_status = QLabel(
            "Oznacz jeden parametr jako Sweep, aby zdefiniować ROI."
        )
        self.roi_status.setObjectName("muted")
        self.roi_status.setWordWrap(True)
        parameter_layout.addWidget(self.roi_status, output_row + 2, 0, 1, 2)
        parameter_note = QLabel(
            "Only rows marked Ustaw or Sweep are stored. Bez zmian keeps the current value "
            "from the Keithley module. OUTPUT is only a plan declaration; this window never "
            "energizes the instrument."
        )
        parameter_note.setObjectName("muted")
        parameter_note.setWordWrap(True)
        parameter_layout.addWidget(parameter_note, output_row + 3, 0, 1, 2)
        parameter_scroll = QScrollArea()
        parameter_scroll.setWidgetResizable(True)
        parameter_scroll.setFrameShape(QFrame.Shape.NoFrame)
        parameter_scroll.setWidget(parameter_card)
        workspace.addWidget(parameter_scroll)
        workspace.setStretchFactor(0, 3)
        workspace.setStretchFactor(1, 2)
        workspace.setSizes([660, 430])
        layout.addWidget(workspace, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply selected actions")
        layout.addWidget(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._validate)
        self.mode.currentTextChanged.connect(self._source_mode_changed)
        self.open_roi_button.clicked.connect(self._open_selected_roi)
        for selector in self.parameter_selectors.values():
            selector.currentIndexChanged.connect(self._update_roi_button)
        if snapshot is not None:
            self.configuration_panel.load_snapshot(snapshot)
        self._source_mode_changed()
        self._update_roi_button()

    def configuration_snapshot(self) -> KeithleyConfigurationSnapshot:
        return self.configuration_panel.snapshot()

    def planned_parameter_actions(self) -> list[dict[str, object]]:
        snapshot = self.configuration_snapshot()
        value_by_parameter = {
            "source.level": snapshot.source_level,
            "source.compliance": snapshot.compliance,
            "measurement.nplc": snapshot.nplc,
            "measurement.settling_time": snapshot.settling_time,
            "measurement.sense_mode": snapshot.sense_mode,
            "source.range": snapshot.source_range,
            "measurement.voltage_range": snapshot.measure_voltage_range,
            "measurement.current_range": snapshot.measure_current_range,
        }
        actions: list[dict[str, object]] = [
            {
                "parameter_id": parameter_id,
                "mode": str(selector.currentData()),
                "value": value_by_parameter[parameter_id],
            }
            for parameter_id, selector in self.parameter_selectors.items()
            if selector.currentData() != "unchanged"
        ]
        for action in actions:
            parameter_id = str(action["parameter_id"])
            if (
                action["mode"] == "sweep"
                and parameter_id in self._loaded_segments_by_parameter
            ):
                action["segments"] = self._loaded_segments_by_parameter[parameter_id]
        return actions

    def selected_output_policy(self) -> str:
        return str(self.output_policy.currentData())

    def _sweep_parameter_ids(self) -> list[str]:
        return [
            parameter_id
            for parameter_id, selector in self.parameter_selectors.items()
            if selector.currentData() == "sweep"
        ]

    def _update_roi_button(self, *_args: object) -> None:
        sweep_ids = self._sweep_parameter_ids()
        self.open_roi_button.setEnabled(len(sweep_ids) == 1)
        if len(sweep_ids) > 1:
            self.roi_status.setText(
                "Wybierz tylko jedną oś Sweep w tym węźle."
            )
            return
        if not sweep_ids:
            self.roi_status.setText(
                "Oznacz jeden parametr jako Sweep, aby zdefiniować ROI."
            )
            return
        parameter_id = sweep_ids[0]
        segments = self._loaded_segments_by_parameter.get(parameter_id)
        self.roi_status.setText(
            f"ROI zapisane: {len(segments)} przedział(y)."
            if segments
            else "ROI nie zostało jeszcze zdefiniowane."
        )

    def _store_roi_segments(
        self,
        parameter_id: str,
        segments: list[dict[str, object]],
    ) -> None:
        self._loaded_segments_by_parameter[parameter_id] = [
            dict(segment) for segment in segments
        ]
        self._update_roi_button()

    def _open_selected_roi(self) -> None:
        sweep_ids = self._sweep_parameter_ids()
        if len(sweep_ids) != 1:
            self._update_roi_button()
            return
        parameter_id = sweep_ids[0]
        snapshot = self.configuration_snapshot()
        definition = _keithley_roi_definition(snapshot, parameter_id)
        roi_dialog = SweepGeneratorDialog(
            definition,
            self,
            initial_segments=self._loaded_segments_by_parameter.get(parameter_id),
        )
        roi_dialog.setWindowTitle(
            f"Keithley {snapshot.channel} — ROI · {definition['label']}"
        )
        if roi_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._store_roi_segments(parameter_id, roi_dialog.segment_data())

    def load_plan_actions(
        self,
        actions: list[dict[str, object]],
        output_policy: str,
    ) -> None:
        for action in actions:
            selector = self.parameter_selectors.get(str(action.get("parameter_id", "")))
            if selector is None:
                continue
            index = selector.findData(str(action.get("mode", "unchanged")))
            if index >= 0:
                selector.setCurrentIndex(index)
            segments = action.get("segments")
            if isinstance(segments, list):
                self._loaded_segments_by_parameter[str(action.get("parameter_id", ""))] = [
                    dict(segment) for segment in segments if isinstance(segment, dict)
                ]
        output_index = self.output_policy.findData(output_policy)
        if output_index >= 0:
            self.output_policy.setCurrentIndex(output_index)
        self._update_roi_button()

    def _source_mode_changed(self, *_args: object) -> None:
        measure_only = self.mode.currentText() == "measure_only"
        for parameter_id in ("source.level", "source.compliance", "source.range"):
            selector = self.parameter_selectors[parameter_id]
            selector.setEnabled(not measure_only)
            if measure_only:
                selector.setCurrentIndex(0)

    def _validate(self) -> bool:
        snapshot = self.configuration_snapshot()
        mode = snapshot.source_mode
        level_dimension = DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
        compliance_dimension = (
            DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT
        )
        try:
            request = KeithleySourceRequest(
                channel=snapshot.channel,  # type: ignore[arg-type]
                mode=mode,  # type: ignore[arg-type]
                level_si=(
                    0.0
                    if mode == "measure_only"
                    else parse_quantity(snapshot.source_level, level_dimension).si_value
                ),
                compliance_si=(
                    0.0
                    if mode == "measure_only"
                    else parse_quantity(snapshot.compliance, compliance_dimension).si_value
                ),
                nplc=float(snapshot.nplc.replace(",", ".")),
                settle_time_s=parse_quantity(
                    snapshot.settling_time, DIMENSION_TIME
                ).si_value,
                sense_mode=snapshot.sense_mode,  # type: ignore[arg-type]
                source_autorange=snapshot.source_autorange,
                source_range_si=KeithleyPage._manual_range(
                    snapshot.source_range, level_dimension, snapshot.source_autorange
                ),
                measure_voltage_autorange=snapshot.measure_voltage_autorange,
                measure_voltage_range_si=KeithleyPage._manual_range(
                    snapshot.measure_voltage_range,
                    DIMENSION_VOLTAGE,
                    snapshot.measure_voltage_autorange,
                ),
                measure_current_autorange=snapshot.measure_current_autorange,
                measure_current_range_si=KeithleyPage._manual_range(
                    snapshot.measure_current_range,
                    DIMENSION_CURRENT,
                    snapshot.measure_current_autorange,
                ),
            )
            validate_keithley_source(
                self._settings.keithley.safety.channels[snapshot.channel], request
            )
        except Exception as exc:
            QMessageBox.warning(self, "Keithley configuration", str(exc))
            return False
        return True

    def accept(self) -> None:
        if (
            sum(
                action.get("mode") == "sweep"
                for action in self.planned_parameter_actions()
            )
            > 1
        ):
            QMessageBox.warning(
                self,
                "Keithley node",
                "One device node can define only one sweep axis. "
                "Nest another Keithley node to create the next loop.",
            )
            return
        if (
            not self.planned_parameter_actions()
            and self.selected_output_policy() == "unchanged"
        ):
            QMessageBox.information(
                self,
                "Keithley node",
                "Select Ustaw or Sweep for at least one parameter, or choose an OUTPUT action.",
            )
            return
        if self._validate():
            super().accept()


class KeithleyPage(QWidget):
    status = Signal(str)

    def __init__(self, controller: DeviceController, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._station_settings = settings
        self._limit_fields: dict[str, LimitField] = {}
        self._output_states = {"A": False, "B": False}
        self._pending_channels: dict[str, str] = {}
        self._pending_config_modes: dict[str, str] = {}
        self._configured_channels: set[str] = set()
        self._auto_enable_channel: str | None = None
        self._confirmed_output_settings: dict[str, tuple[object, ...]] = {}
        self._pending_output_signature: tuple[object, ...] | None = None
        self._measure_pending = False
        self._ramp_pending = False
        self._live_next_channel = "A"
        self._history_started_at = time.monotonic()
        self._history_window_s = 30.0
        self._measurement_history: dict[str, list[dict[str, float]]] = {"A": [], "B": []}
        self.history_widgets: dict[str, dict[str, object]] = {}
        self._armed_until_ui = 0.0
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(1000)
        self._live_timer.timeout.connect(self._request_live_measurement)
        self._arm_timer = QTimer(self)
        self._arm_timer.setInterval(200)
        self._arm_timer.timeout.connect(self._update_arm_status)
        layout = QVBoxLayout(self)
        self.banner = NotificationBanner()
        layout.addWidget(self.banner)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        hero = QFrame()
        hero.setObjectName("keithleyHero")
        hero_layout = QHBoxLayout(hero)
        title = QLabel("Keithley 2600 — Dual-channel SMU")
        title.setObjectName("keithleyPageTitle")
        hero_layout.addWidget(title)
        hero_layout.addStretch(1)
        self.live_measurements = QCheckBox("Live A/B")
        self.live_measurements.setToolTip(
            "Alternately measures channels A and B. This never enables an output."
        )
        self.live_interval = QSpinBox()
        self.live_interval.setRange(100, 60_000)
        self.live_interval.setValue(1000)
        self.live_interval.setSuffix(" ms")
        self.live_interval.setFixedWidth(108)
        self.live_interval.setToolTip(
            "Interval between alternating A/B measurements. Each channel is sampled every "
            "approximately two intervals when both are enabled."
        )
        self.last_update = QLabel("No measurements yet")
        self.last_update.setObjectName("keithleyLastUpdate")
        self.last_update.setMinimumWidth(150)
        hero_layout.addWidget(self.live_measurements)
        hero_layout.addWidget(self.live_interval)
        hero_layout.addWidget(self.last_update)
        self.device_led = QLabel("●")
        self.device_led.setObjectName("keithleyLed")
        self.device_state = QLabel("DISCONNECTED")
        self.device_state.setObjectName("keithleyState")
        hero_layout.addWidget(self.device_led)
        hero_layout.addWidget(self.device_state)
        hero.setMaximumHeight(60)
        layout.addWidget(hero)
        channel_grid = QGridLayout()
        channel_grid.setSpacing(12)
        self.channel_cards: dict[str, dict[str, QLabel | QFrame]] = {}
        for column, channel_name in enumerate(("A", "B")):
            channel_grid.addWidget(self._build_channel_card(channel_name), 0, column)
        layout.addLayout(channel_grid)
        source_tab = QWidget()
        source_layout = QVBoxLayout(source_tab)
        source_layout.setContentsMargins(8, 6, 8, 6)
        source_layout.setSpacing(6)
        self.configuration_panel = KeithleyConfigurationPanel(settings, source_tab)
        source_layout.addWidget(self.configuration_panel)
        self.channel = self.configuration_panel.channel
        self.mode = self.configuration_panel.mode
        self.level = self.configuration_panel.level
        self.compliance = self.configuration_panel.compliance
        self.nplc = self.configuration_panel.nplc
        self.settle = self.configuration_panel.settle
        self.sense_mode = self.configuration_panel.sense_mode
        self.source_autorange = self.configuration_panel.source_autorange
        self.source_range = self.configuration_panel.source_range
        self.measure_voltage_autorange = (
            self.configuration_panel.measure_voltage_autorange
        )
        self.measure_voltage_range = self.configuration_panel.measure_voltage_range
        self.measure_current_autorange = (
            self.configuration_panel.measure_current_autorange
        )
        self.measure_current_range = self.configuration_panel.measure_current_range
        self.level_field = self.configuration_panel.level_field
        self.compliance_field = self.configuration_panel.compliance_field
        self.source_range_field = self.configuration_panel.source_range_field
        self.keithley_form = self.configuration_panel.form
        self._limit_fields = self.configuration_panel.limit_fields
        workflow = QFrame()
        workflow.setObjectName("keithleyOutputWorkflow")
        workflow_layout = QVBoxLayout(workflow)
        workflow_layout.setContentsMargins(7, 5, 7, 5)
        self.output_readiness = QLabel()
        self.output_readiness.setWordWrap(True)
        self.output_readiness.setObjectName("keithleyInterlockStatus")
        self.output_readiness.setToolTip(
            "OUTPUT interlock status. All checks must pass before a channel can be enabled."
        )
        workflow_layout.addWidget(self.output_readiness)
        source_layout.addWidget(workflow)
        ramp_panel = QFrame()
        ramp_panel.setObjectName("keithleyRampPanel")
        ramp_layout = QVBoxLayout(ramp_panel)
        ramp_layout.setContentsMargins(7, 5, 7, 5)
        ramp_title = QLabel("Manual source ramp — OUTPUT must already be ON")
        ramp_title.setObjectName("sectionTitle")
        ramp_layout.addWidget(ramp_title)
        ramp_form = QGridLayout()
        self.ramp_target = _line("1 mA")
        self.ramp_step = _line("100 uA")
        self.ramp_settle = _line("100 ms")
        self.ramp_deadline = _line("10 s")
        for column, (label, widget) in enumerate(
            (
                ("Target", self.ramp_target),
                ("Maximum step", self.ramp_step),
                ("Dwell / point", self.ramp_settle),
                ("Deadline", self.ramp_deadline),
            )
        ):
            ramp_form.addWidget(QLabel(label), 0, column)
            ramp_form.addWidget(widget, 1, column)
        ramp_layout.addLayout(ramp_form)
        ramp_actions = QHBoxLayout()
        self.ramp_preview_button = QPushButton("Preview ramp")
        self.ramp_execute_button = QPushButton("Ramp to target")
        self.ramp_execute_button.setObjectName("outputOnButton")
        self.ramp_preview = QLabel("Preview the ramp before execution.")
        self.ramp_preview.setWordWrap(True)
        self.ramp_preview.setObjectName("muted")
        ramp_actions.addWidget(self.ramp_preview_button)
        ramp_actions.addWidget(self.ramp_execute_button)
        ramp_actions.addWidget(self.ramp_preview, 1)
        ramp_layout.addLayout(ramp_actions)
        source_layout.addWidget(ramp_panel)
        buttons = QHBoxLayout()
        measure = QPushButton("Measure selected channel")
        self.output_toggle = QPushButton("OUTPUT OFF")
        self.output_toggle.setCheckable(True)
        self.output_toggle.setObjectName("outputOffButton")
        self.output_toggle.setVisible(False)
        buttons.addWidget(measure)
        source_layout.addLayout(buttons)
        self.readout = QLabel()
        self.readout.hide()
        source_layout.addStretch(1)
        source_scroll = self._scroll_widget(source_tab)
        source_scroll.setObjectName("keithleyControlPanel")
        source_scroll.setMinimumWidth(610)
        history_tab = QWidget()
        history_layout = QHBoxLayout(history_tab)
        history_layout.setContentsMargins(6, 0, 0, 0)
        history_layout.setSpacing(8)
        for channel_name in ("A", "B"):
            history_layout.addWidget(self._build_keithley_history_panel(channel_name), 1)
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setObjectName("keithleyWorkspace")
        self.workspace_splitter.addWidget(source_scroll)
        self.workspace_splitter.addWidget(history_tab)
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 5)
        self.workspace_splitter.setSizes([680, 1140])
        self.workspace_splitter.setChildrenCollapsible(False)
        layout.addWidget(self.workspace_splitter, 1)
        measure.clicked.connect(self.request_measurement)
        self.output_toggle.toggled.connect(self._output_toggled)
        self.ramp_preview_button.clicked.connect(self._preview_manual_ramp)
        self.ramp_execute_button.clicked.connect(self._execute_manual_ramp)
        self.live_measurements.toggled.connect(self._toggle_live_measurements)
        self.live_interval.valueChanged.connect(self._live_timer.setInterval)
        controller.result.connect(self._result)
        controller.error.connect(self._error)
        controller.state_changed.connect(self._device_state_changed)
        self._active_channel = self.channel.currentText()
        self._active_mode = self.mode.currentText()
        self._source_value_cache: dict[tuple[str, str], tuple[str, str, str]] = {
            (self._active_channel, self._active_mode): (
                self.level.text(), self.compliance.text(), self.source_range.text()
            )
        }
        self.channel.currentTextChanged.connect(self._channel_changed)
        self.mode.currentTextChanged.connect(self._mode_changed)
        self.channel.currentTextChanged.connect(self._selected_channel_changed)
        self._selected_channel_changed(self.channel.currentText())
        self._update_source_mode_ui()
        self._update_output_readiness()
        self._update_ramp_defaults()
        self._install_keithley_help(
            measure=measure,
            output_toggle=self.output_toggle,
        )

    @staticmethod
    def _scroll_widget(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

    def _build_keithley_history_panel(self, channel: str) -> QFrame:
        panel = QFrame()
        panel.setObjectName("keithleyChannelCard")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(7, 6, 7, 6)
        panel_layout.setSpacing(4)
        header = QHBoxLayout()
        title = QLabel(f"CHANNEL {channel} — rolling 30 s history")
        title.setObjectName("keithleyHistoryTitle")
        metric = QComboBox()
        metric.addItem("DC resistance |V/I|", ("resistance", "Resistance", "Ω"))
        metric.addItem("Voltage", ("voltage", "Voltage", "V"))
        metric.addItem("Current", ("current", "Current", "A"))
        metric.addItem("Power V×I", ("power", "Power", "W"))
        clear = QPushButton("Clear history")
        clear.setProperty("compact", True)
        metric.setFixedHeight(28)
        clear.setFixedHeight(28)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(metric)
        header.addWidget(clear)
        panel_layout.addLayout(header)
        note = QLabel("ROLLING 30 s  •  DC resistance |V/I|  •  not complex impedance")
        note.setObjectName("keithleyHistoryNote")
        panel_layout.addWidget(note)
        plot = SpectrumPlotWidget(legend=False, compact_toolbar=True)
        plot.setMinimumHeight(220)
        plot.set_title(f"Channel {channel} — DC resistance")
        plot.set_labels(x="Elapsed time", x_unit="s", y="Resistance", y_unit="Ω")
        plot.status_changed.connect(self.status.emit)
        panel_layout.addWidget(plot, 1)
        self.history_widgets[channel] = {"plot": plot, "metric": metric, "clear": clear}
        metric.currentIndexChanged.connect(
            lambda _index, selected=channel: self._refresh_keithley_history_plot(selected)
        )
        clear.clicked.connect(
            lambda _checked=False, selected=channel: self._clear_keithley_history(selected)
        )
        return panel

    def _refresh_keithley_history_plot(self, channel: str) -> None:
        controls = self.history_widgets[channel]
        plot = controls["plot"]
        metric = controls["metric"]
        if not isinstance(plot, SpectrumPlotWidget) or not isinstance(metric, QComboBox):
            return
        key, caption, unit = metric.currentData()
        history = self._measurement_history[channel]
        plot.set_title(f"Channel {channel} — {caption}")
        plot.set_labels(x="Elapsed time", x_unit="s", y=caption, y_unit=unit)
        plot.set_trace(
            f"CH {channel} {caption}",
            [point["elapsed_s"] for point in history],
            [point[key] for point in history],
            color="#00a67d" if channel == "A" else "#2196f3",
            primary=True,
        )

    def _clear_keithley_history(self, channel: str) -> None:
        self._measurement_history[channel].clear()
        plot = self.history_widgets[channel]["plot"]
        if isinstance(plot, SpectrumPlotWidget):
            plot.clear()
        self.status.emit(f"Keithley CH {channel} measurement history cleared")

    def _build_channel_card(self, channel: str) -> QFrame:
        card = QFrame()
        card.setObjectName("keithleyChannelCard")
        card.setProperty("selected", False)
        card.setMaximumHeight(154)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 6, 8, 6)
        card_layout.setSpacing(4)
        header = QHBoxLayout()
        name = QLabel(f"CHANNEL {channel}")
        name.setObjectName("keithleyCardTitle")
        led = QLabel("●")
        led.setObjectName("keithleyOutputLed")
        output = QLabel("OUTPUT OFF")
        output.setObjectName("keithleyOutputState")
        header.addWidget(name)
        header.addStretch(1)
        header.addWidget(led)
        header.addWidget(output)
        card_layout.addLayout(header)
        meters = QGridLayout()
        values: dict[str, QLabel] = {}
        definitions = (
            ("voltage", "VOLTAGE", "V"),
            ("current", "CURRENT", "A"),
            ("resistance", "RESISTANCE (derived V/I)", "Ω"),
            ("power", "POWER (derived V×I)", "W"),
        )
        for index, (key, caption, unit) in enumerate(definitions):
            tile = QFrame()
            tile.setObjectName("keithleyMeterTile")
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(7, 3, 7, 3)
            tile_layout.setSpacing(1)
            caption_label = QLabel(caption)
            caption_label.setObjectName("muted")
            value = QLabel(f"— {unit}")
            value.setObjectName("keithleyMeterValue")
            tile_layout.addWidget(caption_label)
            tile_layout.addWidget(value)
            meters.addWidget(tile, 0, index)
            values[key] = value
        card_layout.addLayout(meters)
        footer = QHBoxLayout()
        compliance = QLabel("COMPLIANCE: clear")
        compliance.setObjectName("keithleyComplianceClear")
        select = QPushButton(f"Select CH {channel}")
        select.setProperty("compact", True)
        select.clicked.connect(lambda _checked=False, ch=channel: self.channel.setCurrentText(ch))
        measure = QPushButton(f"Measure CH {channel}")
        measure.setProperty("compact", True)
        measure.clicked.connect(lambda _checked=False, ch=channel: self.request_measurement(ch))
        output_action = QPushButton("OUTPUT OFF")
        output_action.setProperty("compact", True)
        output_action.setObjectName("outputOffButton")
        output_action.clicked.connect(
            lambda _checked=False, ch=channel: self._request_channel_output(ch)
        )
        footer.addWidget(compliance)
        footer.addStretch(1)
        footer.addWidget(select)
        footer.addWidget(measure)
        footer.addWidget(output_action)
        for button in (select, measure, output_action):
            button.setFixedHeight(28)
        card_layout.addLayout(footer)
        self.channel_cards[channel] = {
            "card": card,
            "led": led,
            "output": output,
            "compliance": compliance,
            "select": select,
            "measure": measure,
            "output_action": output_action,
            **values,
        }
        return card

    @staticmethod
    def _set_help(widget: QWidget, title: str, text: str) -> None:
        help_text = f"<b>{title}</b><br>{text}"
        widget.setToolTip(help_text)
        widget.setToolTipDuration(25_000)
        widget.setWhatsThis(help_text)
        widget.setAccessibleDescription(f"{title}. {text}")

    def _install_keithley_help(
        self,
        *,
        measure: QPushButton,
        output_toggle: QPushButton,
    ) -> None:
        help_items = {
            self.channel: ("Channel", "Selects SMU channel A or B. Both channels are electrically independent and have separate source values, measurements and safety limits."),
            self.mode: ("Source mode", "Current forces a programmed current and uses Voltage compliance as its protection. Voltage forces a programmed voltage and uses Current compliance. Measure only does not program or enable a source."),
            self.level: ("Source value", "The quantity Keithley actively tries to force. In Current mode this is current; in Voltage mode this is voltage. MIN/MAX are the laboratory-approved range for this programmed value."),
            self.compliance: ("Compliance safety limit", "The maximum allowed opposite quantity. Current mode limits voltage; Voltage mode limits current. Reaching compliance means the requested source value cannot be maintained safely."),
            self.nplc: ("NPLC", "Number of power-line cycles integrated for one measurement. Higher values reduce noise but make readings slower. For 50 Hz mains, NPLC 1 integrates for approximately 20 ms."),
            self.settle: ("Settling time", "Delay allowed after changing a source point before a measurement is taken. Longer settling can improve stability but increases sweep duration."),
            self.sense_mode: ("Sense mode", "2-wire measures through the source leads and includes lead/contact resistance. 4-wire uses separate sense leads to remove most lead-voltage error; it requires correct Kelvin wiring."),
            self.source_autorange: ("Source autorange", "Lets Keithley choose the source range automatically. Disable only when a qualified measurement procedure requires a fixed range."),
            self.source_range: ("Manual source range", "Maximum magnitude supported by the selected fixed source range. AUTO uses autorange. A manual value does not set the output; it selects instrument resolution/headroom."),
            self.measure_voltage_autorange: ("Voltage measurement autorange", "Automatically selects the voltage measurement range. Usually the safest default when the expected voltage is not precisely known."),
            self.measure_voltage_range: ("Voltage measurement range", "Fixed voltage measurement range used only when voltage autorange is disabled. It is not Voltage compliance and does not energize the output."),
            self.measure_current_autorange: ("Current measurement autorange", "Automatically selects the current measurement range. It changes measurement range, not the sourced current or compliance."),
            self.measure_current_range: ("Current measurement range", "Fixed current measurement range used only when current autorange is disabled. It is not Current compliance."),
            measure: ("Measure selected channel", "Reads voltage and current from the selected SMU channel. Power and resistance shown in the cards are calculated from those I/V readings."),
            output_toggle: ("OUTPUT ON/OFF", "ON validates and confirms the visible source settings, configures the channel with OUTPUT OFF, performs the internal safety unlock and then energizes the terminals. OFF immediately starts the safe ramp-to-zero and disables the output."),
            self.live_measurements: ("Live readout", "Alternately requests I/V readings from enabled channels every second. It never enables an output, but it does generate continuous instrument traffic."),
            self.device_led: ("Keithley connection state", "Grey means disconnected, green verified/output-safe, amber energized and red indicates compliance, fault or unknown state."),
            self.device_state: ("Device state", "Connection and safety state reported by the Keithley adapter. This is separate from the individual A/B output indicators."),
            self.ramp_target: ("Ramp target", "Final Current or Voltage source level. It is validated against the channel laboratory limits and active DUT envelope before any command is sent."),
            self.ramp_step: ("Maximum ramp step", "Largest allowed change between adjacent source points. The adapter rejects values above the approved ramp_current_step_max or ramp_voltage_step_max."),
            self.ramp_settle: ("Ramp dwell", "Time allowed after every source step before the atomic I/V safety measurement."),
            self.ramp_deadline: ("Ramp deadline", "Maximum wall-clock time for the complete operation. Timeout triggers a best-effort OFF of both SMU outputs."),
            self.ramp_preview_button: ("Preview ramp", "Calculates the finite point sequence without contacting the instrument. Execution still queries the actual starting source level."),
            self.ramp_execute_button: ("Ramp active output", "Changes an already enabled source through bounded points. It never turns an output on; every point measures I/V and trips OFF on failure."),
        }
        for widget, (title, description) in help_items.items():
            self._set_help(widget, title, description)

        for channel, card in self.channel_cards.items():
            self._set_help(card["card"], f"Channel {channel} overview", "Live overview of this channel. Voltage and current are direct readings; resistance and power are derived from the latest I/V pair.")
            self._set_help(card["led"], f"Channel {channel} output LED", "Green is confirmed OUTPUT OFF, amber is OUTPUT ON, and grey means the output state is not known or the device is disconnected.")
            self._set_help(card["output"], f"Channel {channel} output state", "Shows the last state confirmed by a successful connect, configure, enable, ramp-off or compliance-stop operation.")
            self._set_help(card["voltage"], "Measured voltage", "Direct voltage reading returned by Keithley for this channel.")
            self._set_help(card["current"], "Measured current", "Direct current reading returned by Keithley for this channel.")
            self._set_help(card["resistance"], "Derived resistance", "Calculated as |V/I| from the latest reading. It is not a dedicated resistance measurement and becomes infinity when current is effectively zero.")
            self._set_help(card["power"], "Derived power", "Calculated as V × I from the latest reading. Sign describes source/load direction; magnitude describes electrical power.")
            self._set_help(card["compliance"], "Compliance indicator", "ACTIVE means the measured opposite quantity reached the programmed compliance threshold. The safety policy may immediately disable outputs.")
            self._set_help(card["select"], f"Select channel {channel}", "Makes this channel active in the configuration form without changing its electrical output.")
            self._set_help(card["measure"], f"Measure channel {channel}", "Requests one voltage/current reading for this channel without enabling its output.")
            self._set_help(card["output_action"], f"Channel {channel} OUTPUT", "Enables or disables this channel. Enabling validates and confirms the visible source settings before energizing the terminals; disabling ramps safely to zero.")
        self.workspace_splitter.setToolTip(
            "Source controls and independent A/B time histories remain visible together. "
            "Resistance is derived as |V/I| and is not complex AC impedance."
        )

    def _selected_channel_changed(self, selected: str) -> None:
        for channel, widgets in self.channel_cards.items():
            card = widgets["card"]
            card.setProperty("selected", channel == selected)
            card.style().unpolish(card)
            card.style().polish(card)
        self.output_toggle.blockSignals(True)
        self.output_toggle.setChecked(self._output_states[selected])
        self.output_toggle.blockSignals(False)
        self._style_output_toggle(self._output_states[selected])
        self._update_output_readiness()

    def _output_prerequisites(self) -> tuple[bool, list[str]]:
        channel = self.channel.currentText()
        safety = self._station_settings.keithley.safety
        checks = [
            (not self._station_settings.outputs_locked, "profile approved"),
            (safety.allow_output_enable, "Keithley output permission enabled"),
            (safety.channels[channel].enabled, f"channel {channel} enabled"),
            (self.device_state.text() != "DISCONNECTED", "device connected"),
        ]
        return all(value for value, _label in checks), [
            f"{'✓' if value else '✕'} {label}" for value, label in checks
        ]

    def _update_output_readiness(self) -> None:
        if not hasattr(self, "output_readiness"):
            return
        ready, checks = self._output_prerequisites()
        self.output_readiness.setText("Output readiness: " + " • ".join(checks))
        self.output_toggle.setEnabled(ready or self._output_states[self.channel.currentText()])
        safety = self._station_settings.keithley.safety
        common_ready = (
            not self._station_settings.outputs_locked
            and safety.allow_output_enable
            and self.device_state.text() != "DISCONNECTED"
        )
        for channel, card in self.channel_cards.items():
            card["output_action"].setEnabled(
                self._output_states[channel] or (common_ready and safety.channels[channel].enabled)
            )

    def _update_arm_status(self) -> None:
        remaining = self._armed_until_ui - time.monotonic()
        if remaining <= 0:
            self._armed_until_ui = 0.0
            self._arm_timer.stop()
        self._update_output_readiness()

    def _device_state_changed(self, state: str) -> None:
        normalized = state.upper()
        self.device_state.setText(normalized.replace("_", " "))
        color = {
            "DISCONNECTED": "#91a0b2",
            "VERIFIED": "#38d996",
            "OUTPUT_OFF": "#38d996",
            "OUTPUT_ON": "#ffcc66",
            "COMPLIANCE": "#ff657a",
            "FAULT": "#ff657a",
            "UNKNOWN": "#ff657a",
        }.get(normalized, "#91a0b2")
        self.device_led.setStyleSheet(f"color: {color};")
        if normalized == "DISCONNECTED":
            self._live_timer.stop()
            self.live_measurements.setChecked(False)
            self._configured_channels.clear()
            self._armed_until_ui = 0.0
            self._arm_timer.stop()
            self._output_states = {"A": False, "B": False}
            self._reset_output_toggle()
            for channel in ("A", "B"):
                widgets = self.channel_cards[channel]
                widgets["output"].setText("OUTPUT UNKNOWN")
                widgets["led"].setStyleSheet("color: #91a0b2;")
        elif normalized == "VERIFIED":
            # Connection qualification explicitly forces and verifies both outputs OFF.
            self._set_channel_output("A", False)
            self._set_channel_output("B", False)
        self._update_output_readiness()

    @staticmethod
    def _engineering(value: float, unit: str) -> str:
        magnitude = abs(value)
        scales = ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p"))
        for scale, prefix in scales:
            if magnitude >= scale or scale == 1e-12:
                return f"{value / scale:.7g} {prefix}{unit}"
        return f"{value:.7g} {unit}"

    def _update_channel_measurement(self, measurement: object) -> None:
        channel = str(getattr(measurement, "channel"))
        voltage = float(getattr(measurement, "voltage_v"))
        current = float(getattr(measurement, "current_a"))
        power = float(getattr(measurement, "power_w"))
        resistance = abs(voltage / current) if abs(current) > 1e-15 else math.inf
        widgets = self.channel_cards[channel]
        widgets["voltage"].setText(self._engineering(voltage, "V"))
        widgets["current"].setText(self._engineering(current, "A"))
        widgets["power"].setText(self._engineering(power, "W"))
        widgets["resistance"].setText("∞ Ω" if not math.isfinite(resistance) else self._engineering(resistance, "Ω"))
        compliance = bool(getattr(measurement, "compliance_detected", False))
        widgets["compliance"].setText("COMPLIANCE: ACTIVE" if compliance else "COMPLIANCE: clear")
        widgets["compliance"].setObjectName("keithleyComplianceActive" if compliance else "keithleyComplianceClear")
        widgets["compliance"].style().unpolish(widgets["compliance"])
        widgets["compliance"].style().polish(widgets["compliance"])
        if compliance:
            self._set_channel_output(channel, False)
        elapsed = time.monotonic() - self._history_started_at
        history = self._measurement_history[channel]
        history.append(
            {
                "elapsed_s": elapsed,
                "voltage": voltage,
                "current": current,
                "resistance": resistance,
                "power": power,
            }
        )
        cutoff = elapsed - self._history_window_s
        if cutoff > 0:
            history[:] = [point for point in history if point["elapsed_s"] >= cutoff]
        if len(history) > 2000:
            del history[: len(history) - 2000]
        self._refresh_keithley_history_plot(channel)
        self.last_update.setText(
            f"CH {channel}  •  {elapsed:.1f} s  •  {len(history)} pts"
        )

    def _set_channel_output(self, channel: str, enabled: bool) -> None:
        self._output_states[channel] = enabled
        widgets = self.channel_cards[channel]
        widgets["output"].setText("OUTPUT ON" if enabled else "OUTPUT OFF")
        widgets["led"].setStyleSheet(f"color: {'#ffcc66' if enabled else '#38d996'};")
        action = widgets["output_action"]
        action.setText("OUTPUT ON" if enabled else "OUTPUT OFF")
        action.setObjectName("outputOnButton" if enabled else "outputOffButton")
        action.style().unpolish(action)
        action.style().polish(action)
        if channel == self.channel.currentText():
            self.output_toggle.blockSignals(True)
            self.output_toggle.setChecked(enabled)
            self.output_toggle.blockSignals(False)
            self._style_output_toggle(enabled)
        self._update_ramp_defaults()

    def request_measurement(self, channel: str | None = None) -> None:
        if self._measure_pending:
            return
        selected = channel or self.channel.currentText()
        self._pending_channels["measure"] = selected
        self._measure_pending = True
        self._controller.call("measure", selected)

    def _request_channel_output(self, channel: str) -> None:
        self.channel.setCurrentText(channel)
        target_enabled = not self._output_states[channel]
        action = self.channel_cards[channel]["output_action"]
        action.setText("ENABLING…" if target_enabled else "DISABLING…")
        action.setEnabled(False)
        self._output_toggled(target_enabled)

    def request_ramp_off(self) -> None:
        channel = self.channel.currentText()
        self._pending_channels["ramp_to_zero"] = channel
        self.output_toggle.setText("DISABLING…")
        self.output_toggle.setEnabled(False)
        self._controller.call("ramp_to_zero", channel)

    def _toggle_live_measurements(self, enabled: bool) -> None:
        if enabled:
            self._live_timer.setInterval(self.live_interval.value())
            self._request_live_measurement()
            self._live_timer.start()
        else:
            self._live_timer.stop()

    def _request_live_measurement(self) -> None:
        if self._measure_pending or self._ramp_pending:
            return
        enabled = [
            channel
            for channel in ("A", "B")
            if self._station_settings.keithley.safety.channels[channel].enabled
        ]
        if not enabled:
            self.live_measurements.setChecked(False)
            self.status.emit("Keithley live readout stopped: no enabled channels")
            return
        channel = self._live_next_channel if self._live_next_channel in enabled else enabled[0]
        next_index = (enabled.index(channel) + 1) % len(enabled)
        self._live_next_channel = enabled[next_index]
        self.request_measurement(channel)

    def _remember_source_values(self) -> None:
        self._source_value_cache[(self._active_channel, self._active_mode)] = (
            self.level.text(),
            self.compliance.text(),
            self.source_range.text(),
        )

    def _default_source_values(self, channel: str, mode: str) -> tuple[str, str, str]:
        channel_settings = self._station_settings.keithley.safety.channels[channel]
        limits = channel_settings.lab_limits
        defaults = channel_settings.defaults
        if mode == "current":
            return (
                str(defaults.get("source_current", limits.source_current.min)),
                str(defaults.get("voltage_compliance", limits.voltage_compliance.max)),
                "AUTO",
            )
        if mode == "voltage":
            return (
                str(defaults.get("source_voltage", "0 V")),
                str(defaults.get("current_compliance", limits.current_compliance.min)),
                "AUTO",
            )
        return ("0 V", "0 A", "AUTO")

    def _load_source_values(self) -> None:
        values = self._source_value_cache.get(
            (self._active_channel, self._active_mode),
            self._default_source_values(self._active_channel, self._active_mode),
        )
        self.level.setText(values[0])
        self.compliance.setText(values[1])
        self.source_range.setText(values[2])
        if self._active_mode != "measure_only":
            self.level_field.validate_and_clamp()
            self.compliance_field.validate_and_clamp()

    def _channel_changed(self, channel: str) -> None:
        self._remember_source_values()
        self._active_channel = channel
        self._refresh_keithley_limits()
        self._load_source_values()
        self._update_source_mode_ui()

    def _mode_changed(self, mode: str) -> None:
        self._remember_source_values()
        self._active_mode = mode
        self._refresh_keithley_limits()
        self._load_source_values()
        self._update_source_mode_ui()

    def _update_source_mode_ui(self) -> None:
        mode = self.mode.currentText()
        source_visible = mode != "measure_only"
        for widget in (self.level_field, self.compliance_field, self.source_autorange, self.source_range_field):
            self.keithley_form.setRowVisible(widget, source_visible)
        if mode == "current":
            self.keithley_form.labelForField(self.level_field).setText("Source current")
            self.keithley_form.labelForField(self.compliance_field).setText("Voltage compliance (safety limit)")
            self.keithley_form.labelForField(self.source_range_field).setText("Current source range")
        elif mode == "voltage":
            self.keithley_form.labelForField(self.level_field).setText("Source voltage")
            self.keithley_form.labelForField(self.compliance_field).setText("Current compliance (safety limit)")
            self.keithley_form.labelForField(self.source_range_field).setText("Voltage source range")
        self._update_output_readiness()
        self._update_ramp_defaults(reset_values=True)

    def _update_ramp_defaults(self, *, reset_values: bool = False) -> None:
        mode = self.mode.currentText()
        enabled = mode in {"current", "voltage"}
        self.ramp_preview_button.setEnabled(enabled and not self._ramp_pending)
        self.ramp_execute_button.setEnabled(
            enabled and self._output_states[self.channel.currentText()] and not self._ramp_pending
        )
        if not enabled:
            self.ramp_preview.setText("Manual ramp is unavailable in Measure only mode.")
            return
        limits = self._station_settings.keithley.safety.channels[
            self.channel.currentText()
        ].lab_limits
        if reset_values:
            self.ramp_target.setText(self.level.text())
            self.ramp_step.setText(
                limits.ramp_current_step_max
                if mode == "current"
                else limits.ramp_voltage_step_max
            )
            self.ramp_settle.setText(self.settle.text())
            self.ramp_preview.setText(
                "Preview uses the visible source level; execution reads the actual level from the SMU."
            )

    def _manual_ramp_request(self) -> tuple[KeithleyRampRequest, tuple[float, ...]]:
        mode = self.mode.currentText()
        if mode not in {"current", "voltage"}:
            raise ValueError("Select Current or Voltage source mode.")
        dimension = DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
        target = parse_quantity(self.ramp_target.text(), dimension).si_value
        step = parse_quantity(self.ramp_step.text(), dimension).si_value
        settle = parse_quantity(self.ramp_settle.text(), DIMENSION_TIME).si_value
        deadline = parse_quantity(self.ramp_deadline.text(), DIMENSION_TIME).si_value
        source_request = replace(self._source_request(), level_si=target)
        channel_settings = self._station_settings.keithley.safety.channels[
            self.channel.currentText()
        ]
        validate_keithley_source(channel_settings, source_request)
        start = parse_quantity(self.level.text(), dimension).si_value
        levels = build_keithley_ramp_levels(
            start,
            target,
            step,
            max_points=channel_settings.lab_limits.sweep_points_max,
        )
        if len(levels) * settle > deadline:
            raise ValueError("Ramp dwell time exceeds the configured deadline.")
        request = KeithleyRampRequest(
            self.channel.currentText(),  # type: ignore[arg-type]
            target,
            step,
            settle,
            deadline,
        )
        return request, levels

    def _preview_manual_ramp(self) -> None:
        try:
            request, levels = self._manual_ramp_request()
        except Exception as exc:
            self.banner.show_message(f"Invalid ramp: {exc}")
            return
        preview = ", ".join(f"{value:.6g}" for value in levels[:8])
        suffix = " …" if len(levels) > 8 else ""
        self.ramp_preview.setText(
            f"{len(levels)} point(s) • target {request.target_si:.6g} SI • "
            f"estimated dwell {len(levels) * request.settle_time_s:.3g} s • "
            f"levels: {preview}{suffix}"
        )

    def _execute_manual_ramp(self) -> None:
        channel = self.channel.currentText()
        if not self._output_states[channel]:
            self.banner.show_message("Manual ramp requires the selected OUTPUT to be ON.")
            return
        try:
            request, levels = self._manual_ramp_request()
        except Exception as exc:
            self.banner.show_message(f"Invalid ramp: {exc}")
            return
        answer = QMessageBox.warning(
            self,
            "Ramp active Keithley output",
            f"Ramp channel {channel} through {len(levels)} point(s) to "
            f"{self.ramp_target.text()}?\n\n"
            "The output remains energized. Every point performs an I/V safety check; "
            "any error attempts to disable both outputs.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._ramp_pending = True
        self._pending_channels["ramp_to_level"] = channel
        self.ramp_execute_button.setText("RAMPING…")
        self._update_ramp_defaults()
        self.status.emit(f"Keithley CH {channel}: manual ramp started ({len(levels)} points)")
        self._controller.call("ramp_to_level", request)

    def _keithley_limit_values(self, key: str) -> tuple[object, object]:
        limits = self._station_settings.keithley.safety.channels[self.channel.currentText()].lab_limits
        mode = self.mode.currentText()
        if key == "nplc":
            return 0.001, 25
        if key == "settle":
            return (limits.point_settle_time.min, limits.point_settle_time.max) if limits.point_settle_time else ("0 s", "no profile maximum")
        if mode == "measure_only" and key in {"level", "compliance", "source_range"}:
            return "N/A", "N/A"
        if key == "level":
            value = limits.source_current if mode == "current" else limits.source_voltage
            return value.min, value.max
        if key == "compliance":
            value = limits.voltage_compliance if mode == "current" else limits.current_compliance
            return value.min, value.max
        if key == "source_range":
            value = limits.source_current if mode == "current" else limits.source_voltage
            return "> 0", value.max_abs or value.max
        if key == "measure_voltage_range":
            values = limits.measured_voltage_trip
            return "> 0", values.max_abs or values.max
        if key == "measure_current_range":
            values = limits.measured_current_trip
            return "> 0", values.max_abs or values.max
        return "NOT SET", "NOT SET"

    def _keithley_bounded(self, key: str, editor: QWidget) -> LimitField:
        field = LimitField(editor, *self._keithley_limit_values(key))
        field.setProperty("limitKey", key)
        for badge in (field.minimum, field.maximum):
            badge.setMinimumWidth(68)
            badge.setProperty("keithleyCompact", True)
        field.edit_button.setFixedSize(48, 28)
        field.edit_button.setText("Edit")
        self._limit_fields[key] = field
        return field

    def _refresh_keithley_limits(self, *_args: object) -> None:
        for key, field in self._limit_fields.items():
            field.set_limits(*self._keithley_limit_values(key))

    def set_settings(self, settings: StationSettings) -> None:
        self._station_settings = settings
        self.configuration_panel.set_settings(settings)
        self._refresh_keithley_limits()
        self._update_output_readiness()
        self._update_ramp_defaults(reset_values=True)

    def configure(self) -> None:
        try:
            request = self._source_request()
        except Exception as exc:
            self.banner.show_message(f"Invalid Keithley settings: {exc}")
            return
        self._pending_channels["configure"] = self.channel.currentText()
        self._pending_config_modes[self.channel.currentText()] = request.mode
        self._controller.call("configure", request)

    def _source_request(self) -> KeithleySourceRequest:
        mode = self.mode.currentText()
        level_dimension = DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
        compliance_dimension = DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT
        return KeithleySourceRequest(
            channel=self.channel.currentText(),  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            level_si=0.0 if mode == "measure_only" else parse_quantity(self.level.text(), level_dimension).si_value,
            compliance_si=0.0 if mode == "measure_only" else parse_quantity(self.compliance.text(), compliance_dimension).si_value,
            nplc=float(self.nplc.text().replace(",", ".")),
            settle_time_s=parse_quantity(self.settle.text(), "time").si_value,
            sense_mode=self.sense_mode.currentText(),  # type: ignore[arg-type]
            source_autorange=self.source_autorange.isChecked(),
            source_range_si=self._manual_range(self.source_range.text(), level_dimension, self.source_autorange.isChecked()),
            measure_voltage_autorange=self.measure_voltage_autorange.isChecked(),
            measure_voltage_range_si=self._manual_range(self.measure_voltage_range.text(), DIMENSION_VOLTAGE, self.measure_voltage_autorange.isChecked()),
            measure_current_autorange=self.measure_current_autorange.isChecked(),
            measure_current_range_si=self._manual_range(self.measure_current_range.text(), DIMENSION_CURRENT, self.measure_current_autorange.isChecked()),
        )

    @staticmethod
    def _manual_range(text: str, dimension: str, autorange: bool) -> float | None:
        value = text.strip()
        if value.upper() == "AUTO":
            return None
        if autorange:
            raise ValueError("Disable autorange before entering a manual range.")
        return parse_quantity(value, dimension).si_value

    def _output_toggled(self, enabled: bool) -> None:
        channel = self.channel.currentText()
        if not enabled:
            self._style_output_toggle(False)
            if self._output_states[channel]:
                self.request_ramp_off()
            return
        ready, checks = self._output_prerequisites()
        if not ready:
            self.banner.show_message(
                "OUTPUT cannot be enabled. Complete the missing readiness checks: "
                + "; ".join(item for item in checks if item.startswith("✕")),
                timeout_ms=15_000,
            )
            self._reset_output_toggle()
            return
        try:
            request = self._source_request()
        except Exception as exc:
            self.banner.show_message(f"Invalid Keithley settings: {exc}")
            self._reset_output_toggle()
            return
        if request.mode == "measure_only":
            self.banner.show_message("Select Current or Voltage source mode before enabling OUTPUT.")
            self._reset_output_toggle()
            return
        signature = astuple(request)
        if self._confirmed_output_settings.get(channel) != signature:
            source_name = "Current" if request.mode == "current" else "Voltage"
            answer = QMessageBox.warning(
                self, "Enable Keithley output",
                f"Enable physical OUTPUT on channel {channel}?\n\n"
                f"Mode: {source_name}\nSource: {self.level.text()}\n"
                f"Compliance: {self.compliance.text()}\nSense: {self.sense_mode.currentText()}\n\n"
                "The application will configure the channel safely and then energize the terminals.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._reset_output_toggle()
                return
            self._pending_output_signature = signature
        self._auto_enable_channel = channel
        self._pending_channels["configure"] = channel
        self._pending_config_modes[channel] = request.mode
        self.output_toggle.setText("ENABLING…")
        self.output_toggle.setEnabled(False)
        self.status.emit(f"Keithley CH {channel}: validating and configuring before OUTPUT ON")
        self._controller.call("configure", request)

    def _reset_output_toggle(self) -> None:
        self.output_toggle.blockSignals(True)
        self.output_toggle.setChecked(False)
        self.output_toggle.blockSignals(False)
        self._style_output_toggle(False)
        channel = self.channel.currentText()
        if channel in self.channel_cards:
            self._set_channel_output(channel, self._output_states[channel])
            self._update_output_readiness()

    def _style_output_toggle(self, enabled: bool) -> None:
        self.output_toggle.setText("OUTPUT ON" if enabled else "OUTPUT OFF")
        self.output_toggle.setObjectName("outputOnButton" if enabled else "outputOffButton")
        self.output_toggle.style().unpolish(self.output_toggle)
        self.output_toggle.style().polish(self.output_toggle)

    def _result(self, operation: str, result: object) -> None:
        if operation == "measure" and hasattr(result, "current_a"):
            measurement = result
            self._measure_pending = False
            self._update_channel_measurement(measurement)
            self.readout.setText(
                f"I: {measurement.current_a * 1e3:.8g} mA   "
                f"V: {measurement.voltage_v * 1e3:.8g} mV   P: {measurement.power_w * 1e6:.8g} µW"
                + ("   COMPLIANCE" if measurement.compliance_detected else "")
            )
            self.status.emit("Keithley measurement completed")
        elif operation == "configure":
            channel = self._pending_channels.pop("configure", self.channel.currentText())
            mode = self._pending_config_modes.pop(channel, "measure_only")
            if mode == "measure_only":
                self._configured_channels.discard(channel)
            else:
                self._configured_channels.add(channel)
            self._set_channel_output(channel, False)
            self._armed_until_ui = 0.0
            self._update_arm_status()
            self._update_output_readiness()
            if self._auto_enable_channel == channel:
                self._pending_channels["arm"] = channel
                self.status.emit(f"Keithley CH {channel}: configuration accepted; applying safety unlock")
                self._controller.call("arm", channel)
            else:
                self.status.emit(f"Keithley CH {channel} configured while OUTPUT is OFF")
        elif operation == "arm":
            self._armed_until_ui = float(result) if isinstance(result, (int, float)) else time.monotonic() + 30.0
            self._arm_timer.start()
            self._update_arm_status()
            channel = self._pending_channels.pop("arm", self.channel.currentText())
            if self._auto_enable_channel == channel:
                self._pending_channels["set_output"] = channel
                self.status.emit(f"Keithley CH {channel}: safety checks passed; enabling OUTPUT")
                self._controller.call("set_output", (channel, True))
            else:
                self.status.emit("Keithley output unlocked internally; terminals remain OFF")
        elif operation == "set_output":
            channel = self._pending_channels.pop("set_output", self.channel.currentText())
            self._set_channel_output(channel, True)
            if self._pending_output_signature is not None:
                self._confirmed_output_settings[channel] = self._pending_output_signature
            self._pending_output_signature = None
            self._auto_enable_channel = None
            self._armed_until_ui = 0.0
            self._update_arm_status()
            self.status.emit(f"Keithley CH {channel} OUTPUT ON")
        elif operation == "ramp_to_zero":
            channel = self._pending_channels.pop("ramp_to_zero", self.channel.currentText())
            self._set_channel_output(channel, False)
            self._auto_enable_channel = None
            self._armed_until_ui = 0.0
            self._update_arm_status()
            self.status.emit(f"Keithley CH {channel} ramped to zero; OUTPUT OFF")
        elif operation == "ramp_to_level" and hasattr(result, "final_measurement"):
            channel = self._pending_channels.pop("ramp_to_level", self.channel.currentText())
            self._ramp_pending = False
            self.ramp_execute_button.setText("Ramp to target")
            self.level.setText(self.ramp_target.text())
            self._source_value_cache[(channel, self.mode.currentText())] = (
                self.level.text(),
                self.compliance.text(),
                self.source_range.text(),
            )
            self._update_channel_measurement(result.final_measurement)
            self._set_channel_output(channel, True)
            self.ramp_preview.setText(
                f"Ramp completed: {len(result.levels_si)} point(s), target "
                f"{result.target_si:.6g} SI."
            )
            self.status.emit(f"Keithley CH {channel}: manual ramp completed")

    def _error(self, operation: str, error: str) -> None:
        if operation == "measure":
            self._measure_pending = False
        if operation == "configure":
            channel = self._pending_channels.pop("configure", self.channel.currentText())
            self._pending_config_modes.pop(channel, None)
        if operation in {"arm", "set_output"}:
            self._armed_until_ui = 0.0
            self._update_arm_status()
        if operation == "ramp_to_level":
            channel = self._pending_channels.pop("ramp_to_level", self.channel.currentText())
            self._ramp_pending = False
            self.ramp_execute_button.setText("Ramp to target")
            self._set_channel_output(channel, False)
            self._update_ramp_defaults()
        if operation in {"configure", "arm", "set_output", "ramp_to_zero"}:
            self._auto_enable_channel = None
            self._pending_output_signature = None
            if operation == "ramp_to_zero" and self._output_states[self.channel.currentText()]:
                self.output_toggle.blockSignals(True)
                self.output_toggle.setChecked(True)
                self.output_toggle.blockSignals(False)
                self._style_output_toggle(True)
            else:
                self._reset_output_toggle()
            self._update_output_readiness()
        if operation in {
            "configure",
            "measure",
            "set_output",
            "ramp_to_zero",
            "ramp_to_level",
            "arm",
        }:
            QMessageBox.warning(self, "Keithley", error)


class AnritsuPageState(StrEnum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    STARTING_LIVE = "starting_live"
    LIVE = "live"
    AVERAGING_SIGNAL = "averaging_signal"
    AVERAGING_REFERENCE = "averaging_reference"
    ACQUIRING_REFERENCE = "acquiring_reference"
    CONFIGURING = "configuring"
    STOPPING = "stopping"
    ERROR = "error"


class AnritsuSpectrumConfigurationPanel(QFrame):
    """Shared, hardware-neutral spectrum setup for manual and plan hosts."""

    def __init__(
        self,
        settings: StationSettings,
        parent: QWidget | None = None,
        *,
        plan_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("anritsuSpectrumConfigurationPanel")
        self._settings = settings
        self.plan_mode = plan_mode
        self.limit_fields: dict[str, LimitField] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(7)
        self.frequency_representation = QComboBox()
        self.frequency_representation.addItem("Start / Stop", "start_stop")
        self.frequency_representation.addItem("Center / Span", "center_span")
        self.start = _line("1 MHz")
        self.stop = _line("10 MHz")
        self.reference = _line("0 dBm")
        self.points = QComboBox()
        self.frequency_label_a = QLabel("Start")
        self.frequency_label_b = QLabel("Stop")
        form.addRow("Frequency representation", self.frequency_representation)
        form.addRow(
            self.frequency_label_a, self._bounded("frequency", self.start)
        )
        form.addRow(
            self.frequency_label_b, self._bounded("frequency", self.stop)
        )
        form.addRow(
            "Reference level", self._bounded("reference_level", self.reference)
        )
        form.addRow("Points", self.points)
        layout.addLayout(form)
        if plan_mode:
            note = QLabel(
                "Plan editing is offline. Only selected overrides are stored; "
                "no VISA command is sent to Anritsu."
            )
            note.setObjectName("recipeHint")
            note.setWordWrap(True)
            layout.addWidget(note)
        self.frequency_representation.currentIndexChanged.connect(
            self._change_frequency_representation
        )
        self.set_settings(settings)

    def _limit_values(self, key: str) -> tuple[object, object]:
        value = getattr(self._settings.anritsu.safety, key)
        return value.min, value.max

    def _bounded(self, key: str, editor: QWidget) -> LimitField:
        field = LimitField(editor, *self._limit_values(key))
        field.setProperty("limitKey", key)
        if self.plan_mode:
            field.edit_button.setVisible(False)
        self.limit_fields[key + str(len(self.limit_fields))] = field
        return field

    def set_settings(self, settings: StationSettings) -> None:
        self._settings = settings
        minimum, maximum = self._limit_values("sweep_points")
        current = self.points.currentData()
        self.points.clear()
        for value in ANRITSU_SWEEP_POINT_COUNTS:
            if int(minimum) <= value <= int(maximum):
                self.points.addItem(str(value), value)
        index = self.points.findData(current if current is not None else 1001)
        self.points.setCurrentIndex(index if index >= 0 else 0)
        for field in self.limit_fields.values():
            key = str(field.property("limitKey"))
            field.set_limits(*self._limit_values(key))

    def frequency_bounds(self) -> tuple[float, float]:
        first = parse_quantity(self.start.text(), DIMENSION_FREQUENCY).si_value
        second = parse_quantity(self.stop.text(), DIMENSION_FREQUENCY).si_value
        if self.frequency_representation.currentData() == "center_span":
            if second <= 0:
                raise ConfigurationError("Frequency span must be positive.")
            return first - second / 2, first + second / 2
        return first, second

    def configuration_snapshot(self) -> AnritsuConfigurationSnapshot:
        start_hz, stop_hz = self.frequency_bounds()
        return AnritsuConfigurationSnapshot(
            start_hz=start_hz,
            stop_hz=stop_hz,
            reference_level_dbm=parse_quantity(
                self.reference.text(), DIMENSION_DBM
            ).si_value,
            points=int(self.points.currentData()),
            instrument_mode="PLAN_EDIT" if self.plan_mode else "MANUAL",
        )

    def load_snapshot(self, snapshot: AnritsuConfigurationSnapshot) -> None:
        self.frequency_representation.setCurrentIndex(
            self.frequency_representation.findData("start_stop")
        )
        self.start.setText(
            format_quantity_auto(snapshot.start_hz, DIMENSION_FREQUENCY)
        )
        self.stop.setText(
            format_quantity_auto(snapshot.stop_hz, DIMENSION_FREQUENCY)
        )
        self.reference.setText(f"{snapshot.reference_level_dbm:.9g} dBm")
        index = self.points.findData(snapshot.points)
        if index >= 0:
            self.points.setCurrentIndex(index)

    def _change_frequency_representation(self) -> None:
        try:
            first = parse_quantity(self.start.text(), DIMENSION_FREQUENCY).si_value
            second = parse_quantity(self.stop.text(), DIMENSION_FREQUENCY).si_value
            if self.frequency_representation.currentData() == "center_span":
                self.frequency_label_a.setText("Center")
                self.frequency_label_b.setText("Span")
                self.start.setText(
                    format_quantity_auto((first + second) / 2, DIMENSION_FREQUENCY)
                )
                self.stop.setText(
                    format_quantity_auto(second - first, DIMENSION_FREQUENCY)
                )
            else:
                self.frequency_label_a.setText("Start")
                self.frequency_label_b.setText("Stop")
                self.start.setText(
                    format_quantity_auto(first - second / 2, DIMENSION_FREQUENCY)
                )
                self.stop.setText(
                    format_quantity_auto(first + second / 2, DIMENSION_FREQUENCY)
                )
        except Exception:
            return


class AnritsuAdvancedSpectrumPanel(QFrame):
    """Shared hardware-neutral RBW/VBW/input-path configuration panel."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        hardware_options: tuple[str, ...] = (),
    ) -> None:
        super().__init__(parent)
        self.setObjectName("anritsuAdvancedSpectrumPanel")
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.rbw_mode = QComboBox()
        self.rbw_mode.addItem("Automatic", "auto")
        self.rbw_mode.addItem("Manual", "manual")
        self.rbw = _line("1 kHz")
        form.addRow("RBW mode", self.rbw_mode)
        form.addRow("Resolution bandwidth", self.rbw)

        self.vbw_mode = QComboBox()
        self.vbw_mode.addItem("Automatic", "auto")
        self.vbw_mode.addItem("Manual", "manual")
        self.vbw_mode.addItem("Off", "off")
        self.vbw = _line("1 kHz")
        form.addRow("VBW mode", self.vbw_mode)
        form.addRow("Video bandwidth", self.vbw)

        self.detector = QComboBox()
        form.addRow("Detector", self.detector)
        self.attenuation_mode = QComboBox()
        self.attenuation_mode.addItem("Automatic", "auto")
        self.attenuation_mode.addItem("Manual", "manual")
        self.attenuation = QSpinBox()
        self.attenuation.setRange(0, 60)
        self.attenuation.setSingleStep(2)
        self.attenuation.setSuffix(" dB")
        form.addRow("RF attenuation mode", self.attenuation_mode)
        form.addRow("RF attenuation", self.attenuation)
        self.preamplifier = QCheckBox("Enable preamplifier")
        form.addRow("Input gain", self.preamplifier)

        self.sweep_time_mode = QComboBox()
        self.sweep_time_mode.addItem("Automatic", "auto")
        self.sweep_time_mode.addItem("Manual", "manual")
        self.sweep_time = _line("100 ms")
        form.addRow("Sweep-time mode", self.sweep_time_mode)
        form.addRow("Sweep time", self.sweep_time)

        self.refresh_detector_choices(hardware_options)
        self.set_hardware_options(hardware_options)
        for control in (
            self.rbw_mode,
            self.vbw_mode,
            self.attenuation_mode,
            self.sweep_time_mode,
        ):
            control.currentIndexChanged.connect(self.sync_editors)
        self.sync_editors()

    def refresh_detector_choices(self, options: tuple[str, ...]) -> None:
        current = self.detector.currentData()
        detectors = [
            ("Normal peak", "NORM"),
            ("Positive peak", "POS"),
            ("Sample", "SAMP"),
            ("Negative peak", "NEG"),
            ("RMS", "RMS"),
        ]
        if {"016", "116"}.intersection(options):
            detectors.extend(
                [
                    ("Quasi-peak", "QPE"),
                    ("CISPR average", "CAV"),
                    ("CISPR RMS", "CRMS"),
                ]
            )
        self.detector.clear()
        for label, value in detectors:
            self.detector.addItem(label, value)
        index = self.detector.findData(current or "NORM")
        self.detector.setCurrentIndex(max(index, 0))

    def set_hardware_options(self, options: tuple[str, ...]) -> None:
        self.refresh_detector_choices(options)
        has_preamp = bool(ANRITSU_PREAMPLIFIER_OPTIONS.intersection(options))
        self.preamplifier.setEnabled(has_preamp)
        self.preamplifier.setToolTip(
            "Available because a supported preamplifier option was detected."
            if has_preamp
            else "No supported preamplifier option is currently detected."
        )

    def sync_editors(self, *_args: object) -> None:
        self.rbw.setEnabled(self.rbw_mode.currentData() == "manual")
        self.vbw.setEnabled(self.vbw_mode.currentData() == "manual")
        self.attenuation.setEnabled(
            self.attenuation_mode.currentData() == "manual"
        )
        self.sweep_time.setEnabled(
            self.sweep_time_mode.currentData() == "manual"
        )

    def configuration(self) -> AdvancedSpectrumConfig:
        return AdvancedSpectrumConfig(
            rbw_auto=self.rbw_mode.currentData() == "auto",
            rbw_hz=(
                parse_quantity(self.rbw.text(), DIMENSION_FREQUENCY).si_value
                if self.rbw_mode.currentData() == "manual"
                else None
            ),
            vbw_mode=str(self.vbw_mode.currentData()),
            vbw_hz=(
                parse_quantity(self.vbw.text(), DIMENSION_FREQUENCY).si_value
                if self.vbw_mode.currentData() == "manual"
                else None
            ),
            detector=str(self.detector.currentData()),
            attenuation_auto=self.attenuation_mode.currentData() == "auto",
            attenuation_db=(
                float(self.attenuation.value())
                if self.attenuation_mode.currentData() == "manual"
                else None
            ),
            preamplifier_enabled=self.preamplifier.isChecked(),
            sweep_time_auto=self.sweep_time_mode.currentData() == "auto",
            sweep_time_s=(
                parse_quantity(self.sweep_time.text(), DIMENSION_TIME).si_value
                if self.sweep_time_mode.currentData() == "manual"
                else None
            ),
        )

    def load_snapshot(self, snapshot: AdvancedSpectrumSnapshot) -> None:
        for combo, value in (
            (self.rbw_mode, "auto" if snapshot.rbw_auto else "manual"),
            (self.vbw_mode, snapshot.vbw_mode),
            (
                self.attenuation_mode,
                "auto" if snapshot.attenuation_auto else "manual",
            ),
            (
                self.sweep_time_mode,
                "auto" if snapshot.sweep_time_auto else "manual",
            ),
        ):
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)
        self.rbw.setText(format_quantity_auto(snapshot.rbw_hz, DIMENSION_FREQUENCY))
        if snapshot.vbw_hz is not None:
            self.vbw.setText(
                format_quantity_auto(snapshot.vbw_hz, DIMENSION_FREQUENCY)
            )
        detector_index = self.detector.findData(snapshot.detector)
        if detector_index >= 0:
            self.detector.setCurrentIndex(detector_index)
        self.attenuation.setValue(round(snapshot.attenuation_db))
        self.preamplifier.setChecked(snapshot.preamplifier_enabled)
        self.sweep_time.setText(
            format_quantity_auto(snapshot.sweep_time_s, DIMENSION_TIME)
        )
        self.sync_editors()


class AnritsuPage(QWidget):
    status = Signal(str)

    def __init__(
        self,
        controller: DeviceController,
        settings: StationSettings,
        *,
        single_sweep_available: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._station_settings = settings
        self._limit_fields: dict[str, LimitField] = {}
        self._single_sweep_configured = single_sweep_available
        self._trace_supported = True
        self._fetch_pending = False
        self._live_transition_pending = False
        self._latest_trace: SpectrumTrace | None = None
        self._averaged_trace: SpectrumTrace | None = None
        self._reference_trace: SpectrumTrace | None = None
        self._reference_spectrum: ReferenceSpectrum | None = None
        self._pending_reference_kind: str | None = None
        self._device_idn = ""
        self._last_configuration: AnritsuConfigurationSnapshot | None = None
        self._last_advanced_configuration: AdvancedSpectrumSnapshot | None = None
        self._page_state = AnritsuPageState.IDLE
        self._capabilities: object | None = None
        self._averager = LinearPowerAverager()
        self._averaging_active = False
        self._averaging_destination: str | None = None
        self._resume_live_after_averaging = False
        self._live_frame_count = 0
        self._fetch_started_monotonic: float | None = None
        self._last_frame_monotonic: float | None = None
        self._frame_intervals_s: list[float] = []
        self._transfer_durations_s: list[float] = []
        self._stale_frame_count = 0
        self._coalesced_timer_ticks = 0
        self._identical_live_frames = 0
        self._last_live_signature: int | None = None
        self._reconnect_pending = False
        self._sg_supported = False
        self._sg_output_enabled = False
        self._sg_armed = False
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.fetch_live)
        layout = QVBoxLayout(self)
        self.banner = NotificationBanner()
        layout.addWidget(self.banner)
        title_row = QHBoxLayout()
        title = QLabel("Anritsu MS2830A — Spectrum / Live")
        title.setObjectName("pageTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.live_indicator = QLabel("●  LIVE OFF")
        self.live_indicator.setObjectName("anritsuLiveIndicator")
        self.live_indicator.setProperty("liveState", "off")
        self.live_indicator.setToolTip(
            "Confirmed Live acquisition state. The indicator changes to ON only after the "
            "instrument accepts Live startup."
        )
        title_row.addWidget(self.live_indicator)
        layout.addLayout(title_row)
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setObjectName("anritsuWorkspaceSplitter")
        left_panel = QWidget()
        left_panel.setObjectName("anritsuControlPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(10)
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(6)
        setup_header = QHBoxLayout()
        setup_title = QLabel("Acquisition setup")
        setup_title.setObjectName("sectionTitle")
        setup_header.addWidget(setup_title)
        setup_header.addStretch(1)
        self.advanced_spectrum_button = QPushButton("Advanced…")
        self.advanced_spectrum_button.setToolTip(
            "Open qualified RBW, VBW, detector, attenuation, preamplifier and sweep-time controls."
        )
        self.advanced_spectrum_button.clicked.connect(self._show_advanced_spectrum_dialog)
        setup_header.addWidget(self.advanced_spectrum_button)
        self.hardware_info_button = QPushButton("ⓘ")
        self.hardware_info_button.setObjectName("infoButton")
        self.hardware_info_button.setFixedSize(28, 28)
        self.hardware_info_button.setToolTip(
            "Show detected Anritsu hardware options and documented operating limits."
        )
        self.hardware_info_button.clicked.connect(self._show_anritsu_hardware_info)
        setup_header.addWidget(self.hardware_info_button)
        self._advanced_dialog = self._build_advanced_spectrum_dialog()
        left_layout.addLayout(setup_header)
        self.configuration_panel = AnritsuSpectrumConfigurationPanel(
            settings, self
        )
        self.start = self.configuration_panel.start
        self.stop = self.configuration_panel.stop
        self.frequency_representation = (
            self.configuration_panel.frequency_representation
        )
        self.reference = self.configuration_panel.reference
        self.points = self.configuration_panel.points
        self.frequency_label_a = self.configuration_panel.frequency_label_a
        self.frequency_label_b = self.configuration_panel.frequency_label_b
        self._limit_fields = self.configuration_panel.limit_fields
        left_layout.addWidget(self.configuration_panel)
        self.refresh = QSpinBox()
        self.refresh.setRange(10, 5000)
        self.refresh.setValue(500)
        self.refresh.setSuffix(" ms")
        self.refresh.setToolTip(
            "Requested Live polling interval: 10 ms to 5 s. The effective frame rate is "
            "limited by the analyser sweep, VISA transfer and complete TRAC1 processing."
        )
        refresh_form = QFormLayout()
        refresh_form.addRow("Live refresh interval", self.refresh)
        left_layout.addLayout(refresh_form)
        self.hardware_option_info = QLabel()
        self.hardware_range_info = QLabel()
        self.hardware_option_info.hide()
        self.hardware_range_info.hide()
        self._hardware_details_text = ""
        self._update_anritsu_hardware_limits(())
        controls = QGridLayout()
        controls.setSpacing(6)
        self.read_configuration = QPushButton("Read from instrument")
        self.configure_button = QPushButton("Apply configuration")
        self.single = QPushButton("Read current spectrum")
        self.live = QPushButton("Start Live")
        self.abort_button = QPushButton("Abort acquisition")
        self.configure_button.setObjectName("primaryButton")
        self.abort_button.setObjectName("warningButton")
        for button in (
            self.read_configuration,
            self.configure_button,
            self.single,
            self.live,
            self.abort_button,
        ):
            button.setProperty("compact", True)
        controls.addWidget(self.read_configuration, 0, 0, 1, 2)
        controls.addWidget(self.configure_button, 1, 0)
        controls.addWidget(self.single, 1, 1)
        controls.addWidget(self.live, 2, 0)
        controls.addWidget(self.abort_button, 2, 1)
        left_layout.addLayout(controls)
        processing = QFrame()
        processing.setObjectName("anritsuProcessingCard")
        processing_layout = QGridLayout(processing)
        processing_title = QLabel("Averaging and reference processing")
        processing_title.setObjectName("sectionTitle")
        processing_layout.setHorizontalSpacing(6)
        processing_layout.setVerticalSpacing(7)
        processing_layout.addWidget(processing_title, 0, 0, 1, 2)
        self.average_count = QSpinBox()
        self.average_count.setRange(1, 9999)
        self.average_count.setValue(self._station_settings.anritsu.acquisition.application_average_count)
        self.acquire_average = QPushButton("Acquire averaged spectrum")
        self.cancel_average = QPushButton("Cancel averaging")
        self.acquire_average.setProperty("compact", True)
        self.cancel_average.setProperty("compact", True)
        self.cancel_average.setEnabled(False)
        self.average_progress = QProgressBar()
        initial_average_count = self.average_count.value()
        self.average_progress.setRange(0, initial_average_count)
        self.average_progress.setValue(0)
        self.average_progress.setFormat(f"0 / {initial_average_count}")
        processing_layout.addWidget(QLabel("Average count"), 1, 0)
        processing_layout.addWidget(self.average_count, 1, 1)
        processing_layout.addWidget(self.acquire_average, 2, 0)
        processing_layout.addWidget(self.cancel_average, 2, 1)
        processing_layout.addWidget(self.average_progress, 3, 0, 1, 2)
        reference_title = QLabel("Reference")
        reference_title.setObjectName("subsectionTitle")
        processing_layout.addWidget(reference_title, 4, 0, 1, 2)
        self.reference_status = QLabel("No reference")
        self.reference_status.setObjectName("muted")
        self.reference_status.setWordWrap(True)
        processing_layout.addWidget(self.reference_status, 5, 0, 1, 2)
        self.acquire_single_reference = QPushButton("Acquire 1× reference")
        self.use_current_reference = QPushButton("Use current trace")
        self.capture_reference = QPushButton("Acquire N× reference")
        self.clear_reference = QPushButton("Clear reference")
        self.load_reference = QPushButton("Load reference…")
        self.save_reference = QPushButton("Save reference…")
        for button in (
            self.acquire_single_reference,
            self.use_current_reference,
            self.capture_reference,
            self.clear_reference,
            self.load_reference,
            self.save_reference,
        ):
            button.setProperty("compact", True)
        self.use_current_reference.setEnabled(False)
        self.clear_reference.setEnabled(False)
        self.reference_operation = QComboBox()
        self.reference_operation.addItem("No processing", "none")
        self.reference_operation.addItem("Signal − reference [dB]", "difference_db")
        self.reference_operation.addItem("Signal ÷ reference [linear ratio]", "ratio_linear")
        self.reference_operation.addItem("Signal + reference [linear power]", "add_power")
        self.reference_operation.addItem("Signal − reference [linear power]", "subtract_power")
        self.reference_operation.addItem("Signal × reference [linear mW²]", "multiply_linear")
        processing_layout.addWidget(self.acquire_single_reference, 6, 0)
        processing_layout.addWidget(self.use_current_reference, 6, 1)
        processing_layout.addWidget(self.capture_reference, 7, 0)
        processing_layout.addWidget(self.clear_reference, 7, 1)
        processing_layout.addWidget(self.load_reference, 8, 0)
        processing_layout.addWidget(self.save_reference, 8, 1)
        processing_layout.addWidget(QLabel("Reference operation"), 9, 0)
        processing_layout.addWidget(self.reference_operation, 9, 1)
        self.show_raw = QCheckBox("Raw")
        self.show_raw.setChecked(True)
        self.show_average = QCheckBox("Averaged")
        self.show_reference = QCheckBox("Reference")
        self.show_processed = QCheckBox("Processed")
        trace_toggles = QHBoxLayout()
        trace_toggles.setSpacing(10)
        for checkbox in (self.show_raw, self.show_average, self.show_reference, self.show_processed):
            trace_toggles.addWidget(checkbox)
        trace_toggles.addStretch(1)
        processing_layout.addLayout(trace_toggles, 10, 0, 1, 2)
        left_layout.addWidget(processing)
        left_layout.addStretch(1)
        self.spectrum_plot = SpectrumPlotWidget(legend=True)
        self.spectrum_plot.set_title("Current spectrum")
        self.spectrum_plot.set_labels(
            x="Frequency", x_unit="Hz", y="Amplitude", y_unit="dBm"
        )
        self.spectrum_plot.setMinimumHeight(300)
        self.spectrum_plot.status_changed.connect(self.status.emit)
        right_layout.addWidget(self.spectrum_plot, 1)
        self.info = QLabel("Live stopped. Each frame is a complete trace, not a push stream.")
        self.info.setObjectName("muted")
        right_layout.addWidget(self.info)
        left_scroll = QScrollArea()
        left_scroll.setObjectName("anritsuControlScroll")
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setWidget(left_panel)
        left_scroll.setMinimumWidth(320)
        self.workspace_splitter.addWidget(left_scroll)
        self.workspace_splitter.addWidget(right_panel)
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setSizes([680, 1100])
        self.mode_tabs = QTabWidget()
        self.mode_tabs.setObjectName("anritsuModeTabs")
        spectrum_tab = QWidget()
        spectrum_tab_layout = QVBoxLayout(spectrum_tab)
        spectrum_tab_layout.setContentsMargins(0, 0, 0, 0)
        spectrum_tab_layout.addWidget(self.workspace_splitter)
        self.mode_tabs.addTab(spectrum_tab, "Spectrum analyser")
        self.signal_generator_tab = self._build_signal_generator_tab()
        self.signal_generator_tab_index = self.mode_tabs.addTab(
            self.signal_generator_tab, "Signal generator"
        )
        self.mode_tabs.setTabVisible(self.signal_generator_tab_index, False)
        layout.addWidget(self.mode_tabs, 1)
        self.read_configuration.clicked.connect(self.read_configuration_from_instrument)
        self.configure_button.clicked.connect(self.configure)
        self.single.clicked.connect(self.read_once)
        self.live.clicked.connect(self.toggle_live)
        self.abort_button.clicked.connect(lambda: self._controller.call("emergency_off"))
        self.acquire_average.clicked.connect(self.start_averaging)
        self.cancel_average.clicked.connect(self.cancel_averaging)
        self.acquire_single_reference.clicked.connect(self.acquire_reference_once)
        self.use_current_reference.clicked.connect(self.capture_current_reference)
        self.capture_reference.clicked.connect(self.start_reference_averaging)
        self.clear_reference.clicked.connect(self.remove_reference)
        self.load_reference.clicked.connect(self.load_reference_file)
        self.save_reference.clicked.connect(self.save_reference_file)
        self.reference_operation.currentIndexChanged.connect(self._refresh_spectrum_display)
        for checkbox in (self.show_raw, self.show_average, self.show_reference, self.show_processed):
            checkbox.toggled.connect(self._refresh_spectrum_display)
        controller.result.connect(self._result)
        controller.error.connect(self._error)
        controller.state_changed.connect(self._device_state_changed)
        help_items = {
            self.read_configuration: "Read Start, Stop, Reference level, and Points from the connected analyser. This sends query commands only and never changes the instrument or approved safety limits.",
            self.single: "Read the currently displayed TRAC1 spectrum using SCPI queries only. This does not configure or trigger the analyser and does not require an approved safety profile.",
            self.average_count: "Number of complete spectra to average. 200 is common in the Thatec workflow. Averaging is performed in linear mW, not directly in dBm.",
            self.acquire_average: "Passively read N traces at the Live refresh interval and average power in linear mW. No analyser setting or trigger mode is changed.",
            self.cancel_average: "Stop temporal averaging. Already collected temporary frames are discarded; completed raw/reference data are unchanged.",
            self.acquire_single_reference: "Passively fetch one new TRAC1 frame and store that completed frame as the reference. No analyser setting is changed.",
            self.use_current_reference: "Use the latest already acquired trace as the reference without sending a VISA command.",
            self.capture_reference: "Passively acquire and average N traces, then store that completed average as the in-memory reference spectrum.",
            self.clear_reference: "Remove the in-memory reference and all derived display results. It does not delete raw measurements from HDF5.",
            self.load_reference: "Load a Lab Control reference HDF5 artefact. The current analyser is not queried or configured.",
            self.save_reference: "Save the complete reference trace and provenance as a thaTEC/PyThat-compatible HDF5 artefact.",
            self.reference_operation: "Choose point-wise reference mathematics. Difference in dB equals a power ratio expressed logarithmically; linear operations first convert dBm to mW.",
            self.show_raw: "Show the latest untouched trace returned by Anritsu.",
            self.show_average: "Show the application-side linear-power average.",
            self.show_reference: "Overlay the captured reference spectrum.",
            self.show_processed: "Show the selected reference operation result. Non-dBm results use their own Y-axis unit and hide incompatible overlays.",
        }
        for widget, description in help_items.items():
            widget.setToolTip(description)
            widget.setToolTipDuration(25_000)
        self._apply_page_state()

    def _build_signal_generator_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(18, 14, 18, 14)
        heading = QLabel("Optional vector signal generator")
        heading.setObjectName("sectionTitle")
        outer.addWidget(heading)
        explanation = QLabel(
            "This panel is shown only when *OPT? reports option 020/120/021/121. "
            "Configuration explicitly enters SG mode and proves RF OUTPUT OFF. "
            "RF ON additionally requires a qualified protocol, approved limits, profile approval "
            "and a fresh one-shot ARM."
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("muted")
        outer.addWidget(explanation)

        card = QFrame()
        card.setObjectName("anritsuProcessingCard")
        grid = QGridLayout(card)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(9)
        self.sg_status = QLabel("●  RF OUTPUT UNKNOWN")
        self.sg_status.setObjectName("anritsuSgIndicator")
        self.sg_status.setProperty("liveState", "off")
        grid.addWidget(self.sg_status, 0, 0, 1, 4)
        generator = self._station_settings.anritsu.signal_generator
        default_frequency = generator.frequency.min or "1 GHz"
        default_power = generator.power.min or "-30 dBm"
        self.sg_frequency = QLineEdit(str(default_frequency))
        self.sg_power = QLineEdit(str(default_power))
        grid.addWidget(QLabel("Frequency"), 1, 0)
        grid.addWidget(self.sg_frequency, 1, 1, 1, 3)
        grid.addWidget(QLabel("RF power"), 2, 0)
        grid.addWidget(self.sg_power, 2, 1, 1, 3)
        self.sg_read = QPushButton("Read current SG state")
        self.sg_configure = QPushButton("Configure while RF OFF")
        self.sg_arm = QPushButton("ARM RF output")
        self.sg_on = QPushButton("RF OUTPUT ON")
        self.sg_off = QPushButton("RF OUTPUT OFF")
        self.sg_configure.setObjectName("primaryButton")
        self.sg_on.setObjectName("outputOnButton")
        self.sg_off.setObjectName("outputOffButton")
        for button in (
            self.sg_read,
            self.sg_configure,
            self.sg_arm,
            self.sg_on,
            self.sg_off,
        ):
            button.setProperty("compact", True)
        grid.addWidget(self.sg_read, 3, 0, 1, 2)
        grid.addWidget(self.sg_configure, 3, 2, 1, 2)
        grid.addWidget(self.sg_arm, 4, 0)
        grid.addWidget(self.sg_on, 4, 1)
        grid.addWidget(self.sg_off, 4, 2, 1, 2)
        self.sg_limits = QLabel()
        self.sg_limits.setWordWrap(True)
        self.sg_limits.setObjectName("muted")
        grid.addWidget(self.sg_limits, 5, 0, 1, 4)
        outer.addWidget(card)
        outer.addStretch(1)
        self.sg_read.clicked.connect(
            lambda: self._controller.call("read_signal_generator")
        )
        self.sg_configure.clicked.connect(self.configure_signal_generator)
        self.sg_arm.clicked.connect(self.arm_signal_generator)
        self.sg_on.clicked.connect(self.enable_signal_generator)
        self.sg_off.clicked.connect(
            lambda: self._controller.call("set_signal_generator_output", False)
        )
        self._update_signal_generator_limits()
        return tab

    def _build_advanced_spectrum_dialog(self) -> QDialog:
        dialog = QDialog(self)
        dialog.setWindowTitle("Anritsu advanced Spectrum settings")
        dialog.setModal(False)
        dialog.resize(620, 470)
        layout = QVBoxLayout(dialog)
        explanation = QLabel(
            "These controls change bandwidth, detector and the RF input path. Readback is "
            "always available as an explicit diagnostic action. Apply remains locked until "
            "the exact firmware is qualified in the station profile."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.advanced_protocol_status = QLabel()
        self.advanced_protocol_status.setWordWrap(True)
        layout.addWidget(self.advanced_protocol_status)

        self.advanced_configuration_panel = AnritsuAdvancedSpectrumPanel(dialog)
        self.advanced_rbw_mode = self.advanced_configuration_panel.rbw_mode
        self.advanced_rbw = self.advanced_configuration_panel.rbw
        self.advanced_vbw_mode = self.advanced_configuration_panel.vbw_mode
        self.advanced_vbw = self.advanced_configuration_panel.vbw
        self.advanced_detector = self.advanced_configuration_panel.detector
        self.advanced_attenuation_mode = (
            self.advanced_configuration_panel.attenuation_mode
        )
        self.advanced_attenuation = self.advanced_configuration_panel.attenuation
        self.advanced_preamplifier = self.advanced_configuration_panel.preamplifier
        self.advanced_sweep_mode = (
            self.advanced_configuration_panel.sweep_time_mode
        )
        self.advanced_sweep_time = self.advanced_configuration_panel.sweep_time
        layout.addWidget(self.advanced_configuration_panel)

        help_text = QLabel(
            "Documented limits: RBW 1 Hz–31.25 MHz; VBW 1 Hz–10 MHz or Off; "
            "attenuation 0–60 dB in 2 dB steps; frequency-domain sweep 1 ms–1000 s. "
            "Automatic attenuation is blocked when the safety profile defines a minimum."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        actions = QHBoxLayout()
        self.advanced_read_button = QPushButton("Read from instrument")
        self.advanced_apply_button = QPushButton("Apply and verify")
        self.advanced_apply_button.setObjectName("primaryButton")
        close_button = QPushButton("Close")
        actions.addWidget(self.advanced_read_button)
        actions.addWidget(self.advanced_apply_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        layout.addLayout(actions)
        self.advanced_read_button.clicked.connect(self.read_advanced_spectrum)
        self.advanced_apply_button.clicked.connect(self.configure_advanced_spectrum)
        close_button.clicked.connect(dialog.hide)
        self._sync_advanced_editors()
        self._update_advanced_availability()
        return dialog

    def _refresh_advanced_detector_choices(self, options: tuple[str, ...]) -> None:
        if hasattr(self, "advanced_configuration_panel"):
            self.advanced_configuration_panel.set_hardware_options(options)

    def _sync_advanced_editors(self) -> None:
        self.advanced_configuration_panel.sync_editors()

    def _advanced_firmware_qualified(self) -> bool:
        protocol = self._station_settings.anritsu.advanced_spectrum
        firmware = str(getattr(self._capabilities, "firmware", "") or "")
        return (
            protocol.control_protocol == "standard_scpi"
            and firmware in protocol.qualified_firmware
        )

    def _update_advanced_availability(self) -> None:
        if not hasattr(self, "advanced_protocol_status"):
            return
        protocol = self._station_settings.anritsu.advanced_spectrum
        firmware = str(getattr(self._capabilities, "firmware", "") or "unknown")
        qualified = self._advanced_firmware_qualified()
        if qualified:
            text = f"Qualified standard SCPI control for firmware {firmware}."
        else:
            versions = ", ".join(protocol.qualified_firmware) or "none"
            text = (
                f"WRITE LOCKED — protocol={protocol.control_protocol}, connected firmware={firmware}, "
                f"qualified firmware={versions}. Read-only queries remain available."
            )
        self.advanced_protocol_status.setText(text)
        connected = self._page_state != AnritsuPageState.DISCONNECTED
        idle = self._page_state in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}
        self.advanced_read_button.setEnabled(connected and idle)
        self.advanced_apply_button.setEnabled(connected and idle and qualified)

    def _show_advanced_spectrum_dialog(self) -> None:
        self._update_advanced_availability()
        self._advanced_dialog.show()
        self._advanced_dialog.raise_()
        self._advanced_dialog.activateWindow()

    def read_advanced_spectrum(self) -> None:
        self._set_page_state(AnritsuPageState.CONFIGURING)
        self.status.emit("Anritsu advanced Spectrum readback requested")
        self._controller.call("read_advanced_spectrum")

    def configure_advanced_spectrum(self) -> None:
        try:
            config = self.advanced_configuration_panel.configuration()
        except Exception as exc:
            self.banner.show_message(f"Invalid advanced Spectrum settings: {exc}", severity="error")
            return
        if config.preamplifier_enabled:
            answer = QMessageBox.warning(
                self,
                "Enable Anritsu preamplifier",
                "The preamplifier changes the RF input path and may overload at high input power. "
                "Confirm that the approved expected input and attenuation are correct.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Ok:
                return
        self._set_page_state(AnritsuPageState.CONFIGURING)
        self._controller.call("configure_advanced_spectrum", config)

    def _show_advanced_snapshot(self, snapshot: AdvancedSpectrumSnapshot) -> None:
        self._last_advanced_configuration = snapshot
        self.advanced_configuration_panel.load_snapshot(snapshot)

    def _anritsu_limit_values(self, key: str) -> tuple[object, object]:
        safety = self._station_settings.anritsu.safety
        value = getattr(safety, key)
        return value.min, value.max

    def _spectrum_frequency_bounds(self) -> tuple[float, float]:
        first = parse_quantity(self.start.text(), DIMENSION_FREQUENCY).si_value
        second = parse_quantity(self.stop.text(), DIMENSION_FREQUENCY).si_value
        if self.frequency_representation.currentData() == "center_span":
            center, span = first, second
            if not math.isfinite(span) or span <= 0:
                raise ValueError("Frequency span must be finite and positive.")
            return center - span / 2, center + span / 2
        return first, second

    def _set_frequency_bounds(self, start_hz: float, stop_hz: float) -> None:
        if self.frequency_representation.currentData() == "center_span":
            self.start.setText(
                format_quantity_auto((start_hz + stop_hz) / 2, DIMENSION_FREQUENCY)
            )
            self.stop.setText(
                format_quantity_auto(stop_hz - start_hz, DIMENSION_FREQUENCY)
            )
        else:
            self.start.setText(format_quantity_auto(start_hz, DIMENSION_FREQUENCY))
            self.stop.setText(format_quantity_auto(stop_hz, DIMENSION_FREQUENCY))

    def _change_frequency_representation(self) -> None:
        try:
            if self.frequency_representation.currentData() == "center_span":
                start_hz = parse_quantity(
                    self.start.text(), DIMENSION_FREQUENCY
                ).si_value
                stop_hz = parse_quantity(
                    self.stop.text(), DIMENSION_FREQUENCY
                ).si_value
                self.frequency_label_a.setText("Center")
                self.frequency_label_b.setText("Span")
                self.start.setText(
                    format_quantity_auto((start_hz + stop_hz) / 2, DIMENSION_FREQUENCY)
                )
                self.stop.setText(
                    format_quantity_auto(stop_hz - start_hz, DIMENSION_FREQUENCY)
                )
            else:
                center_hz = parse_quantity(
                    self.start.text(), DIMENSION_FREQUENCY
                ).si_value
                span_hz = parse_quantity(
                    self.stop.text(), DIMENSION_FREQUENCY
                ).si_value
                self.frequency_label_a.setText("Start")
                self.frequency_label_b.setText("Stop")
                self.start.setText(
                    format_quantity_auto(center_hz - span_hz / 2, DIMENSION_FREQUENCY)
                )
                self.stop.setText(
                    format_quantity_auto(center_hz + span_hz / 2, DIMENSION_FREQUENCY)
                )
        except Exception as exc:
            self.banner.show_message(
                f"Cannot change frequency representation: {exc}", severity="error"
            )

    def _refresh_point_choices(self, preferred: int | None = None) -> None:
        minimum, maximum = self._anritsu_limit_values("sweep_points")
        current = preferred if preferred is not None else self.points.currentData()
        self.points.clear()
        for value in ANRITSU_SWEEP_POINT_COUNTS:
            if int(minimum) <= value <= int(maximum):
                self.points.addItem(str(value), value)
        index = self.points.findData(current)
        self.points.setCurrentIndex(index if index >= 0 else 0)

    def _anritsu_bounded(self, key: str, editor: QWidget) -> LimitField:
        field = LimitField(editor, *self._anritsu_limit_values(key))
        self._limit_fields[key + str(len(self._limit_fields))] = field
        field.setProperty("limitKey", key)
        return field

    def set_settings(self, settings: StationSettings) -> None:
        self._station_settings = settings
        self.configuration_panel.set_settings(settings)
        self._update_signal_generator_limits()
        self._update_advanced_availability()
        self._apply_page_state()

    def set_capabilities(self, capabilities: object) -> None:
        supports = getattr(capabilities, "supports", lambda _feature: False)
        self._capabilities = capabilities
        self._trace_supported = bool(supports("spectrum_trace"))
        self._sg_supported = bool(supports("signal_generator"))
        options = tuple(getattr(capabilities, "hardware_options", ()) or ())
        self._update_anritsu_hardware_limits(options)
        self._refresh_advanced_detector_choices(options)
        self.advanced_preamplifier.setEnabled(
            bool(ANRITSU_PREAMPLIFIER_OPTIONS.intersection(options))
        )
        self.mode_tabs.setTabVisible(self.signal_generator_tab_index, self._sg_supported)
        if not self._sg_supported and self.mode_tabs.currentIndex() == self.signal_generator_tab_index:
            self.mode_tabs.setCurrentIndex(0)
        self._update_advanced_availability()
        self._apply_page_state()

    def _device_state_changed(self, state: str) -> None:
        if state == "disconnected":
            self._sg_armed = False
            self._sg_output_enabled = False
            self._last_advanced_configuration = None
            self.sg_status.setText("●  RF OUTPUT UNKNOWN")
            self.sg_status.setProperty("liveState", "off")
            self._set_page_state(AnritsuPageState.DISCONNECTED)
        elif state in {"fault", "unknown"}:
            self._set_page_state(AnritsuPageState.ERROR)
        elif self._page_state in {
            AnritsuPageState.DISCONNECTED,
            AnritsuPageState.ERROR,
        }:
            self._set_page_state(AnritsuPageState.IDLE)

    def _set_page_state(self, state: AnritsuPageState) -> None:
        self._page_state = state
        self._apply_page_state()

    def _apply_page_state(self) -> None:
        idle = self._page_state in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}
        live = self._page_state == AnritsuPageState.LIVE
        averaging = self._page_state in {
            AnritsuPageState.AVERAGING_SIGNAL,
            AnritsuPageState.AVERAGING_REFERENCE,
        }
        connected = self._page_state != AnritsuPageState.DISCONNECTED
        self.read_configuration.setEnabled(idle)
        self.configure_button.setEnabled(idle)
        self.single.setEnabled(idle and self._trace_supported)
        self.live.setEnabled((idle or live) and connected and not self._live_transition_pending)
        self.abort_button.setEnabled(connected)
        self.advanced_spectrum_button.setEnabled(connected and idle)
        self.average_count.setEnabled(idle and not averaging)
        self.acquire_average.setEnabled(idle and self._trace_supported)
        self.acquire_single_reference.setEnabled(idle and self._trace_supported)
        self.use_current_reference.setEnabled(idle and self._latest_trace is not None)
        self.capture_reference.setEnabled(idle and self._trace_supported)
        self.load_reference.setEnabled(idle)
        self.save_reference.setEnabled(idle and self._reference_spectrum is not None)
        self.cancel_average.setEnabled(averaging)
        self.clear_reference.setEnabled(
            self._reference_spectrum is not None
            and self._page_state not in {
                AnritsuPageState.STARTING_LIVE,
                AnritsuPageState.STOPPING,
                AnritsuPageState.ACQUIRING_REFERENCE,
            }
        )
        protocol_qualified = (
            self._station_settings.anritsu.signal_generator.control_protocol
            == "basic_scpi"
        )
        self.sg_read.setEnabled(connected and idle and self._sg_supported)
        self.sg_configure.setEnabled(
            connected and idle and self._sg_supported and protocol_qualified
        )
        self.sg_arm.setEnabled(
            connected
            and idle
            and self._sg_supported
            and protocol_qualified
            and not self._sg_output_enabled
        )
        self.sg_on.setEnabled(
            connected
            and idle
            and self._sg_supported
            and protocol_qualified
            and self._sg_armed
            and not self._sg_output_enabled
        )
        self.sg_off.setEnabled(connected and self._sg_supported)
        self._update_advanced_availability()

    def _update_signal_generator_limits(self) -> None:
        generator = self._station_settings.anritsu.signal_generator
        protocol = generator.control_protocol
        frequency = (
            f"{generator.frequency.min} … {generator.frequency.max}"
            if generator.frequency.min is not None
            else "not defined"
        )
        power = (
            f"{generator.power.min} … {generator.power.max}"
            if generator.power.min is not None
            else "not defined"
        )
        permission = self._station_settings.anritsu.safety.signal_generator_output_allowed
        self.sg_limits.setText(
            f"Protocol: {protocol} | Approved frequency: {frequency} | Approved RF power: "
            f"{power} | RF output permission: {'enabled' if permission else 'disabled'}"
        )

    def configure_signal_generator(self) -> None:
        try:
            config = SignalGeneratorConfig(
                frequency_hz=parse_quantity(
                    self.sg_frequency.text(), DIMENSION_FREQUENCY
                ).si_value,
                power_dbm=parse_quantity(self.sg_power.text(), DIMENSION_DBM).si_value,
            )
        except Exception as exc:
            self.banner.show_message(f"Invalid signal-generator settings: {exc}")
            return
        self._sg_armed = False
        self._controller.call("configure_signal_generator", config)

    def arm_signal_generator(self) -> None:
        answer = QMessageBox.warning(
            self,
            "ARM Anritsu RF output",
            "ARM permits one RF OUTPUT ON action for a short time. Confirm the RF cable, "
            "dummy load/DUT power rating, attenuation and emergency stop before continuing.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Ok:
            self._controller.call("arm_signal_generator")

    def enable_signal_generator(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Enable Anritsu RF output?",
            f"Enable RF at {self.sg_frequency.text()} and {self.sg_power.text()}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._controller.call("set_signal_generator_output", True)

    def _show_signal_generator_snapshot(self, result: SignalGeneratorSnapshot) -> None:
        self.sg_frequency.setText(
            format_quantity_auto(result.frequency_hz, DIMENSION_FREQUENCY)
        )
        self.sg_power.setText(f"{result.power_dbm:.9g} dBm")
        self._sg_output_enabled = result.output_enabled
        self._sg_armed = False
        state = "on" if result.output_enabled else "off"
        self.sg_status.setText(
            "●  RF OUTPUT ON" if result.output_enabled else "●  RF OUTPUT OFF"
        )
        self.sg_status.setProperty("liveState", state)
        self.sg_status.style().unpolish(self.sg_status)
        self.sg_status.style().polish(self.sg_status)
        self._apply_page_state()

    def signal_generator_snapshot(self) -> SignalGeneratorSnapshot:
        """Expose the manual SG form as a read-only seed for the sweep editor."""

        return SignalGeneratorSnapshot(
            frequency_hz=parse_quantity(
                self.sg_frequency.text(), DIMENSION_FREQUENCY
            ).si_value,
            power_dbm=parse_quantity(self.sg_power.text(), DIMENSION_DBM).si_value,
            output_enabled=self._sg_output_enabled,
            instrument_mode="PLAN_EDIT",
        )

    def _update_anritsu_hardware_limits(self, options: tuple[str, ...]) -> None:
        frequency_option = frequency_option_for(options)
        if options:
            option_text = ", ".join(options)
            preamplifier = (
                "installed option detected"
                if ANRITSU_PREAMPLIFIER_OPTIONS.intersection(options)
                else "no preamplifier option reported"
            )
            self.hardware_option_info.setText(
                f"Auto-detected by *OPT?: {option_text} | Preamplifier: {preamplifier}."
            )
        else:
            self.hardware_option_info.setText(
                "Hardware options: waiting for connection, or *OPT? was not supported/reported."
            )
        if frequency_option is None:
            frequency_text = (
                "Frequency: option dependent (040: 3.7 GHz, 041: 6.1 GHz, "
                "043: 13.6 GHz, 044: 26.6 GHz, 045: 43.1 GHz)."
            )
            default_sweep_text = "option-dependent"
        else:
            frequency_text = (
                f"Frequency option {frequency_option.code}: documented displayed range "
                f"-100 MHz to {frequency_option.maximum_stop_hz / 1e9:g} GHz."
            )
            default_sweep_text = f"{frequency_option.default_sweep_time_s * 1e3:g} ms"
        self.hardware_range_info.setText(
            f"{frequency_text}\n"
            "Reference level: -120 to +50 dBm (0.01 dB resolution) | "
            "RF attenuation: 0 to 60 dB (2 dB steps).\n"
            "RBW: 1 Hz to 31.25 MHz | VBW: 1 Hz to 10 MHz or OFF | "
            "Input impedance: 50 or 75 ohm.\n"
            f"Sweep time: 1 ms to 1000 s in frequency mode; default for this option: "
            f"{default_sweep_text}. Zero Span: 1 us to 1000 s.\n"
            "Trace points: 11, 21, 41, 51, 101, 201, 251, 401, 501, 1001, 2001, "
            "5001, 10001 | Device averaging: 2 to 9999.\n"
            "Application polling: 10 ms to 5 s | Application averaging: 1 to 9999. "
            "Approved safety badges above may intentionally be stricter."
        )
        self._hardware_details_text = (
            "Detected hardware options\n"
            f"{self.hardware_option_info.text()}\n\n"
            "Documented instrument limits\n"
            f"{self.hardware_range_info.text()}"
        )

    def _show_anritsu_hardware_info(self) -> None:
        QMessageBox.information(
            self,
            "Anritsu hardware information",
            self._hardware_details_text or "Hardware information is not available yet.",
        )

    def configure(self) -> None:
        try:
            start_hz, stop_hz = self._spectrum_frequency_bounds()
            config = SpectrumConfig(
                start_hz=start_hz,
                stop_hz=stop_hz,
                reference_level_dbm=parse_quantity(self.reference.text(), DIMENSION_DBM).si_value,
                points=int(self.points.currentData()),
            )
        except Exception as exc:
            self.banner.show_message(f"Invalid spectrum settings: {exc}")
            return
        self._controller.call("configure", config)

    def read_configuration_from_instrument(self) -> None:
        self.status.emit("Anritsu current-configuration read requested")
        self._controller.call("read_configuration")

    def read_once(self) -> None:
        if self._page_state not in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}:
            return
        self._request_trace()

    def _request_trace(self) -> bool:
        if self._fetch_pending:
            self._coalesced_timer_ticks += 1
            return False
        self._fetch_pending = True
        self._fetch_started_monotonic = time.monotonic()
        self._controller.call("fetch_current_trace", "TRAC1")
        return True

    def toggle_live(self) -> None:
        if self._live_transition_pending:
            return
        if self._timer.isActive():
            self._timer.stop()
            self._live_transition_pending = True
            self.live.setText("Stopping…")
            self._set_live_indicator("stopping")
            self._set_page_state(AnritsuPageState.STOPPING)
            self._controller.call("stop_live")
            return
        self._live_transition_pending = True
        self.live.setText("Starting…")
        self._set_live_indicator("starting")
        self._set_page_state(AnritsuPageState.STARTING_LIVE)
        self._timer.setInterval(self.refresh.value())
        # Live is intentionally passive: do not alter sweep or trace modes.
        # This is compatible with the same current-trace path used by the
        # working one-shot read and avoids unsupported mode probes.
        self._controller.call("start_live", False)

    def _set_live_indicator(self, state: str, frame: int | None = None) -> None:
        labels = {
            "off": "●  LIVE OFF",
            "starting": "●  LIVE STARTING…",
            "on": "●  LIVE ON",
            "paused": "●  LIVE PAUSED",
            "stopping": "●  LIVE STOPPING…",
        }
        text = labels.get(state, labels["off"])
        if state == "on" and frame is not None:
            text += f"  •  FRAME {frame}"
            if self._frame_intervals_s:
                mean_interval = sum(self._frame_intervals_s[-20:]) / len(
                    self._frame_intervals_s[-20:]
                )
                if mean_interval > 0:
                    text += f"  •  {1.0 / mean_interval:.2f} FPS"
            if self._transfer_durations_s:
                text += f"  •  {self._transfer_durations_s[-1] * 1e3:.0f} ms VISA"
        self.live_indicator.setText(text)
        self.live_indicator.setProperty("liveState", state)
        self.live_indicator.style().unpolish(self.live_indicator)
        self.live_indicator.style().polish(self.live_indicator)

    def fetch_live(self) -> None:
        self._request_trace()

    def start_averaging(self) -> None:
        self._start_temporal_averaging("spectrum")

    def start_reference_averaging(self) -> None:
        if not self._confirm_reference_replacement("averaged"):
            return
        self._start_temporal_averaging("reference")

    def _start_temporal_averaging(self, destination: str) -> None:
        if self._averaging_active:
            return
        target = self.average_count.value()
        self._resume_live_after_averaging = self._timer.isActive()
        if self._resume_live_after_averaging:
            self._timer.stop()
            self._set_live_indicator("paused")
        self._averager.reset()
        self._averaging_active = True
        self._averaging_destination = destination
        self._set_page_state(
            AnritsuPageState.AVERAGING_REFERENCE
            if destination == "reference"
            else AnritsuPageState.AVERAGING_SIGNAL
        )
        self.average_progress.setRange(0, target)
        self.average_progress.setValue(0)
        self.average_progress.setFormat(f"0 / {target}")
        label = "reference" if destination == "reference" else "spectrum"
        self.info.setText(f"Averaging {label}: 0 / {target} temporal frames...")
        self.status.emit(
            f"Anritsu passive temporal averaging started: {label}, 0 / {target}"
        )
        # Reuse an already pending Live frame instead of queuing a duplicate
        # VISA query against the same session.
        self._request_trace()

    def cancel_averaging(self) -> None:
        self._finish_temporal_averaging(resume_live=True)
        self.info.setText("Averaging cancelled; completed spectra were not modified.")
        self.status.emit("Anritsu temporal averaging cancelled")

    def _finish_temporal_averaging(self, *, resume_live: bool) -> None:
        was_live = self._resume_live_after_averaging
        should_resume_live = was_live and resume_live
        self._averaging_active = False
        self._averaging_destination = None
        self._resume_live_after_averaging = False
        self._averager.reset()
        if should_resume_live:
            self._timer.setInterval(self.refresh.value())
            self._timer.start()
            self.live.setText("Stop Live")
            self._set_live_indicator("on", self._live_frame_count)
            self._set_page_state(AnritsuPageState.LIVE)
        elif was_live:
            self.live.setText("Start Live")
            self.single.setEnabled(True)
            self._set_live_indicator("off")
            self._set_page_state(AnritsuPageState.IDLE)
        else:
            self._set_page_state(AnritsuPageState.IDLE)

    def _request_next_average_frame(self) -> None:
        if not self._averaging_active or self._fetch_pending:
            return
        self._request_trace()

    def capture_current_reference(self) -> None:
        """Use the latest completed frame locally without issuing a VISA query."""

        if self._latest_trace is None:
            QMessageBox.information(self, "Reference spectrum", "Acquire a spectrum before capturing a reference.")
            return
        if not self._confirm_reference_replacement("single"):
            return
        self._set_reference(self._build_reference(self._latest_trace, kind="single", count=1))
        self.status.emit("Anritsu current trace stored as a single reference")

    def acquire_reference_once(self) -> None:
        """Passively fetch one fresh trace and commit it only after success."""

        if self._page_state not in {AnritsuPageState.IDLE, AnritsuPageState.ERROR}:
            return
        if not self._confirm_reference_replacement("single"):
            return
        self._pending_reference_kind = "single"
        self._set_page_state(AnritsuPageState.ACQUIRING_REFERENCE)
        self.info.setText("Acquiring one fresh reference frame…")
        self.status.emit("Anritsu single-reference acquisition started")
        if not self._request_trace():
            self._pending_reference_kind = None
            self._set_page_state(AnritsuPageState.IDLE)

    def _confirm_reference_replacement(self, new_kind: str) -> bool:
        current = self._reference_spectrum
        if current is None:
            return True
        existing = (
            f"{current.kind}, {current.average_count} frame(s), "
            f"{current.acquired_at_utc.isoformat()}, {current.points} points"
        )
        answer = QMessageBox.question(
            self,
            "Replace reference?",
            f"Existing reference: {existing}.\n\nReplace it with a new {new_kind} reference?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _build_reference(
        self,
        trace: SpectrumTrace,
        *,
        kind: str,
        count: int,
    ) -> ReferenceSpectrum:
        capabilities = self._capabilities
        firmware = str(getattr(capabilities, "firmware", "") or "")
        options = tuple(getattr(capabilities, "hardware_options", ()) or ())
        reference_level: float | None = None
        if self._last_configuration is not None:
            reference_level = self._last_configuration.reference_level_dbm
        else:
            try:
                reference_level = parse_quantity(self.reference.text(), DIMENSION_DBM).si_value
            except Exception:
                pass
        advanced = self._last_advanced_configuration
        return ReferenceSpectrum(
            trace=trace,
            kind=kind,
            average_count=count,
            acquired_at_utc=trace.acquired_at_utc,
            source_device_idn=self._device_idn,
            firmware=firmware,
            hardware_options=options,
            reference_level_dbm=reference_level,
            advanced_configuration_known=advanced is not None,
            rbw_auto=advanced.rbw_auto if advanced is not None else None,
            rbw_hz=advanced.rbw_hz if advanced is not None else None,
            vbw_mode=advanced.vbw_mode if advanced is not None else "",
            vbw_hz=advanced.vbw_hz if advanced is not None else None,
            detector=advanced.detector if advanced is not None else "",
            attenuation_auto=advanced.attenuation_auto if advanced is not None else None,
            attenuation_db=advanced.attenuation_db if advanced is not None else None,
            preamplifier_enabled=(
                advanced.preamplifier_enabled if advanced is not None else None
            ),
            sweep_time_auto=advanced.sweep_time_auto if advanced is not None else None,
            sweep_time_s=advanced.sweep_time_s if advanced is not None else None,
        )

    def _validate_reference_acquisition_compatibility(
        self, reference: ReferenceSpectrum
    ) -> None:
        """Reject processing when known acquisition conditions are not equivalent."""

        current = self._last_advanced_configuration
        if reference.advanced_configuration_known != (current is not None):
            raise ValueError(
                "Advanced acquisition configuration is known for only one spectrum. "
                "Read the instrument settings and acquire a new reference."
            )
        if not reference.advanced_configuration_known or current is None:
            return
        mismatches: list[str] = []
        if reference.rbw_auto != current.rbw_auto or not math.isclose(
            float(reference.rbw_hz), current.rbw_hz, rel_tol=1e-6, abs_tol=1.0
        ):
            mismatches.append("RBW")
        if reference.vbw_mode != current.vbw_mode:
            mismatches.append("VBW mode")
        elif reference.vbw_mode != "off" and (
            reference.vbw_hz is None
            or current.vbw_hz is None
            or not math.isclose(reference.vbw_hz, current.vbw_hz, rel_tol=1e-6, abs_tol=1.0)
        ):
            mismatches.append("VBW")
        if reference.detector != current.detector:
            mismatches.append("detector")
        if reference.attenuation_auto != current.attenuation_auto or not math.isclose(
            float(reference.attenuation_db),
            current.attenuation_db,
            rel_tol=0.0,
            abs_tol=0.01,
        ):
            mismatches.append("input attenuation")
        if reference.preamplifier_enabled != current.preamplifier_enabled:
            mismatches.append("preamplifier")
        if reference.sweep_time_auto != current.sweep_time_auto or not math.isclose(
            float(reference.sweep_time_s),
            current.sweep_time_s,
            rel_tol=1e-6,
            abs_tol=1e-6,
        ):
            mismatches.append("sweep time")
        if mismatches:
            raise ValueError(
                "Acquisition settings differ from the reference: " + ", ".join(mismatches) + "."
            )

    def _set_reference(self, reference: ReferenceSpectrum) -> None:
        self._reference_spectrum = reference
        self._reference_trace = reference.trace
        self.show_reference.setChecked(True)
        self._update_reference_status()
        self._apply_page_state()
        self._refresh_spectrum_display()

    def _update_reference_status(self) -> None:
        reference = self._reference_spectrum
        if reference is None:
            self.reference_status.setText("No reference")
            return
        kind = "averaged" if reference.kind == "averaged" else reference.kind
        start = format_quantity_auto(reference.start_hz, DIMENSION_FREQUENCY)
        stop = format_quantity_auto(reference.stop_hz, DIMENSION_FREQUENCY)
        stored = "saved" if reference.saved_to_file else "memory only"
        self.reference_status.setText(
            f"{kind} · {reference.average_count} frame(s) · {reference.points} points · "
            f"{start}–{stop} · {reference.acquired_at_utc.isoformat()} · {stored}"
        )

    def remove_reference(self) -> None:
        self._reference_trace = None
        self._reference_spectrum = None
        self._pending_reference_kind = None
        self.spectrum_plot.clear_trace("Reference")
        self.spectrum_plot.clear_trace("Processed")
        self.show_reference.setChecked(False)
        self.show_processed.setChecked(False)
        self.reference_operation.setCurrentIndex(0)
        self._update_reference_status()
        self._apply_page_state()
        self._refresh_spectrum_display()
        self.status.emit("Anritsu reference spectrum removed")

    def save_reference_file(self) -> None:
        reference = self._reference_spectrum
        if reference is None:
            self.banner.show_message("There is no reference spectrum to save.")
            return
        directory = str(self._station_settings.storage.get("output_directory", "./measurements"))
        selected, _filter = QFileDialog.getSaveFileName(
            self,
            "Save Anritsu reference",
            str(Path(directory) / "anritsu_reference.h5"),
            "HDF5 measurement (*.h5)",
        )
        if not selected:
            return
        self._save_reference_to(Path(selected))

    def _save_reference_to(self, path: Path) -> None:
        reference = self._reference_spectrum
        if reference is None:
            raise ValueError("There is no reference spectrum to save.")
        try:
            saved = ReferenceHdf5Store.save(path, reference)
        except Exception as exc:
            self.banner.show_message(f"Reference save failed: {exc}", severity="error", timeout_ms=0)
            self.status.emit(f"Anritsu reference save failed: {exc}")
            return
        self._set_reference(saved)
        self.banner.show_message(
            f"Reference saved to {path.name} and verified as a completed HDF5 artefact.",
            severity="success",
        )
        self.status.emit(f"Anritsu reference saved: {path}")

    def load_reference_file(self) -> None:
        directory = str(self._station_settings.storage.get("output_directory", "./measurements"))
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Load Anritsu reference",
            directory,
            "HDF5 measurement (*.h5)",
        )
        if not selected:
            return
        self._load_reference_from(Path(selected))

    def _load_reference_from(self, path: Path) -> None:
        try:
            loaded = ReferenceHdf5Store.load(path)
        except Exception as exc:
            self.banner.show_message(f"Reference load failed: {exc}", severity="error", timeout_ms=0)
            self.status.emit(f"Anritsu reference load failed: {exc}")
            return
        if not self._confirm_reference_replacement("imported"):
            return
        imported = replace(loaded, kind="imported")
        self._set_reference(imported)
        self.banner.show_message(
            f"Reference loaded from {path.name}; the analyser configuration was not changed.",
            severity="success",
        )
        self.status.emit(f"Anritsu reference loaded: {path}")

    def _result(self, operation: str, result: object) -> None:
        if operation == "connect":
            self._device_idn = str(getattr(result, "idn", "") or "")
            self._set_page_state(AnritsuPageState.IDLE)
        if operation == "read_configuration" and isinstance(result, AnritsuConfigurationSnapshot):
            self._last_configuration = result
            self._set_frequency_bounds(result.start_hz, result.stop_hz)
            self.reference.setText(f"{result.reference_level_dbm:.9g} dBm")
            point_index = self.points.findData(result.points)
            if point_index < 0:
                raise ValueError(
                    f"Instrument returned {result.points} points outside the approved UI choices."
                )
            self.points.setCurrentIndex(point_index)
            self.banner.show_message(
                f"Current analyser settings loaded into the form (mode: "
                f"{result.instrument_mode or 'unknown'}). "
                "The instrument and safety limits were not changed.",
                severity="success",
            )
            self.status.emit("Anritsu current configuration read from instrument")
        elif operation in {
            "read_advanced_spectrum",
            "configure_advanced_spectrum",
        } and isinstance(result, AdvancedSpectrumSnapshot):
            self._show_advanced_snapshot(result)
            self._set_page_state(AnritsuPageState.IDLE)
            verb = (
                "configured and verified"
                if operation == "configure_advanced_spectrum"
                else "read without changing the instrument"
            )
            self.banner.show_message(
                f"Advanced Spectrum settings {verb}.", severity="success"
            )
            self.status.emit(f"Anritsu advanced Spectrum settings {verb}")
        elif operation in {
            "read_signal_generator",
            "configure_signal_generator",
        } and isinstance(result, SignalGeneratorSnapshot):
            self._show_signal_generator_snapshot(result)
            verb = "configured and verified" if operation == "configure_signal_generator" else "read"
            self.status.emit(f"Anritsu signal generator {verb}; RF state confirmed")
        elif operation == "arm_signal_generator":
            self._sg_armed = True
            self.sg_status.setText("●  RF ARMED — one enable permitted")
            self.sg_status.setProperty("liveState", "starting")
            self.sg_status.style().unpolish(self.sg_status)
            self.sg_status.style().polish(self.sg_status)
            self._apply_page_state()
            self.status.emit("Anritsu signal generator armed for one RF enable")
        elif operation == "set_signal_generator_output":
            self._sg_output_enabled = bool(result)
            self._sg_armed = False
            state = "on" if self._sg_output_enabled else "off"
            self.sg_status.setText(
                "●  RF OUTPUT ON" if self._sg_output_enabled else "●  RF OUTPUT OFF"
            )
            self.sg_status.setProperty("liveState", state)
            self.sg_status.style().unpolish(self.sg_status)
            self.sg_status.style().polish(self.sg_status)
            self._apply_page_state()
            self.status.emit(
                "Anritsu signal generator RF OUTPUT "
                + ("ON" if self._sg_output_enabled else "OFF")
            )
        elif operation == "configure" and isinstance(result, AnritsuConfigurationSnapshot):
            self._result("read_configuration", result)
            self.status.emit("Anritsu configured and verified by SCPI readback")
        elif operation == "start_live" and isinstance(result, AnritsuConfigurationSnapshot):
            self._live_transition_pending = False
            self._result("read_configuration", result)
            self._live_frame_count = 0
            self._identical_live_frames = 0
            self._last_live_signature = None
            self._last_frame_monotonic = None
            self._frame_intervals_s.clear()
            self._transfer_durations_s.clear()
            self._stale_frame_count = 0
            self._coalesced_timer_ticks = 0
            self._timer.start()
            self.live.setText("Stop Live")
            self._set_live_indicator("on", 0)
            self._set_page_state(AnritsuPageState.LIVE)
            mode = "passive current-trace polling"
            self.info.setText(f"Live started; {mode}. Waiting for first frame...")
            self.status.emit(f"Anritsu Live started: {mode}")
        elif operation == "stop_live":
            self._live_transition_pending = False
            self.live.setText("Start Live")
            self._set_live_indicator("off")
            self._set_page_state(AnritsuPageState.IDLE)
            self.info.setText("Live stopped.")
            self.status.emit("Anritsu Live stopped")
        elif operation in {"fetch_trace", "fetch_current_trace", "single_sweep"} and isinstance(result, SpectrumTrace):
            self._fetch_pending = False
            finished = time.monotonic()
            if self._fetch_started_monotonic is not None:
                self._transfer_durations_s.append(finished - self._fetch_started_monotonic)
                self._transfer_durations_s = self._transfer_durations_s[-100:]
            self._fetch_started_monotonic = None
            if self._pending_reference_kind == "single":
                self._pending_reference_kind = None
                self._latest_trace = result
                self._set_reference(self._build_reference(result, kind="single", count=1))
                self._set_page_state(AnritsuPageState.IDLE)
                self.info.setText(
                    f"Single reference acquired: {len(result.powers_dbm)} points · "
                    f"{result.acquired_at_utc.isoformat()}"
                )
                self.status.emit("Anritsu single-reference acquisition completed")
                return
            if self._averaging_active:
                try:
                    completed = self._averager.add(result.powers_dbm)
                except ValueError as exc:
                    self._finish_temporal_averaging(resume_live=False)
                    self.info.setText(f"Averaging stopped: {exc}")
                    return
                target = self.average_count.value()
                self.average_progress.setValue(completed)
                self.average_progress.setFormat(f"{completed} / {target}")
                label = (
                    "reference" if self._averaging_destination == "reference" else "spectrum"
                )
                self.info.setText(
                    f"Averaging {label}: {completed} / {target} temporal frames..."
                )
                self.status.emit(
                    f"Anritsu temporal averaging progress: {label} {completed} / {target}"
                )
                if completed >= target:
                    averaged = self._averager.result()
                    averaged_trace = SpectrumTrace(
                        frequencies_hz=result.frequencies_hz,
                        powers_dbm=averaged,
                        acquired_at_utc=result.acquired_at_utc,
                        trace_name=(
                            f"{result.trace_name}_REFAVG{target}"
                            if self._averaging_destination == "reference"
                            else f"{result.trace_name}_AVG{target}"
                        ),
                    )
                    self._latest_trace = result
                    if self._averaging_destination == "reference":
                        self._set_reference(
                            self._build_reference(
                                averaged_trace,
                                kind="averaged",
                                count=target,
                            )
                        )
                        completion = f"Averaged reference completed: {target} / {target}"
                    else:
                        self._averaged_trace = averaged_trace
                        self.show_average.setChecked(True)
                        completion = f"Averaged spectrum completed: {target} / {target}"
                    self._finish_temporal_averaging(resume_live=True)
                    self.info.setText(completion)
                    self.status.emit(f"Anritsu {completion.lower()}")
                    self._refresh_spectrum_display()
                else:
                    QTimer.singleShot(self.refresh.value(), self._request_next_average_frame)
            else:
                self._show_trace(result)

    def _show_trace(self, trace: SpectrumTrace) -> None:
        self._latest_trace = trace
        self._apply_page_state()
        self._refresh_spectrum_display()
        live_detail = ""
        if self._timer.isActive():
            now = time.monotonic()
            if self._last_frame_monotonic is not None:
                self._frame_intervals_s.append(now - self._last_frame_monotonic)
                self._frame_intervals_s = self._frame_intervals_s[-100:]
            self._last_frame_monotonic = now
            self._live_frame_count += 1
            signature = hash(trace.powers_dbm)
            if signature == self._last_live_signature:
                self._identical_live_frames += 1
                self._stale_frame_count += 1
                live_detail = f" • unchanged ×{self._identical_live_frames}"
                if self._identical_live_frames == 3:
                    self.banner.show_message(
                        "Live received three identical traces. Verify that Trace A is in Write "
                        "mode, Continuous Sweep is active, and the analyser sweep time is not "
                        "longer than the observation interval.",
                        timeout_ms=15_000,
                    )
            else:
                self._identical_live_frames = 0
                self._stale_frame_count = 0
                live_detail = " • new data"
            self._last_live_signature = signature
            self._set_live_indicator("on", self._live_frame_count)
            live_detail = f" • Live frame {self._live_frame_count}{live_detail}"
            if self._frame_intervals_s:
                effective_ms = (
                    sum(self._frame_intervals_s[-20:])
                    / len(self._frame_intervals_s[-20:])
                    * 1e3
                )
                live_detail += (
                    f" • requested {self.refresh.value()} ms"
                    f" • effective {effective_ms:.0f} ms"
                    f" • coalesced {self._coalesced_timer_ticks}"
                )
        self.info.setText(
            f"{len(trace.powers_dbm)} points • {trace.acquired_at_utc.isoformat()} • "
            f"max {max(trace.powers_dbm):.4g} dBm{live_detail}"
        )

    def _refresh_spectrum_display(self, *_args: object) -> None:
        traces: list[tuple[str, SpectrumTrace, tuple[float, ...], str, str]] = []
        if self._latest_trace is not None and self.show_raw.isChecked():
            traces.append(("Raw", self._latest_trace, self._latest_trace.powers_dbm, "dBm", "#2196f3"))
        if self._averaged_trace is not None and self.show_average.isChecked():
            traces.append(("Averaged", self._averaged_trace, self._averaged_trace.powers_dbm, "dBm", "#00a67d"))
        if self._reference_trace is not None and self.show_reference.isChecked():
            traces.append(("Reference", self._reference_trace, self._reference_trace.powers_dbm, "dBm", "#ffb300"))

        operation = str(self.reference_operation.currentData() or "none")
        processed: tuple[float, ...] | None = None
        processed_unit = "dBm"
        signal = self._averaged_trace or self._latest_trace
        if operation != "none" and signal is not None and self._reference_trace is not None:
            try:
                if not frequency_grids_match(signal.frequencies_hz, self._reference_trace.frequencies_hz):
                    raise ValueError("Reference frequency grid differs from the current spectrum.")
                reference_level = (
                    self._reference_spectrum.reference_level_dbm
                    if self._reference_spectrum is not None
                    else None
                )
                current_level = (
                    self._last_configuration.reference_level_dbm
                    if self._last_configuration is not None
                    else None
                )
                if (
                    reference_level is not None
                    and current_level is not None
                    and not math.isclose(reference_level, current_level, abs_tol=0.005)
                ):
                    raise ValueError(
                        "Reference Level differs from the reference acquisition "
                        f"({current_level:g} dBm current, {reference_level:g} dBm reference)."
                    )
                if self._reference_spectrum is not None:
                    self._validate_reference_acquisition_compatibility(
                        self._reference_spectrum
                    )
                processed, processed_unit = apply_reference_operation(
                    signal.powers_dbm, self._reference_trace.powers_dbm, operation
                )
            except ValueError as exc:
                self.info.setText(f"Reference processing unavailable: {exc}")
            else:
                self.show_processed.setChecked(True)
                if self.show_processed.isChecked():
                    traces.append(("Processed", signal, processed, processed_unit, "#ab47bc"))

        for name in ("Raw", "Averaged", "Reference", "Processed"):
            self.spectrum_plot.clear_trace(name)
        if not traces:
            return
        if processed is not None and processed_unit != "dBm" and self.show_processed.isChecked():
            traces = [item for item in traces if item[0] == "Processed"]
        displayed = 0
        for name, trace, values, _unit, color in traces:
            self.spectrum_plot.set_trace(
                name,
                trace.frequencies_hz,
                values,
                color=color,
                primary=name in {"Processed", "Averaged", "Raw"},
            )
            displayed += sum(
                math.isfinite(frequency) and math.isfinite(value)
                for frequency, value in zip(trace.frequencies_hz, values, strict=True)
            )
        if displayed == 0:
            self.info.setText("No finite spectrum points are available for display.")
            return
        active_unit = traces[-1][3]
        self.spectrum_plot.set_labels(
            x="Frequency", x_unit="Hz", y="Amplitude", y_unit=active_unit
        )

    def _error(self, operation: str, error: str) -> None:
        if operation in {"read_advanced_spectrum", "configure_advanced_spectrum"}:
            self._set_page_state(AnritsuPageState.ERROR)
            self.banner.show_message(
                f"Anritsu advanced Spectrum operation failed: {error}",
                severity="error",
                timeout_ms=0,
            )
            self.status.emit(f"Anritsu {operation} failed: {error}")
            return
        if operation in {
            "read_signal_generator",
            "configure_signal_generator",
            "arm_signal_generator",
            "set_signal_generator_output",
        }:
            self._sg_armed = False
            if operation == "set_signal_generator_output":
                self.sg_status.setText("●  RF OUTPUT UNKNOWN — use RF OFF or E-STOP")
                self.sg_status.setProperty("liveState", "off")
            self._apply_page_state()
            self.banner.show_message(
                f"Anritsu signal-generator operation {operation!r} failed: {error}",
                severity="error",
                timeout_ms=0,
            )
            self.status.emit(f"Anritsu {operation} failed: {error}")
            return
        if operation in {"fetch_trace", "fetch_current_trace", "single_sweep"}:
            self._fetch_pending = False
            if self._averaging_active:
                self._finish_temporal_averaging(resume_live=False)
                self.info.setText(f"Averaging stopped: {error}")
        if operation in {
            "read_configuration", "configure", "start_live", "fetch_trace", "fetch_current_trace",
            "single_sweep", "emergency_off",
        }:
            self._live_transition_pending = False
            self._timer.stop()
            self.live.setText("Start Live")
            self._set_live_indicator("off")
            self._pending_reference_kind = None
            self._set_page_state(AnritsuPageState.ERROR)
            self.banner.show_message(
                f"Anritsu operation {operation!r} failed: {error}. "
                "The last valid spectrum remains visible; retry when communication is stable.",
                severity="error",
                timeout_ms=0,
            )
            self.status.emit(f"Anritsu {operation} failed: {error}")


_SWEEPABLE_PARAMETERS: tuple[dict[str, str], ...] = (
    legacy_ui_parameter_definitions()
)


def _sweep_default(dimension: str) -> tuple[str, str]:
    return {
        DIMENSION_CURRENT: ("0 A", "1 mA"),
        DIMENSION_VOLTAGE: ("0 V", "10 mV"),
        DIMENSION_FREQUENCY: ("1 kHz", "10 kHz"),
        DIMENSION_DBM: ("-30 dBm", "0 dBm"),
    }[dimension]


class DeviceParameterDialog(QDialog):
    """Let an operator select every controllable field for one instrument."""

    def __init__(
        self, parent: QWidget | None = None, *, initial_device: str | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add device controls")
        self.setMinimumSize(460, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose an instrument, then select one or more fields to sweep."))
        self.device = QComboBox()
        self.device.addItems(("Keithley", "Rigol", "Anritsu Spectrum", "Anritsu SG"))
        layout.addWidget(self.device)
        self.operation = QComboBox()
        self.operation.addItem("Create dynamic sweep", "sweep")
        self.operation.addItem("Set one fixed value", "fixed")
        layout.addWidget(self.operation)
        self.fields = QListWidget()
        layout.addWidget(self.fields, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Open point generators")
        layout.addWidget(buttons)
        self.device.currentTextChanged.connect(self._refresh)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        if initial_device is not None:
            self.device.setCurrentText(initial_device)
        self._refresh()

    def _refresh(self) -> None:
        self.fields.clear()
        for definition in _SWEEPABLE_PARAMETERS:
            if definition["device"] != self.device.currentText():
                continue
            item = QListWidgetItem(definition["label"])
            item.setData(Qt.ItemDataRole.UserRole, definition)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.fields.addItem(item)

    def selected(self) -> tuple[dict[str, str], ...]:
        return tuple(
            dict(item.data(Qt.ItemDataRole.UserRole))
            for index in range(self.fields.count())
            if (item := self.fields.item(index)).checkState() == Qt.CheckState.Checked
        )

    def operation_kind(self) -> str:
        return str(self.operation.currentData())

    def accept(self) -> None:
        if not self.selected():
            QMessageBox.information(self, "Device controls", "Select at least one controllable field.")
            return
        super().accept()


class SeamlessRoiCellDelegate(QStyledItemDelegate):
    """Make ROI values edit like a spreadsheet cell, not a nested text box."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("seamlessRoiCellDelegate")

    def createEditor(self, parent: QWidget, _option: Any, _index: Any) -> QLineEdit:
        editor = QLineEdit(parent)
        editor.setFrame(False)
        editor.setContentsMargins(0, 0, 0, 0)
        editor.setStyleSheet(
            """
            QLineEdit {
                border: none;
                border-radius: 0;
                background: #ffffff;
                color: #17212b;
                padding: 0 7px;
                selection-background-color: #cfe8ff;
                selection-color: #17212b;
            }
            """
        )
        return editor

    def updateEditorGeometry(self, editor: QWidget, option: Any, _index: Any) -> None:
        editor.setGeometry(option.rect)


class SweepGeneratorDialog(QDialog):
    """Dynamic interval editor with an exact point scatter preview."""

    def __init__(
        self,
        definition: dict[str, str],
        parent: QWidget | None = None,
        *,
        initial_segments: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.definition = definition
        self.setWindowTitle(f"Point generator — {definition['label']}")
        self.setMinimumSize(640, 560)
        self.resize(1180, 700)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        heading = QLabel(
            "Build any number of inclusive intervals. Each interval uses either a point count "
            "or a physical step; the scatter plot always shows the exact generated points."
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.segment_panel = QWidget()
        self.segment_panel.setMinimumWidth(0)
        left_layout = QVBoxLayout(self.segment_panel)
        left_layout.setContentsMargins(0, 0, 6, 0)
        self.segments = QTableWidget(0, 5)
        roi_palette = self.segments.palette()
        roi_palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
        roi_palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f7f9fc"))
        roi_palette.setColor(QPalette.ColorRole.Text, QColor("#17212b"))
        roi_palette.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
        roi_palette.setColor(QPalette.ColorRole.WindowText, QColor("#17212b"))
        roi_palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
        roi_palette.setColor(QPalette.ColorRole.ButtonText, QColor("#17212b"))
        roi_palette.setColor(QPalette.ColorRole.Highlight, QColor("#e8f3fd"))
        roi_palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#17212b"))
        self.segments.setPalette(roi_palette)
        self.segments.viewport().setPalette(roi_palette)
        self.segments.viewport().setAutoFillBackground(True)
        self.segments.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.segments.setHorizontalHeaderLabels(
            ("Start / value", "Stop", "Method", "Points / step", "Spacing")
        )
        self.segments.setStyleSheet(
            """
            QTableWidget {
                border: 1px solid #cbd7e3;
                border-radius: 8px;
                background-color: #ffffff;
                color: #17212b;
                gridline-color: #dce4ec;
            }
            QTableWidget QTableCornerButton::section {
                background-color: #f5f8fb;
                border: none;
                border-bottom: 1px solid #dce4ec;
            }
            QTableWidget::item {
                padding: 0 7px;
                background-color: #ffffff;
                color: #17212b;
            }
            QTableWidget::item:selected {
                background: #e8f3fd;
                color: #17212b;
            }
            QHeaderView::section {
                background-color: #f5f8fb;
                color: #17212b;
                border: none;
                border-bottom: 1px solid #dce4ec;
                padding: 6px;
            }
            """
        )
        self.segments.setEditTriggers(
            QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self._roi_cell_delegate = SeamlessRoiCellDelegate(self.segments)
        for column in (0, 1, 3):
            self.segments.setItemDelegateForColumn(
                column, self._roi_cell_delegate
            )
        header = self.segments.horizontalHeader()
        header.setMinimumSectionSize(72)
        header.setStretchLastSection(False)
        for column in range(5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        for column, width in ((0, 94), (1, 94), (2, 118), (3, 122), (4, 122)):
            self.segments.setColumnWidth(column, width)
        self.segments.verticalHeader().setMinimumSectionSize(34)
        self.segments.setMinimumHeight(190)
        left_layout.addWidget(self.segments, 1)
        actions = QHBoxLayout()
        self.add_segment = QPushButton("+ Add interval")
        self.remove_segment = QPushButton("Remove interval")
        actions.addWidget(self.add_segment)
        actions.addWidget(self.remove_segment)
        left_layout.addLayout(actions)
        self.splitter.addWidget(self.segment_panel)
        self.plot_panel = QWidget()
        self.plot_panel.setMinimumWidth(0)
        right_layout = QVBoxLayout(self.plot_panel)
        right_layout.setContentsMargins(6, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setMinimumHeight(280)
        self.plot_theme = self._resolved_plot_theme()
        self._apply_plot_theme()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.legend = self.plot.addLegend(offset=(10, 10))
        self._set_plot_labels()
        right_layout.addWidget(self.plot, 1)
        self.preview = QLabel("Add an interval to generate points.")
        self.preview.setWordWrap(True)
        right_layout.addWidget(self.preview)
        self.splitter.addWidget(self.plot_panel)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([450, 700])
        layout.addWidget(self.splitter, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create sweep node")
        layout.addWidget(buttons)
        self.add_segment.clicked.connect(self.add_interval)
        self.remove_segment.clicked.connect(self.remove_interval)
        self.segments.cellChanged.connect(self._refresh_preview)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        if initial_segments:
            for segment in initial_segments:
                self.add_interval(segment)
        else:
            self.add_interval()
        self._update_responsive_layout()
        self._connect_theme_source()

    def _resolved_plot_theme(self) -> str:
        application = QApplication.instance()
        if application is not None:
            active = application.property("activeTheme")
            if str(active).lower() in {"light", "dark"}:
                return str(active).lower()
        owner: QWidget | None = self.parentWidget()
        while owner is not None:
            settings = getattr(owner, "_settings", None)
            ui = getattr(settings, "ui", None)
            if isinstance(ui, dict):
                return effective_theme(str(ui.get("theme", "system")))
            owner = owner.parentWidget()
        return effective_theme("system")

    def _connect_theme_source(self) -> None:
        owner: QWidget | None = self.parentWidget()
        while owner is not None:
            signal = getattr(owner, "theme_changed", None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(self._set_plot_theme)
                return
            owner = owner.parentWidget()

    def _set_plot_theme(self, theme: str) -> None:
        resolved = effective_theme(theme)
        if resolved == self.plot_theme:
            return
        self.plot_theme = resolved
        self._apply_plot_theme()
        self._set_plot_labels()
        self._style_plot_legend()

    def _apply_plot_theme(self) -> None:
        background = "#ffffff" if self.plot_theme == "light" else "#101419"
        foreground = "#243447" if self.plot_theme == "light" else "#e6edf3"
        grid = "#9aa8b6" if self.plot_theme == "light" else "#52606d"
        self.plot.setBackground(background)
        plot_item = self.plot.getPlotItem()
        for name in ("left", "bottom"):
            axis = plot_item.getAxis(name)
            axis.setPen(pg.mkPen(foreground, width=1))
            axis.setTextPen(pg.mkPen(foreground))
        plot_item.getViewBox().setBorder(pg.mkPen(grid, width=1))

    def _set_plot_labels(self) -> None:
        foreground = "#243447" if self.plot_theme == "light" else "#e6edf3"
        self.plot.setLabel("bottom", "Generated point index", color=foreground)
        self.plot.setLabel("left", self.definition["label"], color=foreground)

    def _style_plot_legend(self) -> None:
        foreground = "#243447" if self.plot_theme == "light" else "#e6edf3"
        background = "#ffffffdd" if self.plot_theme == "light" else "#101419dd"
        self.legend.setBrush(pg.mkBrush(background))
        self.legend.setPen(pg.mkPen("#cbd5df" if self.plot_theme == "light" else "#52606d"))
        for _sample, label in self.legend.items:
            try:
                label.setText(label.text, color=foreground)
            except (AttributeError, TypeError):
                continue

    def _update_responsive_layout(self) -> None:
        narrow = self.width() < 1040
        orientation = (
            Qt.Orientation.Vertical if narrow else Qt.Orientation.Horizontal
        )
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
        if narrow:
            self.segment_panel.setMinimumWidth(0)
            self.plot_panel.setMinimumWidth(0)
            self.splitter.setSizes([260, 350])
        else:
            self.segment_panel.setMinimumWidth(540)
            self.plot_panel.setMinimumWidth(420)
            available = max(960, self.splitter.width())
            table_width = max(540, min(620, round(available * 0.5)))
            self.splitter.setSizes([table_width, max(420, available - table_width)])
        self._resize_segment_columns()
        QTimer.singleShot(0, self._resize_segment_columns)

    def _resize_segment_columns(self) -> None:
        """Fit all five ROI fields without hiding Method or Spacing."""

        if not hasattr(self, "segments"):
            return
        viewport_width = self.segments.viewport().width()
        if viewport_width <= 0:
            return
        method_width = 118
        value_width = 122
        spacing_width = 122
        flexible = max(176, viewport_width - method_width - value_width - spacing_width - 2)
        start_width = flexible // 2
        stop_width = flexible - start_width
        for column, width in (
            (0, start_width),
            (1, stop_width),
            (2, method_width),
            (3, value_width),
            (4, spacing_width),
        ):
            self.segments.setColumnWidth(column, width)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "splitter"):
            self._update_responsive_layout()

    def add_interval(self, initial: dict[str, object] | None = None) -> None:
        row = self.segments.rowCount()
        self.segments.blockSignals(True)
        self.segments.insertRow(row)
        start, stop = _sweep_default(self.definition["dimension"])
        values = initial or {}
        method_value = (
            "Single value"
            if "value" in values
            else "Step" if "step" in values else "Points"
        )
        point_value = str(values.get("step", values.get("points", "100")))
        for column, value in (
            (0, str(values.get("value", values.get("start", start)))),
            (1, str(values.get("stop", stop))),
            (3, point_value),
        ):
            self.segments.setItem(row, column, QTableWidgetItem(value))
        method = QComboBox()
        method.setObjectName("roiCellCombo")
        method.addItems(("Points", "Step", "Single value"))
        method.setMinimumWidth(112)
        spacing = QComboBox()
        spacing.setObjectName("roiCellCombo")
        spacing.addItems(("Linear", "Logarithmic"))
        spacing.setMinimumWidth(112)
        cell_combo_style = """
            QComboBox#roiCellCombo {
                border: none;
                border-radius: 0;
                background: #ffffff;
                color: #17212b;
                padding: 0 22px 0 7px;
            }
            QComboBox#roiCellCombo:hover { background: rgba(47, 130, 198, 18); }
            QComboBox#roiCellCombo:disabled { color: #8a98a8; }
            QComboBox#roiCellCombo::drop-down { border: none; width: 20px; }
            QComboBox#roiCellCombo QAbstractItemView {
                background: #ffffff;
                color: #17212b;
                selection-background-color: #e8f3fd;
                selection-color: #17212b;
            }
        """
        method.setStyleSheet(cell_combo_style)
        spacing.setStyleSheet(cell_combo_style)
        method.setCurrentText(method_value)
        spacing.setCurrentText("Logarithmic" if values.get("spacing") == "log" else "Linear")
        method.currentIndexChanged.connect(
            lambda _index, widget=method: self._update_row_method(widget)
        )
        spacing.currentIndexChanged.connect(self._refresh_preview)
        self.segments.setCellWidget(row, 2, method)
        self.segments.setCellWidget(row, 4, spacing)
        self.segments.blockSignals(False)
        self._update_row_method(method)
        self._refresh_preview()

    def _update_row_method(self, method: QComboBox) -> None:
        row = next(
            (
                index
                for index in range(self.segments.rowCount())
                if self.segments.cellWidget(index, 2) is method
            ),
            -1,
        )
        if row < 0:
            return
        single = method.currentText() == "Single value"
        start_item = self.segments.item(row, 0)
        if start_item is not None:
            start_item.setToolTip(
                "Exact value for one measurement"
                if single
                else "Inclusive start value of this interval"
            )
        for column, fallback in ((1, _sweep_default(self.definition["dimension"])[1]), (3, "100")):
            item = self.segments.item(row, column)
            if item is None:
                continue
            if single:
                if item.text() != "—":
                    item.setData(Qt.ItemDataRole.UserRole, item.text())
                item.setText("—")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setData(
                    Qt.ItemDataRole.ForegroundRole, QColor("#8a98a8")
                )
                item.setToolTip("Not used by a Single value stage")
            else:
                stored = item.data(Qt.ItemDataRole.UserRole)
                if item.text() == "—":
                    item.setText(str(stored or fallback))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                item.setData(Qt.ItemDataRole.ForegroundRole, None)
                item.setToolTip(
                    "Inclusive stop value"
                    if column == 1
                    else "Number of points or physical step"
                )
        spacing = self.segments.cellWidget(row, 4)
        if isinstance(spacing, QComboBox):
            spacing.setEnabled(not single)
        self._refresh_preview()

    def remove_interval(self) -> None:
        if self.segments.rowCount() > 1:
            self.segments.removeRow(self.segments.currentRow() if self.segments.currentRow() >= 0 else self.segments.rowCount() - 1)
        self._refresh_preview()

    def select_interval(self, row: int | None) -> None:
        if row is None or not 0 <= row < self.segments.rowCount():
            return
        self.segments.setCurrentCell(row, 0)
        item = self.segments.item(row, 0)
        if item is not None:
            self.segments.scrollToItem(
                item, QAbstractItemView.ScrollHint.PositionAtCenter
            )
        self.segments.setFocus()

    def segment_data(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for row in range(self.segments.rowCount()):
            start = self.segments.item(row, 0)
            stop = self.segments.item(row, 1)
            value = self.segments.item(row, 3)
            method = self.segments.cellWidget(row, 2)
            spacing = self.segments.cellWidget(row, 4)
            if not all((start, stop, value)) or not isinstance(method, QComboBox) or not isinstance(spacing, QComboBox):
                raise ConfigurationError("Every interval needs start, stop and point data.")
            if method.currentText() == "Single value":
                result.append({"value": start.text().strip()})
                continue
            raw: dict[str, object] = {
                "start": start.text().strip(),
                "stop": stop.text().strip(),
                "spacing": "log" if spacing.currentText() == "Logarithmic" else "linear",
            }
            if method.currentText() == "Points":
                raw["points"] = int(value.text())
            else:
                raw["step"] = value.text().strip()
            result.append(raw)
        return result

    def _refresh_preview(self) -> None:
        try:
            segments = self.segment_data()
            point_count = estimate_sweep_point_count(
                segments, self.definition["dimension"]
            )
            if point_count > 100_000:
                self.plot.clear()
                self.preview.setText(
                    f"BLOCKER — {point_count:,} points exceed the 100,000 point "
                    "plan limit. Reduce the point count or increase the step."
                )
                return
            stages = generate_sweep_stage_points(
                segments, self.definition["dimension"]
            )
            points = tuple(point for stage in stages for point in stage)
        except Exception as exc:
            self.plot.clear()
            self.preview.setText(f"Invalid point generator: {exc}")
            return
        self.plot.clear()
        # PlotItem.clear() removes data but preserves the legend, so the labels
        # always describe precisely the intervals visible in this refresh.
        palette = ("#4fa3ff", "#ef9b4b", "#73c991", "#d484d8", "#e6ce55", "#69cbd0")
        point_index = 0
        previous_point: float | None = None
        for stage_index, stage in enumerate(stages):
            if not stage:
                continue
            color = palette[stage_index % len(palette)]
            stride = max(1, math.ceil(len(stage) / 2_000))
            visible_indices = list(range(0, len(stage), stride))
            if visible_indices[-1] != len(stage) - 1:
                visible_indices.append(len(stage) - 1)
            x_values = [point_index + index for index in visible_indices]
            y_values = [stage[index].si_value for index in visible_indices]
            # Shared boundaries are deduplicated in the execution axis. For
            # presentation, reconnect the next stage to the preceding point so
            # 0 → 1 followed by 1 → 0 is shown as one continuous trajectory.
            if previous_point is not None and point_index > 0:
                x_values.insert(0, point_index - 1)
                y_values.insert(0, previous_point)
            self.plot.plot(
                x_values,
                y_values,
                pen=pg.mkPen(color, width=1),
                symbol="o" if len(stage) <= 2_000 else None,
                symbolSize=6,
                symbolBrush=color,
                name=f"Stage {stage_index + 1} ({len(stage):,} points)",
            )
            point_index += len(stage)
            previous_point = stage[-1].si_value
        self._style_plot_legend()
        self.preview.setText(
            f"Generated {len(points):,} unique points • first {points[0].si_value:.12g} SI • "
            f"last {points[-1].si_value:.12g} SI"
        )

    def accept(self) -> None:
        try:
            point_count = estimate_sweep_point_count(
                self.segment_data(), self.definition["dimension"]
            )
        except Exception as exc:
            QMessageBox.warning(self, "Point generator", str(exc))
            return
        if point_count > 100_000:
            QMessageBox.warning(self, "Point generator", "The generator exceeds the 100,000 point safety preview limit.")
            return
        super().accept()


class KeithleySweepBuilderDialog(SweepGeneratorDialog):
    """A device-first sweep window, not a generic point-generator popup."""

    def __init__(
        self, settings: StationSettings, parent: QWidget | None = None,
        *, initial_segments: list[dict[str, object]] | None = None,
    ) -> None:
        self._settings = settings
        definition = next(item for item in _SWEEPABLE_PARAMETERS if item["target"] == "keithley.B.current")
        super().__init__(dict(definition), parent, initial_segments=initial_segments)
        self.setWindowTitle("Keithley 2600 — sweep builder")
        self.resize(980, 650)
        parameters = QFrame()
        parameters.setObjectName("recipeEditorParameters")
        parameter_layout = QVBoxLayout(parameters)
        parameter_layout.setContentsMargins(10, 8, 10, 8)
        title = QLabel("Keithley source settings")
        title.setObjectName("sectionTitle")
        parameter_layout.addWidget(title)
        form = QFormLayout()
        self.channel = QComboBox()
        self.channel.addItems(("A", "B"))
        self.channel.setCurrentText("B")
        self.mode = QComboBox()
        self.mode.addItems(("current", "voltage"))
        self.compliance = _line("67 mV")
        self.nplc = _line("1")
        self.settle_time = _line("100 ms")
        self.sense_mode = QComboBox()
        self.sense_mode.addItems(("2wire", "4wire"))
        form.addRow("Channel", self.channel)
        form.addRow("Source mode", self.mode)
        form.addRow("Compliance", self.compliance)
        form.addRow("NPLC", self.nplc)
        form.addRow("Settling time", self.settle_time)
        form.addRow("Sense", self.sense_mode)
        parameter_layout.addLayout(form)
        guidance = QLabel(
            "The source is configured at every generated point. This window only designs "
            "the recipe; it never enables OUTPUT."
        )
        guidance.setObjectName("muted")
        guidance.setWordWrap(True)
        parameter_layout.addWidget(guidance)
        self.limit_status = QLabel()
        self.limit_status.setObjectName("recipeLimitStatus")
        self.limit_status.setWordWrap(True)
        parameter_layout.addWidget(self.limit_status)
        self.layout().insertWidget(1, parameters)
        self.channel.currentTextChanged.connect(self._parameter_changed)
        self.mode.currentTextChanged.connect(self._parameter_changed)
        self._parameter_changed()

    def _parameter_changed(self) -> None:
        target = f"keithley.{self.channel.currentText()}.{self.mode.currentText()}"
        definition = next(item for item in _SWEEPABLE_PARAMETERS if item["target"] == target)
        previous_dimension = self.definition["dimension"]
        self.definition = dict(definition)
        self.setWindowTitle(f"Keithley 2600 — {definition['label']} sweep")
        self.plot.setLabel("left", definition["label"])
        if previous_dimension != definition["dimension"]:
            self.segments.blockSignals(True)
            self.segments.setRowCount(0)
            self.segments.blockSignals(False)
            self.add_interval()
        else:
            self._refresh_preview()

    def _refresh_preview(self) -> None:
        super()._refresh_preview()
        if not hasattr(self, "limit_status"):
            return
        try:
            points = generate_sweep_points(self.segment_data(), self.definition["dimension"])
            channel = self._settings.keithley.safety.channels[self.channel.currentText()]
            limits = channel.lab_limits
            bounds = (
                limits.source_current
                if self.mode.currentText() == "current"
                else limits.source_voltage
            )
            low = parse_quantity(bounds.min, self.definition["dimension"]).si_value
            high = parse_quantity(bounds.max, self.definition["dimension"]).si_value
            outside = [point for point in points if point.si_value < low or point.si_value > high]
        except Exception:
            self.limit_status.setText("")
            return
        if outside:
            self.limit_status.setText(
                f"BLOCKER — {len(outside):,} point(s) exceed approved range "
                f"{low:.6g} … {high:.6g} SI. Apply is blocked by compiler preflight."
            )
            self.limit_status.setProperty("severity", "blocker")
        else:
            self.limit_status.setText(
                f"✓ All {len(points):,} planned points are inside the approved station range."
            )
            self.limit_status.setProperty("severity", "ok")
        self.limit_status.style().unpolish(self.limit_status)
        self.limit_status.style().polish(self.limit_status)

    def keithley_options(self) -> dict[str, object]:
        return {
            "compliance": self.compliance.text().strip(),
            "nplc": float(self.nplc.text()),
            "settle_time": self.settle_time.text().strip(),
            "sense_mode": self.sense_mode.currentText(),
        }

    def accept(self) -> None:
        try:
            parse_quantity(self.compliance.text(), DIMENSION_VOLTAGE if self.mode.currentText() == "current" else DIMENSION_CURRENT)
            if float(self.nplc.text()) <= 0:
                raise ConfigurationError("NPLC must be positive.")
            parse_quantity(self.settle_time.text(), DIMENSION_TIME)
        except Exception as exc:
            QMessageBox.warning(self, "Keithley sweep", str(exc))
            return
        super().accept()


class FixedValueDialog(QDialog):
    """Create an auditable single setpoint without pretending it is a sweep."""

    def __init__(self, definition: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.definition = definition
        self.setWindowTitle(f"Fixed value — {definition['label']}")
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "This node configures one value at its position in the tree. It does not "
                "create a measurement axis or enable an output."
            )
        )
        form = QFormLayout()
        self.value = _line(_sweep_default(definition["dimension"])[0], 24)
        form.addRow(definition["label"], self.value)
        layout.addLayout(form)
        self.preview = QLabel()
        layout.addWidget(self.preview)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create fixed setting")
        layout.addWidget(buttons)
        self.value.textChanged.connect(self._validate)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._validate()

    def _validate(self) -> None:
        try:
            quantity = parse_quantity(self.value.text(), self.definition["dimension"])
        except Exception as exc:
            self.preview.setText(f"Invalid value: {exc}")
        else:
            self.preview.setText(f"One fixed setpoint: {quantity.si_value:.12g} SI")

    def accept(self) -> None:
        try:
            parse_quantity(self.value.text(), self.definition["dimension"])
        except Exception as exc:
            QMessageBox.warning(self, "Fixed value", str(exc))
            return
        super().accept()


class AnritsuAcquisitionEditorDialog(QDialog):
    """Edit trace and reference-processing policy without touching VISA."""

    operations = (
        ("None — raw spectrum", "none"),
        ("Difference in dB", "difference_db"),
        ("Linear ratio", "ratio_linear"),
        ("Add power", "add_power"),
        ("Subtract power", "subtract_power"),
        ("Multiply linear", "multiply_linear"),
    )

    def __init__(
        self,
        node: RecipeNode,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._reference_only = node.type == "acquire_reference"
        self.setWindowTitle(
            "Anritsu reference acquisition"
            if self._reference_only
            else "Anritsu spectrum acquisition"
        )
        self.setMinimumSize(480, 300)
        layout = QVBoxLayout(self)
        heading = QLabel(
            "Reference acquisition"
            if self._reference_only
            else "Spectrum and reference processing"
        )
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        note = QLabel(
            "This editor changes only the declarative plan. The selected trace "
            "and storage policy are validated again during preflight."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        form = QFormLayout()
        self.trace = QComboBox()
        self.trace.addItems(("TRAC1",))
        self.trace.setCurrentText(str(node.data.get("trace", "TRAC1")))
        form.addRow("Trace", self.trace)
        self.average_count = QSpinBox()
        self.average_count.setRange(1, 9999)
        self.average_count.setValue(int(node.data.get("average_count", 1)))
        self.average_count.setToolTip(
            "Complete spectra are averaged in linear mW, never directly in dBm."
        )
        form.addRow("Average complete spectra", self.average_count)
        self.reference_operation = QComboBox()
        for label, value in self.operations:
            self.reference_operation.addItem(label, value)
        operation = str(node.data.get("reference_operation", "none"))
        index = self.reference_operation.findData(operation)
        self.reference_operation.setCurrentIndex(index if index >= 0 else 0)
        self.store_raw = QCheckBox("Store raw spectrum")
        self.store_raw.setChecked(bool(node.data.get("store_raw", True)))
        self.store_processed = QCheckBox("Store processed spectrum")
        self.store_processed.setChecked(
            bool(node.data.get("store_processed", operation != "none"))
        )
        if not self._reference_only:
            form.addRow("Reference operation", self.reference_operation)
            form.addRow("", self.store_raw)
            form.addRow("", self.store_processed)
        layout.addLayout(form)
        layout.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText(
            "Apply acquisition settings"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.reference_operation.currentIndexChanged.connect(
            self._operation_changed
        )
        self._operation_changed()

    def _operation_changed(self) -> None:
        processed = self.reference_operation.currentData() != "none"
        self.store_processed.setEnabled(processed)
        if not processed:
            self.store_processed.setChecked(False)

    def node_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "trace": self.trace.currentText(),
            "average_count": self.average_count.value(),
        }
        if not self._reference_only:
            fields.update(
                {
                    "reference_operation": str(
                        self.reference_operation.currentData()
                    ),
                    "store_raw": self.store_raw.isChecked(),
                    "store_processed": self.store_processed.isChecked(),
                }
            )
        return fields

    def accept(self) -> None:
        if (
            not self._reference_only
            and not self.store_raw.isChecked()
            and not self.store_processed.isChecked()
        ):
            QMessageBox.warning(
                self,
                "Anritsu acquisition",
                "Store at least the raw or processed spectrum.",
            )
            return
        super().accept()


class CommentEditorDialog(QDialog):
    """Focused editor for human-readable notes embedded in a sweep tree."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("commentEditorDialog")
        self.setWindowTitle("Edit comment")
        self.resize(680, 460)
        self.setMinimumSize(500, 360)
        self.setStyleSheet(
            """
            QDialog#commentEditorDialog { background: #f5f8fc; color: #17212b; }
            QFrame#commentEditorHero {
                background: #ffffff;
                border: 1px solid #d9e2ec;
                border-radius: 10px;
            }
            QLabel#commentEditorIcon {
                color: #7253a6;
                background: #eee9fb;
                border-radius: 8px;
                font-size: 18pt;
                font-weight: 700;
            }
            QLabel#commentEditorTitle { font-size: 15pt; font-weight: 700; }
            QLabel#commentEditorHint, QLabel#commentEditorCounter { color: #647588; }
            QPlainTextEdit#commentEditorText {
                background: #ffffff;
                color: #17212b;
                border: 1px solid #cbd7e3;
                border-radius: 9px;
                padding: 10px;
                selection-background-color: #dceeff;
                font-size: 10pt;
            }
            QPlainTextEdit#commentEditorText:focus { border: 2px solid #4a90d9; }
            QPushButton { min-width: 92px; padding: 7px 14px; border-radius: 6px; }
            """
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(12)

        hero = QFrame()
        hero.setObjectName("commentEditorHero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(14, 12, 14, 12)
        icon = QLabel("“")
        icon.setObjectName("commentEditorIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(42, 42)
        hero_layout.addWidget(icon)
        heading = QVBoxLayout()
        title = QLabel("Comment in measurement sequence")
        title.setObjectName("commentEditorTitle")
        hint = QLabel(
            "Describe the purpose of this step, sample preparation or an operator note. "
            "The comment does not send commands to any instrument."
        )
        hint.setObjectName("commentEditorHint")
        hint.setWordWrap(True)
        heading.addWidget(title)
        heading.addWidget(hint)
        hero_layout.addLayout(heading, 1)
        layout.addWidget(hero)

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("commentEditorText")
        self.editor.setPlaceholderText(
            "For example: Wait until the sample temperature is stable before acquisition…"
        )
        self.editor.setPlainText(text)
        layout.addWidget(self.editor, 1)
        footer = QHBoxLayout()
        self.counter = QLabel()
        self.counter.setObjectName("commentEditorCounter")
        footer.addWidget(self.counter)
        footer.addStretch(1)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save comment")
        footer.addWidget(self.buttons)
        layout.addLayout(footer)
        self.editor.textChanged.connect(self._update_counter)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self._update_counter()
        self.editor.setFocus()

    def comment_text(self) -> str:
        return self.editor.toPlainText().strip()

    def _update_counter(self) -> None:
        count = len(self.editor.toPlainText())
        self.counter.setText(f"{count:,} characters")
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setEnabled(bool(self.comment_text()))


class RigolNodeEditorDialog(QDialog):
    """Offline carrier/output editor with one optional local sweep axis."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        settings: StationSettings | None = None,
        snapshot: RigolConfigurationSnapshot | None = None,
        parameter_actions: list[dict[str, object]] | None = None,
        channel: int = 1,
        output_policy: str = "unchanged",
    ) -> None:
        super().__init__(parent)
        snapshot = snapshot or RigolConfigurationSnapshot(channel=channel)
        self._settings = settings
        self._working_segments: dict[str, list[dict[str, object]]] = {}
        self.plan_mode = True
        self.hardware_actions_enabled = False
        self.setWindowTitle("Rigol DG1032Z — configure sweep node")
        self.resize(780, 640)
        layout = QVBoxLayout(self)
        heading = QLabel("Rigol DG1032Z · Carrier and output")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        description = QLabel(
            "The complete carrier snapshot is stored in the recipe. Select one of "
            "Frequency, HighL or LowL as a local ROI axis when required."
        )
        description.setWordWrap(True)
        description.setObjectName("recipeHint")
        layout.addWidget(description)
        content = QSplitter(Qt.Orientation.Horizontal)
        carrier = QFrame()
        form = QFormLayout(carrier)
        self.channel = QComboBox()
        self.channel.addItem("Channel 1", 1)
        self.channel.addItem("Channel 2", 2)
        channel_index = self.channel.findData(snapshot.channel)
        self.channel.setCurrentIndex(channel_index if channel_index >= 0 else 0)
        form.addRow("Channel", self.channel)
        self.waveform = QComboBox()
        self.waveform.addItems(("SIN", "SQU", "RAMP", "PULS", "NOIS", "DC"))
        self.waveform.setCurrentText(snapshot.waveform)
        self.frequency = _line(snapshot.frequency)
        self.high_level = _line(snapshot.high_level)
        self.low_level = _line(snapshot.low_level)
        self.output_load = _line(snapshot.output_load)
        self.phase = _line(snapshot.phase_deg)
        self.duty = _line(snapshot.square_duty_percent)
        self.symmetry = _line(snapshot.ramp_symmetry_percent)
        self.pulse_width = _line(snapshot.pulse_width)
        self.pulse_leading = _line(snapshot.pulse_leading)
        self.pulse_trailing = _line(snapshot.pulse_trailing)
        self.dut_impedance = _line(snapshot.dut_min_impedance)
        self.output_polarity = QComboBox()
        self.output_polarity.addItems(("NORM", "INV"))
        self.output_polarity.setCurrentText(snapshot.output_polarity)
        self.output_mode = QComboBox()
        self.output_mode.addItems(("NORM", "GAT"))
        self.output_mode.setCurrentText(snapshot.output_mode)
        self.gate_polarity = QComboBox()
        self.gate_polarity.addItems(("NORM", "INV"))
        self.gate_polarity.setCurrentText(snapshot.gate_polarity)
        self.sync_enabled = QCheckBox("SYNC enabled")
        self.sync_enabled.setChecked(snapshot.sync_enabled)
        self.sync_polarity = QComboBox()
        self.sync_polarity.addItems(("NORM", "INV"))
        self.sync_polarity.setCurrentText(snapshot.sync_polarity)
        self.sync_delay = _line(snapshot.sync_delay)
        for label, widget in (
            ("Waveform", self.waveform),
            ("Frequency", self.frequency),
            ("HighL", self.high_level),
            ("LowL", self.low_level),
            ("Load", self.output_load),
            ("Phase [deg]", self.phase),
            ("Square duty [%]", self.duty),
            ("Ramp symmetry [%]", self.symmetry),
            ("Pulse width", self.pulse_width),
            ("Pulse leading edge", self.pulse_leading),
            ("Pulse trailing edge", self.pulse_trailing),
            ("Minimum DUT impedance", self.dut_impedance),
            ("Output polarity", self.output_polarity),
            ("Output mode", self.output_mode),
            ("Gate polarity", self.gate_polarity),
            ("", self.sync_enabled),
            ("SYNC polarity", self.sync_polarity),
            ("SYNC delay", self.sync_delay),
        ):
            form.addRow(label, widget)
        content.addWidget(carrier)
        actions_frame = QFrame()
        actions_layout = QFormLayout(actions_frame)
        self.parameter_selectors: dict[str, QComboBox] = {}
        for parameter_id, label in (
            ("carrier.frequency", "Frequency"),
            ("carrier.high_level", "HighL"),
            ("carrier.low_level", "LowL"),
        ):
            selector = QComboBox()
            selector.addItem("Set fixed", "set")
            selector.addItem("Sweep — ROI required", "sweep")
            selector.currentIndexChanged.connect(self._selection_changed)
            self.parameter_selectors[parameter_id] = selector
            actions_layout.addRow(label, selector)
        self.open_roi_button = QPushButton("Edit selected ROI…")
        self.open_roi_button.setEnabled(False)
        self.open_roi_button.clicked.connect(self._open_roi)
        actions_layout.addRow(self.open_roi_button)
        self.output_policy = QComboBox()
        self.output_policy.addItem("Leave OUTPUT unchanged", "unchanged")
        self.output_policy.addItem("Switch OUTPUT ON", "on")
        self.output_policy.addItem("Switch OUTPUT OFF", "off")
        output_index = self.output_policy.findData(output_policy)
        self.output_policy.setCurrentIndex(output_index if output_index >= 0 else 0)
        actions_layout.addRow("Output", self.output_policy)
        content.addWidget(actions_frame)
        content.setStretchFactor(0, 3)
        content.setStretchFactor(1, 2)
        layout.addWidget(content, 1)
        note = QLabel(
            "Plan editing is offline. Hardware sweep/modulation/burst stay manual-only; "
            "recipe axes use validated point updates with readback."
        )
        note.setWordWrap(True)
        note.setObjectName("recipeHint")
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Apply Rigol configuration"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.load_plan_actions(parameter_actions or [])
        self._selection_changed()

    def selected_channel(self) -> int:
        return int(self.channel.currentData())

    def selected_output_policy(self) -> str:
        return str(self.output_policy.currentData())

    def configuration_snapshot(self) -> RigolConfigurationSnapshot:
        return RigolConfigurationSnapshot(
            channel=self.selected_channel(),
            waveform=self.waveform.currentText(),
            frequency=self.frequency.text().strip(),
            high_level=self.high_level.text().strip(),
            low_level=self.low_level.text().strip(),
            output_load=self.output_load.text().strip(),
            phase_deg=self.phase.text().strip(),
            square_duty_percent=self.duty.text().strip(),
            ramp_symmetry_percent=self.symmetry.text().strip(),
            pulse_width=self.pulse_width.text().strip(),
            pulse_leading=self.pulse_leading.text().strip(),
            pulse_trailing=self.pulse_trailing.text().strip(),
            dut_min_impedance=self.dut_impedance.text().strip(),
            output_polarity=self.output_polarity.currentText(),
            output_mode=self.output_mode.currentText(),
            gate_polarity=self.gate_polarity.currentText(),
            sync_enabled=self.sync_enabled.isChecked(),
            sync_polarity=self.sync_polarity.currentText(),
            sync_delay=self.sync_delay.text().strip(),
        )

    def planned_parameter_actions(self) -> list[dict[str, object]]:
        values = {
            "carrier.frequency": self.frequency.text().strip(),
            "carrier.high_level": self.high_level.text().strip(),
            "carrier.low_level": self.low_level.text().strip(),
        }
        result: list[dict[str, object]] = []
        for parameter_id, selector in self.parameter_selectors.items():
            action: dict[str, object] = {
                "parameter_id": parameter_id,
                "mode": str(selector.currentData()),
                "value": values[parameter_id],
            }
            if action["mode"] == "sweep" and parameter_id in self._working_segments:
                action["segments"] = [
                    dict(segment)
                    for segment in self._working_segments[parameter_id]
                ]
            result.append(action)
        return result

    def load_plan_actions(self, actions: list[dict[str, object]]) -> None:
        for action in actions:
            parameter_id = str(action.get("parameter_id", ""))
            selector = self.parameter_selectors.get(parameter_id)
            if selector is None:
                continue
            index = selector.findData(str(action.get("mode", "set")))
            if index >= 0:
                selector.setCurrentIndex(index)
            segments = action.get("segments")
            if isinstance(segments, list):
                self._working_segments[parameter_id] = [
                    dict(segment)
                    for segment in segments
                    if isinstance(segment, dict)
                ]

    def _selected_sweep_parameter(self) -> str | None:
        selected = [
            parameter_id
            for parameter_id, selector in self.parameter_selectors.items()
            if selector.currentData() == "sweep"
        ]
        return selected[0] if len(selected) == 1 else None

    def _selection_changed(self, *_args: object) -> None:
        self.open_roi_button.setEnabled(
            self._selected_sweep_parameter() is not None
        )

    def _open_roi(self) -> None:
        parameter_id = self._selected_sweep_parameter()
        if parameter_id is None:
            return
        dimension = (
            DIMENSION_FREQUENCY
            if parameter_id == "carrier.frequency"
            else DIMENSION_VOLTAGE
        )
        labels = {
            "carrier.frequency": "Carrier frequency",
            "carrier.high_level": "High level",
            "carrier.low_level": "Low level",
        }
        dialog = SweepGeneratorDialog(
            {
                "device": "Rigol",
                "label": labels[parameter_id],
                "target": (
                    f"rigol.{self.selected_channel()}."
                    f"{parameter_id.removeprefix('carrier.')}"
                ),
                "dimension": dimension,
            },
            self,
            initial_segments=self._working_segments.get(parameter_id),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._working_segments[parameter_id] = dialog.segment_data()

    def accept(self) -> None:
        sweeps = [
            action
            for action in self.planned_parameter_actions()
            if action["mode"] == "sweep"
        ]
        if len(sweeps) > 1:
            QMessageBox.warning(
                self, "Rigol node", "One Rigol node supports one local sweep axis."
            )
            return
        if sweeps and "segments" not in sweeps[0]:
            QMessageBox.warning(
                self, "Rigol node", "Define ROI for the selected sweep parameter."
            )
            return
        snapshot = self.configuration_snapshot()
        if self._settings is not None:
            try:
                config = RigolChannelConfig(
                    channel=snapshot.channel,
                    waveform=snapshot.waveform,
                    frequency_hz=parse_quantity(
                        snapshot.frequency, DIMENSION_FREQUENCY
                    ).si_value,
                    high_level_v=parse_quantity(
                        snapshot.high_level, DIMENSION_VOLTAGE
                    ).si_value,
                    low_level_v=parse_quantity(
                        snapshot.low_level, DIMENSION_VOLTAGE
                    ).si_value,
                    output_load=snapshot.output_load,
                    phase_deg=float(snapshot.phase_deg.replace(",", ".")),
                    dut_min_impedance_ohm=parse_quantity(
                        snapshot.dut_min_impedance, DIMENSION_RESISTANCE
                    ).si_value,
                )
                validate_rigol_waveform(
                    channel=self._settings.rigol.safety.channels[
                        str(snapshot.channel)
                    ],
                    safety=self._settings.rigol.safety,
                    waveform=config.waveform,
                    frequency=config.frequency_hz,
                    high_level=config.high_level_v,
                    low_level=config.low_level_v,
                    output_load=config.output_load,
                    dut_min_impedance=config.dut_min_impedance_ohm,
                )
                sync_delay_s = parse_quantity(
                    snapshot.sync_delay, DIMENSION_TIME
                ).si_value
                if not 0 <= sync_delay_s <= 10:
                    raise ConfigurationError(
                        "Rigol SYNC delay must be in the range 0..10 s."
                    )
            except Exception as exc:
                QMessageBox.warning(self, "Rigol configuration", str(exc))
                return
        super().accept()


class AnritsuNodeEditorDialog(QDialog):
    """Offline Anritsu spectrum DeviceNode editor using the shared panel."""

    parameter_specs = (
        ("spectrum.start_frequency", "Start frequency", True),
        ("spectrum.stop_frequency", "Stop frequency", True),
        ("spectrum.reference_level", "Reference level", True),
        ("spectrum.points", "Trace points", False),
        ("advanced.rbw_mode", "RBW mode", False),
        ("advanced.rbw", "Resolution bandwidth", False),
        ("advanced.vbw_mode", "VBW mode", False),
        ("advanced.vbw", "Video bandwidth", False),
        ("advanced.detector", "Detector", False),
        ("advanced.attenuation_mode", "RF attenuation mode", False),
        ("advanced.attenuation", "RF attenuation", False),
        ("advanced.preamplifier_enabled", "Preamplifier", False),
        ("advanced.sweep_time_mode", "Sweep-time mode", False),
        ("advanced.sweep_time", "Sweep time", False),
    )

    def __init__(
        self,
        settings: StationSettings,
        parent: QWidget | None = None,
        *,
        snapshot: AnritsuConfigurationSnapshot | None = None,
    ) -> None:
        super().__init__(parent)
        self.plan_mode = True
        self.hardware_actions_enabled = False
        self._working_segments: dict[str, list[dict[str, object]]] = {}
        self.setWindowTitle("Anritsu MS2830A — configure sweep node")
        self.resize(1180, 760)
        self.setMinimumSize(680, 480)
        layout = QVBoxLayout(self)
        heading = QLabel("Anritsu MS2830A · Spectrum analyser")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        self.content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter.setChildrenCollapsible(False)
        left = QFrame()
        left.setObjectName("recipeEditorParameters")
        left_layout = QVBoxLayout(left)
        parameter_tabs = QTabWidget()
        self.configuration_panel = AnritsuSpectrumConfigurationPanel(
            settings, parameter_tabs, plan_mode=True
        )
        self.configuration_panel.frequency_representation.setCurrentIndex(
            self.configuration_panel.frequency_representation.findData("start_stop")
        )
        self.configuration_panel.frequency_representation.setEnabled(False)
        if snapshot is not None:
            self.configuration_panel.load_snapshot(snapshot)
        self.advanced_panel = AnritsuAdvancedSpectrumPanel(parameter_tabs)
        self.advanced_panel.preamplifier.setEnabled(True)
        self.advanced_panel.preamplifier.setToolTip(
            "The selected state is validated against detected Anritsu hardware "
            "options before execution."
        )
        parameter_tabs.addTab(self.configuration_panel, "Spectrum setup")
        parameter_tabs.addTab(self.advanced_panel, "Bandwidth & input path")
        left_layout.addWidget(parameter_tabs)
        self.content_splitter.addWidget(left)
        right = QFrame()
        right.setObjectName("recipeEditorParameters")
        right_layout = QGridLayout(right)
        title = QLabel("Select what this node controls")
        title.setObjectName("sectionTitle")
        right_layout.addWidget(title, 0, 0, 1, 2)
        self.parameter_selectors: dict[str, QComboBox] = {}
        for row, (parameter_id, label, sweepable) in enumerate(
            self.parameter_specs, start=1
        ):
            right_layout.addWidget(QLabel(label), row, 0)
            selector = QComboBox()
            selector.addItem("Bez zmian", "unchanged")
            selector.addItem("Ustaw", "set")
            if sweepable:
                selector.addItem("Sweep — ROI wymagane", "sweep")
            selector.currentIndexChanged.connect(self._selection_changed)
            self.parameter_selectors[parameter_id] = selector
            right_layout.addWidget(selector, row, 1)
        operation_row = len(self.parameter_specs) + 1
        self.acquire_single = QCheckBox("Acquire single spectrum at this tree position")
        self.acquire_single.setChecked(False)
        self.acquire_single.setVisible(False)
        right_layout.addWidget(self.acquire_single, operation_row, 0, 1, 2)
        self.trace = QComboBox()
        self.trace.addItems(("TRAC1",))
        self.trace_label = QLabel("Trace")
        self.trace_label.setVisible(False)
        self.trace.setVisible(False)
        right_layout.addWidget(self.trace_label, operation_row + 1, 0)
        right_layout.addWidget(self.trace, operation_row + 1, 1)
        self.open_roi_button = QPushButton("Przejdź do ROI…")
        self.open_roi_button.setEnabled(False)
        self.open_roi_button.clicked.connect(self._open_roi)
        right_layout.addWidget(
            self.open_roi_button, operation_row + 2, 0, 1, 2
        )
        note = QLabel(
            "Only selected parameters are stored. Unselected values remain exactly "
            "as currently configured in the Anritsu module. Spectrum acquisition "
            "is a separate Acquire spectrum once block placed inside the loop."
        )
        note.setObjectName("recipeHint")
        note.setWordWrap(True)
        right_layout.addWidget(note, operation_row + 3, 0, 1, 2)
        right_layout.setRowStretch(operation_row + 4, 1)
        self.content_splitter.addWidget(right)
        self.content_splitter.setSizes([570, 390])
        layout.addWidget(self.content_splitter, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText(
            "Apply Anritsu node"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)  # type: ignore[arg-type]
        orientation = (
            Qt.Orientation.Vertical
            if self.width() < 900
            else Qt.Orientation.Horizontal
        )
        if self.content_splitter.orientation() != orientation:
            self.content_splitter.setOrientation(orientation)
            self.content_splitter.setSizes(
                [max(260, self.height() // 2), max(220, self.height() // 2)]
                if orientation == Qt.Orientation.Vertical
                else [570, 390]
            )

    def _parameter_value(self, parameter_id: str) -> str:
        panel = self.configuration_panel
        advanced = self.advanced_panel
        return {
            "spectrum.start_frequency": panel.start.text().strip(),
            "spectrum.stop_frequency": panel.stop.text().strip(),
            "spectrum.reference_level": panel.reference.text().strip(),
            "spectrum.points": str(panel.points.currentData()),
            "advanced.rbw_mode": str(advanced.rbw_mode.currentData()),
            "advanced.rbw": advanced.rbw.text().strip(),
            "advanced.vbw_mode": str(advanced.vbw_mode.currentData()),
            "advanced.vbw": advanced.vbw.text().strip(),
            "advanced.detector": str(advanced.detector.currentData()),
            "advanced.attenuation_mode": str(
                advanced.attenuation_mode.currentData()
            ),
            "advanced.attenuation": f"{advanced.attenuation.value()} dB",
            "advanced.preamplifier_enabled": (
                "true" if advanced.preamplifier.isChecked() else "false"
            ),
            "advanced.sweep_time_mode": str(
                advanced.sweep_time_mode.currentData()
            ),
            "advanced.sweep_time": advanced.sweep_time.text().strip(),
        }[parameter_id]

    @staticmethod
    def _roi_definition(parameter_id: str, label: str) -> dict[str, str]:
        dimensions = {
            "spectrum.start_frequency": DIMENSION_FREQUENCY,
            "spectrum.stop_frequency": DIMENSION_FREQUENCY,
            "spectrum.reference_level": DIMENSION_DBM,
        }
        if parameter_id not in dimensions:
            raise ConfigurationError(
                f"Anritsu parameter {parameter_id!r} cannot be a sweep axis."
            )
        return {
            "device": "Anritsu Spectrum",
            "label": f"Spectrum · {label.lower()}",
            "target": f"anritsu.{parameter_id}",
            "dimension": dimensions[parameter_id],
        }

    def _selected_sweep_parameter(self) -> str | None:
        selected = [
            parameter_id
            for parameter_id, selector in self.parameter_selectors.items()
            if selector.currentData() == "sweep"
        ]
        return selected[0] if len(selected) == 1 else None

    def _selection_changed(self) -> None:
        self.open_roi_button.setEnabled(
            self._selected_sweep_parameter() is not None
        )

    def _open_roi(self) -> None:
        parameter_id = self._selected_sweep_parameter()
        if parameter_id is None:
            return
        label = next(
            label
            for candidate, label, _sweepable in self.parameter_specs
            if candidate == parameter_id
        )
        dialog = SweepGeneratorDialog(
            self._roi_definition(parameter_id, label),
            self,
            initial_segments=self._working_segments.get(parameter_id),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._working_segments[parameter_id] = dialog.segment_data()

    def planned_parameter_actions(self) -> list[dict[str, object]]:
        actions: list[dict[str, object]] = []
        for parameter_id, selector in self.parameter_selectors.items():
            mode = str(selector.currentData())
            if mode == "unchanged":
                continue
            action: dict[str, object] = {
                "parameter_id": parameter_id,
                "mode": mode,
                "value": self._parameter_value(parameter_id),
            }
            if mode == "sweep" and parameter_id in self._working_segments:
                action["segments"] = [
                    dict(segment)
                    for segment in self._working_segments[parameter_id]
                ]
            actions.append(action)
        return actions

    def load_plan_actions(
        self,
        actions: list[dict[str, object]],
        *,
        acquire_single: bool,
        trace: str,
    ) -> None:
        for action in actions:
            parameter_id = str(action.get("parameter_id", ""))
            selector = self.parameter_selectors.get(parameter_id)
            if selector is None:
                continue
            self._load_parameter_value(
                parameter_id, str(action.get("value", ""))
            )
            index = selector.findData(str(action.get("mode", "unchanged")))
            if index >= 0:
                selector.setCurrentIndex(index)
            segments = action.get("segments")
            if isinstance(segments, list):
                self._working_segments[parameter_id] = [
                    dict(segment)
                    for segment in segments
                    if isinstance(segment, dict)
                ]
        self.acquire_single.setChecked(acquire_single)
        trace_index = self.trace.findText(trace)
        if trace_index >= 0:
            self.trace.setCurrentIndex(trace_index)
        self._selection_changed()

    def _load_parameter_value(self, parameter_id: str, value: str) -> None:
        panel = self.configuration_panel
        advanced = self.advanced_panel
        line_edits = {
            "spectrum.start_frequency": panel.start,
            "spectrum.stop_frequency": panel.stop,
            "spectrum.reference_level": panel.reference,
            "advanced.rbw": advanced.rbw,
            "advanced.vbw": advanced.vbw,
            "advanced.sweep_time": advanced.sweep_time,
        }
        if parameter_id in line_edits:
            line_edits[parameter_id].setText(value)
            return
        combos = {
            "advanced.rbw_mode": advanced.rbw_mode,
            "advanced.vbw_mode": advanced.vbw_mode,
            "advanced.detector": advanced.detector,
            "advanced.attenuation_mode": advanced.attenuation_mode,
            "advanced.sweep_time_mode": advanced.sweep_time_mode,
        }
        if parameter_id in combos:
            index = combos[parameter_id].findData(value)
            if index >= 0:
                combos[parameter_id].setCurrentIndex(index)
            return
        if parameter_id == "spectrum.points":
            try:
                index = panel.points.findData(int(value))
            except ValueError:
                index = -1
            if index >= 0:
                panel.points.setCurrentIndex(index)
            return
        if parameter_id == "advanced.attenuation":
            try:
                parsed = parse_quantity(value, "dimensionless", require_unit=False)
                advanced.attenuation.setValue(round(parsed.si_value))
            except (ConfigurationError, ValueError):
                try:
                    advanced.attenuation.setValue(
                        round(float(value.lower().replace("db", "").strip()))
                    )
                except ValueError:
                    pass
            return
        if parameter_id == "advanced.preamplifier_enabled":
            advanced.preamplifier.setChecked(value.strip().lower() == "true")

    def accept(self) -> None:
        actions = self.planned_parameter_actions()
        action_by_parameter = {
            str(action["parameter_id"]): action for action in actions
        }
        for mode_parameter, value_parameter in (
            ("advanced.rbw_mode", "advanced.rbw"),
            ("advanced.vbw_mode", "advanced.vbw"),
            ("advanced.attenuation_mode", "advanced.attenuation"),
            ("advanced.sweep_time_mode", "advanced.sweep_time"),
        ):
            mode_action = action_by_parameter.get(mode_parameter)
            value_action = action_by_parameter.get(value_parameter)
            manual = (
                mode_action is not None
                and str(mode_action.get("value", "")).lower() == "manual"
            )
            if manual != (value_action is not None):
                QMessageBox.warning(
                    self,
                    "Anritsu node",
                    f"{mode_parameter}='manual' and {value_parameter} "
                    "must be selected together.",
                )
                return
        sweep_actions = [action for action in actions if action["mode"] == "sweep"]
        if len(sweep_actions) > 1:
            QMessageBox.warning(
                self, "Anritsu node", "One Anritsu node supports one sweep axis."
            )
            return
        if any("segments" not in action for action in sweep_actions):
            QMessageBox.warning(
                self, "Anritsu node", "Define ROI for the selected sweep parameter."
            )
            return
        if not actions and not self.acquire_single.isChecked():
            QMessageBox.information(
                self, "Anritsu node", "Select a parameter or acquisition operation."
            )
            return
        super().accept()


class AnritsuSignalGeneratorNodeEditorDialog(QDialog):
    """Offline editor for an Anritsu SG DeviceNode and its single local ROI."""

    parameter_specs = (
        ("sg.frequency", "RF frequency", DIMENSION_FREQUENCY),
        ("sg.power", "RF power", DIMENSION_DBM),
    )

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        frequency: str = "1 GHz",
        power: str = "-30 dBm",
        parameter_actions: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Anritsu MS2830A — signal generator sweep node")
        self.resize(620, 420)
        self.setMinimumSize(520, 360)
        self._working_segments: dict[str, list[dict[str, object]]] = {}
        layout = QVBoxLayout(self)
        heading = QLabel("Anritsu MS2830A · Signal generator")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        form = QGridLayout()
        self.frequency = QLineEdit(frequency)
        self.power = QLineEdit(power)
        self.parameter_selectors: dict[str, QComboBox] = {}
        for row, (parameter_id, label, _dimension) in enumerate(
            self.parameter_specs
        ):
            field = self.frequency if parameter_id == "sg.frequency" else self.power
            selector = QComboBox()
            selector.addItem("Bez zmian", "unchanged")
            selector.addItem("Ustaw", "set")
            selector.addItem("Sweep — ROI wymagane", "sweep")
            selector.currentIndexChanged.connect(self._selection_changed)
            self.parameter_selectors[parameter_id] = selector
            form.addWidget(QLabel(label), row, 0)
            form.addWidget(field, row, 1)
            form.addWidget(selector, row, 2)
        layout.addLayout(form)
        self.open_roi_button = QPushButton("Edytuj ROI…")
        self.open_roi_button.setEnabled(False)
        self.open_roi_button.clicked.connect(self._open_roi)
        layout.addWidget(self.open_roi_button)
        note = QLabel(
            "Konfiguracja SG przy każdym punkcie pozostawia RF OFF. "
            "Włączenie RF wymaga osobnych, jawnych kroków ARM i OUTPUT ON "
            "wewnątrz pętli oraz przechodzi pełny preflight bezpieczeństwa."
        )
        note.setObjectName("recipeHint")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText(
            "Apply Anritsu SG node"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.load_plan_actions(parameter_actions or [])

    def _selected_sweep_parameter(self) -> str | None:
        selected = [
            parameter_id
            for parameter_id, selector in self.parameter_selectors.items()
            if selector.currentData() == "sweep"
        ]
        return selected[0] if len(selected) == 1 else None

    def _selection_changed(self) -> None:
        self.open_roi_button.setEnabled(
            self._selected_sweep_parameter() is not None
        )

    def _open_roi(self) -> None:
        parameter_id = self._selected_sweep_parameter()
        if parameter_id is None:
            return
        label, dimension = next(
            (label, dimension)
            for candidate, label, dimension in self.parameter_specs
            if candidate == parameter_id
        )
        dialog = SweepGeneratorDialog(
            {
                "device": "Anritsu SG",
                "label": f"Signal generator · {label.lower()}",
                "target": f"anritsu.{parameter_id}",
                "dimension": dimension,
            },
            self,
            initial_segments=self._working_segments.get(parameter_id),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._working_segments[parameter_id] = dialog.segment_data()

    def load_plan_actions(self, actions: list[dict[str, object]]) -> None:
        for action in actions:
            parameter_id = str(action.get("parameter_id", ""))
            selector = self.parameter_selectors.get(parameter_id)
            if selector is None:
                continue
            value = str(action.get("value", ""))
            (self.frequency if parameter_id == "sg.frequency" else self.power).setText(
                value
            )
            index = selector.findData(str(action.get("mode", "unchanged")))
            if index >= 0:
                selector.setCurrentIndex(index)
            segments = action.get("segments")
            if isinstance(segments, list):
                self._working_segments[parameter_id] = [
                    dict(segment)
                    for segment in segments
                    if isinstance(segment, dict)
                ]
        self._selection_changed()

    def planned_parameter_actions(self) -> list[dict[str, object]]:
        actions: list[dict[str, object]] = []
        for parameter_id, _label, _dimension in self.parameter_specs:
            mode = str(self.parameter_selectors[parameter_id].currentData())
            if mode == "unchanged":
                continue
            action: dict[str, object] = {
                "parameter_id": parameter_id,
                "mode": mode,
                "value": (
                    self.frequency.text().strip()
                    if parameter_id == "sg.frequency"
                    else self.power.text().strip()
                ),
            }
            if mode == "sweep" and parameter_id in self._working_segments:
                action["segments"] = [
                    dict(segment)
                    for segment in self._working_segments[parameter_id]
                ]
            actions.append(action)
        return actions

    def accept(self) -> None:
        try:
            parse_quantity(self.frequency.text(), DIMENSION_FREQUENCY)
            parse_quantity(self.power.text(), DIMENSION_DBM)
        except ConfigurationError as exc:
            QMessageBox.warning(self, "Anritsu SG node", str(exc))
            return
        actions = self.planned_parameter_actions()
        sweeps = [action for action in actions if action["mode"] == "sweep"]
        if len(sweeps) > 1:
            QMessageBox.warning(
                self, "Anritsu SG node", "One SG node supports one sweep axis."
            )
            return
        if any("segments" not in action for action in sweeps):
            QMessageBox.warning(
                self, "Anritsu SG node", "Define ROI for the selected sweep parameter."
            )
            return
        if not actions:
            QMessageBox.information(
                self, "Anritsu SG node", "Select at least one parameter."
            )
            return
        super().accept()


class SweepLibraryButton(QToolButton):
    """Clickable library card that also exposes a stable drag payload."""

    mime_type = "application/x-lab-control-sweep-block"

    def __init__(self, drag_kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.drag_kind = drag_kind
        self.setProperty("dragKind", drag_kind)
        self._drag_start = None

    def drag_mime_data(self) -> QMimeData:
        mime = QMimeData()
        mime.setData(self.mime_type, self.drag_kind.encode("utf-8"))
        return mime

    def mousePressEvent(self, event: Any) -> None:
        self._drag_start = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if (
            self._drag_start is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and (event.position().toPoint() - self._drag_start).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            drag = QDrag(self)
            drag.setMimeData(self.drag_mime_data())
            drag.setPixmap(self.icon().pixmap(24, 24))
            drag.exec(Qt.DropAction.CopyAction)
            self._drag_start = None
            return
        super().mouseMoveEvent(event)


class RecipeTreeWidget(QTreeWidget):
    """Tree that requests a validated YAML move instead of mutating Qt items."""

    move_requested = Signal(str, str, str, int)
    library_drop_requested = Signal(str, str, str, int)
    library_mime_type = SweepLibraryButton.mime_type

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event: Any) -> None:
        if event.mimeData().hasFormat(self.library_mime_type):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: Any) -> None:
        if event.mimeData().hasFormat(self.library_mime_type):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: Any) -> None:
        target = self.itemAt(event.position().toPoint())
        if event.mimeData().hasFormat(self.library_mime_type):
            if target is None:
                event.ignore()
                return
            destination = self._drop_destination(target)
            if destination is None:
                event.ignore()
                return
            parent_id, branch, index = destination
            kind = bytes(event.mimeData().data(self.library_mime_type)).decode("utf-8")
            self.library_drop_requested.emit(kind, parent_id, branch, index)
            event.acceptProposedAction()
            return
        source = self.currentItem()
        if source is None or target is None:
            event.ignore()
            return
        source_node = source.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(source_node, RecipeNode):
            event.ignore()
            return
        destination = self._drop_destination(target)
        if destination is None:
            event.ignore()
            return
        parent_id, branch, index = destination
        self.move_requested.emit(source_node.id, parent_id, branch, index)
        event.accept()

    def _drop_destination(self, target: QTreeWidgetItem) -> tuple[str, str, int] | None:
        indicator = self.dropIndicatorPosition()
        target_node = target.data(0, Qt.ItemDataRole.UserRole)
        if (
            indicator is QAbstractItemView.DropIndicatorPosition.OnItem
            and isinstance(target_node, RecipeNode)
            and target_node.type in {"sequence", "sweep", "repeat", "if"}
        ):
            return target_node.id, "children", target.childCount()
        if target.text(0).startswith("Else") and target.parent() is not None:
            parent_node = target.parent().data(0, Qt.ItemDataRole.UserRole)
            if isinstance(parent_node, RecipeNode) and parent_node.type == "if":
                return parent_node.id, "else", target.childCount()

        parent = target.parent()
        if parent is None:
            return None
        index = parent.indexOfChild(target)
        if indicator is QAbstractItemView.DropIndicatorPosition.BelowItem:
            index += 1
        if parent.text(0).startswith("Finally"):
            return "__finally__", "children", index
        if parent.text(0).startswith("Else") and parent.parent() is not None:
            owner = parent.parent().data(0, Qt.ItemDataRole.UserRole)
            return (owner.id, "else", index) if isinstance(owner, RecipeNode) else None
        owner = parent.data(0, Qt.ItemDataRole.UserRole)
        return (owner.id, "children", index) if isinstance(owner, RecipeNode) else None


class RecipePage(QWidget):
    status = Signal(str)
    run_requested = Signal(object)
    plan_preflight_changed = Signal(object)
    operator_row_role = int(Qt.ItemDataRole.UserRole) + 17

    def __init__(self, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(
            """
            QFrame#recipeHero { border: 0; background: transparent; }
            QLabel#recipePageTitle { font-size: 16pt; font-weight: 700; }
            QToolBar#recipeCommandBar {
                background: transparent;
                border: 0;
                spacing: 5px;
                padding: 0;
            }
            QToolBar#recipeCommandBar QToolButton {
                border: 0;
                border-radius: 5px;
                padding: 5px 8px;
                background: transparent;
            }
            QToolBar#recipeCommandBar QToolButton:hover { background: rgba(40, 130, 210, 22); }
            QToolBar#recipeCommandBar::separator { width: 1px; background: rgba(122, 145, 167, 80); margin: 5px 7px; }
            QLabel#recipeProfileBadge {
                color: #18844c;
                border: 1px solid rgba(24, 132, 76, 100);
                border-radius: 11px;
                padding: 3px 9px;
                font-weight: 700;
            }
            QFrame#recipeActionBar { border: 0; background: transparent; }
            QFrame#recipeLibrary { border: 0; background: transparent; }
            QFrame#recipeInspector, QFrame#recipeEditorParameters {
                border: 1px solid rgba(122, 145, 167, 80);
                border-radius: 10px;
                background: palette(base);
            }
            QToolButton#recipeToolButton, QToolButton#recipePrimaryToolButton {
                border: 0;
                border-radius: 6px;
                padding: 6px 8px;
                background: transparent;
            }
            QToolButton#recipeToolButton:hover { background: rgba(40, 130, 210, 24); }
            QToolButton#recipePrimaryToolButton {
                background: rgba(23, 105, 170, 28);
                color: #1769aa;
            }
            QToolButton#recipePrimaryToolButton:hover { background: rgba(23, 105, 170, 48); }
            QToolButton#recipeLibraryButton {
                border: 0;
                border-radius: 7px;
                padding: 8px;
                min-height: 44px;
                text-align: left;
                background: palette(base);
            }
            QToolButton#recipeLibraryButton:hover {
                border-color: #2f82c6;
                background: rgba(40, 130, 210, 28);
            }
            QLabel#recipeLibraryHeading { font-weight: 800; font-size: 9pt; letter-spacing: 0.6px; }
            QFrame#recipeLibraryGroup { border: 0; background: transparent; }
            QLabel#recipeLibraryGroupTitle { font-weight: 700; font-size: 9pt; color: palette(mid); }
            QLabel#recipeLibraryBadge {
                border-radius: 7px;
                padding: 1px 5px;
                background: rgba(100, 130, 160, 20);
                font-size: 8pt;
            }
            QToolButton#recipeLibraryAction {
                border: 1px solid #e1e8ef;
                border-radius: 8px;
                padding: 9px 10px;
                min-height: 32px;
                text-align: left;
                background: #ffffff;
            }
            QToolButton#recipeLibraryAction:hover {
                border-color: #8cc4f2;
                background: #f1f7fc;
            }
            QToolButton#recipeLibraryAction[deviceKind="structure"] { color: #3479c9; }
            QToolButton#recipeLibraryAction[deviceKind="keithley"] { color: #c63e42; }
            QToolButton#recipeLibraryAction[deviceKind="rigol"] { color: #9a7100; }
            QToolButton#recipeLibraryAction[deviceKind="anritsu"] { color: #18844c; }
            QToolButton#recipeLibraryAction[deviceKind="timing"] { color: #7645bb; }
            QLabel#recipeHint {
                border-radius: 6px;
                padding: 7px;
                color: palette(mid);
                background: rgba(100, 130, 160, 12);
            }
            QTreeWidget#recipeTree { border: 0; background: palette(base); alternate-background-color: transparent; }
            QTreeWidget#recipeTree::item { min-height: 28px; padding: 3px 5px; border: 0; }
            QTreeWidget#recipeTree::item:hover { background: rgba(40, 130, 210, 16); }
            QTreeWidget#recipeTree::item:selected { background: rgba(40, 130, 210, 22); color: palette(text); border: 1px solid rgba(40, 130, 210, 110); }
            QHeaderView::section { background: transparent; border: 0; border-bottom: 1px solid rgba(122, 145, 167, 80); padding: 6px; font-size: 8pt; }
            QLabel#recipeLimitStatus[severity="ok"] {
                color: #19805c;
                background: rgba(25, 128, 92, 28);
                border-radius: 6px;
                padding: 7px;
            }
            QLabel#recipeLimitStatus[severity="blocker"] {
                color: #b4233a;
                background: rgba(180, 35, 58, 24);
                border-radius: 6px;
                padding: 7px;
            }
            """
        )
        self._settings = settings
        self._keithley_snapshot_provider = None
        self._rigol_snapshot_provider = None
        self._anritsu_snapshot_provider = None
        self._anritsu_sg_snapshot_provider = None
        # The Recipe Builder owns a high-density workspace.  Do not use
        # palette(base) here: on Windows it may resolve to a stale dark system
        # palette after the application has switched to the light stylesheet.
        self.setStyleSheet(
            self.styleSheet()
            + """
            QFrame#recipeInspector, QFrame#recipeEditorParameters {
                background: #ffffff;
                color: #17212b;
                border-color: #d9e2ec;
            }
            QTreeWidget#recipeTree {
                background: #ffffff;
                color: #17212b;
                border: 1px solid #d9e2ec;
                alternate-background-color: #f8fafc;
            }
            QTreeWidget#recipeTree::item { color: #17212b; }
            QTreeWidget#recipeTree::item:hover { background: #f1f7fc; }
            QTreeWidget#recipeTree::item:selected {
                background: #e8f3fd;
                color: #102a43;
                border: 1px solid #8cc4f2;
            }
            QTreeWidget#recipeTree QHeaderView::section {
                background: #f8fafc;
                color: #526273;
                border-bottom: 1px solid #d9e2ec;
            }
            """
        )
        self._plan = None
        self._preflight_thread: QThread | None = None
        self._preflight_worker: RecipePreflightWorker | None = None
        self._preflight_source: str | None = None
        self._repository = RecipeRepository()
        self._loading_source = False
        self._tree_undo: list[str] = []
        self._tree_redo: list[str] = []
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(750)
        self._autosave_timer.timeout.connect(self._autosave)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        hero = QFrame()
        hero.setObjectName("recipeHero")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(14, 10, 14, 10)
        title = QLabel("Sweep builder")
        title.setObjectName("recipePageTitle")
        hero_layout.addWidget(title)
        subtitle = QLabel(
            "Build the measurement from instrument blocks. Select a device on the left, "
            "then edit the selected block by double-clicking it in the plan."
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        hero_layout.addWidget(subtitle)
        layout.addWidget(hero)
        self.recipe_command_bar = QToolBar()
        self.recipe_command_bar.setObjectName("recipeCommandBar")
        self.recipe_command_bar.setMovable(False)
        self.recipe_command_bar.setIconSize(QSize(18, 18))
        self.recipe_command_bar.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        path_line = QHBoxLayout()
        self.path = _line("recipes/example_nested_sweep.yml", 42)
        self.restore_button = QPushButton("Restore autosave")
        self.restore_button.setEnabled(False)
        self.run_button = QPushButton("Run plan")
        self.run_button.setEnabled(False)

        def command_action(
            text: str,
            icon: QStyle.StandardPixmap,
            callback: Callable[[], None],
            *,
            shortcut: QKeySequence | QKeySequence.StandardKey | None = None,
        ) -> QAction:
            action = QAction(self.style().standardIcon(icon), text, self)
            action.setToolTip(text)
            action.setStatusTip(text)
            if shortcut is not None:
                action.setShortcut(shortcut)
            # QAction.triggered emits ``checked: bool``. Adapt that signal once
            # here so commands keep their natural no-argument API, including
            # methods with keyword-only options such as ``load_editor``.
            action.triggered.connect(
                lambda _checked=False, command=callback: command()
            )
            self.recipe_command_bar.addAction(action)
            return action

        self.new_recipe_action = command_action(
            "New",
            QStyle.StandardPixmap.SP_FileIcon,
            self.new_recipe,
            shortcut=QKeySequence.StandardKey.New,
        )
        self.load_recipe_action = command_action(
            "Load recipe",
            QStyle.StandardPixmap.SP_DialogOpenButton,
            self.browse_recipe,
            shortcut=QKeySequence.StandardKey.Open,
        )
        self.save_recipe_action = command_action(
            "Save recipe",
            QStyle.StandardPixmap.SP_DialogSaveButton,
            self.save_recipe,
            shortcut=QKeySequence.StandardKey.Save,
        )
        self.recipe_command_bar.addSeparator()
        self.undo_tree_action = command_action(
            "Undo",
            QStyle.StandardPixmap.SP_ArrowBack,
            self.undo_tree_edit,
            shortcut=QKeySequence.StandardKey.Undo,
        )
        self.redo_tree_action = command_action(
            "Redo",
            QStyle.StandardPixmap.SP_ArrowForward,
            self.redo_tree_edit,
            shortcut=QKeySequence.StandardKey.Redo,
        )
        self.recipe_command_bar.addSeparator()
        self.compile_recipe_action = command_action(
            "Validate & preview",
            QStyle.StandardPixmap.SP_DialogApplyButton,
            self.compile_recipe_async,
            shortcut=QKeySequence("Ctrl+Return"),
        )
        self.recipe_command_bar.addSeparator()
        self.library_visibility_action = QAction("Library", self)
        self.library_visibility_action.setCheckable(True)
        self.library_visibility_action.setChecked(True)
        self.library_visibility_action.setToolTip("Show or hide the node library")
        self.library_visibility_action.toggled.connect(
            lambda visible: self._set_workspace_panel_visible(
                "library", visible
            )
        )
        self.recipe_command_bar.addAction(self.library_visibility_action)
        self.inspector_visibility_action = QAction("Inspector", self)
        self.inspector_visibility_action.setCheckable(True)
        self.inspector_visibility_action.setChecked(True)
        self.inspector_visibility_action.setToolTip(
            "Show or hide the selected-node inspector"
        )
        self.inspector_visibility_action.toggled.connect(
            lambda visible: self._set_workspace_panel_visible(
                "inspector", visible
            )
        )
        self.recipe_command_bar.addAction(self.inspector_visibility_action)
        self.recipe_command_bar.addSeparator()
        self.recipe_profile_badge = QLabel(
            "Profile: LOCKED" if self._settings.outputs_locked else "Profile: APPROVED"
        )
        self.recipe_profile_badge.setObjectName("recipeProfileBadge")
        self.recipe_command_bar.addWidget(self.recipe_profile_badge)
        layout.addWidget(self.recipe_command_bar)
        path_line.addWidget(self.path, 1)
        path_line.addWidget(QLabel("Recipe file"))
        path_line.addWidget(self.restore_button)
        path_line.addWidget(self.run_button)
        layout.addLayout(path_line)
        action_frame = QFrame()
        action_frame.setObjectName("recipeActionBar")
        builder_actions = QHBoxLayout(action_frame)
        builder_actions.setContentsMargins(10, 7, 10, 7)
        builder_actions.setSpacing(6)
        self.node_kind = QComboBox()
        self.node_kind.addItem("Sequence group", "sequence")
        self.node_kind.addItem("Repeat group", "repeat")
        self.node_kind.addItem("If / else group", "if")
        self.node_kind.addItem("Wait", "wait")
        self.node_kind.addItem("Measure Keithley", "measure_keithley")
        self.node_kind.addItem("Acquire Anritsu reference", "acquire_reference")
        self.node_kind.addItem("Acquire Anritsu spectrum", "acquire_spectrum")
        self.node_kind.addItem("Keithley ramp to zero", "ramp_keithley_to_zero")
        self.node_kind.addItem("Keithley output OFF", "set_keithley_output")
        self.node_kind.addItem("Rigol output OFF", "set_rigol_output")
        self.node_kind.addItem("Anritsu SG ARM", "arm_anritsu_sg_output")
        self.node_kind.addItem("Anritsu SG RF ON", "set_anritsu_sg_output_on")
        self.node_kind.addItem("Anritsu SG output OFF", "set_anritsu_sg_output")
        self.node_kind.addItem("Comment", "comment")
        def tool_button(
            text: str, tooltip: str, icon: QStyle.StandardPixmap, *, primary: bool = False
        ) -> QToolButton:
            button = QToolButton()
            button.setText(text)
            button.setToolTip(tooltip)
            button.setIcon(self.style().standardIcon(icon))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setObjectName("recipePrimaryToolButton" if primary else "recipeToolButton")
            return button

        self.add_node_button = tool_button(
            "+ Add node", "Add the selected structural or measurement node", QStyle.StandardPixmap.SP_FileDialogNewFolder
        )
        self.add_controls_button = tool_button(
            "+ Device controls / point generator", "Choose instrument parameters and create a fixed setting or point generator", QStyle.StandardPixmap.SP_ComputerIcon, primary=True
        )
        self.edit_device_button = tool_button(
            "Device settings",
            "Open a separate configuration window for the selected instrument",
            QStyle.StandardPixmap.SP_ComputerIcon,
            primary=True,
        )
        self.edit_generator_button = tool_button(
            "Edit ROI",
            "Open only the point/interval editor for the selected sweep",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
        )
        self.delete_node_button = tool_button(
            "Delete", "Delete the selected node (Delete)", QStyle.StandardPixmap.SP_TrashIcon
        )
        self.duplicate_node_button = tool_button(
            "Duplicate", "Duplicate the selected leaf node (Ctrl+D)", QStyle.StandardPixmap.SP_FileDialogContentsView
        )
        self.move_up_button = tool_button(
            "Up", "Move the selected node up (Alt+Up)", QStyle.StandardPixmap.SP_ArrowUp
        )
        self.move_down_button = tool_button(
            "Down", "Move the selected node down (Alt+Down)", QStyle.StandardPixmap.SP_ArrowDown
        )
        builder_actions.addWidget(self.edit_device_button)
        builder_actions.addWidget(self.edit_generator_button)
        builder_actions.addWidget(self.delete_node_button)
        builder_actions.addWidget(self.duplicate_node_button)
        builder_actions.addWidget(self.move_up_button)
        builder_actions.addWidget(self.move_down_button)
        builder_actions.addStretch(1)
        layout.addWidget(action_frame)
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setObjectName("recipeWorkspaceSplitter")
        self.workspace_splitter.setChildrenCollapsible(False)
        self._workspace_layout_updating = False
        self._workspace_user_resized = False
        self._last_workspace_layout_width = -1
        self._workspace_visibility_override: dict[str, bool | None] = {
            "library": None,
            "inspector": None,
        }
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Declarative YAML recipe — no Python code and no raw SCPI.")
        self.editor.setMinimumWidth(320)
        self.tree = RecipeTreeWidget()
        self.tree.setObjectName("recipeTree")
        self.tree.setHeaderLabels(["Measurement sequence", "Role / expansion", "Status"])
        tree_header = self.tree.header()
        tree_header.setStretchLastSection(False)
        tree_header.setMinimumSectionSize(72)
        tree_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        tree_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 190)
        self.tree.setColumnWidth(2, 92)
        self.tree.setAlternatingRowColors(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setToolTip(
            "Drag a non-root node to reorder it or place it inside Sequence, Sweep, Repeat or If. "
            "The YAML is re-parsed immediately; nodes cannot cross the finally boundary."
        )
        self.builder_tabs = QTabWidget()
        self.builder_tabs.setMinimumWidth(390)
        self.builder_tabs.addTab(self.tree, "Measurement tree")
        self.builder_tabs.addTab(self.editor, "YAML source")
        self.library_panel = self._build_device_library()
        self.workspace_splitter.addWidget(self.library_panel)
        self.workspace_splitter.addWidget(self.builder_tabs)
        self.inspector_panel = QWidget()
        self.inspector_panel.setMinimumWidth(280)
        self.inspector_panel.setMaximumWidth(620)
        inspector_layout = QVBoxLayout(self.inspector_panel)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_card = QFrame()
        inspector_card.setObjectName("recipeInspector")
        inspector_card_layout = QVBoxLayout(inspector_card)
        inspector_card_layout.setContentsMargins(12, 12, 12, 12)
        inspector_title = QLabel("Inspector")
        inspector_title.setObjectName("sectionTitle")
        self.inspector_summary = QLabel("Select a node to see its measurement role and configuration.")
        self.inspector_summary.setWordWrap(True)
        self.inspector_summary.setObjectName("muted")
        self.open_editor_button = QPushButton("Open parameter editor")
        self.open_editor_button.setObjectName("primaryButton")
        self.open_editor_button.setEnabled(False)
        self.inspector = QPlainTextEdit()
        self.inspector.setReadOnly(True)
        self.inspector.setPlaceholderText("Select a recipe node to inspect its fields and expansion.")
        inspector_card_layout.addWidget(inspector_title)
        inspector_card_layout.addWidget(self.inspector_summary)
        inspector_card_layout.addWidget(self.open_editor_button)
        inspector_card_layout.addWidget(self.inspector, 1)
        inspector_layout.addWidget(inspector_card, 1)
        self.workspace_splitter.addWidget(self.inspector_panel)
        self.workspace_splitter.setStretchFactor(0, 1)
        self.workspace_splitter.setStretchFactor(1, 4)
        self.workspace_splitter.setStretchFactor(2, 2)
        self.workspace_splitter.splitterMoved.connect(self._workspace_splitter_moved)
        layout.addWidget(self.workspace_splitter, 1)
        self._update_workspace_layout(force=True)
        self.version_label = QLabel("No saved version history.")
        self.version_label.setObjectName("muted")
        layout.addWidget(self.version_label)
        self.summary = QLabel("The recipe has not been compiled.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.restore_button.clicked.connect(self.restore_autosave)
        self.run_button.clicked.connect(self.request_run)
        self.editor.textChanged.connect(self._source_changed)
        self.tree.currentItemChanged.connect(self._node_selected)
        self.tree.itemClicked.connect(self._operator_row_clicked)
        self.tree.itemDoubleClicked.connect(self._open_node_editor)
        self.tree.move_requested.connect(self._move_recipe_node)
        self.tree.library_drop_requested.connect(self._drop_library_block)
        self.add_node_button.clicked.connect(self._add_basic_node)
        self.add_controls_button.clicked.connect(self._add_device_controls)
        self.edit_device_button.clicked.connect(
            self._edit_selected_device_settings
        )
        self.edit_generator_button.clicked.connect(self._edit_selected_roi)
        self.delete_node_button.clicked.connect(self._delete_selected_node)
        self.duplicate_node_button.clicked.connect(self._duplicate_selected_node)
        self.move_up_button.clicked.connect(lambda: self._move_selected_sibling(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selected_sibling(1))
        self.open_editor_button.clicked.connect(self._open_current_node_editor)
        self.path.editingFinished.connect(self._update_repository_state)
        self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)
        self._tree_shortcuts = (
            QShortcut(QKeySequence("Delete"), self.tree, activated=self._delete_selected_node),
            QShortcut(QKeySequence("Ctrl+D"), self.tree, activated=self._duplicate_selected_node),
            QShortcut(QKeySequence("Alt+Up"), self.tree, activated=lambda: self._move_selected_sibling(-1)),
            QShortcut(QKeySequence("Alt+Down"), self.tree, activated=lambda: self._move_selected_sibling(1)),
            QShortcut(QKeySequence("Ctrl+Shift+G"), self.tree, activated=self._edit_selected_node),
            QShortcut(QKeySequence("Return"), self.tree, activated=self._edit_selected_node),
            QShortcut(QKeySequence("Enter"), self.tree, activated=self._edit_selected_node),
        )
        self.load_editor(show_error=False)

    def _workspace_splitter_moved(self, _position: int, _index: int) -> None:
        if not self._workspace_layout_updating:
            self._workspace_user_resized = True

    def _set_workspace_panel_visible(self, panel: str, visible: bool) -> None:
        self._workspace_visibility_override[panel] = visible
        widget = (
            getattr(self, "library_panel", None)
            if panel == "library"
            else getattr(self, "inspector_panel", None)
        )
        if isinstance(widget, QWidget):
            widget.setVisible(visible)
            self._workspace_user_resized = False
            self._update_workspace_layout(force=True)

    def _update_workspace_layout(self, *, force: bool = False) -> None:
        """Keep Library, measurement tree and Inspector useful at every width."""

        if not hasattr(self, "workspace_splitter"):
            return
        available = max(480, self.workspace_splitter.width() or self.width() - 28)
        if not force and (
            self._workspace_user_resized
            or abs(available - self._last_workspace_layout_width) < 24
        ):
            return
        self._last_workspace_layout_width = available
        inspector_override = self._workspace_visibility_override["inspector"]
        library_override = self._workspace_visibility_override["library"]
        show_inspector = (
            available >= 1_200
            if inspector_override is None
            else inspector_override
        )
        show_library = (
            available >= 760 if library_override is None else library_override
        )
        for action, widget, visible in (
            (
                self.library_visibility_action,
                self.library_panel,
                show_library,
            ),
            (
                self.inspector_visibility_action,
                self.inspector_panel,
                show_inspector,
            ),
        ):
            action.blockSignals(True)
            action.setChecked(visible)
            action.blockSignals(False)
            widget.setVisible(visible)
        if not show_library:
            sizes = [0, available, 0]
            self._workspace_layout_updating = True
            try:
                self.workspace_splitter.setSizes(sizes)
            finally:
                self._workspace_layout_updating = False
            return
        if not show_inspector:
            library = max(210, min(260, round(available * 0.24)))
            sizes = [library, max(390, available - library), 0]
            self._workspace_layout_updating = True
            try:
                self.workspace_splitter.setSizes(sizes)
            finally:
                self._workspace_layout_updating = False
            return
        if available >= 1600:
            library = max(270, min(340, round(available * 0.17)))
            inspector = max(420, min(560, round(available * 0.25)))
        elif available >= 1250:
            library = max(235, min(300, round(available * 0.19)))
            inspector = max(340, min(450, round(available * 0.27)))
        else:
            library = max(210, min(245, round(available * 0.21)))
            inspector = max(280, min(330, round(available * 0.28)))
        tree = max(390, available - library - inspector)
        self._workspace_layout_updating = True
        try:
            self.workspace_splitter.setSizes([library, tree, inspector])
        finally:
            self._workspace_layout_updating = False

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._update_workspace_layout()

    def _build_device_library(self) -> QFrame:
        """Build a dense, searchable node library modelled after the operator workspace."""

        scroll = QScrollArea()
        scroll.setObjectName("recipeLibraryScroll")
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(210)
        scroll.setMaximumWidth(400)
        panel = QFrame()
        panel.setObjectName("recipeLibrary")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(7)
        heading = QLabel("NODE LIBRARY")
        heading.setObjectName("recipeLibraryHeading")
        layout.addWidget(heading)
        self.library_search = _line("", 24)
        self.library_search.setPlaceholderText("Search nodes and actions…")
        self.library_search.setClearButtonEnabled(True)
        layout.addWidget(self.library_search)
        self._library_action_buttons: list[QToolButton] = []

        def group(title: str, badge: str) -> QVBoxLayout:
            group_frame = QFrame()
            group_frame.setObjectName("recipeLibraryGroup")
            group_layout = QVBoxLayout(group_frame)
            group_layout.setContentsMargins(7, 6, 7, 7)
            group_layout.setSpacing(2)
            caption = QHBoxLayout()
            label = QLabel(title.upper())
            label.setObjectName("recipeLibraryGroupTitle")
            count = QLabel(badge)
            count.setObjectName("recipeLibraryBadge")
            caption.addWidget(label)
            caption.addStretch(1)
            caption.addWidget(count)
            group_layout.addLayout(caption)
            layout.addWidget(group_frame)
            return group_layout

        def action(
            target_layout: QVBoxLayout,
            text: str,
            description: str,
            kind: str,
            icon: QStyle.StandardPixmap,
            callback: object,
            *,
            drag_kind: str,
        ) -> None:
            button = SweepLibraryButton(drag_kind)
            button.setObjectName("recipeLibraryAction")
            button.setProperty("deviceKind", kind)
            button.setText(text)
            button.setToolTip(description)
            button.setIcon(self.style().standardIcon(icon))
            button.setIconSize(QSize(18, 18))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setSizePolicy(button.sizePolicy().horizontalPolicy(), button.sizePolicy().verticalPolicy())
            button.clicked.connect(callback)  # type: ignore[arg-type]
            target_layout.addWidget(button)
            self._library_action_buttons.append(button)

        devices = group("Devices", "4")
        action(
            devices, "Keithley 2600", "Source-measure unit module", "keithley",
            QStyle.StandardPixmap.SP_DriveHDIcon,
            lambda: self._library_add_device("keithley"),
            drag_kind="device:keithley",
        )
        action(
            devices, "Rigol DG1032Z", "Function generator module", "rigol",
            QStyle.StandardPixmap.SP_DriveFDIcon,
            lambda: self._library_add_device("rigol"),
            drag_kind="device:rigol",
        )
        action(
            devices, "Anritsu configuration", "Configure Spectrum Analyzer settings once", "anritsu",
            QStyle.StandardPixmap.SP_ComputerIcon,
            lambda: self._library_add_device("anritsu"),
            drag_kind="device:anritsu",
        )
        action(
            devices,
            "Anritsu signal generator",
            "Configure RF frequency or power; RF remains OFF until explicit ARM/ON steps",
            "anritsu",
            QStyle.StandardPixmap.SP_ComputerIcon,
            lambda: self._library_add_device("anritsu_sg"),
            drag_kind="device:anritsu_sg",
        )

        acquisition = group("Acquisition", "2")
        action(
            acquisition,
            "Acquire reference",
            "Acquire and freeze the Anritsu reference used by processed checkpoints",
            "anritsu",
            QStyle.StandardPixmap.SP_DialogSaveButton,
            lambda: self._library_add_basic("acquire_reference"),
            drag_kind="flow:acquire_reference",
        )
        action(
            acquisition,
            "Acquire spectrum once",
            "Read one qualified TRAC1 spectrum at the current loop point",
            "anritsu",
            QStyle.StandardPixmap.SP_MediaPlay,
            lambda: self._library_add_basic("acquire_spectrum"),
            drag_kind="flow:acquire_spectrum",
        )

        flow = group("Flow", "6")
        action(
            flow, "Wait", "Add a settling delay", "timing",
            QStyle.StandardPixmap.SP_BrowserReload,
            lambda: self._library_add_basic("wait"),
            drag_kind="flow:wait",
        )
        action(
            flow, "Sequence / group", "Add a sequential container", "structure",
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
            lambda: self._library_add_basic("sequence"),
            drag_kind="flow:sequence",
        )
        action(
            flow, "Repeat", "Add a repeat container", "structure",
            QStyle.StandardPixmap.SP_DirIcon,
            lambda: self._library_add_basic("repeat"),
            drag_kind="flow:repeat",
        )
        action(
            flow, "Comment", "Document this part of the measurement", "structure",
            QStyle.StandardPixmap.SP_MessageBoxInformation,
            lambda: self._library_add_basic("comment"),
            drag_kind="flow:comment",
        )
        action(
            flow,
            "Anritsu SG ARM",
            "Create one short-lived permission token for the next RF ON step",
            "anritsu",
            QStyle.StandardPixmap.SP_DialogApplyButton,
            lambda: self._library_add_basic("arm_anritsu_sg_output"),
            drag_kind="flow:arm_anritsu_sg_output",
        )
        action(
            flow,
            "Anritsu SG RF ON",
            "Enable RF using a fresh ARM token; blocked by preflight unless approved",
            "anritsu",
            QStyle.StandardPixmap.SP_MediaPlay,
            lambda: self._library_add_basic("set_anritsu_sg_output_on"),
            drag_kind="flow:set_anritsu_sg_output_on",
        )
        layout.addStretch(1)
        hint = QLabel(
            "Drag a device or flow block into the measurement tree. "
            "Every device block must be configured before validation; incomplete "
            "blocks are rejected without executing their children."
        )
        hint.setObjectName("recipeHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.library_search.textChanged.connect(self._filter_library_actions)
        scroll.setWidget(panel)
        return scroll

    def _filter_library_actions(self, query: str) -> None:
        needle = query.strip().casefold()
        for button in self._library_action_buttons:
            haystack = f"{button.text()} {button.toolTip()}".casefold()
            button.setVisible(not needle or needle in haystack)

    def _library_add_basic(
        self,
        kind: str,
        *,
        parent_id: str | None = None,
        branch: str | None = None,
        index: int | None = None,
    ) -> None:
        combo_index = self.node_kind.findData(kind)
        if combo_index < 0:
            raise ConfigurationError(f"Library node kind {kind!r} is not registered.")
        self.node_kind.setCurrentIndex(combo_index)
        self._add_basic_node(
            parent_id=parent_id,
            branch=branch,
            insert_index=index,
        )

    def _library_add_device(
        self,
        device: str,
        *,
        parent_id: str | None = None,
        branch: str | None = None,
        index: int | None = None,
    ) -> None:
        labels = {
            "keithley": "Keithley 2600",
            "rigol": "Rigol DG1032Z",
            "anritsu": "Anritsu MS2830A",
            "anritsu_sg": "Anritsu MS2830A Signal Generator",
        }
        if device not in labels:
            raise ConfigurationError(f"Unknown device module {device!r}.")
        if parent_id is None or branch is None:
            parent_id, branch = self._builder_parent()
        if device == "anritsu_sg":
            node = {
                "id": self._new_node_id("anritsu-sg"),
                "type": "sequence",
                "text": "Anritsu SG — configuration required · RF OFF",
                "device_module": "anritsu_sg",
                "label": labels[device],
                "operation": "configure_selected_parameters",
                "configuration_required": True,
                "parameter_actions": [],
                "children": [],
            }
        elif device == "anritsu":
            node = {
                "id": self._new_node_id("anritsu-spectrum"),
                "type": "sequence",
                "text": "Anritsu MS2830A · acquire single spectrum",
                "trace": "TRAC1",
                "device_module": "anritsu",
                "label": labels[device],
                "operation": "configure_selected_parameters",
                "configuration_required": True,
                "acquire_single": False,
                "parameter_actions": [],
                "children": [],
            }
        else:
            node = {
                "id": self._new_node_id(device),
                "type": "sequence",
                "text": f"{labels[device]} — configuration required",
                "device_module": device,
                "label": labels[device],
                "configuration_required": True,
                "children": [
                    {
                        "id": self._new_node_id("comment"),
                        "type": "comment",
                        "text": "Drop nested devices or flow actions here",
                    }
                ],
            }
        source = add_recipe_node(
            self.editor.toPlainText(),
            parent_id=parent_id,
            branch=branch,
            index=index,
            node=node,
        )
        if device == "anritsu_sg":
            self._apply_builder_source(
                source, "Added editable Anritsu signal-generator module"
            )
            self.summary.setText(
                "Anritsu SG added. Double-click it to configure frequency/power; "
                "RF remains OFF until explicit ARM/ON actions."
            )
        elif device == "anritsu":
            self._apply_builder_source(
                source, "Added editable Anritsu spectrum module"
            )
            self.summary.setText(
                "Anritsu configuration added. Double-click it to select Spectrum "
                "settings. Add Acquire spectrum once separately inside a loop."
            )
        else:
            self._apply_builder_source(source, f"Added {labels[device]} module placeholder")
            self.summary.setText(
                f"{labels[device]} added — configuration required. "
                "This placeholder cannot execute instrument commands."
            )

    def _drop_library_block(
        self,
        drag_kind: str,
        parent_id: str,
        branch: str,
        index: int,
    ) -> None:
        try:
            category, separator, kind = drag_kind.partition(":")
            if not separator:
                return
            if category == "device":
                self._library_add_device(
                    kind, parent_id=parent_id, branch=branch, index=index
                )
                return
            if category != "flow":
                return
            self._library_add_basic(
                kind,
                parent_id=parent_id,
                branch=branch,
                index=index,
            )
        except Exception as exc:
            self.status.emit(f"Library drop rejected without changing the plan: {exc}")
            QMessageBox.warning(
                self,
                "Cannot add block",
                f"The block was not added. The existing measurement tree is unchanged.\n\n{exc}",
            )

    def _library_add_fixed_keithley(self, mode: str) -> None:
        definition = next(
            item
            for item in _SWEEPABLE_PARAMETERS
            if item["target"] == f"keithley.B.{mode}"
        )
        dialog = FixedValueDialog(definition, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            parent_id, branch = self._builder_parent()
            source = add_recipe_node(
                self.editor.toPlainText(),
                parent_id=parent_id,
                branch=branch,
                node=self._fixed_node_from_dialog(definition, dialog),
            )
            self._apply_builder_source(source, f"Added fixed Keithley {mode} setting")
        except Exception as exc:
            QMessageBox.warning(self, "Add Keithley setting", str(exc))

    def set_settings(self, settings: StationSettings) -> None:
        self._settings = settings
        self.recipe_profile_badge.setText(
            "Profile: LOCKED" if settings.outputs_locked else "Profile: APPROVED"
        )
        self._plan = None
        self.run_button.setEnabled(False)
        self.plan_preflight_changed.emit(None)

    def set_keithley_snapshot_provider(self, provider: object) -> None:
        """Bind read-only access to the manual Keithley form state."""

        self._keithley_snapshot_provider = provider

    def set_rigol_snapshot_provider(self, provider: object) -> None:
        """Bind read-only access to the manual Rigol carrier form state."""

        self._rigol_snapshot_provider = provider

    def set_anritsu_snapshot_provider(self, provider: object) -> None:
        """Bind read-only access to the manual Anritsu spectrum form state."""

        self._anritsu_snapshot_provider = provider

    def set_anritsu_sg_snapshot_provider(self, provider: object) -> None:
        """Bind read-only access to the manual Anritsu SG form state."""

        self._anritsu_sg_snapshot_provider = provider

    def _source_changed(self) -> None:
        if self._loading_source:
            return
        self._plan = None
        self.run_button.setEnabled(False)
        self.plan_preflight_changed.emit(None)
        self.summary.setText("YAML changed; compile it again before running.")
        self._autosave_timer.start()

    def load_editor(self, *, show_error: bool = True) -> None:
        try:
            source = Path(self.path.text()).read_text(encoding="utf-8")
        except Exception as exc:
            if show_error:
                QMessageBox.warning(self, "Recipe", f"Cannot load YAML: {exc}")
            else:
                self.summary.setText(f"Example not loaded: {exc}")
            return
        self._loading_source = True
        self.editor.setPlainText(source)
        self._loading_source = False
        self._tree_undo.clear()
        self._tree_redo.clear()
        self._update_tree_history_controls()
        self._plan = None
        self.run_button.setEnabled(False)
        self.plan_preflight_changed.emit(None)
        try:
            recipe = parse_recipe_text(source, origin=self.path.text())
            self._populate_recipe_tree(recipe.root, recipe.finally_nodes, None)
            self.summary.setText("Recipe loaded. Compile it before running.")
        except Exception as exc:
            self._emit_tree_diagnostic(
                "TREE_LOAD_REJECTED",
                path=self.path.text(),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            self.summary.setText(
                "Recipe could not be rendered. The previous tree remains visible; "
                "see Event log for diagnostics."
            )
        self._update_repository_state()
        self.status.emit("Recipe loaded into the editor")

    def new_recipe(self, *, confirm: bool = True) -> None:
        """Start an empty, valid plan without touching the currently saved file."""

        if confirm and self.editor.toPlainText().strip():
            answer = QMessageBox.question(
                self,
                "New sweep",
                "Start a new empty sweep? Unsaved changes in the editor will be replaced.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        source = (
            "schema_version: 1\n"
            "name: Untitled sweep\n"
            "root:\n"
            "  id: sequence-main\n"
            "  type: sequence\n"
            "  children: []\n"
            "finally: []\n"
        )
        self.path.setText(str(Path("recipes") / "untitled_sweep.yml"))
        self._apply_builder_source(source, "Created a new empty sweep")
        self._tree_undo.clear()
        self._tree_redo.clear()
        self._update_tree_history_controls()
        self.status.emit("New empty sweep ready")

    def browse_recipe(self) -> None:
        """Choose a YAML recipe with the native file explorer and load it."""

        current_text = self.path.text().strip()
        current = Path(current_text).expanduser() if current_text else Path("recipes")
        if not current.is_absolute():
            current = (Path.cwd() / current).resolve()
        initial_location = current if current.exists() else current.parent
        selected, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Open sweep recipe",
            str(initial_location),
            "YAML recipes (*.yml *.yaml);;All files (*)",
        )
        if not selected:
            return
        self.path.setText(str(Path(selected)))
        self.load_editor()

    def save_recipe(self) -> None:
        source = self.editor.toPlainText()
        try:
            result = self._repository.save(self.path.text(), source)
        except Exception as exc:
            QMessageBox.warning(self, "Recipe", f"YAML was not saved: {exc}")
            return
        self.restore_button.setEnabled(False)
        self._update_repository_state()
        suffix = f"; previous version: {result.backup_path}" if result.backup_path else ""
        self.status.emit(f"Recipe saved atomically: {result.path}{suffix}")

    def restore_autosave(self) -> None:
        source = self._repository.load_recovery(self.path.text())
        if source is None:
            self.restore_button.setEnabled(False)
            return
        self.editor.setPlainText(source)
        self._tree_undo.clear()
        self._tree_redo.clear()
        self._update_tree_history_controls()
        self.restore_button.setEnabled(False)
        self.status.emit("Unsaved recipe recovery restored into the editor")

    def _autosave(self) -> None:
        source = self.editor.toPlainText()
        if not source.strip() or not self.path.text().strip():
            return
        try:
            recovery = self._repository.autosave(self.path.text(), source)
        except Exception as exc:
            self.status.emit(f"Recipe autosave failed: {exc}")
            return
        self.restore_button.setEnabled(True)
        self.restore_button.setToolTip(f"Unsaved editor recovery: {recovery}")

    def _update_repository_state(self) -> None:
        versions = self._repository.versions(self.path.text())
        recovery = self._repository.has_newer_recovery(self.path.text())
        self.restore_button.setEnabled(recovery)
        self.version_label.setText(
            f"Immutable previous versions: {len(versions)}"
            + (" • newer autosave recovery available" if recovery else "")
        )

    def compile_recipe(self) -> None:
        """Synchronous preflight API used by deterministic tests and automation."""

        try:
            recipe = parse_recipe_text(self.editor.toPlainText(), origin=self.path.text())
            plan = RecipeCompiler(self._settings).compile(recipe)
            estimate = PlanEstimator(self._settings).estimate(plan)
        except Exception as exc:
            self._emit_tree_diagnostic(
                "TREE_COMPILE_REJECTED",
                path=self.path.text(),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            QMessageBox.warning(self, "Recipe", str(exc))
            return
        self._accept_preflight(recipe, plan, estimate)

    def compile_recipe_async(self) -> None:
        """Run operator-triggered validation without blocking the Qt event loop."""

        if (
            self._preflight_thread is not None
            and self._preflight_thread.isRunning()
        ):
            self._preflight_thread.requestInterruption()
            self.compile_recipe_action.setEnabled(False)
            self.compile_recipe_action.setText("Cancelling…")
            self.summary.setText("Cancelling recipe validation…")
            return
        source = self.editor.toPlainText()
        self._plan = None
        self.run_button.setEnabled(False)
        self.plan_preflight_changed.emit(None)
        self._preflight_source = source
        self.compile_recipe_action.setText("Cancel validation")
        self.summary.setText(
            "Validating recipe and estimating time/data size in the background…"
        )
        self.status.emit("Recipe validation started")
        thread = QThread(self)
        worker = RecipePreflightWorker(
            self._settings, source, self.path.text()
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._preflight_succeeded)
        worker.failed.connect(self._preflight_failed)
        worker.cancelled.connect(self._preflight_cancelled)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._preflight_finished)
        thread.finished.connect(thread.deleteLater)
        self._preflight_thread = thread
        self._preflight_worker = worker
        thread.start()

    def _preflight_succeeded(
        self, recipe: object, plan: object, estimate: object
    ) -> None:
        if self._preflight_source != self.editor.toPlainText():
            self.summary.setText(
                "Recipe changed during validation; the stale result was discarded. "
                "Validate the current source again."
            )
            self.status.emit("Stale recipe validation discarded")
            return
        if not isinstance(plan, ExecutionPlan) or not isinstance(
            estimate, PlanEstimate
        ):
            self._preflight_failed("Preflight returned an invalid result.")
            return
        if not hasattr(recipe, "root") or not hasattr(recipe, "finally_nodes"):
            self._preflight_failed("Preflight returned an invalid recipe snapshot.")
            return
        self._accept_preflight(recipe, plan, estimate)

    def _preflight_failed(self, error: str) -> None:
        self._emit_tree_diagnostic(
            "TREE_COMPILE_REJECTED",
            path=self.path.text(),
            error_type="BackgroundPreflightError",
            error=error,
        )
        self.summary.setText(f"Validation blocked: {error}")
        QMessageBox.warning(self, "Recipe", error)

    def _preflight_cancelled(self) -> None:
        self.summary.setText("Recipe validation cancelled; Run remains disabled.")
        self.status.emit("Recipe validation cancelled")

    def _preflight_finished(self) -> None:
        self.compile_recipe_action.setText("Validate & preview")
        self.compile_recipe_action.setEnabled(True)
        self._preflight_thread = None
        self._preflight_worker = None
        self._preflight_source = None

    def cancel_preflight(self, *, wait_ms: int = 3_000) -> None:
        """Stop an in-flight validation before the owning window is destroyed."""

        thread = self._preflight_thread
        if thread is None or not thread.isRunning():
            return
        thread.requestInterruption()
        thread.quit()
        thread.wait(wait_ms)

    def _accept_preflight(
        self, recipe: object, plan: ExecutionPlan, estimate: PlanEstimate
    ) -> None:
        self._plan = plan
        self.run_button.setEnabled(True)
        self._populate_recipe_tree(recipe.root, recipe.finally_nodes, plan)  # type: ignore[attr-defined]
        self.summary.setText(
            f"Plan: {len(plan.actions)} actions • {plan.total_points} checkpoints • "
            f"{plan.total_spectra} spectra • hash {plan.sha256}\n"
            f"Estimated nominal duration: {_human_duration(estimate.nominal_duration_s)} • "
            f"retry upper model: {_human_duration(estimate.retry_upper_duration_s)}\n"
            f"Uncompressed data upper estimate: {_human_bytes(estimate.total_upper_bytes)} • "
            f"spectrum values: {estimate.spectrum_values:,}\n"
            + (
                "Warnings: " + " | ".join(estimate.warnings) + "\n"
                if estimate.warnings
                else "Warnings: none from static preflight\n"
            )
            + "Compilation sends no instrument commands. Execution requires an approved profile and Run Engine."
        )
        self.status.emit("Recipe compiled")
        self.plan_preflight_changed.emit((plan, estimate))

    def _populate_recipe_tree(
        self,
        root: RecipeNode,
        finally_nodes: tuple[RecipeNode, ...],
        plan: object | None,
    ) -> None:
        selected_id: str | None = None
        current = self.tree.currentItem()
        current_node = (
            current.data(0, Qt.ItemDataRole.UserRole)
            if current is not None
            else None
        )
        if isinstance(current_node, RecipeNode):
            selected_id = current_node.id
        expanded_ids: set[str] = set()

        def remember_expansion(item: QTreeWidgetItem) -> None:
            item_node = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(item_node, RecipeNode) and item.isExpanded():
                expanded_ids.add(item_node.id)
            for child_index in range(item.childCount()):
                remember_expansion(item.child(child_index))

        for top_index in range(self.tree.topLevelItemCount()):
            remember_expansion(self.tree.topLevelItem(top_index))
        previous_scroll = self.tree.verticalScrollBar().value()
        top_level_items: list[QTreeWidgetItem] = []
        occurrences: dict[str, int] = {}
        if plan is not None:
            for action in plan.actions:  # type: ignore[union-attr]
                occurrences[action.node_id] = occurrences.get(action.node_id, 0) + 1

        def add_node(node: RecipeNode, parent: QTreeWidgetItem | None = None) -> None:
            count = occurrences.get(node.id, 0)
            detail = node.type + (f" • {count} action(s)" if plan is not None else "")
            label, detail, icon = self._tree_presentation(node, count, plan is not None)
            if node.data.get("disabled") is True:
                label = f"Disabled — {label}"
                detail = "Skipped with all children"
            status_text, status_color = self._tree_status(node, detail)
            item = QTreeWidgetItem([label, detail, status_text])
            item.setIcon(0, self._tree_node_icon(node, icon))
            item.setToolTip(0, f"{node.id}\nDouble-click to edit; right-click for actions.")
            item.setToolTip(2, status_text.title())
            item.setData(2, Qt.ItemDataRole.ForegroundRole, QColor(status_color))
            status_font = item.font(2)
            status_font.setBold(True)
            status_font.setPointSize(8)
            item.setFont(2, status_font)
            item.setData(0, Qt.ItemDataRole.UserRole, node)
            if parent is None:
                top_level_items.append(item)
            else:
                parent.addChild(item)
            self._add_operator_control_rows(node, item)
            self._add_native_sweep_roi_rows(node, item)
            for child in node.children:
                add_node(child, item)
            if node.else_children:
                else_item = QTreeWidgetItem(["Else branch", "Conditional alternative", "●"])
                item.addChild(else_item)
                for child in node.else_children:
                    add_node(child, else_item)

        add_node(root)
        if finally_nodes:
            cleanup = QTreeWidgetItem(["Finally — safe shutdown", "Guaranteed cleanup", "●"])
            cleanup.setIcon(0, self._tree_badge_icon("Safety", "#455363", "OFF"))
            cleanup.setData(0, Qt.ItemDataRole.UserRole, None)
            top_level_items.append(cleanup)
            for node in finally_nodes:
                add_node(node, cleanup)
        # Commit only after every row, synthetic control and icon was built.
        # A presentation exception must never erase the operator's current tree.
        self.tree.clear()
        self.tree.addTopLevelItems(top_level_items)
        if expanded_ids:
            for node_id in expanded_ids:
                item = self._find_tree_item(node_id)
                if item is not None:
                    item.setExpanded(True)
        else:
            self.tree.expandAll()
        selected = (
            self._find_tree_item(selected_id) if selected_id is not None else None
        )
        if selected is not None:
            self.tree.setCurrentItem(selected)
        elif self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self.tree.verticalScrollBar().setValue(previous_scroll)

    @staticmethod
    def _keithley_parameter_label(node: RecipeNode, parameter_id: str) -> str:
        return {
            "source.level": (
                "Source current"
                if node.data.get("source_mode") == "current"
                else "Source voltage"
            ),
            "source.compliance": "Voltage compliance"
            if node.data.get("source_mode") == "current"
            else "Current compliance",
            "measurement.nplc": "NPLC",
            "measurement.settling_time": "Settling time",
            "measurement.sense_mode": "Sense mode",
            "source.range": "Source range",
            "measurement.voltage_range": "Measure V range",
            "measurement.current_range": "Measure I range",
        }.get(parameter_id, parameter_id)

    @staticmethod
    def _keithley_parameter_dimension(
        node: RecipeNode, parameter_id: str
    ) -> str | None:
        source_is_current = node.data.get("source_mode") == "current"
        return {
            "source.level": (
                DIMENSION_CURRENT if source_is_current else DIMENSION_VOLTAGE
            ),
            "source.compliance": (
                DIMENSION_VOLTAGE if source_is_current else DIMENSION_CURRENT
            ),
            "measurement.settling_time": DIMENSION_TIME,
        }.get(parameter_id)

    def _add_operator_control_rows(
        self, node: RecipeNode, parent: QTreeWidgetItem
    ) -> None:
        """Render device overrides as read-only operator controls below the module."""

        device_module = str(node.data.get("device_module", ""))
        if device_module not in {"keithley", "anritsu", "anritsu_sg", "rigol"}:
            return
        raw_actions = node.data.get("parameter_actions")
        actions = (
            [action for action in raw_actions if isinstance(action, dict)]
            if isinstance(raw_actions, list)
            else []
        )

        def informational_row(
            columns: list[str],
            *,
            color: str,
            badge: str,
            metadata: dict[str, object] | None = None,
            owner: QTreeWidgetItem = parent,
        ) -> QTreeWidgetItem:
            row = QTreeWidgetItem(columns)
            if metadata is None:
                row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            else:
                row.setData(0, self.operator_row_role, metadata)
                row.setToolTip(
                    0,
                    "Click to open the ROI editor directly for this sweep axis.",
                )
                link_font = row.font(0)
                link_font.setUnderline(True)
                row.setFont(0, link_font)
                row.setData(
                    0, Qt.ItemDataRole.ForegroundRole, QColor("#1769aa")
                )
            row.setIcon(0, self._tree_badge_icon("Operator control", color, badge))
            if metadata is None:
                row.setToolTip(
                    0,
                    "Operator control summary. Double-click the parent device node to edit it.",
                )
            row.setData(2, Qt.ItemDataRole.ForegroundRole, QColor(color))
            status_font = row.font(2)
            status_font.setBold(True)
            status_font.setPointSize(8)
            row.setFont(2, status_font)
            owner.addChild(row)
            return row

        for action in actions:
            parameter_id = str(action.get("parameter_id", ""))
            label = (
                self._keithley_parameter_label(node, parameter_id)
                if device_module == "keithley"
                else {
                    "spectrum.start_frequency": "Start frequency",
                    "spectrum.stop_frequency": "Stop frequency",
                    "spectrum.reference_level": "Reference level",
                    "spectrum.points": "Trace points",
                    "sg.frequency": "RF frequency",
                    "sg.power": "RF power",
                    "advanced.rbw_mode": "RBW mode",
                    "advanced.rbw": "Resolution bandwidth",
                    "advanced.vbw_mode": "VBW mode",
                    "advanced.vbw": "Video bandwidth",
                    "advanced.detector": "Detector",
                    "advanced.attenuation_mode": "RF attenuation mode",
                    "advanced.attenuation": "RF attenuation",
                    "advanced.preamplifier_enabled": "Preamplifier",
                    "advanced.sweep_time_mode": "Sweep-time mode",
                    "advanced.sweep_time": "Sweep time",
                }.get(parameter_id, parameter_id)
            )
            mode = str(action.get("mode", "set"))
            value = str(action.get("value", ""))
            if mode != "sweep":
                informational_row(
                    [label, f"Set to {value}", "SET"],
                    color="#536577",
                    badge="=",
                )
                continue
            segments = action.get("segments")
            dimension = (
                self._keithley_parameter_dimension(node, parameter_id)
                if device_module == "keithley"
                else {
                    "spectrum.start_frequency": DIMENSION_FREQUENCY,
                    "spectrum.stop_frequency": DIMENSION_FREQUENCY,
                      "spectrum.reference_level": DIMENSION_DBM,
                      "sg.frequency": DIMENSION_FREQUENCY,
                      "sg.power": DIMENSION_DBM,
                  }.get(parameter_id)
            )
            if not isinstance(segments, list) or not segments or dimension is None:
                informational_row(
                    [label, "Sweep range requires ROI definition", "ROI"],
                    color="#c77800",
                    badge="!",
                )
                continue
            try:
                generated = generate_sweep_points(segments, dimension)
                stages = generate_sweep_stage_points(segments, dimension)
                first_value = segments[0].get(
                    "value", segments[0].get("start", "?")
                )
                last_value = segments[-1].get(
                    "value", segments[-1].get("stop", "?")
                )
                sweep_detail = (
                    f"{first_value} → {last_value} · "
                    f"{len(generated):,} pts · {len(segments)} ROI"
                )
            except Exception:
                informational_row(
                    [label, "Invalid sweep range", "ERROR"],
                    color="#b4233a",
                    badge="!",
                )
                continue
            sweep_row = informational_row(
                [label, sweep_detail, "SWEEP"],
                color="#1769aa",
                badge="S",
                metadata={
                    "kind": "sweep_parameter",
                    "device_module": device_module,
                    "owner_node_id": node.id,
                    "parameter_id": parameter_id,
                    "stage_index": None,
                },
            )
            for index, (segment, stage_points) in enumerate(
                zip(segments, stages, strict=True), start=1
            ):
                single_value = segment.get("value")
                spacing = (
                    "Single value"
                    if single_value is not None
                    else str(segment.get("spacing", "linear")).title()
                )
                stage_range = (
                    str(single_value)
                    if single_value is not None
                    else f"{segment.get('start', '?')} → {segment.get('stop', '?')}"
                )
                stage = QTreeWidgetItem(
                    [
                        f"ROI {index}",
                        f"{stage_range} · {len(stage_points):,} pts · {spacing}",
                        "STAGE",
                    ]
                )
                stage.setData(
                    0,
                    self.operator_row_role,
                    {
                        "kind": "roi_stage",
                        "device_module": device_module,
                        "owner_node_id": node.id,
                        "parameter_id": parameter_id,
                        "stage_index": index - 1,
                    },
                )
                stage.setIcon(0, self._tree_badge_icon("ROI stage", "#6f8aa3", str(index)))
                stage_link_font = stage.font(0)
                stage_link_font.setUnderline(True)
                stage.setFont(0, stage_link_font)
                stage.setData(
                    0, Qt.ItemDataRole.ForegroundRole, QColor("#1769aa")
                )
                stage.setForeground(1, QColor("#647588"))
                stage.setForeground(2, QColor("#647588"))
                stage.setToolTip(
                    0,
                    "Click to open this stage directly in the ROI editor.",
                )
                sweep_row.addChild(stage)

        if device_module == "anritsu" and node.data.get("acquire_single"):
            informational_row(
                [
                    "Single spectrum",
                    f"Acquire {node.data.get('trace', 'TRAC1')} at each parent-loop point",
                    "ACQUIRE",
                ],
                color="#18844c",
                badge="A",
            )

        output_policy = str(node.data.get("output_policy", "unchanged"))
        if output_policy in {"on", "off"}:
            is_on = output_policy == "on"
            informational_row(
                [
                    "Output",
                    f"Switch {output_policy.upper()} when entering this node",
                    output_policy.upper(),
                ],
                color="#18844c" if is_on else "#536577",
                badge="ON" if is_on else "OFF",
            )

    @staticmethod
    def _native_sweep_segments(node: RecipeNode) -> list[dict[str, object]]:
        """Return one visual ROI contract for either supported sweep syntax."""

        raw_segments = node.data.get("segments")
        if isinstance(raw_segments, list) and raw_segments:
            return [
                dict(segment)
                for segment in raw_segments
                if isinstance(segment, dict)
            ]
        if node.type != "sweep":
            return []
        if not all(key in node.data for key in ("start", "stop", "points")):
            return []
        return [
            {
                "start": node.data["start"],
                "stop": node.data["stop"],
                "points": node.data["points"],
                "spacing": node.data.get("spacing", "linear"),
            }
        ]

    def _add_native_sweep_roi_rows(
        self, node: RecipeNode, parent: QTreeWidgetItem
    ) -> None:
        """Expose every native sweep interval as a direct, editable ROI row."""

        if node.type != "sweep":
            return
        target = str(node.data.get("target", ""))
        definition = next(
            (
                item
                for item in _SWEEPABLE_PARAMETERS
                if item["target"] == target
            ),
            None,
        )
        dimension = (
            definition["dimension"]
            if definition is not None
            else SWEEP_DIMENSIONS.get(target)
        )
        segments = self._native_sweep_segments(node)
        if dimension is None or not segments:
            return
        try:
            stages = generate_sweep_stage_points(segments, dimension)
        except Exception as exc:
            self._emit_tree_diagnostic(
                "TREE_ROI_RENDER_REJECTED",
                node_id=node.id,
                target=target,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return
        for index, (segment, stage_points) in enumerate(
            zip(segments, stages, strict=True), start=1
        ):
            single_value = segment.get("value")
            stage_range = (
                str(single_value)
                if single_value is not None
                else f"{segment.get('start', '?')} → {segment.get('stop', '?')}"
            )
            spacing = (
                "Single value"
                if single_value is not None
                else str(segment.get("spacing", "linear")).title()
            )
            row = QTreeWidgetItem(
                [
                    f"ROI {index}",
                    f"{stage_range} · {len(stage_points):,} pts · {spacing}",
                    "STAGE",
                ]
            )
            row.setData(
                0,
                self.operator_row_role,
                {
                    "kind": "native_sweep_roi",
                    "owner_node_id": node.id,
                    "parameter_id": target,
                    "stage_index": index - 1,
                },
            )
            row.setIcon(
                0, self._tree_badge_icon("ROI stage", "#6f8aa3", str(index))
            )
            link_font = row.font(0)
            link_font.setUnderline(True)
            row.setFont(0, link_font)
            row.setData(0, Qt.ItemDataRole.ForegroundRole, QColor("#1769aa"))
            row.setForeground(1, QColor("#647588"))
            row.setForeground(2, QColor("#647588"))
            row.setToolTip(0, "Click to edit this ROI without changing loop children.")
            parent.addChild(row)

    @staticmethod
    def _tree_status(node: RecipeNode, detail: str) -> tuple[str, str]:
        if node.data.get("disabled") is True:
            return "DISABLED", "#6b7785"
        if node.data.get("configuration_required"):
            return "SETUP", "#c77800"
        if (
            node.data.get("operation") == "configure_selected_parameters"
            and (
                node.data.get("device_module")
                not in {"keithley", "rigol", "anritsu", "anritsu_sg"}
                or not isinstance(node.data.get("configuration"), dict)
            )
        ):
            return "PREVIEW", "#b4233a"
        if detail.startswith("Sweep axis"):
            return "SWEEP", "#1769aa"
        if detail.startswith("Fixed setting") or detail.startswith("Fixed configuration"):
            return "FIXED", "#536577"
        if node.type in {"wait", "repeat", "sequence", "comment"}:
            return "FLOW", "#7253a6"
        if node.type.startswith("set_") or node.type.startswith("ramp_"):
            return "SAFE", "#18844c"
        if node.type in {"acquire_reference", "acquire_spectrum"}:
            return "ACQUIRE", "#18844c"
        return "READY", "#536577"

    def _tree_presentation(
        self, node: RecipeNode, count: int, compiled: bool
    ) -> tuple[str, str, QStyle.StandardPixmap]:
        """Translate YAML-oriented data into concise operator-facing tree rows."""

        detail = node.type.replace("_", " ")
        icon = QStyle.StandardPixmap.SP_FileIcon
        if node.type == "sequence" and not node.data.get("device_module"):
            return "Measurement sequence", "Container", QStyle.StandardPixmap.SP_FileDialogDetailedView
        if node.type == "sweep":
            target = str(node.data.get("target", ""))
            definition = next((item for item in _SWEEPABLE_PARAMETERS if item["target"] == target), None)
            label = definition["label"] if definition else target
            try:
                dimension = definition["dimension"] if definition else SWEEP_DIMENSIONS[target]
                segments = node.data.get("segments")
                if isinstance(segments, list):
                    points = generate_sweep_points(segments, dimension)
                else:
                    points = generate_sweep_points(
                        [{
                            "start": node.data["start"], "stop": node.data["stop"],
                            "points": node.data["points"], "spacing": node.data.get("spacing", "linear"),
                        }], dimension
                    )
                suffix = f" • {len(points):,} pts"
            except Exception:
                suffix = " • invalid points"
            return (
                f"{label} sweep{suffix}",
                "Sweep axis" + (f" • {count} action(s)" if compiled else ""),
                QStyle.StandardPixmap.SP_MediaPlay,
            )
        if node.type == "configure_keithley":
            return (
                f"Keithley {node.data.get('channel', '?')} · {node.data.get('mode', 'source')} = {node.data.get('level', '')}",
                "Fixed setting",
                QStyle.StandardPixmap.SP_DriveHDIcon,
            )
        if node.type == "configure_rigol":
            return f"Rigol CH{node.data.get('channel', '?')} configuration", "Fixed setting", QStyle.StandardPixmap.SP_MediaPlay
        if node.type == "configure_anritsu":
            return "Anritsu spectrum configuration", "Analyzer setting", QStyle.StandardPixmap.SP_ComputerIcon
        if node.type == "acquire_reference":
            return (
                f"Acquire reference spectrum - {node.data.get('trace', 'TRAC1')}",
                "Anritsu reference",
                QStyle.StandardPixmap.SP_DialogSaveButton,
            )
        if node.type == "acquire_spectrum":
            operation = str(node.data.get("reference_operation", "none"))
            if operation == "difference_db":
                return (
                    f"Acquire spectrum - {node.data.get('trace', 'TRAC1')} - "
                    "store raw + raw-reference",
                    "Anritsu processed acquisition",
                    QStyle.StandardPixmap.SP_MediaPlay,
                )
            return (
                f"Acquire single spectrum · {node.data.get('trace', 'TRAC1')}",
                "Anritsu acquisition",
                QStyle.StandardPixmap.SP_MediaPlay,
            )
        if node.type == "update_keithley_level":
            return (
                f"Set Keithley {node.data.get('channel', '?')} "
                f"{node.data.get('mode', 'source')} = {node.data.get('level', '')}",
                "Point update - OUTPUT unchanged",
                QStyle.StandardPixmap.SP_DriveHDIcon,
            )
        if node.type == "update_rigol_frequency":
            return (
                f"Set Rigol CH{node.data.get('channel', '?')} frequency = "
                f"{node.data.get('frequency', '')}",
                "Point update - OUTPUT unchanged",
                QStyle.StandardPixmap.SP_MediaPlay,
            )
        if node.type == "arm_anritsu_sg_output":
            return (
                "ARM Anritsu SG RF output",
                "One-shot safety permission",
                QStyle.StandardPixmap.SP_DialogApplyButton,
            )
        if node.type == "set_anritsu_sg_output":
            enabled = bool(node.data.get("enabled", False))
            return (
                f"Anritsu SG RF OUTPUT {'ON' if enabled else 'OFF'}",
                "Energization" if enabled else "Safety action",
                (
                    QStyle.StandardPixmap.SP_MediaPlay
                    if enabled
                    else QStyle.StandardPixmap.SP_BrowserStop
                ),
            )
        if node.type == "measure_keithley":
            return f"Measure Keithley {node.data.get('channel', '?')}", "Measurement", QStyle.StandardPixmap.SP_DialogApplyButton
        if node.type == "wait":
            return f"Wait {node.data.get('duration', '')}", "Timing", QStyle.StandardPixmap.SP_BrowserReload
        if node.type == "comment":
            text = " ".join(str(node.data.get("text", "")).split())
            return (
                text or "Empty comment",
                "Comment",
                QStyle.StandardPixmap.SP_FileIcon,
            )
        if node.data.get("device_module") == "anritsu_sg":
            actions = [
                action
                for action in node.data.get("parameter_actions", [])
                if isinstance(action, dict)
            ]
            sweep = next(
                (action for action in actions if action.get("mode") == "sweep"),
                None,
            )
            return (
                f"Anritsu SG · {len(actions)} parameter(s) · RF OFF",
                "Sweep axis" if sweep is not None else "Fixed configuration",
                QStyle.StandardPixmap.SP_ComputerIcon,
            )
        if node.data.get("device_module") == "anritsu":
            raw_actions = node.data.get("parameter_actions")
            actions = (
                [action for action in raw_actions if isinstance(action, dict)]
                if isinstance(raw_actions, list)
                else []
            )
            sweep = next(
                (action for action in actions if action.get("mode") == "sweep"),
                None,
            )
            acquisition = " · acquire once" if node.data.get("acquire_single") else ""
            return (
                f"Anritsu Spectrum · {len(actions)} parameter(s){acquisition}",
                "Sweep axis" if sweep is not None else "Fixed configuration",
                QStyle.StandardPixmap.SP_ComputerIcon,
            )
        if node.data.get("device_module") == "rigol":
            channel = int(node.data.get("channel", 1))
            output = str(node.data.get("output_policy", "unchanged"))
            output_label = (
                f"OUTPUT {output.upper()}"
                if output in {"on", "off"}
                else "OUTPUT unchanged"
            )
            return (
                f"Rigol CH{channel} · {output_label}",
                "Fixed configuration",
                QStyle.StandardPixmap.SP_DriveFDIcon,
            )
        if node.data.get("device_module"):
            raw_actions = node.data.get("parameter_actions")
            actions = (
                [action for action in raw_actions if isinstance(action, dict)]
                if isinstance(raw_actions, list)
                else []
            )
            output_only = str(node.data.get("output_policy", "unchanged"))
            if (
                node.data.get("device_module") == "keithley"
                and not actions
                and output_only in {"on", "off"}
            ):
                return (
                    f"Keithley {node.data.get('channel', '?')} · "
                    f"OUTPUT {output_only.upper()}",
                    "Fixed configuration",
                    QStyle.StandardPixmap.SP_BrowserStop,
                )
            if node.data.get("device_module") == "keithley" and actions:
                def action_label(action: dict[str, object]) -> str:
                    parameter_id = str(action.get("parameter_id", "parameter"))
                    return {
                        "source.level": (
                            "Source current"
                            if node.data.get("source_mode") == "current"
                            else "Source voltage"
                        ),
                        "source.compliance": "Compliance",
                        "measurement.nplc": "NPLC",
                        "measurement.settling_time": "Settling time",
                        "measurement.sense_mode": "Sense mode",
                        "source.range": "Source range",
                        "measurement.voltage_range": "Measure V range",
                        "measurement.current_range": "Measure I range",
                    }.get(parameter_id, parameter_id)

                output = str(node.data.get("output_policy", "unchanged"))
                output_suffix = (
                    " · OUTPUT ON"
                    if output == "on"
                    else " · OUTPUT OFF" if output == "off" else ""
                )
                sweep_action = next(
                    (action for action in actions if action.get("mode") == "sweep"),
                    None,
                )
                point_suffix = ""
                sweep_complete = False
                if sweep_action is not None:
                    segments = sweep_action.get("segments")
                    if isinstance(segments, list) and segments:
                        parameter_id = str(sweep_action.get("parameter_id", ""))
                        source_mode = str(node.data.get("source_mode", "current"))
                        dimension = {
                            "source.level": (
                                DIMENSION_CURRENT
                                if source_mode == "current"
                                else DIMENSION_VOLTAGE
                            ),
                            "source.compliance": (
                                DIMENSION_VOLTAGE
                                if source_mode == "current"
                                else DIMENSION_CURRENT
                            ),
                            "measurement.settling_time": DIMENSION_TIME,
                        }.get(parameter_id)
                        if dimension is not None:
                            try:
                                point_count = len(
                                    generate_sweep_points(segments, dimension)
                                )
                                point_suffix = f" · {point_count:,} pts"
                                sweep_complete = True
                            except Exception:
                                point_suffix = " · invalid ROI"
                if len(actions) == 1:
                    action = actions[0]
                    label = (
                        f"Keithley {node.data.get('channel', '?')} · "
                        f"{action_label(action)} = {action.get('value', '')}"
                        f"{point_suffix}{output_suffix}"
                    )
                else:
                    label = (
                        f"Keithley {node.data.get('channel', '?')} · "
                        f"{len(actions)} parameters{point_suffix}{output_suffix}"
                    )
                has_sweep = any(action.get("mode") == "sweep" for action in actions)
                return (
                    label,
                    (
                        "Sweep axis"
                        if has_sweep and sweep_complete
                        else "ROI required" if has_sweep else "Fixed configuration"
                    ),
                    (
                        QStyle.StandardPixmap.SP_MediaPlay
                        if has_sweep
                        else QStyle.StandardPixmap.SP_DriveHDIcon
                    ),
                )
            override = node.data.get("parameter_override")
            if (
                node.data.get("device_module") == "keithley"
                and isinstance(override, dict)
            ):
                parameter_id = str(override.get("parameter_id", "parameter"))
                parameter_label = {
                    "source.level": (
                        "Source current"
                        if node.data.get("source_mode") == "current"
                        else "Source voltage"
                    ),
                    "source.compliance": "Compliance",
                    "measurement.nplc": "NPLC",
                    "measurement.settling_time": "Settling time",
                    "measurement.sense_mode": "Sense mode",
                    "source.range": "Source range",
                    "measurement.voltage_range": "Measure V range",
                    "measurement.current_range": "Measure I range",
                }.get(parameter_id, parameter_id)
                return (
                    f"Keithley {node.data.get('channel', '?')} · "
                    f"{parameter_label} = "
                    f"{override.get('value', '')}",
                    "Fixed setting",
                    QStyle.StandardPixmap.SP_DriveHDIcon,
                )
            configuration = node.data.get("configuration")
            if (
                node.data.get("device_module") == "keithley"
                and isinstance(configuration, dict)
            ):
                axis = node.data.get("axis")
                if isinstance(axis, dict):
                    segments = axis.get("segments")
                    point_count = ""
                    if isinstance(segments, list):
                        try:
                            dimension = (
                                DIMENSION_CURRENT
                                if configuration.get("source_mode") == "current"
                                else DIMENSION_VOLTAGE
                            )
                            point_count = (
                                f" · {len(generate_sweep_points(segments, dimension)):,} pts"
                            )
                        except Exception:
                            point_count = " · invalid ROI"
                    return (
                        f"Keithley {configuration.get('channel', '?')} · "
                        f"{configuration.get('source_mode', 'source')} sweep{point_count}",
                        "Sweep axis",
                        QStyle.StandardPixmap.SP_MediaPlay,
                    )
                return (
                    f"Keithley {configuration.get('channel', '?')} · "
                    f"{configuration.get('source_mode', 'source')} = "
                    f"{configuration.get('source_level', '')}",
                    "Fixed setting",
                    QStyle.StandardPixmap.SP_DriveHDIcon,
                )
            return (
                str(node.data.get("label", "Device module")),
                "Configuration required",
                QStyle.StandardPixmap.SP_ComputerIcon,
            )
        if node.type.startswith("set_") or node.type.startswith("ramp_"):
            return detail.title(), "Safety action", QStyle.StandardPixmap.SP_BrowserStop
        return node.id, detail + (f" • {count} action(s)" if compiled else ""), icon

    def _tree_node_icon(self, node: RecipeNode, fallback: QStyle.StandardPixmap) -> QIcon:
        target = str(node.data.get("target", ""))
        if (
            target.startswith("keithley.")
            or node.type.startswith("configure_keithley")
            or node.type in {"measure_keithley", "update_keithley_level"}
        ):
            return self._tree_badge_icon("Keithley", "#d94343", "K")
        if (
            target.startswith("rigol.")
            or node.type.startswith("configure_rigol")
            or node.type.startswith("set_rigol")
            or node.type == "update_rigol_frequency"
        ):
            return self._tree_badge_icon("Rigol", "#d7a50b", "R")
        if (
            target.startswith("anritsu.")
            or "anritsu" in node.type
            or node.type in {"acquire_reference", "acquire_spectrum"}
        ):
            return self._tree_badge_icon("Anritsu", "#269c5a", "A")
        module = node.data.get("device_module")
        if module == "keithley":
            return self._tree_badge_icon("Keithley", "#d94343", "K")
        if module == "rigol":
            return self._tree_badge_icon("Rigol", "#d7a50b", "R")
        if module == "anritsu":
            return self._tree_badge_icon("Anritsu", "#269c5a", "A")
        if node.type in {"wait", "repeat"}:
            return self._tree_badge_icon("Timing", "#8a55d7", "T")
        if node.type == "sequence":
            return self._tree_badge_icon("Structure", "#3178d4", "≡")
        if node.type.startswith("set_") or node.type.startswith("ramp_"):
            return self._tree_badge_icon("Safety", "#455363", "OFF")
        return self.style().standardIcon(fallback)

    @staticmethod
    def _tree_badge_icon(_name: str, color: str, text: str) -> QIcon:
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(1, 1, 22, 22, 5, 5)
        painter.setPen(QColor("#ffffff"))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(8 if len(text) == 1 else 5)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
        return QIcon(pixmap)

    def _node_selected(
        self,
        item: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if item is None:
            self.inspector.clear()
            self.inspector_summary.setText("Select a node to see its measurement role and configuration.")
            self.open_editor_button.setEnabled(False)
            self.edit_device_button.setEnabled(False)
            self.edit_generator_button.setEnabled(False)
            return
        operator_row = item.data(0, self.operator_row_role)
        if isinstance(operator_row, dict):
            stage_index = operator_row.get("stage_index")
            parameter_id = str(operator_row.get("parameter_id", ""))
            owner_id = str(operator_row.get("owner_node_id", ""))
            stage_label = (
                f"ROI {int(stage_index) + 1}"
                if isinstance(stage_index, int)
                else "Sweep axis"
            )
            self.inspector_summary.setText(
                f"<b>{stage_label}</b><br>{parameter_id} · direct ROI editor"
            )
            self.open_editor_button.setEnabled(True)
            self.open_editor_button.setText("Edit ROI")
            self.edit_device_button.setEnabled(False)
            self.edit_generator_button.setEnabled(True)
            self.inspector.setPlainText(
                f"Device node: {owner_id}\n"
                f"Parameter: {parameter_id}\n"
                f"Selected: {stage_label}\n\n"
                "Click this row or use Open parameter editor to edit ROI directly. "
                "The device configuration window will not be opened."
            )
            return
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(node, RecipeNode):
            self.inspector_summary.setText("Safety cleanup runs after success, operator stop and faults.")
            self.open_editor_button.setEnabled(False)
            self.edit_device_button.setEnabled(False)
            self.edit_generator_button.setEnabled(False)
            self.inspector.setPlainText(
                "Finally actions run during normal completion, operator stop and fault cleanup. "
                "They may only ramp Keithley to zero or disable outputs."
            )
            return
        label, detail, _icon = self._tree_presentation(node, 0, False)
        self.inspector_summary.setText(
            f"<b>{label}</b><br>{detail} • {len(node.children)} child node(s)"
        )
        has_device_settings = bool(
            node.data.get("device_module")
            or self._legacy_device_configuration_node(node) is not None
        )
        has_roi = node.type == "sweep" or (
            bool(node.data.get("device_module"))
            and any(
                isinstance(action, dict) and action.get("mode") == "sweep"
                for action in node.data.get("parameter_actions", [])
            )
        )
        is_comment = node.type == "comment"
        is_acquisition = node.type in {
            "acquire_reference",
            "acquire_spectrum",
        }
        self.open_editor_button.setText(
            "Device settings"
            if has_device_settings
            else "Edit ROI"
            if has_roi
            else "Acquisition settings"
            if is_acquisition
            else "Edit comment"
        )
        self.open_editor_button.setEnabled(
            has_device_settings or has_roi or is_comment or is_acquisition
        )
        self.edit_device_button.setEnabled(has_device_settings)
        self.edit_generator_button.setEnabled(has_roi)
        actions = (
            tuple(action for action in self._plan.actions if action.node_id == node.id)
            if self._plan is not None
            else ()
        )
        setpoints: dict[str, tuple[float, float]] = {}
        for action in actions:
            for name, value in action.setpoints_si.items():
                previous = setpoints.get(name, (value, value))
                setpoints[name] = (min(previous[0], value), max(previous[1], value))
        lines = [
            f"ID: {node.id}",
            f"Type: {node.type}",
            f"Children: {len(node.children)}",
            f"Else children: {len(node.else_children)}",
            f"Expanded actions: {len(actions)}",
            "",
            "Fields:",
            json.dumps(node.data, ensure_ascii=False, indent=2, default=str),
        ]
        if setpoints:
            lines.extend(("", "Expanded setpoint ranges:"))
            lines.extend(
                f"  {name}: {minimum:.12g} .. {maximum:.12g} SI"
                for name, (minimum, maximum) in sorted(setpoints.items())
            )
        self.inspector.setPlainText("\n".join(lines))

    def _operator_row_clicked(
        self, item: QTreeWidgetItem, _column: int
    ) -> None:
        metadata = item.data(0, self.operator_row_role)
        if (
            isinstance(metadata, dict)
            and metadata.get("kind")
            in {"sweep_parameter", "roi_stage", "native_sweep_roi"}
        ):
            if metadata.get("kind") == "native_sweep_roi":
                self._edit_native_sweep_roi_from_tree(dict(metadata))
            else:
                self._edit_device_roi_from_tree(dict(metadata))

    def _move_recipe_node(
        self,
        node_id: str,
        destination_parent_id: str,
        destination_branch: str,
        destination_index: int,
    ) -> None:
        self._emit_tree_diagnostic(
            "TREE_MOVE_REQUESTED",
            node_id=node_id,
            destination_parent_id=destination_parent_id,
            destination_branch=destination_branch,
            destination_index=destination_index,
        )
        try:
            moved_source = move_recipe_node(
                self.editor.toPlainText(),
                node_id=node_id,
                destination_parent_id=destination_parent_id,
                destination_branch=destination_branch,
                destination_index=destination_index,
            )
            parse_recipe_text(moved_source, origin=self.path.text())
        except Exception as exc:
            self._emit_tree_diagnostic(
                "TREE_MOVE_REJECTED",
                node_id=node_id,
                destination_parent_id=destination_parent_id,
                destination_branch=destination_branch,
                destination_index=destination_index,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            QMessageBox.warning(self, "Recipe move rejected", str(exc))
            return
        self._apply_builder_source(
            moved_source,
            f"Recipe node {node_id} moved to {destination_parent_id}.{destination_branch}",
        )

    def _find_tree_item(self, node_id: str) -> QTreeWidgetItem | None:
        def visit(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            node = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(node, RecipeNode) and node.id == node_id:
                return item
            for child_index in range(item.childCount()):
                found = visit(item.child(child_index))
                if found is not None:
                    return found
            return None

        for top_index in range(self.tree.topLevelItemCount()):
            found = visit(self.tree.topLevelItem(top_index))
            if found is not None:
                return found
        return None

    def _builder_parent(self) -> tuple[str, str]:
        """Return the selected container, or the root as the safe default."""

        item = self.tree.currentItem()
        if item is not None:
            if item.text(0).startswith("Finally"):
                return "__finally__", "children"
            node = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(node, RecipeNode) and node.type in {"sequence", "sweep", "repeat", "if"}:
                return node.id, "children"
            parent = item.parent()
            if parent is not None:
                if parent.text(0).startswith("Finally"):
                    return "__finally__", "children"
                if parent.text(0).startswith("Else") and parent.parent() is not None:
                    owner = parent.parent().data(0, Qt.ItemDataRole.UserRole)
                    if isinstance(owner, RecipeNode):
                        return owner.id, "else"
                owner = parent.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(owner, RecipeNode):
                    return owner.id, "children"
        root = self.tree.topLevelItem(0)
        node = root.data(0, Qt.ItemDataRole.UserRole) if root is not None else None
        if isinstance(node, RecipeNode):
            return node.id, "children"
        raise ConfigurationError("Load a valid recipe before editing its tree.")

    @staticmethod
    def _new_node_id(prefix: str) -> str:
        return f"{prefix}-{uuid4().hex[:8]}"

    def _update_tree_history_controls(self) -> None:
        can_undo = bool(self._tree_undo)
        can_redo = bool(self._tree_redo)
        self.undo_tree_action.setEnabled(can_undo)
        self.redo_tree_action.setEnabled(can_redo)
        self.undo_tree_action.setToolTip(
            "Undo the last Tree Builder operation" if self._tree_undo else "No Tree Builder operation to undo"
        )
        self.redo_tree_action.setToolTip(
            "Redo the last Tree Builder operation" if self._tree_redo else "No Tree Builder operation to redo"
        )

    def _restore_tree_history_source(self, source: str, status: str) -> None:
        recipe = parse_recipe_text(source, origin="tree-builder-history")
        self._loading_source = True
        self.editor.setPlainText(source)
        self._loading_source = False
        self._plan = None
        self.run_button.setEnabled(False)
        self.plan_preflight_changed.emit(None)
        self._populate_recipe_tree(recipe.root, recipe.finally_nodes, None)
        self.summary.setText("Recipe tree changed; compile it again before running.")
        self._autosave_timer.start()
        self.status.emit(status)

    def undo_tree_edit(self) -> None:
        if not self._tree_undo:
            return
        current = self.editor.toPlainText()
        source = self._tree_undo.pop()
        self._tree_redo.append(current)
        self._restore_tree_history_source(source, "Tree Builder change undone")
        self._update_tree_history_controls()

    def redo_tree_edit(self) -> None:
        if not self._tree_redo:
            return
        current = self.editor.toPlainText()
        source = self._tree_redo.pop()
        self._tree_undo.append(current)
        self._restore_tree_history_source(source, "Tree Builder change redone")
        self._update_tree_history_controls()

    def _apply_builder_source(self, source: str, status: str) -> None:
        previous = self.editor.toPlainText()
        previous_plan = self._plan
        try:
            recipe = parse_recipe_text(source, origin="tree-builder")
        except Exception as exc:
            self._emit_tree_diagnostic(
                "TREE_SOURCE_REJECTED",
                operation=status,
                source_length=len(source),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        self._plan = None
        try:
            self._populate_recipe_tree(recipe.root, recipe.finally_nodes, None)
        except Exception as exc:
            self._plan = previous_plan
            self._emit_tree_diagnostic(
                "TREE_RENDER_REJECTED",
                operation=status,
                root_id=recipe.root.id,
                source_length=len(source),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise
        if source != previous:
            self._tree_undo.append(previous)
            self._tree_redo.clear()
        self._loading_source = True
        self.editor.setPlainText(source)
        self._loading_source = False
        self.run_button.setEnabled(False)
        self.plan_preflight_changed.emit(None)
        self.summary.setText("Recipe tree changed; compile it again before running.")
        self._autosave_timer.start()
        self.status.emit(status)
        self._update_tree_history_controls()

    def _emit_tree_diagnostic(self, event: str, **context: object) -> None:
        """Send one searchable, structured Tree Builder record to Event log."""

        payload = json.dumps(
            context, ensure_ascii=False, sort_keys=True, default=str
        )
        self.status.emit(f"{event} | {payload}")

    def _add_basic_node(
        self,
        *,
        parent_id: str | None = None,
        branch: str | None = None,
        insert_index: int | None = None,
    ) -> None:
        kind = str(self.node_kind.currentData())
        if parent_id is None or branch is None:
            parent_id, branch = self._builder_parent()
        node_id = self._new_node_id(kind)
        defaults: dict[str, dict[str, object]] = {
            "sequence": {
                "id": node_id,
                "type": "sequence",
                "children": [
                    {
                        "id": self._new_node_id("comment"),
                        "type": "comment",
                        "text": "Add actions to this sequence",
                    }
                ],
            },
            "repeat": {
                "id": node_id,
                "type": "repeat",
                "count": 2,
                "children": [
                    {
                        "id": self._new_node_id("comment"),
                        "type": "comment",
                        "text": "Add repeated actions here",
                    }
                ],
            },
            "if": {
                "id": node_id,
                "type": "if",
                "condition": True,
                "children": [
                    {
                        "id": self._new_node_id("comment"),
                        "type": "comment",
                        "text": "Then branch",
                    }
                ],
                "else": [
                    {
                        "id": self._new_node_id("comment"),
                        "type": "comment",
                        "text": "Else branch",
                    }
                ],
            },
            "wait": {"id": node_id, "type": "wait", "duration": "100 ms"},
            "measure_keithley": {"id": node_id, "type": "measure_keithley", "channel": "B"},
            "acquire_reference": {
                "id": node_id,
                "type": "acquire_reference",
                "trace": "TRAC1",
                "average_count": 1,
            },
            "acquire_spectrum": {
                "id": node_id,
                "type": "acquire_spectrum",
                "trace": "TRAC1",
                "average_count": 1,
            },
            "comment": {"id": node_id, "type": "comment", "text": "Describe this step"},
            "ramp_keithley_to_zero": {
                "id": node_id,
                "type": "ramp_keithley_to_zero",
                "channel": "B",
                "deadline": "10 s",
            },
            "set_keithley_output": {
                "id": node_id,
                "type": "set_keithley_output",
                "channel": "B",
                "enabled": False,
            },
            "set_rigol_output": {
                "id": node_id,
                "type": "set_rigol_output",
                "channel": 1,
                "enabled": False,
            },
            "set_anritsu_sg_output": {
                "id": node_id,
                "type": "set_anritsu_sg_output",
                "enabled": False,
            },
            "arm_anritsu_sg_output": {
                "id": node_id,
                "type": "arm_anritsu_sg_output",
            },
            "set_anritsu_sg_output_on": {
                "id": node_id,
                "type": "set_anritsu_sg_output",
                "enabled": True,
            },
        }
        try:
            source = add_recipe_node(
                self.editor.toPlainText(),
                parent_id=parent_id,
                branch=branch,
                index=insert_index,
                node=defaults[kind],
            )
            self._apply_builder_source(source, f"Added {kind} node")
        except Exception as exc:
            QMessageBox.warning(self, "Add recipe node", str(exc))

    def _add_device_controls(self, device: str | None = None) -> None:
        if device == "Keithley":
            dialog = KeithleySweepBuilderDialog(self._settings, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            parent_id, branch = self._builder_parent()
            try:
                node = self._sweep_node_from_generator(
                    dialog.definition,
                    dialog.segment_data(),
                    keithley_options=dialog.keithley_options(),
                )
                source = add_recipe_node(
                    self.editor.toPlainText(), parent_id=parent_id, branch=branch, node=node
                )
                self._apply_builder_source(source, "Added Keithley sweep node")
            except Exception as exc:
                QMessageBox.warning(self, "Add Keithley sweep", str(exc))
            return
        picker = DeviceParameterDialog(self, initial_device=device)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        definitions = picker.selected()
        operation = picker.operation_kind()
        parent_id, branch = self._builder_parent()
        source = self.editor.toPlainText()
        added = 0
        for definition in definitions:
            dialog: QDialog
            dialog = SweepGeneratorDialog(definition, self) if operation == "sweep" else FixedValueDialog(definition, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                continue
            try:
                node = (
                    self._sweep_node_from_generator(definition, dialog.segment_data())
                    if isinstance(dialog, SweepGeneratorDialog)
                    else self._fixed_node_from_dialog(definition, dialog)
                )
                source = add_recipe_node(
                    source,
                    parent_id=parent_id,
                    branch=branch,
                    node=node,
                )
                added += 1
            except Exception as exc:
                QMessageBox.warning(self, "Add sweep", str(exc))
                return
        if added:
            noun = "device control node(s)" if operation == "fixed" else "dynamic sweep node(s)"
            self._apply_builder_source(source, f"Added {added} {noun}")

    def _edit_selected_node(self) -> None:
        """Open the most specific editor while keeping device and ROI tasks separate."""

        item = self.tree.currentItem()
        operator_row = (
            item.data(0, self.operator_row_role) if item is not None else None
        )
        if isinstance(operator_row, dict):
            self._edit_selected_roi()
            return
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if isinstance(node, RecipeNode) and node.type == "comment":
            self._edit_comment_node(node)
            return
        if isinstance(node, RecipeNode) and node.type in {
            "acquire_reference",
            "acquire_spectrum",
        }:
            self._edit_anritsu_acquisition_node(node)
            return
        if isinstance(node, RecipeNode) and (
            node.data.get("device_module")
            or self._legacy_device_configuration_node(node) is not None
        ):
            self._edit_selected_device_settings()
            return
        self._edit_selected_roi()

    def _edit_selected_roi(self) -> None:
        item = self.tree.currentItem()
        operator_row = (
            item.data(0, self.operator_row_role) if item is not None else None
        )
        if isinstance(operator_row, dict):
            if operator_row.get("kind") == "native_sweep_roi":
                self._edit_native_sweep_roi_from_tree(dict(operator_row))
            else:
                self._edit_device_roi_from_tree(dict(operator_row))
            return
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if isinstance(node, RecipeNode) and node.type == "sweep":
            self._edit_selected_generator(node=node)
            return
        if isinstance(node, RecipeNode) and node.data.get("device_module"):
            sweep = next(
                (
                    action
                    for action in node.data.get("parameter_actions", [])
                    if isinstance(action, dict) and action.get("mode") == "sweep"
                ),
                None,
            )
            if sweep is not None:
                self._edit_device_roi_from_tree(
                    {
                        "device_module": node.data.get("device_module"),
                        "owner_node_id": node.id,
                        "parameter_id": sweep.get("parameter_id"),
                        "stage_index": None,
                    }
                )
                return
        QMessageBox.information(
            self,
            "Edit ROI",
            "Select an ROI row, a sweep axis, or a device module containing a sweep.",
        )

    @staticmethod
    def _legacy_device_configuration_node(
        node: RecipeNode,
    ) -> RecipeNode | None:
        if node.type in {
            "configure_keithley",
            "configure_rigol",
            "configure_anritsu",
            "configure_anritsu_advanced",
            "configure_anritsu_sg",
        }:
            return node
        if node.type == "sweep":
            return next(
                (
                    child
                    for child in node.children
                    if child.type
                    in {
                        "configure_keithley",
                        "configure_rigol",
                        "configure_anritsu",
                        "configure_anritsu_sg",
                    }
                ),
                None,
            )
        return None

    def _edit_selected_device_settings(self) -> None:
        item = self.tree.currentItem()
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(node, RecipeNode):
            QMessageBox.information(
                self, "Device settings", "Select a device or its sweep axis first."
            )
            return
        if node.data.get("device_module") == "keithley":
            self._edit_keithley_module_node(node)
            return
        if node.data.get("device_module") == "rigol":
            self._edit_rigol_module_node(node)
            return
        if node.data.get("device_module") == "anritsu":
            self._edit_anritsu_module_node(node)
            return
        if node.data.get("device_module") == "anritsu_sg":
            self._edit_anritsu_sg_module_node(node)
            return
        configuration = self._legacy_device_configuration_node(node)
        if configuration is None:
            QMessageBox.information(
                self,
                "Device settings",
                "The selected object has no device configuration window.",
            )
            return
        if configuration.type == "configure_keithley":
            self._edit_legacy_keithley_configuration(configuration)
        elif configuration.type == "configure_rigol":
            self._edit_legacy_rigol_configuration(configuration)
        elif configuration.type == "configure_anritsu":
            self._edit_legacy_anritsu_configuration(configuration)
        elif configuration.type == "configure_anritsu_sg":
            QMessageBox.information(
                self,
                "Anritsu SG settings",
                "Signal-generator plan editing will be available in the Anritsu module.",
            )
        else:
            QMessageBox.information(
                self,
                "Device settings",
                "Select the parent Anritsu configuration node.",
            )

    def _edit_device_roi_from_tree(self, metadata: dict[str, object]) -> None:
        if metadata.get("device_module") == "anritsu_sg":
            owner = self._find_tree_item(str(metadata.get("owner_node_id", "")))
            node = (
                owner.data(0, Qt.ItemDataRole.UserRole)
                if owner is not None
                else None
            )
            if isinstance(node, RecipeNode):
                self._edit_anritsu_sg_module_node(node)
        elif metadata.get("device_module") == "anritsu":
            self._edit_anritsu_roi_from_tree(metadata)
        elif metadata.get("device_module") == "rigol":
            self._edit_rigol_roi_from_tree(metadata)
        else:
            self._edit_keithley_roi_from_tree(metadata)

    def _edit_native_sweep_roi_from_tree(
        self, metadata: dict[str, object]
    ) -> None:
        owner_id = str(metadata.get("owner_node_id", ""))
        owner_item = self._find_tree_item(owner_id)
        node = (
            owner_item.data(0, Qt.ItemDataRole.UserRole)
            if owner_item is not None
            else None
        )
        if not isinstance(node, RecipeNode) or node.type != "sweep":
            QMessageBox.warning(
                self, "ROI editor", "The owning sweep no longer exists."
            )
            return
        stage_index = metadata.get("stage_index")
        self._edit_selected_generator(
            node=node,
            stage_index=stage_index if isinstance(stage_index, int) else None,
        )

    def _edit_keithley_roi_from_tree(
        self, metadata: dict[str, object]
    ) -> None:
        owner_id = str(metadata.get("owner_node_id", ""))
        parameter_id = str(metadata.get("parameter_id", ""))
        owner_item = self._find_tree_item(owner_id)
        node = (
            owner_item.data(0, Qt.ItemDataRole.UserRole)
            if owner_item is not None
            else None
        )
        if not isinstance(node, RecipeNode):
            QMessageBox.warning(
                self, "ROI editor", "The owning device node no longer exists."
            )
            return
        raw_actions = node.data.get("parameter_actions")
        actions = (
            [dict(action) for action in raw_actions if isinstance(action, dict)]
            if isinstance(raw_actions, list)
            else []
        )
        sweep_action = next(
            (
                action
                for action in actions
                if action.get("parameter_id") == parameter_id
                and action.get("mode") == "sweep"
            ),
            None,
        )
        if sweep_action is None:
            QMessageBox.warning(
                self,
                "ROI editor",
                "The selected parameter is no longer configured as a sweep axis.",
            )
            return
        snapshot = replace(
            self._current_keithley_snapshot(),
            channel=str(node.data.get("channel", "B")),
            source_mode=str(node.data.get("source_mode", "current")),
        )
        for action in actions:
            snapshot = self._snapshot_with_override(snapshot, action)
        definition = self._keithley_roi_definition(snapshot, parameter_id)
        initial = sweep_action.get("segments")
        dialog = SweepGeneratorDialog(
            definition,
            self,
            initial_segments=(
                [dict(segment) for segment in initial if isinstance(segment, dict)]
                if isinstance(initial, list)
                else None
            ),
        )
        dialog.setWindowTitle(
            f"Keithley {snapshot.channel} — ROI · {definition['label']}"
        )
        stage_index = metadata.get("stage_index")
        dialog.select_interval(stage_index if isinstance(stage_index, int) else None)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._apply_keithley_roi_segments(
                node, parameter_id, dialog.segment_data()
            )
        except Exception as exc:
            QMessageBox.warning(self, "ROI editor", str(exc))

    def _apply_keithley_roi_segments(
        self,
        node: RecipeNode,
        parameter_id: str,
        segments: list[dict[str, object]],
    ) -> None:
        raw_actions = node.data.get("parameter_actions")
        actions = (
            [dict(action) for action in raw_actions if isinstance(action, dict)]
            if isinstance(raw_actions, list)
            else []
        )
        target = next(
            (
                action
                for action in actions
                if action.get("parameter_id") == parameter_id
                and action.get("mode") == "sweep"
            ),
            None,
        )
        if target is None:
            raise ConfigurationError(
                "The selected Keithley parameter is not a sweep axis."
            )
        snapshot = replace(
            self._current_keithley_snapshot(),
            channel=str(node.data.get("channel", "B")),
            source_mode=str(node.data.get("source_mode", "current")),
        )
        for action in actions:
            snapshot = self._snapshot_with_override(snapshot, action)
        definition = self._keithley_roi_definition(snapshot, parameter_id)
        generate_sweep_points(segments, definition["dimension"])
        target["segments"] = [dict(segment) for segment in segments]
        replacement = self._configured_keithley_node(
            node,
            snapshot,
            parameter_actions=actions,
            output_policy=str(node.data.get("output_policy", "unchanged")),
        )
        source = replace_recipe_node(
            self.editor.toPlainText(), node_id=node.id, node=replacement
        )
        self._apply_builder_source(
            source, f"Updated ROI for {parameter_id} in {node.id}"
        )

    def _edit_keithley_module_node(self, node: RecipeNode) -> None:
        snapshot = self._current_keithley_snapshot()
        raw_actions = node.data.get("parameter_actions")
        actions = (
            [dict(action) for action in raw_actions if isinstance(action, dict)]
            if isinstance(raw_actions, list)
            else []
        )
        legacy_override = node.data.get("parameter_override")
        if not actions and isinstance(legacy_override, dict):
            actions = [{"mode": "set", **legacy_override}]
        for action in actions:
            snapshot = self._snapshot_with_override(snapshot, action)
        dialog = KeithleyNodeEditorDialog(
            self._settings, self, snapshot=snapshot
        )
        dialog.load_plan_actions(
            actions, str(node.data.get("output_policy", "unchanged"))
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        snapshot = dialog.configuration_snapshot()
        parameter_actions = self._edit_keithley_rois(
            snapshot, dialog.planned_parameter_actions()
        )
        if parameter_actions is None:
            return
        replacement = self._configured_keithley_node(
            node,
            snapshot,
            parameter_actions=parameter_actions,
            output_policy=dialog.selected_output_policy(),
        )
        try:
            source = replace_recipe_node(
                self.editor.toPlainText(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(source, f"Configured {replacement['label']}")
        except Exception as exc:
            QMessageBox.warning(self, "Keithley node", str(exc))

    def _edit_anritsu_module_node(self, node: RecipeNode) -> None:
        snapshot = self._current_anritsu_snapshot()
        configuration = node.data.get("configuration")
        if isinstance(configuration, dict):
            try:
                snapshot = replace(
                    snapshot,
                    start_hz=parse_quantity(
                        configuration["start_frequency"], DIMENSION_FREQUENCY
                    ).si_value,
                    stop_hz=parse_quantity(
                        configuration["stop_frequency"], DIMENSION_FREQUENCY
                    ).si_value,
                    reference_level_dbm=parse_quantity(
                        configuration["reference_level"], DIMENSION_DBM
                    ).si_value,
                    points=int(configuration["points"]),
                )
            except (KeyError, TypeError, ValueError, ConfigurationError):
                pass
        raw_actions = node.data.get("parameter_actions")
        actions = (
            [dict(action) for action in raw_actions if isinstance(action, dict)]
            if isinstance(raw_actions, list)
            else []
        )
        for action in actions:
            snapshot = self._anritsu_snapshot_with_override(snapshot, action)
        dialog = AnritsuNodeEditorDialog(
            self._settings, self, snapshot=snapshot
        )
        dialog.load_plan_actions(
            actions,
            acquire_single=bool(node.data.get("acquire_single", False)),
            trace=str(node.data.get("trace", "TRAC1")),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        snapshot = dialog.configuration_panel.configuration_snapshot()
        replacement = self._configured_anritsu_node(
            node,
            snapshot=snapshot,
            parameter_actions=dialog.planned_parameter_actions(),
            acquire_single=dialog.acquire_single.isChecked(),
            trace=dialog.trace.currentText(),
        )
        try:
            source = replace_recipe_node(
                self.editor.toPlainText(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(source, "Configured Anritsu spectrum module")
        except Exception as exc:
            QMessageBox.warning(self, "Anritsu node", str(exc))

    def _edit_anritsu_sg_module_node(self, node: RecipeNode) -> None:
        configuration = node.data.get("configuration")
        snapshot = self._current_anritsu_sg_snapshot()
        frequency = format_quantity_auto(
            snapshot.frequency_hz, DIMENSION_FREQUENCY
        )
        power = f"{snapshot.power_dbm:.9g} dBm"
        if isinstance(configuration, dict):
            frequency = str(configuration.get("frequency", frequency))
            power = str(configuration.get("power", power))
        actions = [
            dict(action)
            for action in node.data.get("parameter_actions", [])
            if isinstance(action, dict)
        ]
        dialog = AnritsuSignalGeneratorNodeEditorDialog(
            self,
            frequency=frequency,
            power=power,
            parameter_actions=actions,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        replacement = self._configured_anritsu_sg_node(
            node,
            frequency=dialog.frequency.text().strip(),
            power=dialog.power.text().strip(),
            parameter_actions=dialog.planned_parameter_actions(),
        )
        try:
            source = replace_recipe_node(
                self.editor.toPlainText(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source, "Configured Anritsu signal-generator module · RF OFF"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Anritsu SG node", str(exc))

    def _edit_rigol_module_node(self, node: RecipeNode) -> None:
        snapshot = self._current_rigol_snapshot()
        configuration = node.data.get("configuration")
        if isinstance(configuration, dict):
            try:
                snapshot = RigolConfigurationSnapshot(**configuration)
            except TypeError:
                pass
        raw_actions = node.data.get("parameter_actions")
        actions = (
            [dict(action) for action in raw_actions if isinstance(action, dict)]
            if isinstance(raw_actions, list)
            else []
        )
        dialog = RigolNodeEditorDialog(
            self,
            settings=self._settings,
            snapshot=snapshot,
            parameter_actions=actions,
            output_policy=str(node.data.get("output_policy", "unchanged")),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        replacement = self._configured_rigol_node(
            node,
            snapshot=dialog.configuration_snapshot(),
            parameter_actions=dialog.planned_parameter_actions(),
            output_policy=dialog.selected_output_policy(),
        )
        try:
            source = replace_recipe_node(
                self.editor.toPlainText(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source,
                f"Configured Rigol CH{replacement['channel']} OUTPUT "
                f"{str(replacement['output_policy']).upper()}",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Rigol node", str(exc))

    def _edit_rigol_roi_from_tree(
        self, metadata: dict[str, object]
    ) -> None:
        owner_id = str(metadata.get("owner_node_id", ""))
        parameter_id = str(metadata.get("parameter_id", ""))
        owner_item = self._find_tree_item(owner_id)
        node = (
            owner_item.data(0, Qt.ItemDataRole.UserRole)
            if owner_item is not None
            else None
        )
        if not isinstance(node, RecipeNode):
            QMessageBox.warning(
                self, "ROI editor", "The owning Rigol node no longer exists."
            )
            return
        actions = [
            dict(action)
            for action in node.data.get("parameter_actions", [])
            if isinstance(action, dict)
        ]
        action = next(
            (
                candidate
                for candidate in actions
                if candidate.get("parameter_id") == parameter_id
                and candidate.get("mode") == "sweep"
            ),
            None,
        )
        if action is None:
            return
        suffix = {
            "carrier.frequency": "frequency",
            "carrier.high_level": "high_level",
            "carrier.low_level": "low_level",
        }.get(parameter_id)
        if suffix is None:
            return
        dimension = (
            DIMENSION_FREQUENCY
            if parameter_id == "carrier.frequency"
            else DIMENSION_VOLTAGE
        )
        initial = action.get("segments")
        dialog = SweepGeneratorDialog(
            {
                "device": "Rigol",
                "label": f"Rigol CH{node.data.get('channel')} · {suffix}",
                "target": f"rigol.{node.data.get('channel')}.{suffix}",
                "dimension": dimension,
            },
            self,
            initial_segments=(
                [
                    dict(segment)
                    for segment in initial
                    if isinstance(segment, dict)
                ]
                if isinstance(initial, list)
                else None
            ),
        )
        stage_index = metadata.get("stage_index")
        dialog.select_interval(
            stage_index if isinstance(stage_index, int) else None
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        action["segments"] = dialog.segment_data()
        configuration = node.data.get("configuration")
        try:
            snapshot = (
                RigolConfigurationSnapshot(**configuration)
                if isinstance(configuration, dict)
                else self._current_rigol_snapshot()
            )
        except TypeError:
            snapshot = self._current_rigol_snapshot()
        replacement = self._configured_rigol_node(
            node,
            snapshot=snapshot,
            parameter_actions=actions,
            output_policy=str(node.data.get("output_policy", "unchanged")),
        )
        source = replace_recipe_node(
            self.editor.toPlainText(), node_id=node.id, node=replacement
        )
        self._apply_builder_source(
            source, f"Updated Rigol ROI for {parameter_id}"
        )

    def _edit_legacy_keithley_configuration(self, node: RecipeNode) -> None:
        snapshot = replace(
            self._current_keithley_snapshot(),
            channel=str(node.data.get("channel", "B")),
            source_mode=str(node.data.get("mode", "current")),
            source_level=str(node.data.get("level", "1 mA")),
            compliance=str(node.data.get("compliance", "67 mV")),
            nplc=str(node.data.get("nplc", "1")),
            settling_time=str(node.data.get("settle_time", "100 ms")),
            sense_mode=str(node.data.get("sense_mode", "2wire")),
            source_autorange=bool(node.data.get("source_autorange", True)),
            source_range=str(node.data.get("source_range", "AUTO")),
            measure_voltage_autorange=bool(
                node.data.get("measure_voltage_autorange", True)
            ),
            measure_voltage_range=str(
                node.data.get("measure_voltage_range", "AUTO")
            ),
            measure_current_autorange=bool(
                node.data.get("measure_current_autorange", True)
            ),
            measure_current_range=str(
                node.data.get("measure_current_range", "AUTO")
            ),
        )
        dialog = KeithleyNodeEditorDialog(
            self._settings, self, snapshot=snapshot
        )
        for selector in dialog.parameter_selectors.values():
            index = selector.findData("set")
            if index >= 0:
                selector.setCurrentIndex(index)
        dialog.output_policy.setEnabled(False)
        dialog.output_policy.setToolTip(
            "Legacy configuration nodes do not change OUTPUT."
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.configuration_snapshot()
        replacement = self._node_to_mapping(node)
        replacement.update(
            {
                "channel": updated.channel,
                "mode": updated.source_mode,
                "level": updated.source_level,
                "compliance": updated.compliance,
                "nplc": float(updated.nplc.replace(",", ".")),
                "settle_time": updated.settling_time,
                "sense_mode": updated.sense_mode,
                "source_autorange": updated.source_autorange,
                "source_range": updated.source_range,
                "measure_voltage_autorange": updated.measure_voltage_autorange,
                "measure_voltage_range": updated.measure_voltage_range,
                "measure_current_autorange": updated.measure_current_autorange,
                "measure_current_range": updated.measure_current_range,
            }
        )
        source = replace_recipe_node(
            self.editor.toPlainText(), node_id=node.id, node=replacement
        )
        self._apply_builder_source(source, f"Updated Keithley settings {node.id}")

    def _edit_legacy_rigol_configuration(self, node: RecipeNode) -> None:
        snapshot = RigolConfigurationSnapshot(
            channel=int(node.data.get("channel", 1)),
            waveform=str(node.data.get("waveform", "SIN")),
            frequency=str(node.data.get("frequency", "1 kHz")),
            high_level=str(node.data.get("high_level", "1 mV")),
            low_level=str(node.data.get("low_level", "-1 mV")),
            output_load=str(node.data.get("output_load", "HIGHZ")),
            phase_deg=str(node.data.get("phase_deg", "0")),
            square_duty_percent=str(
                node.data.get("square_duty_percent", "50")
            ),
            ramp_symmetry_percent=str(
                node.data.get("ramp_symmetry_percent", "50")
            ),
            pulse_width=str(node.data.get("pulse_width", "100 us")),
            pulse_leading=str(node.data.get("pulse_leading", "10 ns")),
            pulse_trailing=str(node.data.get("pulse_trailing", "10 ns")),
            dut_min_impedance=str(
                node.data.get("dut_min_impedance", "50 ohm")
            ),
        )
        dialog = RigolNodeEditorDialog(
            self,
            settings=self._settings,
            snapshot=snapshot,
            output_policy="unchanged",
        )
        dialog.output_policy.setEnabled(False)
        dialog.output_policy.setToolTip(
            "Legacy configuration nodes do not change OUTPUT."
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.configuration_snapshot()
        replacement = self._node_to_mapping(node)
        replacement.update(
            {
                "channel": updated.channel,
                "waveform": updated.waveform,
                "frequency": updated.frequency,
                "high_level": updated.high_level,
                "low_level": updated.low_level,
                "output_load": updated.output_load,
                "phase_deg": float(updated.phase_deg.replace(",", ".")),
                "dut_min_impedance": updated.dut_min_impedance,
            }
        )
        for key, value, waveform in (
            ("square_duty_percent", updated.square_duty_percent, "SQU"),
            ("ramp_symmetry_percent", updated.ramp_symmetry_percent, "RAMP"),
            ("pulse_width", updated.pulse_width, "PULS"),
            ("pulse_leading", updated.pulse_leading, "PULS"),
            ("pulse_trailing", updated.pulse_trailing, "PULS"),
        ):
            if updated.waveform == waveform:
                replacement[key] = value
            else:
                replacement.pop(key, None)
        source = replace_recipe_node(
            self.editor.toPlainText(), node_id=node.id, node=replacement
        )
        self._apply_builder_source(source, f"Updated Rigol settings {node.id}")

    def _edit_legacy_anritsu_configuration(self, node: RecipeNode) -> None:
        current = self._current_anritsu_snapshot()
        try:
            snapshot = replace(
                current,
                start_hz=parse_quantity(
                    node.data.get("start_frequency"), DIMENSION_FREQUENCY
                ).si_value,
                stop_hz=parse_quantity(
                    node.data.get("stop_frequency"), DIMENSION_FREQUENCY
                ).si_value,
                reference_level_dbm=parse_quantity(
                    node.data.get("reference_level"), DIMENSION_DBM
                ).si_value,
                points=int(node.data.get("points", 1001)),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Anritsu settings", str(exc))
            return
        actions = [
            {
                "parameter_id": "spectrum.start_frequency",
                "mode": "set",
                "value": str(node.data.get("start_frequency")),
            },
            {
                "parameter_id": "spectrum.stop_frequency",
                "mode": "set",
                "value": str(node.data.get("stop_frequency")),
            },
            {
                "parameter_id": "spectrum.reference_level",
                "mode": "set",
                "value": str(node.data.get("reference_level")),
            },
            {
                "parameter_id": "spectrum.points",
                "mode": "set",
                "value": str(node.data.get("points", 1001)),
            },
        ]
        dialog = AnritsuNodeEditorDialog(
            self._settings, self, snapshot=snapshot
        )
        dialog.load_plan_actions(
            actions, acquire_single=False, trace=str(node.data.get("trace", "TRAC1"))
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.configuration_panel.configuration_snapshot()
        replacement = self._node_to_mapping(node)
        replacement.update(
            {
                "start_frequency": f"{updated.start_hz:.12g} Hz",
                "stop_frequency": f"{updated.stop_hz:.12g} Hz",
                "reference_level": f"{updated.reference_level_dbm:.12g} dBm",
                "points": updated.points,
            }
        )
        source = replace_recipe_node(
            self.editor.toPlainText(), node_id=node.id, node=replacement
        )
        self._apply_builder_source(source, f"Updated Anritsu settings {node.id}")

    @staticmethod
    def _configured_rigol_node(
        node: RecipeNode,
        *,
        channel: int | None = None,
        snapshot: RigolConfigurationSnapshot | None = None,
        parameter_actions: list[dict[str, object]] | None = None,
        output_policy: str,
    ) -> dict[str, object]:
        snapshot = snapshot or RigolConfigurationSnapshot(channel=channel or 1)
        channel = snapshot.channel
        if channel not in {1, 2}:
            raise ConfigurationError("Rigol output channel must be 1 or 2.")
        if output_policy not in {"unchanged", "on", "off"}:
            raise ConfigurationError(
                "Rigol output policy must be unchanged, on or off."
            )
        actions = parameter_actions or [
            {
                "parameter_id": "carrier.frequency",
                "mode": "set",
                "value": snapshot.frequency,
            },
            {
                "parameter_id": "carrier.high_level",
                "mode": "set",
                "value": snapshot.high_level,
            },
            {
                "parameter_id": "carrier.low_level",
                "mode": "set",
                "value": snapshot.low_level,
            },
        ]
        allowed = {
            "carrier.frequency",
            "carrier.high_level",
            "carrier.low_level",
        }
        if any(action.get("parameter_id") not in allowed for action in actions):
            raise ConfigurationError("Rigol node contains an unsupported parameter action.")
        sweeps = [action for action in actions if action.get("mode") == "sweep"]
        if len(sweeps) > 1:
            raise ConfigurationError("One Rigol node supports one local sweep axis.")
        roi_required = any(
            not isinstance(action.get("segments"), list) or not action.get("segments")
            for action in sweeps
        )
        children = [
            RecipePage._node_to_mapping(child)
            for child in node.children
            if not (
                child.type == "comment"
                and child.data.get("text")
                == "Drop nested devices or flow actions here"
            )
        ]
        return {
            "id": node.id,
            "type": "sequence",
            "text": f"Rigol CH{channel} · OUTPUT {output_policy.upper()}",
            "device_module": "rigol",
            "label": "Rigol DG1032Z",
            "operation": "configure_selected_parameters",
            "configuration_required": roi_required,
            "channel": channel,
            "parameter_actions": actions,
            "output_policy": output_policy,
            "roi_required": roi_required,
            "configuration": {
                field: getattr(snapshot, field)
                for field in snapshot.__dataclass_fields__
            },
            "children": children,
        }

    def _current_rigol_snapshot(self) -> RigolConfigurationSnapshot:
        provider = self._rigol_snapshot_provider
        if callable(provider):
            snapshot = provider()
            if isinstance(snapshot, RigolConfigurationSnapshot):
                return snapshot
        return RigolConfigurationSnapshot()

    def _current_anritsu_snapshot(self) -> AnritsuConfigurationSnapshot:
        provider = self._anritsu_snapshot_provider
        if callable(provider):
            snapshot = provider()
            if isinstance(snapshot, AnritsuConfigurationSnapshot):
                return snapshot
        return AnritsuConfigurationSnapshot(
            start_hz=1e6,
            stop_hz=10e6,
            reference_level_dbm=0.0,
            points=1001,
            instrument_mode="PLAN_EDIT",
        )

    def _current_anritsu_sg_snapshot(self) -> SignalGeneratorSnapshot:
        provider = self._anritsu_sg_snapshot_provider
        if callable(provider):
            try:
                snapshot = provider()
            except (ConfigurationError, ValueError):
                snapshot = None
            if isinstance(snapshot, SignalGeneratorSnapshot):
                return snapshot
        generator = self._settings.anritsu.signal_generator
        frequency = generator.frequency.min or "1 GHz"
        power = generator.power.min or "-30 dBm"
        return SignalGeneratorSnapshot(
            frequency_hz=parse_quantity(
                frequency, DIMENSION_FREQUENCY
            ).si_value,
            power_dbm=parse_quantity(power, DIMENSION_DBM).si_value,
            output_enabled=False,
            instrument_mode="PLAN_EDIT",
        )

    @staticmethod
    def _anritsu_snapshot_with_override(
        snapshot: AnritsuConfigurationSnapshot,
        override: dict[str, object],
    ) -> AnritsuConfigurationSnapshot:
        parameter_id = str(override.get("parameter_id", ""))
        value = str(override.get("value", ""))
        try:
            if parameter_id == "spectrum.start_frequency":
                return replace(
                    snapshot,
                    start_hz=parse_quantity(value, DIMENSION_FREQUENCY).si_value,
                )
            if parameter_id == "spectrum.stop_frequency":
                return replace(
                    snapshot,
                    stop_hz=parse_quantity(value, DIMENSION_FREQUENCY).si_value,
                )
            if parameter_id == "spectrum.reference_level":
                return replace(
                    snapshot,
                    reference_level_dbm=parse_quantity(value, DIMENSION_DBM).si_value,
                )
            if parameter_id == "spectrum.points":
                return replace(snapshot, points=int(value))
        except (ConfigurationError, ValueError):
            return snapshot
        return snapshot

    @staticmethod
    def _configured_anritsu_sg_node(
        node: RecipeNode,
        *,
        frequency: str,
        power: str,
        parameter_actions: list[dict[str, object]],
    ) -> dict[str, object]:
        allowed = {"sg.frequency", "sg.power"}
        if any(
            action.get("parameter_id") not in allowed
            or action.get("mode") not in {"set", "sweep"}
            for action in parameter_actions
        ):
            raise ConfigurationError(
                "Anritsu SG node contains an unsupported parameter action."
            )
        sweeps = [
            action for action in parameter_actions if action.get("mode") == "sweep"
        ]
        if len(sweeps) > 1:
            raise ConfigurationError("One Anritsu SG node supports one sweep axis.")
        roi_required = any(
            not isinstance(action.get("segments"), list)
            or not action.get("segments")
            for action in sweeps
        )
        return {
            "id": node.id,
            "type": "sequence",
            "text": (
                f"Anritsu SG · {len(parameter_actions)} parameter(s) · RF OFF"
            ),
            "device_module": "anritsu_sg",
            "label": "Anritsu MS2830A Signal Generator",
            "operation": "configure_selected_parameters",
            "configuration_required": roi_required,
            "roi_required": roi_required,
            "parameter_actions": parameter_actions,
            "configuration": {
                "frequency": frequency,
                "power": power,
            },
            "children": [
                RecipePage._node_to_mapping(child) for child in node.children
            ],
        }

    @staticmethod
    def _configured_anritsu_node(
        node: RecipeNode,
        *,
        snapshot: AnritsuConfigurationSnapshot | None = None,
        parameter_actions: list[dict[str, object]],
        acquire_single: bool,
        trace: str,
    ) -> dict[str, object]:
        allowed = {
            "spectrum.start_frequency",
            "spectrum.stop_frequency",
            "spectrum.reference_level",
            "spectrum.points",
            "advanced.rbw_mode",
            "advanced.rbw",
            "advanced.vbw_mode",
            "advanced.vbw",
            "advanced.detector",
            "advanced.attenuation_mode",
            "advanced.attenuation",
            "advanced.preamplifier_enabled",
            "advanced.sweep_time_mode",
            "advanced.sweep_time",
        }
        if any(action.get("parameter_id") not in allowed for action in parameter_actions):
            raise ConfigurationError("Anritsu node contains an unsupported parameter action.")
        sweeps = [action for action in parameter_actions if action.get("mode") == "sweep"]
        if len(sweeps) > 1:
            raise ConfigurationError("One Anritsu node supports one sweep axis.")
        roi_required = any(
            not isinstance(action.get("segments"), list) or not action.get("segments")
            for action in sweeps
        )
        children = [
            RecipePage._node_to_mapping(child)
            for child in node.children
        ]
        existing = next(
            (child for child in node.children if child.type == "acquire_spectrum"),
            None,
        )
        if acquire_single and existing is None:
            children.append(
                {
                    "id": f"anritsu-acquire-{uuid4().hex[:8]}",
                    "type": "acquire_spectrum",
                    "trace": trace,
                }
            )
        snapshot = snapshot or AnritsuConfigurationSnapshot(
            start_hz=1e6,
            stop_hz=10e6,
            reference_level_dbm=0.0,
            points=1001,
            instrument_mode="PLAN_EDIT",
        )
        return {
            "id": node.id,
            "type": "sequence",
            "text": (
                f"Anritsu Spectrum · {len(parameter_actions)} parameter(s)"
                + (" · acquire once" if acquire_single else "")
            ),
            "device_module": "anritsu",
            "label": "Anritsu MS2830A",
            "operation": "configure_selected_parameters",
            "configuration_required": roi_required,
            "roi_required": roi_required,
            "parameter_actions": parameter_actions,
            "acquire_single": acquire_single,
            "trace": trace,
            "configuration": {
                "start_frequency": f"{snapshot.start_hz:.12g} Hz",
                "stop_frequency": f"{snapshot.stop_hz:.12g} Hz",
                "reference_level": f"{snapshot.reference_level_dbm:.12g} dBm",
                "points": snapshot.points,
            },
            "children": children,
        }

    def _edit_anritsu_roi_from_tree(
        self, metadata: dict[str, object]
    ) -> None:
        owner_id = str(metadata.get("owner_node_id", ""))
        parameter_id = str(metadata.get("parameter_id", ""))
        owner_item = self._find_tree_item(owner_id)
        node = owner_item.data(0, Qt.ItemDataRole.UserRole) if owner_item else None
        if not isinstance(node, RecipeNode):
            QMessageBox.warning(self, "ROI editor", "The Anritsu node no longer exists.")
            return
        actions = [
            dict(action)
            for action in node.data.get("parameter_actions", [])
            if isinstance(action, dict)
        ]
        action = next(
            (
                candidate for candidate in actions
                if candidate.get("parameter_id") == parameter_id
                and candidate.get("mode") == "sweep"
            ),
            None,
        )
        if action is None:
            return
        label = next(
            label for candidate, label, _ in AnritsuNodeEditorDialog.parameter_specs
            if candidate == parameter_id
        )
        definition = AnritsuNodeEditorDialog._roi_definition(parameter_id, label)
        initial = action.get("segments")
        dialog = SweepGeneratorDialog(
            definition,
            self,
            initial_segments=(
                [dict(segment) for segment in initial if isinstance(segment, dict)]
                if isinstance(initial, list) else None
            ),
        )
        stage_index = metadata.get("stage_index")
        dialog.select_interval(stage_index if isinstance(stage_index, int) else None)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        action["segments"] = dialog.segment_data()
        configuration = node.data.get("configuration")
        snapshot = self._current_anritsu_snapshot()
        if isinstance(configuration, dict):
            try:
                snapshot = replace(
                    snapshot,
                    start_hz=parse_quantity(
                        configuration["start_frequency"], DIMENSION_FREQUENCY
                    ).si_value,
                    stop_hz=parse_quantity(
                        configuration["stop_frequency"], DIMENSION_FREQUENCY
                    ).si_value,
                    reference_level_dbm=parse_quantity(
                        configuration["reference_level"], DIMENSION_DBM
                    ).si_value,
                    points=int(configuration["points"]),
                )
            except (KeyError, TypeError, ValueError, ConfigurationError):
                pass
        replacement = self._configured_anritsu_node(
            node,
            snapshot=snapshot,
            parameter_actions=actions,
            acquire_single=bool(node.data.get("acquire_single", False)),
            trace=str(node.data.get("trace", "TRAC1")),
        )
        source = replace_recipe_node(
            self.editor.toPlainText(), node_id=node.id, node=replacement
        )
        self._apply_builder_source(source, f"Updated Anritsu ROI for {parameter_id}")

    def _edit_comment_node(self, node: RecipeNode) -> None:
        dialog = CommentEditorDialog(str(node.data.get("text", "")), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        replacement = self._node_to_mapping(node)
        replacement["text"] = dialog.comment_text()
        try:
            source = replace_recipe_node(
                self.editor.toPlainText(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(source, f"Updated comment {node.id}")
        except Exception as exc:
            QMessageBox.warning(self, "Edit comment", str(exc))

    def _edit_anritsu_acquisition_node(self, node: RecipeNode) -> None:
        dialog = AnritsuAcquisitionEditorDialog(node, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        replacement = self._node_to_mapping(node)
        for field in (
            "trace",
            "average_count",
            "reference_operation",
            "store_raw",
            "store_processed",
        ):
            replacement.pop(field, None)
        replacement.update(dialog.node_fields())
        try:
            source = replace_recipe_node(
                self.editor.toPlainText(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source, f"Updated Anritsu acquisition {node.id}"
            )
        except Exception as exc:
            QMessageBox.warning(self, "Anritsu acquisition", str(exc))

    @staticmethod
    def _configured_keithley_node(
        node: RecipeNode,
        snapshot: KeithleyConfigurationSnapshot,
        *,
        parameter_actions: list[dict[str, object]] | None = None,
        output_policy: str = "unchanged",
    ) -> dict[str, object]:
        actions = (
            [
                {
                    "parameter_id": "source.level",
                    "mode": "set",
                    "value": snapshot.source_level,
                }
            ]
            if parameter_actions is None
            else parameter_actions
        )
        allowed_parameters = {
            "source.level",
            "source.compliance",
            "measurement.nplc",
            "measurement.settling_time",
            "measurement.sense_mode",
            "source.range",
            "measurement.voltage_range",
            "measurement.current_range",
        }
        if any(action.get("parameter_id") not in allowed_parameters for action in actions):
            raise ConfigurationError("Keithley node contains an unsupported parameter action.")
        if output_policy not in {"unchanged", "on", "off"}:
            raise ConfigurationError("Keithley output policy must be unchanged, on or off.")
        sweep_actions = [action for action in actions if action.get("mode") == "sweep"]
        if len(sweep_actions) > 1:
            raise ConfigurationError(
                "A Keithley device node supports one sweep axis; nest another node "
                "to create another loop."
            )
        roi_required = any(
            not isinstance(action.get("segments"), list)
            or not action.get("segments")
            for action in sweep_actions
        )
        result: dict[str, object] = {
            "id": node.id,
            "type": "sequence",
            "text": f"Keithley {snapshot.channel} · {len(actions)} selected action(s)",
            "device_module": "keithley",
            "label": "Keithley 2600",
            "configuration_required": roi_required,
            "channel": snapshot.channel,
            "source_mode": snapshot.source_mode,
            "operation": "configure_selected_parameters",
            "parameter_actions": actions,
            "output_policy": output_policy,
            "roi_required": roi_required,
            "configuration": {
                "channel": snapshot.channel,
                "source_mode": snapshot.source_mode,
                "source_level": snapshot.source_level,
                "compliance": snapshot.compliance,
                "nplc": snapshot.nplc,
                "settling_time": snapshot.settling_time,
                "sense_mode": snapshot.sense_mode,
                "source_autorange": snapshot.source_autorange,
                "source_range": snapshot.source_range,
                "measure_voltage_autorange": snapshot.measure_voltage_autorange,
                "measure_voltage_range": snapshot.measure_voltage_range,
                "measure_current_autorange": snapshot.measure_current_autorange,
                "measure_current_range": snapshot.measure_current_range,
            },
        }
        if node.children:
            result["children"] = [
                RecipePage._node_to_mapping(child) for child in node.children
            ]
        return result

    @staticmethod
    def _keithley_roi_definition(
        snapshot: KeithleyConfigurationSnapshot,
        parameter_id: str,
    ) -> dict[str, str]:
        return _keithley_roi_definition(snapshot, parameter_id)

    def _edit_keithley_rois(
        self,
        snapshot: KeithleyConfigurationSnapshot,
        actions: list[dict[str, object]],
    ) -> list[dict[str, object]] | None:
        completed: list[dict[str, object]] = []
        for action in actions:
            if action.get("mode") != "sweep":
                completed.append(dict(action))
                continue
            definition = self._keithley_roi_definition(
                snapshot, str(action.get("parameter_id", ""))
            )
            initial = action.get("segments")
            if isinstance(initial, list) and initial:
                try:
                    generate_sweep_points(initial, definition["dimension"])
                except Exception:
                    pass
                else:
                    completed.append(dict(action))
                    continue
            roi_dialog = SweepGeneratorDialog(
                definition,
                self,
                initial_segments=(
                    [dict(segment) for segment in initial if isinstance(segment, dict)]
                    if isinstance(initial, list)
                    else None
                ),
            )
            roi_dialog.setWindowTitle(
                f"Keithley {snapshot.channel} — ROI · {definition['label']}"
            )
            if roi_dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            completed_action = dict(action)
            completed_action["segments"] = roi_dialog.segment_data()
            completed.append(completed_action)
        return completed

    def _current_keithley_snapshot(self) -> KeithleyConfigurationSnapshot:
        provider = self._keithley_snapshot_provider
        if callable(provider):
            snapshot = provider()
            if isinstance(snapshot, KeithleyConfigurationSnapshot):
                return snapshot
        return KeithleyConfigurationSnapshot()

    @staticmethod
    def _snapshot_with_override(
        snapshot: KeithleyConfigurationSnapshot,
        override: dict[str, object],
    ) -> KeithleyConfigurationSnapshot:
        parameter_id = str(override.get("parameter_id", ""))
        value = str(override.get("value", ""))
        field_by_parameter = {
            "source.level": "source_level",
            "source.compliance": "compliance",
            "measurement.nplc": "nplc",
            "measurement.settling_time": "settling_time",
            "measurement.sense_mode": "sense_mode",
            "source.range": "source_range",
            "measurement.voltage_range": "measure_voltage_range",
            "measurement.current_range": "measure_current_range",
        }
        field = field_by_parameter.get(parameter_id)
        return replace(snapshot, **{field: value}) if field is not None else snapshot

    def _open_current_node_editor(self) -> None:
        item = self.tree.currentItem()
        if item is not None:
            self._edit_selected_node()

    def _open_node_editor(self, item: QTreeWidgetItem, _column: int) -> None:
        """Open the appropriate parameter window from a direct tree interaction."""

        self.tree.setCurrentItem(item)
        operator_row = item.data(0, self.operator_row_role)
        if isinstance(operator_row, dict):
            self._edit_selected_roi()
            return
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(node, RecipeNode):
            return
        if (
            node.type in {
                "sweep",
                "configure_keithley",
                "configure_rigol",
                "configure_anritsu",
                "configure_anritsu_advanced",
                "configure_anritsu_sg",
                "acquire_reference",
                "acquire_spectrum",
                "comment",
            }
            or node.data.get("device_module")
            in {"keithley", "anritsu", "anritsu_sg", "rigol"}
        ):
            self._edit_selected_node()
            return
        self.inspector.setFocus()
        self.status.emit(
            f"{node.id} is selected. Its full configuration is shown in the node inspector."
        )

    def _edit_selected_fixed_keithley(self, node: RecipeNode) -> None:
        channel = str(node.data.get("channel", ""))
        mode = str(node.data.get("mode", ""))
        target = f"keithley.{channel}.{mode}"
        definition = next((item for item in _SWEEPABLE_PARAMETERS if item["target"] == target), None)
        if definition is None or mode not in {"current", "voltage"}:
            QMessageBox.information(
                self, "Edit Keithley setting", "This node is not a source current or voltage setting."
            )
            return
        dialog = FixedValueDialog(definition, self)
        dialog.value.setText(str(node.data.get("level", "")))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        replacement = self._node_to_mapping(node)
        replacement["level"] = dialog.value.text().strip()
        try:
            source = replace_recipe_node(
                self.editor.toPlainText(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(source, f"Updated fixed Keithley setting {node.id}")
        except Exception as exc:
            QMessageBox.warning(self, "Edit Keithley setting", str(exc))

    def _show_tree_context_menu(self, position: object) -> None:
        """Expose the same safe commands under the currently pointed node."""

        point = position.toPoint() if hasattr(position, "toPoint") else position
        item = self.tree.itemAt(point)
        if item is not None:
            self.tree.setCurrentItem(item)
        menu = QMenu(self.tree)
        menu.addAction("New empty sweep", self.new_recipe)
        menu.addSeparator()
        menu.addAction("Add node", self._add_basic_node)
        menu.addAction("Add device control / point generator", self._add_device_controls)
        menu.addSeparator()
        edit_device = menu.addAction(
            "Device settings", self._edit_selected_device_settings
        )
        edit_roi = menu.addAction("Edit ROI", self._edit_selected_roi)
        edit_comment = menu.addAction("Edit comment", self._edit_selected_node)
        edit_acquisition = menu.addAction(
            "Acquisition settings", self._edit_selected_node
        )
        toggle_enabled = menu.addAction(
            "Enable selected node"
            if isinstance(node := (
                self.tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
                if self.tree.currentItem() is not None
                else None
            ), RecipeNode)
            and node.data.get("disabled") is True
            else "Disable selected node",
            self._toggle_selected_node_disabled,
        )
        duplicate = menu.addAction("Duplicate selected node", self._duplicate_selected_node)
        delete = menu.addAction("Delete selected node", self._delete_selected_node)
        move_up = menu.addAction("Move selected node up", lambda: self._move_selected_sibling(-1))
        move_down = menu.addAction("Move selected node down", lambda: self._move_selected_sibling(1))
        selected = self.tree.currentItem()
        node = selected.data(0, Qt.ItemDataRole.UserRole) if selected is not None else None
        operator_row = (
            selected.data(0, self.operator_row_role)
            if selected is not None
            else None
        )
        editable = isinstance(node, RecipeNode)
        has_device_settings = bool(
            editable
            and (
                node.data.get("device_module")
                or self._legacy_device_configuration_node(node) is not None
            )
        )
        has_roi = bool(
            isinstance(operator_row, dict)
            or (
                editable
                and (
                    node.type == "sweep"
                    or (
                        node.data.get("device_module")
                        and any(
                            isinstance(action, dict)
                            and action.get("mode") == "sweep"
                            for action in node.data.get("parameter_actions", [])
                        )
                    )
                )
            )
        )
        edit_device.setEnabled(has_device_settings)
        edit_roi.setEnabled(has_roi)
        edit_comment.setEnabled(editable and node.type == "comment")
        edit_acquisition.setEnabled(
            editable
            and node.type in {"acquire_reference", "acquire_spectrum"}
        )
        toggle_enabled.setEnabled(
            editable and not self._tree_item_is_in_finally(selected)
        )
        duplicate.setEnabled(editable and selected.parent() is not None)
        delete.setEnabled(editable)
        move_up.setEnabled(editable and selected.parent() is not None)
        move_down.setEnabled(editable and selected.parent() is not None)
        menu.exec(self.tree.viewport().mapToGlobal(point))

    def _fixed_node_from_dialog(
        self, definition: dict[str, str], dialog: FixedValueDialog
    ) -> dict[str, object]:
        """Use the same device actions for a literal setpoint and a sweep point."""

        target = definition["target"]
        value = dialog.value.text().strip()
        if target.startswith("keithley."):
            _device, channel, mode = target.split(".")
            channel_settings = self._settings.keithley.safety.channels[channel]
            is_current = mode == "current"
            return {
                "id": self._new_node_id("configure-keithley"),
                "type": "configure_keithley",
                "channel": channel,
                "mode": "current" if is_current else "voltage",
                "level": value,
                "compliance": (
                    channel_settings.lab_limits.voltage_compliance.max
                    if is_current
                    else channel_settings.lab_limits.current_compliance.max
                ),
                "nplc": 1.0,
                "settle_time": "100 ms",
            }
        if target.startswith("rigol."):
            _device, channel, field = target.split(".")
            defaults = self._settings.rigol.safety.channels[channel].defaults
            return {
                "id": self._new_node_id("configure-rigol"),
                "type": "configure_rigol",
                "channel": int(channel),
                "waveform": str(defaults.get("waveform", "SIN")),
                "frequency": value if field == "frequency" else defaults.get("frequency", "1 kHz"),
                "high_level": value if field == "high_level" else defaults.get("high_level", "1 mV"),
                "low_level": value if field == "low_level" else defaults.get("low_level", "-1 mV"),
                "output_load": defaults.get("output_load_setting", "HIGHZ"),
            }
        if target.startswith("anritsu.spectrum."):
            field = target.rsplit(".", 1)[1]
            defaults = self._settings.anritsu.safety.defaults
            return {
                "id": self._new_node_id("configure-anritsu"),
                "type": "configure_anritsu",
                "start_frequency": value if field == "start_frequency" else defaults.get("start_frequency", "1 MHz"),
                "stop_frequency": value if field == "stop_frequency" else defaults.get("stop_frequency", "10 MHz"),
                "reference_level": value if field == "reference_level" else defaults.get("reference_level", "0 dBm"),
                "points": int(defaults.get("sweep_points", 1001)),
            }
        field = target.rsplit(".", 1)[1]
        defaults = self._settings.anritsu.safety.defaults
        return {
            "id": self._new_node_id("configure-anritsu-sg"),
            "type": "configure_anritsu_sg",
            "frequency": value if field == "frequency" else defaults.get("sg_frequency", "1 GHz"),
            "power": value if field == "power" else defaults.get("sg_power", "-30 dBm"),
        }

    def _edit_selected_generator(
        self,
        *,
        node: RecipeNode | None = None,
        stage_index: int | None = None,
    ) -> None:
        item = self.tree.currentItem()
        if node is None:
            node = (
                item.data(0, Qt.ItemDataRole.UserRole)
                if item is not None
                else None
            )
        if not isinstance(node, RecipeNode) or node.type != "sweep":
            QMessageBox.information(self, "Edit generator", "Select a generated sweep node first.")
            return
        target = str(node.data.get("target", ""))
        definition = next((item for item in _SWEEPABLE_PARAMETERS if item["target"] == target), None)
        segments = self._native_sweep_segments(node)
        if definition is None or not segments:
            QMessageBox.information(
                self,
                "Edit generator",
                "This sweep target cannot be represented by the visual ROI editor.",
            )
            return
        if target.startswith("keithley."):
            dialog: SweepGeneratorDialog = KeithleySweepBuilderDialog(
                self._settings, self, initial_segments=segments
            )
            _device, channel, mode = target.split(".")
            assert isinstance(dialog, KeithleySweepBuilderDialog)
            dialog.channel.setCurrentText(channel)
            dialog.mode.setCurrentText(mode)
            existing_config = next(
                (child for child in node.children if child.type == "configure_keithley"), None
            )
            if existing_config is not None:
                dialog.compliance.setText(str(existing_config.data.get("compliance", dialog.compliance.text())))
                dialog.nplc.setText(str(existing_config.data.get("nplc", dialog.nplc.text())))
                dialog.settle_time.setText(str(existing_config.data.get("settle_time", dialog.settle_time.text())))
                dialog.sense_mode.setCurrentText(str(existing_config.data.get("sense_mode", dialog.sense_mode.currentText())))
        else:
            dialog = SweepGeneratorDialog(definition, self, initial_segments=segments)
        dialog.select_interval(stage_index)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        children = [self._node_to_mapping(child) for child in node.children]
        if isinstance(dialog, KeithleySweepBuilderDialog):
            options = dialog.keithley_options()
            for child in children:
                if child["type"] == "configure_keithley":
                    child.update(options)
        replacement = {"id": node.id, "type": "sweep", **node.data}
        for legacy_field in ("start", "stop", "points", "spacing"):
            replacement.pop(legacy_field, None)
        replacement.update(
            {
                "target": target,
                "segments": dialog.segment_data(),
                "children": children,
            }
        )
        try:
            source = replace_recipe_node(
                self.editor.toPlainText(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(source, f"Updated point generator {node.id}")
        except Exception as exc:
            QMessageBox.warning(self, "Edit generator", str(exc))

    @staticmethod
    def _node_to_mapping(node: RecipeNode) -> dict[str, object]:
        result: dict[str, object] = {"id": node.id, "type": node.type, **node.data}
        if node.children:
            result["children"] = [RecipePage._node_to_mapping(child) for child in node.children]
        if node.else_children:
            result["else"] = [RecipePage._node_to_mapping(child) for child in node.else_children]
        return result

    def _sweep_node_from_generator(
        self,
        definition: dict[str, str],
        segments: list[dict[str, object]],
        *,
        keithley_options: dict[str, object] | None = None,
    ) -> dict[str, object]:
        target = definition["target"]
        node_id = self._new_node_id("sweep")
        if target.startswith("keithley."):
            _device, channel, mode = target.split(".")
            channel_settings = self._settings.keithley.safety.channels[channel]
            is_current = mode == "current"
            compliance = (
                channel_settings.lab_limits.voltage_compliance.max
                if is_current
                else channel_settings.lab_limits.current_compliance.max
            )
            child: dict[str, object] = {
                "id": self._new_node_id("configure-keithley"),
                "type": "configure_keithley",
                "channel": channel,
                "mode": "current" if is_current else "voltage",
                "level": "${" + target + "}",
                "compliance": (keithley_options or {}).get("compliance", compliance),
                "nplc": (keithley_options or {}).get("nplc", 1.0),
                "settle_time": (keithley_options or {}).get("settle_time", "100 ms"),
                "sense_mode": (keithley_options or {}).get("sense_mode", "2wire"),
            }
        elif target.startswith("rigol."):
            _device, channel, field = target.split(".")
            defaults = self._settings.rigol.safety.channels[channel].defaults
            child = {
                "id": self._new_node_id("configure-rigol"),
                "type": "configure_rigol",
                "channel": int(channel),
                "waveform": str(defaults.get("waveform", "SIN")),
                "frequency": "${" + target + "}" if field == "frequency" else defaults.get("frequency", "1 kHz"),
                "high_level": "${" + target + "}" if field == "high_level" else defaults.get("high_level", "1 mV"),
                "low_level": "${" + target + "}" if field == "low_level" else defaults.get("low_level", "-1 mV"),
                "output_load": defaults.get("output_load_setting", "HIGHZ"),
            }
        elif target.startswith("anritsu.spectrum."):
            field = target.rsplit(".", 1)[1]
            defaults = self._settings.anritsu.safety.defaults
            child = {
                "id": self._new_node_id("configure-anritsu"),
                "type": "configure_anritsu",
                "start_frequency": "${" + target + "}" if field == "start_frequency" else defaults.get("start_frequency", "1 MHz"),
                "stop_frequency": "${" + target + "}" if field == "stop_frequency" else defaults.get("stop_frequency", "10 MHz"),
                "reference_level": "${" + target + "}" if field == "reference_level" else defaults.get("reference_level", "0 dBm"),
                "points": int(defaults.get("sweep_points", 1001)),
            }
        else:
            field = target.rsplit(".", 1)[1]
            defaults = self._settings.anritsu.safety.defaults
            child = {
                "id": self._new_node_id("configure-anritsu-sg"),
                "type": "configure_anritsu_sg",
                "frequency": "${" + target + "}" if field == "frequency" else defaults.get("sg_frequency", "1 GHz"),
                "power": "${" + target + "}" if field == "power" else defaults.get("sg_power", "-30 dBm"),
            }
        return {
            "id": node_id,
            "type": "sweep",
            "target": target,
            "segments": segments,
            "children": [child],
        }

    def _delete_selected_node(self) -> None:
        item = self.tree.currentItem()
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(node, RecipeNode):
            QMessageBox.information(self, "Delete node", "Select a recipe node first.")
            return
        try:
            source = delete_recipe_node(self.editor.toPlainText(), node_id=node.id)
            self._apply_builder_source(source, f"Deleted {node.id}")
        except Exception as exc:
            QMessageBox.warning(self, "Delete node", str(exc))

    @staticmethod
    def _tree_item_is_in_finally(item: QTreeWidgetItem | None) -> bool:
        current = item
        while current is not None:
            if (
                current.data(0, Qt.ItemDataRole.UserRole) is None
                and current.text(0).startswith("Finally")
            ):
                return True
            current = current.parent()
        return False

    def _toggle_selected_node_disabled(self) -> None:
        item = self.tree.currentItem()
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(node, RecipeNode):
            return
        if self._tree_item_is_in_finally(item):
            QMessageBox.warning(
                self,
                "Disable node",
                "Finally safety actions cannot be disabled.",
            )
            return
        replacement = self._node_to_mapping(node)
        disabled = node.data.get("disabled") is not True
        if disabled:
            replacement["disabled"] = True
        else:
            replacement.pop("disabled", None)
        try:
            source = replace_recipe_node(
                self.editor.toPlainText(), node_id=node.id, node=replacement
            )
            self._apply_builder_source(
                source,
                f"{'Disabled' if disabled else 'Enabled'} {node.id}",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Enable / disable node", str(exc))

    def _duplicate_selected_node(self) -> None:
        item = self.tree.currentItem()
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        parent = item.parent() if item is not None else None
        if not isinstance(node, RecipeNode) or parent is None:
            QMessageBox.information(
                self, "Duplicate node", "Select a non-root recipe node to duplicate."
            )
            return
        if parent.text(0).startswith("Else") and parent.parent() is not None:
            owner = parent.parent().data(0, Qt.ItemDataRole.UserRole)
            if not isinstance(owner, RecipeNode):
                return
            parent_id, branch = owner.id, "else"
        elif parent.text(0).startswith("Finally"):
            parent_id, branch = "__finally__", "children"
        else:
            owner = parent.data(0, Qt.ItemDataRole.UserRole)
            if not isinstance(owner, RecipeNode):
                return
            parent_id, branch = owner.id, "children"
        copy = self._clone_node_mapping(self._node_to_mapping(node))
        try:
            source = add_recipe_node(
                self.editor.toPlainText(),
                parent_id=parent_id,
                branch=branch,
                index=parent.indexOfChild(item) + 1,
                node=copy,
            )
            self._apply_builder_source(source, f"Duplicated {node.id}")
        except Exception as exc:
            QMessageBox.warning(self, "Duplicate node", str(exc))

    def _clone_node_mapping(
        self, mapping: dict[str, object]
    ) -> dict[str, object]:
        clone = deepcopy(mapping)
        clone["id"] = self._new_node_id(str(clone.get("type", "node")))
        for branch in ("children", "else"):
            raw_children = clone.get(branch)
            if isinstance(raw_children, list):
                clone[branch] = [
                    self._clone_node_mapping(child)
                    for child in raw_children
                    if isinstance(child, dict)
                ]
        return clone

    def _move_selected_sibling(self, delta: int) -> None:
        item = self.tree.currentItem()
        node = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        parent = item.parent() if item is not None else None
        if not isinstance(node, RecipeNode) or parent is None:
            return
        parent_id, branch = self._builder_parent()
        index = parent.indexOfChild(item)
        target = max(0, min(index + delta, parent.childCount() - 1))
        if target == index:
            return
        self._move_recipe_node(node.id, parent_id, branch, target)

    def request_run(self) -> None:
        if self._plan is not None:
            self.run_requested.emit(self._plan)


class RunMonitorPage(QWidget):
    stop_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel("Recipe execution")
        title.setObjectName("pageTitle")
        self.state = QLabel("IDLE")
        self.state.setObjectName("readout")
        self.heartbeat = QLabel("Heartbeat: —")
        self.heartbeat.setObjectName("muted")
        self.eta = QLabel("ETA: —")
        self.eta.setObjectName("muted")
        telemetry = QGridLayout()
        self.current_path = QLabel("Current node: —")
        self.current_path.setWordWrap(True)
        self.current_setpoints = QLabel("Setpoints (SI): —")
        self.current_setpoints.setWordWrap(True)
        self.current_measurements = QLabel("Measurements (SI): —")
        self.current_measurements.setWordWrap(True)
        self.storage_rate = QLabel("Storage: —")
        self.storage_rate.setWordWrap(True)
        telemetry.addWidget(self.current_path, 0, 0)
        telemetry.addWidget(self.storage_rate, 0, 1)
        telemetry.addWidget(self.current_setpoints, 1, 0)
        telemetry.addWidget(self.current_measurements, 1, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        controls = QHBoxLayout()
        pause = QPushButton("Pause after point")
        resume = QPushButton("Resume")
        stop = QPushButton("Stop safely")
        controls.addWidget(pause)
        controls.addWidget(resume)
        controls.addWidget(stop)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.warnings = QPlainTextEdit()
        self.warnings.setReadOnly(True)
        self.warnings.setMaximumHeight(95)
        self.warnings.setPlaceholderText("No run warnings.")
        self.spectrum_preview = SpectrumPlotWidget(legend=False)
        self.spectrum_preview.set_labels(
            x="Frequency",
            x_unit="Hz",
            y="Amplitude",
            y_unit="dBm",
        )
        self.spectrum_preview.set_title("Latest stored spectrum checkpoint")
        monitor_splitter = QSplitter(Qt.Orientation.Horizontal)
        monitor_splitter.addWidget(self.events)
        monitor_splitter.addWidget(self.spectrum_preview)
        monitor_splitter.setStretchFactor(0, 1)
        monitor_splitter.setStretchFactor(1, 2)
        layout.addWidget(title)
        layout.addWidget(self.state)
        layout.addWidget(self.heartbeat)
        layout.addWidget(self.eta)
        layout.addLayout(telemetry)
        layout.addWidget(self.progress)
        layout.addLayout(controls)
        layout.addWidget(self.warnings)
        layout.addWidget(monitor_splitter, 1)
        pause.clicked.connect(self.pause_requested)
        resume.clicked.connect(self.resume_requested)
        stop.clicked.connect(self.stop_requested)
        self._eta_started = 0.0
        self._model_duration_s = 0.0
        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(1000)
        self._eta_timer.timeout.connect(self._update_eta)

    def run_started(self, actions: int, estimated_duration_s: float = 0.0) -> None:
        self.state.setText("RUNNING")
        self.progress.setRange(0, max(actions, 1))
        self.progress.setValue(0)
        self.events.clear()
        self.warnings.clear()
        self.spectrum_preview.clear()
        self.current_path.setText("Current node: waiting for first action")
        self.current_setpoints.setText("Setpoints (SI): —")
        self.current_measurements.setText("Measurements (SI): —")
        self.storage_rate.setText("Storage: waiting for first checkpoint")
        self.heartbeat.setText("Heartbeat: waiting for first operation")
        self._eta_started = time.monotonic()
        self._model_duration_s = max(0.0, estimated_duration_s)
        self._eta_timer.start()
        self._update_eta()

    def _update_eta(self) -> None:
        if not self._eta_started:
            self.eta.setText("ETA: —")
            return
        elapsed = max(0.0, time.monotonic() - self._eta_started)
        completed = self.progress.value()
        total = max(1, self.progress.maximum())
        empirical_remaining = (
            elapsed / completed * max(0, total - completed)
            if completed
            else self._model_duration_s
        )
        model_remaining = max(0.0, self._model_duration_s - elapsed)
        remaining = max(empirical_remaining, model_remaining)
        self.eta.setText(
            f"Elapsed: {_human_duration(elapsed)} • "
            f"estimated remaining: {_human_duration(remaining)}"
        )

    def update_heartbeat(self, data: dict[str, object]) -> None:
        self.heartbeat.setText(
            "Heartbeat: "
            f"{data.get('kind', 'operation')} • attempt {data.get('attempt', '—')} • "
            f"elapsed {float(data.get('elapsed_s', 0.0)):.2f} s • "
            f"remaining {float(data.get('remaining_s', 0.0)):.2f} s"
        )

    def append_event(self, name: str, data: dict[str, object]) -> None:
        if name == "action_finished":
            self.progress.setValue(min(self.progress.value() + 1, self.progress.maximum()))
        elif name == "action_started":
            self.current_path.setText(
                f"Current node: {data.get('node_id', '—')} • {data.get('kind', '—')} • "
                f"action {int(data.get('action_index', 0)) + 1}/{self.progress.maximum()}"
            )
            self.current_setpoints.setText(
                "Setpoints (SI): " + self._format_scalars(data.get("setpoints_si"))
            )
        elif name == "point_stored":
            self.current_measurements.setText(
                "Measurements (SI): " + self._format_scalars(data.get("measurements_si"))
            )
            self.storage_rate.setText(
                f"Storage: point {data.get('stored_points', '—')} • "
                f"write {float(data.get('write_elapsed_s', 0.0)) * 1000:.1f} ms • "
                f"average {float(data.get('average_write_rate_points_per_s', 0.0)):.2f} point/s • "
                f"spectrum {data.get('spectrum_points', 0)} values"
            )
        if name == "pause_pending":
            self.state.setText("PAUSED")
        elif name == "run_fault":
            self.state.setText("FAULT")
        elif name == "watchdog_timeout":
            self.state.setText("FAULT • WATCHDOG TIMEOUT")
        if name in {
            "action_retry",
            "compliance_detected",
            "run_fault",
            "watchdog_timeout",
            "safe_finally_error",
        }:
            self.warnings.appendPlainText(f"{name}: {data}")
        self.events.appendPlainText(f"{name}: {data}")

    def update_spectrum_preview(self, data: dict[str, object]) -> None:
        frequencies = data.get("frequency_hz")
        powers = data.get("power_dbm")
        if not isinstance(frequencies, (tuple, list)) or not isinstance(powers, (tuple, list)):
            return
        self.spectrum_preview.set_trace(
            "Stored spectrum",
            frequencies,
            powers,
            primary=True,
        )
        self.spectrum_preview.set_title(
            f"Stored point {data.get('point_index', '—')} • "
            f"{data.get('source_points', len(powers))} source values"
        )

    @staticmethod
    def _format_scalars(value: object) -> str:
        if not isinstance(value, dict) or not value:
            return "—"
        return " • ".join(
            f"{key}={float(number):.6g}" for key, number in sorted(value.items())
        )

    def complete(self, result: object) -> None:
        self._eta_timer.stop()
        run_result = result["result"]
        self.state.setText(f"{run_result.state.value.upper()} • {run_result.stored_points} points")
        self.events.appendPlainText(f"File: {result['path']}")

    def failed(self, error: str) -> None:
        self._eta_timer.stop()
        self.state.setText("FAULT")
        self.events.appendPlainText(error)


class ResultsPage(QWidget):
    """Browse immutable run files without opening an instrument session."""

    resume_requested = Signal(object)

    def __init__(self, output_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._output_dir = Path(output_dir)
        self._selected_path: Path | None = None
        layout = QVBoxLayout(self)
        title = QLabel("Results")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.location = QLabel()
        self.location.setObjectName("muted")
        layout.addWidget(self.location)
        actions = QHBoxLayout()
        refresh = QPushButton("Refresh file list")
        self.resume_button = QPushButton("Resume from safe checkpoint")
        self.resume_button.setEnabled(False)
        self.resume_button.setToolTip(
            "Available only for interrupted runs containing a confirmed safe boundary."
        )
        actions.addWidget(refresh)
        actions.addWidget(self.resume_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.runs = QTreeWidget()
        self.runs.setHeaderLabels(["File", "State", "Spectra", "Points"])
        self.runs.setMinimumWidth(240)
        self.runs.setColumnWidth(0, 220)
        splitter.addWidget(self.runs)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.details_tabs = QTabWidget()
        self.metadata = QPlainTextEdit()
        self.recipe_snapshot = QPlainTextEdit()
        self.settings_snapshot = QPlainTextEdit()
        for widget in (self.metadata, self.recipe_snapshot, self.settings_snapshot):
            widget.setReadOnly(True)
        self.details_tabs.addTab(self.metadata, "Metadata")
        self.details_tabs.addTab(self.recipe_snapshot, "Recipe")
        self.details_tabs.addTab(self.settings_snapshot, "Settings")
        right_layout.addWidget(self.details_tabs)

        self.points = QTreeWidget()
        self.points.setHeaderLabels(["Point", "State", "UTC time", "Data"])
        self.points.setMinimumHeight(150)
        self.points.setColumnWidth(0, 70)
        self.points.setColumnWidth(1, 80)
        self.points.setColumnWidth(2, 210)
        right_layout.addWidget(self.points)

        self.spectrum_plot = SpectrumPlotWidget(legend=False)
        self.spectrum_plot.set_title("Select a point containing a stored spectrum")
        self.spectrum_plot.setMinimumHeight(280)
        right_layout.addWidget(self.spectrum_plot, 1)
        self.spectrum_info = QLabel("Spectra are read from HDF5 without contacting instruments.")
        self.spectrum_info.setObjectName("muted")
        right_layout.addWidget(self.spectrum_info)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        refresh.clicked.connect(self.refresh)
        self.resume_button.clicked.connect(self._request_resume)
        self.runs.currentItemChanged.connect(self._run_selected)
        self.points.currentItemChanged.connect(self._point_selected)
        self.refresh()

    def refresh(self) -> None:
        self.location.setText(f"Directory: {self._output_dir.resolve()}")
        self.resume_button.setEnabled(False)
        self.runs.clear()
        self.points.clear()
        self._clear_details()
        for summary in Hdf5RunReader.list_runs(self._output_dir):
            item = QTreeWidgetItem(
                [
                    summary.path.name,
                    summary.status,
                    str(summary.spectrum_count),
                    str(summary.point_count),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, str(summary.path))
            item.setToolTip(0, str(summary.path.resolve()))
            self.runs.addTopLevelItem(item)
        if self.runs.topLevelItemCount() == 0:
            self.metadata.setPlainText("No HDF5 files in the results directory.")
        elif self._selected_path is not None:
            for index in range(self.runs.topLevelItemCount()):
                candidate = self.runs.topLevelItem(index)
                if Path(str(candidate.data(0, Qt.ItemDataRole.UserRole))) == self._selected_path:
                    self.runs.setCurrentItem(candidate)
                    break

    def _run_selected(self, item: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        self.points.clear()
        self._clear_spectrum()
        if item is None:
            self.resume_button.setEnabled(False)
            return
        path = Path(str(item.data(0, Qt.ItemDataRole.UserRole)))
        self._selected_path = path
        try:
            detail = Hdf5RunReader.detail(path)
            points = Hdf5RunReader.points(path)
        except Exception as exc:
            self.resume_button.setEnabled(False)
            self.metadata.setPlainText(f"Cannot read result:\n{exc}")
            self.recipe_snapshot.clear()
            self.settings_snapshot.clear()
            return
        self.resume_button.setEnabled(detail.summary.status in {"aborted", "faulted", "incomplete"})
        self._show_detail(detail)
        for point in points:
            fields = {**point.setpoints, **point.measurements}
            suffix = " • spectrum" if point.has_spectrum else ""
            point_item = QTreeWidgetItem(
                [
                    str(point.index),
                    point.status,
                    point.timestamp_utc or "—",
                    f"{len(fields)} values{suffix}",
                ]
            )
            point_item.setData(0, Qt.ItemDataRole.UserRole, point)
            point_item.setToolTip(3, self._point_tooltip(point))
            self.points.addTopLevelItem(point_item)

    def _request_resume(self) -> None:
        if self._selected_path is not None and self.resume_button.isEnabled():
            self.resume_requested.emit(self._selected_path)

    def _show_detail(self, detail: RunDetail) -> None:
        summary = detail.summary
        lines = [
            f"File: {summary.path}",
            f"State: {summary.status}",
            f"Created (UTC): {summary.created_at_utc or 'missing'}",
            f"Application version: {summary.application_version or 'missing'}",
            f"Plan hash: {summary.plan_sha256 or 'missing'}",
            f"Checkpoints: {summary.point_count}; stored spectra: {summary.spectrum_count}",
            "",
            "Instrument identities:",
        ]
        lines.extend(f"  {name}: {idn}" for name, idn in sorted(detail.device_idn.items()))
        lines.extend(("", "Authenticated operator:", self._format_json(detail.operator_context)))
        lines.extend(("", "Capabilities (snapshot):", self._format_json(detail.capabilities)))
        if detail.events:
            lines.extend(("", f"Recent events ({len(detail.events)}):"))
            lines.extend(
                f"  {event.timestamp_utc} [{event.severity}] {event.name}"
                for event in detail.events[-20:]
            )
        self.metadata.setPlainText("\n".join(lines))
        self.recipe_snapshot.setPlainText(detail.recipe_yaml)
        self.settings_snapshot.setPlainText(detail.settings_yaml)

    @staticmethod
    def _format_json(value: object) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)

    def _point_selected(self, item: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        self._clear_spectrum()
        if item is None or self._selected_path is None:
            return
        point = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(point, StoredPoint):
            return
        if not point.has_spectrum:
            self.spectrum_info.setText("This checkpoint contains no spectrum.")
            return
        try:
            trace = Hdf5RunReader.spectrum(self._selected_path, point.index, max_points=2_000)
        except Exception as exc:
            self.spectrum_info.setText(f"Cannot read spectrum: {exc}")
            return
        if trace is None:
            self.spectrum_info.setText("No spectrum for the selected checkpoint.")
            return
        self.spectrum_plot.set_trace(
            "Stored spectrum",
            trace.frequencies_hz,
            trace.powers_dbm,
            primary=True,
        )
        self.spectrum_plot.set_title(f"Spectrum at point {point.index} ({trace.trace_name})")
        self.spectrum_plot.auto_range()
        self.spectrum_info.setText(
            f"{trace.source_point_count} points in file; interactive peak-preserving display • "
            f"{trace.acquired_at_utc or 'missing time'} • max {max(trace.powers_dbm):.4g} dBm"
        )

    @staticmethod
    def _point_tooltip(point: StoredPoint) -> str:
        payload = {"setpoints": point.setpoints, "measurements": point.measurements, "metadata": point.metadata}
        return ResultsPage._format_json(payload)

    def _clear_details(self) -> None:
        self.metadata.clear()
        self.recipe_snapshot.clear()
        self.settings_snapshot.clear()
        self._clear_spectrum()

    def _clear_spectrum(self) -> None:
        self.spectrum_plot.clear()
        self.spectrum_plot.set_title("Select a point containing a stored spectrum")
        self.spectrum_info.setText("Spectra are read from HDF5 without contacting instruments.")


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
        self.setWindowTitle("Lab Control — Rigol · Keithley · Anritsu" + suffix)
        self.resize(1360, 880)
        self._controllers = {name: DeviceController(self._make_adapter(name), self) for name in ("rigol", "keithley", "anritsu")}
        for controller in self._controllers.values():
            controller.set_operation_guard(self._guard_manual_operation)
        self._device_states = {"rigol": "disconnected", "keithley": "disconnected", "anritsu": "disconnected"}
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

    def _make_adapter(self, name: str):
        if name == "rigol":
            return RigolAdapter(self._settings, session_factory=SimulatedVisaFactory("rigol") if self._simulation else None)
        if name == "keithley":
            return KeithleyAdapter(self._settings, session_factory=SimulatedVisaFactory("keithley") if self._simulation else None)
        if name == "anritsu":
            return AnritsuAdapter(self._settings, session_factory=SimulatedVisaFactory("anritsu") if self._simulation else None)
        raise ValueError(f"Unknown adapter {name!r}.")

    def _build(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.setCentralWidget(self.tabs)
        self.dashboard = DashboardPage(self._settings, discovery_enabled=not self._simulation)
        self.rigol_page = RigolPage(self._controllers["rigol"], self._settings)
        self.keithley_page = KeithleyPage(self._controllers["keithley"], self._settings)
        self.anritsu_page = AnritsuPage(
            self._controllers["anritsu"],
            self._settings,
            single_sweep_available=self._settings.anritsu.acquisition.single_sweep_mode == "standard_scpi_opc",
        )
        self.recipe_page = RecipePage(self._settings)
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
        for widget, name in (
            (self.dashboard, "Dashboard"),
            (self.rigol_page, "Rigol"),
            (self.keithley_page, "Keithley"),
            (self.anritsu_page, "Anritsu"),
            (self.recipe_page, "Sweeps"),
            (self.run_monitor, "Execution"),
            (self.results_page, "Results"),
            (self.settings_page, "Settings"),
        ):
            self.tabs.addTab(widget, name)
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
        for page in (self.rigol_page, self.keithley_page, self.anritsu_page, self.recipe_page, self.settings_page):
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
        icon_dir = Path(__file__).resolve().parent / "assets" / "icons"
        icon_files = (
            "dashboard.svg", "rigol.svg", "keithley.svg", "anritsu.svg",
            "recipes.svg", "execution.svg", "results.svg", "settings.svg",
        )
        labels = ("Dashboard", "Rigol", "Keithley", "Anritsu", "Sweeps", "Execution", "Results", "Settings")
        for index, (label, icon_file) in enumerate(zip(labels, icon_files, strict=True)):
            if index in {4, 6}:
                ribbon.addSeparator()
            action = QAction(QIcon(str(icon_dir / icon_file)), label, self)
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
        for device in ("rigol", "keithley", "anritsu"):
            label = QLabel(f"● {device.title()}: OFFLINE")
            label.setObjectName("compactDeviceStatus")
            label.setAccessibleName(f"{device.title()} connection and output state")
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
            card.connect_requested.connect(lambda current=controller: current.call("connect"))
            card.disconnect_requested.connect(lambda current=controller: current.call("disconnect"))
            card.test_requested.connect(
                lambda current=controller, current_card=card: (
                    current_card.set_testing(True),
                    current.call("test_communication"),
                )
            )
            controller.state_changed.connect(card.update_state)
            controller.state_changed.connect(lambda state, device=name: self._set_device_state(device, state))
            controller.result.connect(
                lambda operation, result, device=name, current=card: self._device_result(
                    device, current, operation, result
                )
            )
            controller.error.connect(lambda operation, error, device=name: self._device_error(device, operation, error))
            controller.traffic.connect(
                lambda message, device=name: self._log(f"{device.upper()} VISA {message}")
            )
            if name == "rigol":
                controller.capabilities_changed.connect(self.rigol_page.set_capabilities)
            elif name == "anritsu":
                controller.capabilities_changed.connect(self.anritsu_page.set_capabilities)

    def _device_result(self, device: str, card: DeviceCard, operation: str, result: object) -> None:
        if operation == "connect":
            card.update_identity(result)
            self.dashboard.mark_identity_verified(device)
            self._log(f"Connected: {getattr(result, 'idn', result)}")
        elif operation == "disconnect":
            self._log("Instrument disconnected")
        elif operation == "replace_adapter":
            card.set_reconfiguring(False)
            card.update_state("disconnected")
            card.identity.setText("IDN: not connected")
            self._log(f"VISA ADAPTER REPLACE COMPLETE: {card.resource.text()}")
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
            self.dashboard.mark_identity_verified(device)
            self._log(
                f"Communication test passed: {result.get('idn', '')}; "
                f"features={features}; options={options}"
            )

    def _device_error(self, device: str, operation: str, error: str) -> None:
        if operation == "replace_adapter":
            self.dashboard.cards[device].set_reconfiguring(False)
        elif operation == "test_communication":
            card = self.dashboard.cards[device]
            card.set_testing(False)
            card.update_state("fault")
            card.identity.setText(f"TEST FAILED: {error}")
        self._log(f"{device}/{operation}: {error}")
        self.dashboard.record_device_error(device, error)

    def _set_device_state(self, device: str, state: str) -> None:
        self._device_states[device] = state
        self.dashboard.update_device_state(device, state)
        label = self.toolbar_device_status.get(device)
        if label is not None:
            label.setText(f"● {device.title()}: {state.replace('_', ' ').upper()}")
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
        self.recipe_page.set_settings(self._settings)
        profile_state = "LOCKED" if self._settings.outputs_locked else "APPROVED"
        self.profile_status.setText(f"Profile: {profile_state}")
        self.profile_status.setObjectName("profileLocked" if self._settings.outputs_locked else "profileApproved")
        self.profile_status.style().unpolish(self.profile_status)
        self.profile_status.style().polish(self.profile_status)
        for name, controller in self._controllers.items():
            self.dashboard.cards[name].set_reconfiguring(True)
            connection = getattr(self._settings, name).connection
            self._log(
                f"VISA ADAPTER REPLACE QUEUED [{name}]: resource={connection.resource!r}, "
                f"backend={connection.visa_backend!r}"
            )
            controller.reconfigure(self._make_adapter(name))
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

    def _set_run_ui_locked(self, locked: bool) -> None:
        for index in (1, 2, 3, 4, 7):
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
