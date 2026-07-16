"""Main PySide6 application window and manual-control pages."""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, QSettings, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSpinBox,
    QTabWidget,
    QToolBar,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.devices.anritsu import (
    AnritsuAdapter,
    AnritsuConfigurationSnapshot,
    SpectrumConfig,
    SpectrumTrace,
)
from app.devices.discovery import DiscoveredInstrument
from app.devices.keithley import KeithleyAdapter, KeithleySourceRequest
from app.devices.rigol import (
    RigolAdapter,
    RigolBurstConfig,
    RigolChannelConfig,
    RigolFrequencySweepConfig,
    RigolModulationConfig,
    RigolOutputConfig,
)
from app.domain.errors import ConfigurationError
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
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from app.safety.rigol_current import validate_rigol_frequency_sweep, validate_rigol_waveform
from app.safety.anritsu import ANRITSU_SWEEP_POINT_COUNTS
from app.storage import Hdf5RunReader, RunDetail, StoredPoint
from app.spectrum import (
    LinearPowerAverager,
    apply_reference_operation,
    frequency_grids_match,
)
from app.ui.settings_page import SettingsPage
from app.ui.run_worker import RunController
from app.ui.workers import DeviceController
from app.ui.discovery_worker import VisaDiscoveryWorker
from app.ui.design_system import effective_theme
from app.ui.widgets import NotificationBanner, SpectrumPlotWidget


def _line(value: str, width: int = 14) -> QLineEdit:
    edit = QLineEdit(value)
    edit.setMinimumWidth(width * 8)
    return edit


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
            self.assign_button.setEnabled(True)
            self._show_pending_assignment()

    def _request_assignment(self) -> None:
        payload = self.detected_resources.currentData()
        if isinstance(payload, tuple) and len(payload) == 3:
            self.assign_resource_requested.emit(payload)

    def _detected_selection_changed(self, index: int) -> None:
        pending = index >= 0 and self.detected_resources.isEnabled()
        self.assign_button.setEnabled(pending)
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
        self._settings = settings
        for name, device in (
            ("rigol", settings.rigol),
            ("keithley", settings.keithley),
            ("anritsu", settings.anritsu),
        ):
            self.cards[name].update_resource(device.connection.resource, device.connection.visa_backend)
        self._refresh_card_resource_choices()
        profile = "✓ approved" if not settings.outputs_locked else "✕ unverified — outputs locked"
        rigol_serial = "✓" if settings.rigol.identity.require_serial_match else "✕"
        anritsu = (
            "✓ configured operations enabled"
            if settings.anritsu.safety.acquisition_allowed
            else "◐ passive spectrum read available; configuration locked"
        )
        self.checklist.setText(
            "Readiness:\n"
            f"• Profile: {profile}\n"
            f"• Rigol serial-number binding: {rigol_serial}\n"
            f"• Anritsu acquisition: {anritsu}\n"
            "• Declare the DUT and verify every limit before OUTPUT ON."
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
            assignment.setEnabled(result.resource != "—" and result.idn is not None)
            self.discovery_table.setCellWidget(row, 0, assignment)
            assign_button = QPushButton("Assign")
            assign_button.setProperty("compact", True)
            assign_button.setEnabled(result.resource != "—" and result.idn is not None)
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
        self.save_assignments.setEnabled(assignable > 0)
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


class RigolPage(QWidget):
    status = Signal(str)

    def __init__(self, controller: DeviceController, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._station_settings = settings
        self._limit_fields: dict[QWidget, LimitField] = {}
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
        arm = QPushButton("ARM (30 s)")
        arm.setObjectName("warningButton")
        output_on = QPushButton("OUTPUT ON")
        output_on.setObjectName("outputOnButton")
        output_off = QPushButton("OUTPUT OFF")
        output_off.setObjectName("outputOffButton")
        self.output_scroll = self._form_page(
            "Output path and SYNC",
            "Output-path changes are allowed only while OUTPUT is OFF. OUTPUT ON requires ARM and confirmation.",
            (
                ("Generator load setting", self.load),
                ("Output polarity", self.output_polarity),
                ("Output mode", self.output_mode),
                ("Gate polarity", self.gate_polarity),
                ("", self.sync_enabled),
                ("SYNC polarity", self.sync_polarity),
                ("SYNC delay", self.sync_delay),
            ),
            (configure_output, self.sync_phases_button, arm, output_on, output_off),
        )

        self.advanced = QTabWidget()
        self.advanced.setObjectName("rigolAdvancedTabs")
        self.advanced.addTab(self._modulation_tab(), "Modulation")
        self.advanced.addTab(self._sweep_tab(), "Sweep")
        self.advanced.addTab(self._burst_tab(), "Burst")
        for index in range(self.advanced.count()):
            self.advanced.setTabEnabled(index, False)

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
        arm.clicked.connect(self.arm_output)
        output_on.clicked.connect(lambda: self.request_output(True))
        output_off.clicked.connect(lambda: self._controller.call("set_output", (int(self.channel.currentText()), False)))
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
            arm=arm,
            output_on=output_on,
            output_off=output_off,
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
        arm: QPushButton,
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
            arm: ("ARM", "Creates a 30-second permission window for OUTPUT ON after safety validation. ARM does not energize the connector."),
            output_on: ("OUTPUT ON", "Energizes the physical BNC output. It requires a valid ARM and an additional confirmation dialog."),
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
        self.advanced.setTabEnabled(0, bool(supports("modulation")))
        self.advanced.setTabEnabled(1, bool(supports("frequency_sweep")))
        self.advanced.setTabEnabled(2, bool(supports("burst")))
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
        channel = self.channel.currentText()
        answer = QMessageBox.warning(
            self,
            "OUTPUT ON Rigol",
            f"Enable the physical Rigol CH{channel} output? A valid ARM is required.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._controller.call("set_output", (int(channel), enabled))

    def _result(self, operation: str, result: object) -> None:
        if operation == "configure" and hasattr(result, "peak_absolute_current_a"):
            estimate = result
            self.estimate.setText(
                "Estimated load current (not measured): "
                f"{estimate.peak_absolute_current_a * 1e3:.6g} mA; "
                f"estimated DUT power: {estimate.peak_estimated_dut_power_w * 1e3:.6g} mW; "
                f"Vth High/Low: {estimate.open_circuit_high_v:.6g} / {estimate.open_circuit_low_v:.6g} V"
            )
            self.status.emit("Rigol configured while OUTPUT is OFF")
        elif operation in {"configure_modulation", "configure_sweep", "configure_burst"}:
            self.status.emit(f"Rigol: {operation} configured while OUTPUT is OFF")
        elif operation == "configure_output":
            self.status.emit("Rigol: output path confirmed while OUTPUT is OFF")
        elif operation == "arm":
            self.status.emit("Rigol armed for 30 seconds; OUTPUT ON requires separate confirmation")
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
            QMessageBox.warning(self, "Rigol", error)


class KeithleyPage(QWidget):
    status = Signal(str)

    def __init__(self, controller: DeviceController, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._station_settings = settings
        self._limit_fields: dict[str, LimitField] = {}
        self._output_states = {"A": False, "B": False}
        self._pending_channels: dict[str, str] = {}
        self._measure_pending = False
        self._live_next_channel = "A"
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(1000)
        self._live_timer.timeout.connect(self._request_live_measurement)
        layout = QVBoxLayout(self)
        self.banner = NotificationBanner()
        layout.addWidget(self.banner)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        hero = QFrame()
        hero.setObjectName("keithleyHero")
        hero_layout = QHBoxLayout(hero)
        title = QLabel("Keithley 2600 — Dual-channel SMU")
        title.setObjectName("pageTitle")
        hero_layout.addWidget(title)
        hero_layout.addStretch(1)
        self.device_led = QLabel("●")
        self.device_led.setObjectName("keithleyLed")
        self.device_state = QLabel("DISCONNECTED")
        self.device_state.setObjectName("keithleyState")
        hero_layout.addWidget(self.device_led)
        hero_layout.addWidget(self.device_state)
        layout.addWidget(hero)
        channel_grid = QGridLayout()
        channel_grid.setSpacing(12)
        self.channel_cards: dict[str, dict[str, QLabel | QFrame]] = {}
        for column, channel_name in enumerate(("A", "B")):
            channel_grid.addWidget(self._build_channel_card(channel_name), 0, column)
        layout.addLayout(channel_grid)
        self.control_tabs = QTabWidget()
        self.control_tabs.setObjectName("keithleyControlTabs")
        source_tab = QWidget()
        source_layout = QVBoxLayout(source_tab)
        source_title = QLabel("Source and measurement configuration")
        source_title.setObjectName("sectionTitle")
        source_layout.addWidget(source_title)
        form = QFormLayout()
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
        self.level_field = self._keithley_bounded("level", self.level)
        self.compliance_field = self._keithley_bounded("compliance", self.compliance)
        self.source_range_field = self._keithley_bounded("source_range", self.source_range)
        for label, widget in (
            ("Channel", self.channel),
            ("Source mode", self.mode),
            ("Source current", self.level_field),
            ("Voltage compliance (safety limit)", self.compliance_field),
            ("NPLC", self._keithley_bounded("nplc", self.nplc)),
            ("Settling time", self._keithley_bounded("settle", self.settle)),
            ("Sense mode", self.sense_mode),
            ("", self.source_autorange),
            ("Current source range (AUTO or value with unit)", self.source_range_field),
            ("", self.measure_voltage_autorange),
            ("Measure V range (AUTO or value with unit)", self._keithley_bounded("measure_voltage_range", self.measure_voltage_range)),
            ("", self.measure_current_autorange),
            ("Measure I range (AUTO or value with unit)", self._keithley_bounded("measure_current_range", self.measure_current_range)),
        ):
            form.addRow(label, widget)
        self.keithley_form = form
        source_layout.addLayout(form)
        buttons = QHBoxLayout()
        self.configure_button = QPushButton("Configure current source while OUTPUT is OFF")
        self.configure_button.setObjectName("primaryButton")
        measure = QPushButton("Measure selected channel")
        arm = QPushButton("ARM (30 s)")
        on = QPushButton("OUTPUT ON")
        on.setObjectName("outputOnButton")
        off = QPushButton("Ramp to zero + OFF")
        off.setObjectName("outputOffButton")
        for button in (self.configure_button, measure, arm, on, off):
            buttons.addWidget(button)
        source_layout.addLayout(buttons)
        live_bar = QHBoxLayout()
        self.live_measurements = QCheckBox("Live readout for channels A and B")
        self.live_measurements.setToolTip(
            "Alternately measures channels A and B every second. This never enables an output."
        )
        live_bar.addWidget(self.live_measurements)
        live_bar.addStretch(1)
        self.last_update = QLabel("No measurements yet")
        self.last_update.setObjectName("muted")
        live_bar.addWidget(self.last_update)
        source_layout.addLayout(live_bar)
        self.readout = QLabel("Select Measure or enable Live readout")
        self.readout.setObjectName("readout")
        source_layout.addWidget(self.readout)
        source_layout.addStretch(1)
        self.control_tabs.addTab(self._scroll_widget(source_tab), "Control & ranges")
        layout.addWidget(self.control_tabs, 1)
        self.configure_button.clicked.connect(self.configure)
        measure.clicked.connect(self.request_measurement)
        arm.clicked.connect(self.arm_output)
        on.clicked.connect(self.request_output)
        off.clicked.connect(self.request_ramp_off)
        self.live_measurements.toggled.connect(self._toggle_live_measurements)
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
        self._install_keithley_help(
            configure=self.configure_button,
            measure=measure,
            arm=arm,
            output_on=on,
            output_off=off,
        )

    @staticmethod
    def _scroll_widget(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

    def _build_channel_card(self, channel: str) -> QFrame:
        card = QFrame()
        card.setObjectName("keithleyChannelCard")
        card.setProperty("selected", False)
        card_layout = QVBoxLayout(card)
        header = QHBoxLayout()
        name = QLabel(f"CHANNEL {channel}")
        name.setObjectName("cardTitle")
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
            tile_layout.setContentsMargins(10, 7, 10, 7)
            caption_label = QLabel(caption)
            caption_label.setObjectName("muted")
            value = QLabel(f"— {unit}")
            value.setObjectName("keithleyMeterValue")
            tile_layout.addWidget(caption_label)
            tile_layout.addWidget(value)
            meters.addWidget(tile, index // 2, index % 2)
            values[key] = value
        card_layout.addLayout(meters)
        footer = QHBoxLayout()
        compliance = QLabel("COMPLIANCE: clear")
        compliance.setObjectName("keithleyComplianceClear")
        select = QPushButton(f"Select CH {channel}")
        select.clicked.connect(lambda _checked=False, ch=channel: self.channel.setCurrentText(ch))
        measure = QPushButton(f"Measure CH {channel}")
        measure.clicked.connect(lambda _checked=False, ch=channel: self.request_measurement(ch))
        footer.addWidget(compliance)
        footer.addStretch(1)
        footer.addWidget(select)
        footer.addWidget(measure)
        card_layout.addLayout(footer)
        self.channel_cards[channel] = {
            "card": card,
            "led": led,
            "output": output,
            "compliance": compliance,
            "select": select,
            "measure": measure,
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
        configure: QPushButton,
        measure: QPushButton,
        arm: QPushButton,
        output_on: QPushButton,
        output_off: QPushButton,
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
            configure: ("Configure safely", "Validates source, compliance, ranges, NPLC and sensing, then programs the selected channel while OUTPUT is forced OFF."),
            measure: ("Measure selected channel", "Reads voltage and current from the selected SMU channel. Power and resistance shown in the cards are calculated from those I/V readings."),
            arm: ("ARM", "Creates a one-use 30-second permission window for OUTPUT ON. ARM itself does not energize the terminals."),
            output_on: ("OUTPUT ON", "Energizes the selected channel using its validated source and compliance settings. Requires prior configuration, approved safety profile, ARM and confirmation."),
            output_off: ("Ramp to zero and OFF", "Moves the programmed source toward zero using profile-limited steps, then disables the selected output. This is safer than an abrupt change for sensitive DUTs."),
            self.live_measurements: ("Live readout", "Alternately requests I/V readings from enabled channels every second. It never enables an output, but it does generate continuous instrument traffic."),
            self.device_led: ("Keithley connection state", "Grey means disconnected, green verified/output-safe, amber energized and red indicates compliance, fault or unknown state."),
            self.device_state: ("Device state", "Connection and safety state reported by the Keithley adapter. This is separate from the individual A/B output indicators."),
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
        self.control_tabs.setTabToolTip(
            0,
            "Source mode, programmed source value, compliance protection, integration time, wiring and source/measurement ranges.",
        )

    def _selected_channel_changed(self, selected: str) -> None:
        for channel, widgets in self.channel_cards.items():
            card = widgets["card"]
            card.setProperty("selected", channel == selected)
            card.style().unpolish(card)
            card.style().polish(card)

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
            for channel in ("A", "B"):
                widgets = self.channel_cards[channel]
                widgets["output"].setText("OUTPUT UNKNOWN")
                widgets["led"].setStyleSheet("color: #91a0b2;")
        elif normalized == "VERIFIED":
            # Connection qualification explicitly forces and verifies both outputs OFF.
            self._set_channel_output("A", False)
            self._set_channel_output("B", False)

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
        resistance = voltage / current if abs(current) > 1e-15 else math.inf
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
        self.last_update.setText(f"Last update: CH {channel}")

    def _set_channel_output(self, channel: str, enabled: bool) -> None:
        self._output_states[channel] = enabled
        widgets = self.channel_cards[channel]
        widgets["output"].setText("OUTPUT ON" if enabled else "OUTPUT OFF")
        widgets["led"].setStyleSheet(f"color: {'#ffcc66' if enabled else '#38d996'};")

    def request_measurement(self, channel: str | None = None) -> None:
        if self._measure_pending:
            return
        selected = channel or self.channel.currentText()
        self._pending_channels["measure"] = selected
        self._measure_pending = True
        self._controller.call("measure", selected)

    def request_ramp_off(self) -> None:
        channel = self.channel.currentText()
        self._pending_channels["ramp_to_zero"] = channel
        self._controller.call("ramp_to_zero", channel)

    def _toggle_live_measurements(self, enabled: bool) -> None:
        if enabled:
            self._request_live_measurement()
            self._live_timer.start()
        else:
            self._live_timer.stop()

    def _request_live_measurement(self) -> None:
        if self._measure_pending:
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
            self.configure_button.setText("Configure current source while OUTPUT is OFF")
        elif mode == "voltage":
            self.keithley_form.labelForField(self.level_field).setText("Source voltage")
            self.keithley_form.labelForField(self.compliance_field).setText("Current compliance (safety limit)")
            self.keithley_form.labelForField(self.source_range_field).setText("Voltage source range")
            self.configure_button.setText("Configure voltage source while OUTPUT is OFF")
        else:
            self.configure_button.setText("Configure measurement-only mode (OUTPUT OFF)")

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
        self._limit_fields[key] = field
        return field

    def _refresh_keithley_limits(self, *_args: object) -> None:
        for key, field in self._limit_fields.items():
            field.set_limits(*self._keithley_limit_values(key))

    def set_settings(self, settings: StationSettings) -> None:
        self._station_settings = settings
        self._refresh_keithley_limits()

    def configure(self) -> None:
        try:
            mode = self.mode.currentText()
            level_dimension = DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
            compliance_dimension = DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT
            request = KeithleySourceRequest(
                channel=self.channel.currentText(),  # type: ignore[arg-type]
                mode=mode,  # type: ignore[arg-type]
                level_si=0.0 if mode == "measure_only" else parse_quantity(self.level.text(), level_dimension).si_value,
                compliance_si=0.0 if mode == "measure_only" else parse_quantity(self.compliance.text(), compliance_dimension).si_value,
                nplc=float(self.nplc.text().replace(",", ".")),
                settle_time_s=parse_quantity(self.settle.text(), "time").si_value,
                sense_mode=self.sense_mode.currentText(),  # type: ignore[arg-type]
                source_autorange=self.source_autorange.isChecked(),
                source_range_si=self._manual_range(
                    self.source_range.text(), level_dimension, self.source_autorange.isChecked()
                ),
                measure_voltage_autorange=self.measure_voltage_autorange.isChecked(),
                measure_voltage_range_si=self._manual_range(
                    self.measure_voltage_range.text(), DIMENSION_VOLTAGE, self.measure_voltage_autorange.isChecked()
                ),
                measure_current_autorange=self.measure_current_autorange.isChecked(),
                measure_current_range_si=self._manual_range(
                    self.measure_current_range.text(), DIMENSION_CURRENT, self.measure_current_autorange.isChecked()
                ),
            )
        except Exception as exc:
            self.banner.show_message(f"Invalid Keithley settings: {exc}")
            return
        self._pending_channels["configure"] = self.channel.currentText()
        self._controller.call("configure", request)

    @staticmethod
    def _manual_range(text: str, dimension: str, autorange: bool) -> float | None:
        value = text.strip()
        if value.upper() == "AUTO":
            return None
        if autorange:
            raise ValueError("Disable autorange before entering a manual range.")
        return parse_quantity(value, dimension).si_value

    def arm_output(self) -> None:
        channel = self.channel.currentText()
        answer = QMessageBox.question(
            self,
            "ARM Keithley",
            f"Arm Keithley CH{channel} for 30 seconds? This does not enable the output yet.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._pending_channels["arm"] = channel
            self._controller.call("arm", channel)

    def request_output(self) -> None:
        channel = self.channel.currentText()
        answer = QMessageBox.warning(
            self,
            "OUTPUT ON Keithley",
            f"Enable the physical Keithley CH{channel} output? A valid ARM is required.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._pending_channels["set_output"] = channel
            self._controller.call("set_output", (channel, True))

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
            self._set_channel_output(channel, False)
            self.status.emit("Keithley configured while OUTPUT is OFF")
        elif operation == "arm":
            self.status.emit("Keithley armed for 30 seconds; OUTPUT ON requires separate confirmation")
        elif operation == "set_output":
            channel = self._pending_channels.pop("set_output", self.channel.currentText())
            self._set_channel_output(channel, True)
            self.status.emit(f"Keithley CH {channel} OUTPUT ON")
        elif operation == "ramp_to_zero":
            channel = self._pending_channels.pop("ramp_to_zero", self.channel.currentText())
            self._set_channel_output(channel, False)
            self.status.emit(f"Keithley CH {channel} ramped to zero; OUTPUT OFF")

    def _error(self, operation: str, error: str) -> None:
        if operation == "measure":
            self._measure_pending = False
        if operation in {"configure", "measure", "set_output", "ramp_to_zero", "arm"}:
            QMessageBox.warning(self, "Keithley", error)


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
        self._fetch_pending = False
        self._latest_trace: SpectrumTrace | None = None
        self._averaged_trace: SpectrumTrace | None = None
        self._reference_trace: SpectrumTrace | None = None
        self._averager = LinearPowerAverager()
        self._averaging_active = False
        self._averaging_destination: str | None = None
        self._resume_live_after_averaging = False
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.fetch_live)
        layout = QVBoxLayout(self)
        self.banner = NotificationBanner()
        layout.addWidget(self.banner)
        title = QLabel("Anritsu MS2830A — Spectrum / Live")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
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
        setup_title = QLabel("Acquisition setup")
        setup_title.setObjectName("sectionTitle")
        left_layout.addWidget(setup_title)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(7)
        self.start = _line("1 MHz")
        self.stop = _line("10 MHz")
        self.reference = _line("0 dBm")
        self.points = QComboBox()
        self._refresh_point_choices(1001)
        self.refresh = QSpinBox()
        self.refresh.setRange(100, 5000)
        self.refresh.setValue(500)
        self.refresh.setSuffix(" ms")
        for label, widget in (
            ("Start", self._anritsu_bounded("frequency", self.start)),
            ("Stop", self._anritsu_bounded("frequency", self.stop)),
            ("Reference level", self._anritsu_bounded("reference_level", self.reference)),
            ("Points", self._anritsu_bounded("sweep_points", self.points)),
            ("Live refresh interval", self.refresh),
        ):
            form.addRow(label, widget)
        left_layout.addLayout(form)
        controls = QGridLayout()
        controls.setSpacing(6)
        self.read_configuration = QPushButton("Read from instrument")
        configure = QPushButton("Apply configuration")
        self.single = QPushButton("Read current spectrum")
        self.live = QPushButton("Start Live")
        abort = QPushButton("Abort")
        configure.setObjectName("primaryButton")
        abort.setObjectName("warningButton")
        for button in (self.read_configuration, configure, self.single, self.live, abort):
            button.setProperty("compact", True)
        controls.addWidget(self.read_configuration, 0, 0, 1, 2)
        controls.addWidget(configure, 1, 0)
        controls.addWidget(self.single, 1, 1)
        controls.addWidget(self.live, 2, 0)
        controls.addWidget(abort, 2, 1)
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
        self.capture_reference = QPushButton("Acquire averaged reference")
        self.clear_reference = QPushButton("Clear reference")
        self.capture_reference.setProperty("compact", True)
        self.clear_reference.setProperty("compact", True)
        self.clear_reference.setEnabled(False)
        self.reference_operation = QComboBox()
        self.reference_operation.addItem("No processing", "none")
        self.reference_operation.addItem("Signal − reference [dB]", "difference_db")
        self.reference_operation.addItem("Signal ÷ reference [linear ratio]", "ratio_linear")
        self.reference_operation.addItem("Signal + reference [linear power]", "add_power")
        self.reference_operation.addItem("Signal − reference [linear power]", "subtract_power")
        self.reference_operation.addItem("Signal × reference [linear mW²]", "multiply_linear")
        processing_layout.addWidget(self.capture_reference, 4, 0)
        processing_layout.addWidget(self.clear_reference, 4, 1)
        processing_layout.addWidget(QLabel("Reference operation"), 5, 0)
        processing_layout.addWidget(self.reference_operation, 5, 1)
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
        processing_layout.addLayout(trace_toggles, 6, 0, 1, 2)
        left_layout.addWidget(processing)
        left_layout.addStretch(1)
        self.spectrum_plot = SpectrumPlotWidget(legend=True)
        self.spectrum_plot.set_title("Current spectrum")
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
        layout.addWidget(self.workspace_splitter, 1)
        self.read_configuration.clicked.connect(self.read_configuration_from_instrument)
        configure.clicked.connect(self.configure)
        self.single.clicked.connect(
            lambda: self._controller.call("fetch_current_trace", "TRAC1")
        )
        self.live.clicked.connect(self.toggle_live)
        abort.clicked.connect(lambda: self._controller.call("emergency_off"))
        self.acquire_average.clicked.connect(self.start_averaging)
        self.cancel_average.clicked.connect(self.cancel_averaging)
        self.capture_reference.clicked.connect(self.start_reference_averaging)
        self.clear_reference.clicked.connect(self.remove_reference)
        self.reference_operation.currentIndexChanged.connect(self._refresh_spectrum_display)
        for checkbox in (self.show_raw, self.show_average, self.show_reference, self.show_processed):
            checkbox.toggled.connect(self._refresh_spectrum_display)
        controller.result.connect(self._result)
        controller.error.connect(self._error)
        help_items = {
            self.read_configuration: "Read Start, Stop, Reference level, and Points from the connected analyser. This sends query commands only and never changes the instrument or approved safety limits.",
            self.single: "Read the currently displayed TRAC1 spectrum using SCPI queries only. This does not configure or trigger the analyser and does not require an approved safety profile.",
            self.average_count: "Number of complete spectra to average. 200 is common in the Thatec workflow. Averaging is performed in linear mW, not directly in dBm.",
            self.acquire_average: "Passively read N traces at the Live refresh interval and average power in linear mW. No analyser setting or trigger mode is changed.",
            self.cancel_average: "Stop temporal averaging. Already collected temporary frames are discarded; completed raw/reference data are unchanged.",
            self.capture_reference: "Passively acquire and average N traces, then store that completed average as the in-memory reference spectrum.",
            self.clear_reference: "Remove the in-memory reference and all derived display results. It does not delete raw measurements from HDF5.",
            self.reference_operation: "Choose point-wise reference mathematics. Difference in dB equals a power ratio expressed logarithmically; linear operations first convert dBm to mW.",
            self.show_raw: "Show the latest untouched trace returned by Anritsu.",
            self.show_average: "Show the application-side linear-power average.",
            self.show_reference: "Overlay the captured reference spectrum.",
            self.show_processed: "Show the selected reference operation result. Non-dBm results use their own Y-axis unit and hide incompatible overlays.",
        }
        for widget, description in help_items.items():
            widget.setToolTip(description)
            widget.setToolTipDuration(25_000)

    def _anritsu_limit_values(self, key: str) -> tuple[object, object]:
        safety = self._station_settings.anritsu.safety
        value = getattr(safety, key)
        return value.min, value.max

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
        self._refresh_point_choices()
        for field in self._limit_fields.values():
            field.set_limits(*self._anritsu_limit_values(str(field.property("limitKey"))))

    def set_capabilities(self, capabilities: object) -> None:
        supports = getattr(capabilities, "supports", lambda _feature: False)
        self.single.setEnabled(bool(supports("spectrum_trace")))

    def configure(self) -> None:
        try:
            config = SpectrumConfig(
                start_hz=parse_quantity(self.start.text(), DIMENSION_FREQUENCY).si_value,
                stop_hz=parse_quantity(self.stop.text(), DIMENSION_FREQUENCY).si_value,
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

    def toggle_live(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self._controller.call("stop_live")
            self.live.setText("Start Live")
            self.info.setText("Live stopped.")
            return
        self._timer.setInterval(self.refresh.value())
        self._controller.call("start_live")

    def fetch_live(self) -> None:
        if not self._fetch_pending:
            self._fetch_pending = True
            self._controller.call("fetch_current_trace", "TRAC1")

    def start_averaging(self) -> None:
        self._start_temporal_averaging("spectrum")

    def start_reference_averaging(self) -> None:
        self._start_temporal_averaging("reference")

    def _start_temporal_averaging(self, destination: str) -> None:
        if self._averaging_active:
            return
        target = self.average_count.value()
        self._resume_live_after_averaging = self._timer.isActive()
        if self._resume_live_after_averaging:
            self._timer.stop()
        self._averager.reset()
        self._averaging_active = True
        self._averaging_destination = destination
        self.average_progress.setRange(0, target)
        self.average_progress.setValue(0)
        self.average_progress.setFormat(f"0 / {target}")
        self.average_count.setEnabled(False)
        self.acquire_average.setEnabled(False)
        self.capture_reference.setEnabled(False)
        self.cancel_average.setEnabled(True)
        self.live.setEnabled(False)
        label = "reference" if destination == "reference" else "spectrum"
        self.info.setText(f"Averaging {label}: 0 / {target} temporal frames...")
        self.status.emit(
            f"Anritsu passive temporal averaging started: {label}, 0 / {target}"
        )
        # Reuse an already pending Live frame instead of queuing a duplicate
        # VISA query against the same session.
        if not self._fetch_pending:
            self._fetch_pending = True
            self._controller.call("fetch_current_trace", "TRAC1")

    def cancel_averaging(self) -> None:
        self._finish_temporal_averaging(resume_live=True)
        self.info.setText("Averaging cancelled; completed spectra were not modified.")
        self.status.emit("Anritsu temporal averaging cancelled")

    def _finish_temporal_averaging(self, *, resume_live: bool) -> None:
        should_resume_live = self._resume_live_after_averaging and resume_live
        self._averaging_active = False
        self._averaging_destination = None
        self._resume_live_after_averaging = False
        self._averager.reset()
        self.acquire_average.setEnabled(True)
        self.capture_reference.setEnabled(True)
        self.cancel_average.setEnabled(False)
        self.average_count.setEnabled(True)
        self.live.setEnabled(True)
        if should_resume_live:
            self._timer.setInterval(self.refresh.value())
            self._timer.start()
            self.live.setText("Stop Live")

    def _request_next_average_frame(self) -> None:
        if not self._averaging_active or self._fetch_pending:
            return
        self._fetch_pending = True
        self._controller.call("fetch_current_trace", "TRAC1")

    def capture_current_reference(self) -> None:
        """Use the latest single frame as a reference for API compatibility."""

        if self._latest_trace is None:
            QMessageBox.information(self, "Reference spectrum", "Acquire a spectrum before capturing a reference.")
            return
        self._reference_trace = self._latest_trace
        self.clear_reference.setEnabled(True)
        self.show_reference.setChecked(True)
        self._refresh_spectrum_display()
        self.status.emit("Anritsu reference spectrum captured in memory")

    def remove_reference(self) -> None:
        self._reference_trace = None
        self.spectrum_plot.clear_trace("Reference")
        self.spectrum_plot.clear_trace("Processed")
        self.clear_reference.setEnabled(False)
        self.show_reference.setChecked(False)
        self.show_processed.setChecked(False)
        self.reference_operation.setCurrentIndex(0)
        self._refresh_spectrum_display()
        self.status.emit("Anritsu reference spectrum removed")

    def _result(self, operation: str, result: object) -> None:
        if operation == "read_configuration" and isinstance(result, AnritsuConfigurationSnapshot):
            self.start.setText(format_quantity_auto(result.start_hz, DIMENSION_FREQUENCY))
            self.stop.setText(format_quantity_auto(result.stop_hz, DIMENSION_FREQUENCY))
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
        elif operation == "configure" and isinstance(result, AnritsuConfigurationSnapshot):
            self._result("read_configuration", result)
            self.status.emit("Anritsu configured and verified by SCPI readback")
        elif operation == "start_live" and isinstance(result, AnritsuConfigurationSnapshot):
            self._result("read_configuration", result)
            self._timer.start()
            self.live.setText("Stop Live")
            self.status.emit("Anritsu Live started")
        elif operation in {"fetch_trace", "fetch_current_trace", "single_sweep"} and isinstance(result, SpectrumTrace):
            self._fetch_pending = False
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
                        self._reference_trace = averaged_trace
                        self.clear_reference.setEnabled(True)
                        self.show_reference.setChecked(True)
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
        self._refresh_spectrum_display()
        self.info.setText(
            f"{len(trace.powers_dbm)} points • {trace.acquired_at_utc.isoformat()} • "
            f"max {max(trace.powers_dbm):.4g} dBm"
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
                [frequency / 1e6 for frequency in trace.frequencies_hz],
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
        self.spectrum_plot.set_labels(y="Amplitude", y_unit=active_unit)

    def _error(self, operation: str, error: str) -> None:
        if operation in {"fetch_trace", "fetch_current_trace", "single_sweep"}:
            self._fetch_pending = False
            if self._averaging_active:
                self._finish_temporal_averaging(resume_live=False)
                self.info.setText(f"Averaging stopped: {error}")
        if operation in {
            "read_configuration", "configure", "start_live", "fetch_trace", "fetch_current_trace",
            "single_sweep", "emergency_off",
        }:
            self._timer.stop()
            self.live.setText("Start Live")
            QMessageBox.warning(self, "Anritsu", error)


class RecipePage(QWidget):
    status = Signal(str)
    run_requested = Signal(object)

    def __init__(self, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._plan = None
        layout = QVBoxLayout(self)
        title = QLabel("Measurement recipes")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        path_line = QHBoxLayout()
        self.path = _line("recipes/example_nested_sweep.yml", 42)
        load_button = QPushButton("Load into editor")
        save_button = QPushButton("Save YAML")
        compile_button = QPushButton("Compile")
        self.run_button = QPushButton("Run plan")
        self.run_button.setEnabled(False)
        path_line.addWidget(self.path, 1)
        path_line.addWidget(load_button)
        path_line.addWidget(save_button)
        path_line.addWidget(compile_button)
        path_line.addWidget(self.run_button)
        layout.addLayout(path_line)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Declarative YAML recipe — no Python code and no raw SCPI.")
        self.editor.setMinimumWidth(320)
        splitter.addWidget(self.editor)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Node", "Type / details"])
        splitter.addWidget(self.tree)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.summary = QLabel("The recipe has not been compiled.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        load_button.clicked.connect(self.load_editor)
        save_button.clicked.connect(self.save_recipe)
        compile_button.clicked.connect(self.compile_recipe)
        self.run_button.clicked.connect(self.request_run)
        self.editor.textChanged.connect(self._source_changed)
        self.load_editor(show_error=False)

    def set_settings(self, settings: StationSettings) -> None:
        self._settings = settings
        self._plan = None
        self.run_button.setEnabled(False)

    def _source_changed(self) -> None:
        self._plan = None
        self.run_button.setEnabled(False)
        self.summary.setText("YAML changed; compile it again before running.")

    def load_editor(self, *, show_error: bool = True) -> None:
        try:
            source = Path(self.path.text()).read_text(encoding="utf-8")
        except Exception as exc:
            if show_error:
                QMessageBox.warning(self, "Recipe", f"Cannot load YAML: {exc}")
            else:
                self.summary.setText(f"Example not loaded: {exc}")
            return
        self.editor.setPlainText(source)
        self.status.emit("Recipe loaded into the editor")

    def save_recipe(self) -> None:
        source = self.editor.toPlainText()
        try:
            parse_recipe_text(source, origin=self.path.text())
            path = Path(self.path.text())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        except Exception as exc:
            QMessageBox.warning(self, "Recipe", f"YAML was not saved: {exc}")
            return
        self.status.emit(f"Recipe saved: {self.path.text()}")

    def compile_recipe(self) -> None:
        try:
            recipe = parse_recipe_text(self.editor.toPlainText(), origin=self.path.text())
            plan = RecipeCompiler(self._settings).compile(recipe)
        except Exception as exc:
            QMessageBox.warning(self, "Recipe", str(exc))
            return
        self.tree.clear()
        self._plan = plan
        self.run_button.setEnabled(True)
        for action in plan.actions:
            item = QTreeWidgetItem([action.node_id, action.kind])
            item.setToolTip(1, str(action.setpoints_si))
            self.tree.addTopLevelItem(item)
        self.summary.setText(
            f"Plan: {len(plan.actions)} actions • {plan.total_points} spectra • hash {plan.sha256}\n"
            "Compilation sends no instrument commands. Execution requires an approved profile and Run Engine."
        )
        self.status.emit("Recipe compiled")

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
        layout.addWidget(title)
        layout.addWidget(self.state)
        layout.addWidget(self.progress)
        layout.addLayout(controls)
        layout.addWidget(self.events, 1)
        pause.clicked.connect(self.pause_requested)
        resume.clicked.connect(self.resume_requested)
        stop.clicked.connect(self.stop_requested)

    def run_started(self, actions: int) -> None:
        self.state.setText("RUNNING")
        self.progress.setRange(0, max(actions, 1))
        self.progress.setValue(0)
        self.events.clear()

    def append_event(self, name: str, data: dict[str, object]) -> None:
        if name == "action_finished":
            self.progress.setValue(min(self.progress.value() + 1, self.progress.maximum()))
        if name == "pause_pending":
            self.state.setText("PAUSED")
        elif name == "run_fault":
            self.state.setText("FAULT")
        self.events.appendPlainText(f"{name}: {data}")

    def complete(self, result: object) -> None:
        run_result = result["result"]
        self.state.setText(f"{run_result.state.value.upper()} • {run_result.stored_points} points")
        self.events.appendPlainText(f"File: {result['path']}")

    def failed(self, error: str) -> None:
        self.state.setText("FAULT")
        self.events.appendPlainText(error)


class ResultsPage(QWidget):
    """Browse immutable run files without opening an instrument session."""

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
        refresh = QPushButton("Refresh file list")
        layout.addWidget(refresh)

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
        self.runs.currentItemChanged.connect(self._run_selected)
        self.points.currentItemChanged.connect(self._point_selected)
        self.refresh()

    def refresh(self) -> None:
        self.location.setText(f"Directory: {self._output_dir.resolve()}")
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
            return
        path = Path(str(item.data(0, Qt.ItemDataRole.UserRole)))
        self._selected_path = path
        try:
            detail = Hdf5RunReader.detail(path)
            points = Hdf5RunReader.points(path)
        except Exception as exc:
            self.metadata.setPlainText(f"Cannot read result:\n{exc}")
            self.recipe_snapshot.clear()
            self.settings_snapshot.clear()
            return
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
            [frequency / 1e6 for frequency in trace.frequencies_hz],
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

    def __init__(self, settings_path: str | Path = ".config/settings.yml", *, simulation: bool = False) -> None:
        super().__init__()
        self._repository = SettingsRepository(settings_path)
        self._simulation = simulation
        persisted = self._repository.load().settings
        self._settings = simulated_station_settings(persisted) if simulation else persisted
        suffix = " — SIMULATION" if simulation else ""
        self.setWindowTitle("Lab Control — Rigol · Keithley · Anritsu" + suffix)
        self.resize(1360, 880)
        self._controllers = {name: DeviceController(self._make_adapter(name), self) for name in ("rigol", "keithley", "anritsu")}
        self._device_states = {"rigol": "disconnected", "keithley": "disconnected", "anritsu": "disconnected"}
        self._run_controller = RunController(self)
        self._build()
        self._apply_accessibility()
        self._connect_controllers()
        self._restore_workspace()

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
        self.run_monitor = RunMonitorPage()
        self.results_page = ResultsPage(str(self._settings.storage.get("output_directory", "./measurements")))
        self.settings_page = SettingsPage(self._repository, read_only=self._simulation)
        for field in self.rigol_page.findChildren(LimitField):
            field.edit_button.setEnabled(not self._simulation)
            field.edit_requested.connect(lambda field=field: self._edit_device_limit("rigol", field))
        for field in self.keithley_page.findChildren(LimitField):
            editable = not self._simulation and str(field.property("limitKey")) != "nplc"
            field.edit_button.setEnabled(editable)
            if editable:
                field.edit_requested.connect(lambda field=field: self._edit_device_limit("keithley", field))
            else:
                field.edit_button.setToolTip("NPLC range is fixed by the instrument and is not a laboratory safety limit.")
        for field in self.anritsu_page.findChildren(LimitField):
            field.edit_button.setEnabled(not self._simulation)
            field.edit_requested.connect(lambda field=field: self._edit_device_limit("anritsu", field))
        for widget, name in (
            (self.dashboard, "Dashboard"),
            (self.rigol_page, "Rigol"),
            (self.keithley_page, "Keithley"),
            (self.anritsu_page, "Anritsu"),
            (self.recipe_page, "Recipes"),
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
        labels = ("Dashboard", "Rigol", "Keithley", "Anritsu", "Recipes", "Execution", "Results", "Settings")
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
            controller.result.connect(lambda operation, result, current=card: self._device_result(current, operation, result))
            controller.error.connect(lambda operation, error, device=name: self._device_error(device, operation, error))
            controller.traffic.connect(
                lambda message, device=name: self._log(f"{device.upper()} VISA {message}")
            )
            if name == "rigol":
                controller.capabilities_changed.connect(self.rigol_page.set_capabilities)
            elif name == "anritsu":
                controller.capabilities_changed.connect(self.anritsu_page.set_capabilities)

    def _device_result(self, card: DeviceCard, operation: str, result: object) -> None:
        if operation == "connect":
            card.update_identity(result)
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
            card.identity.setText(
                f"TEST PASS: {result.get('vendor', '')} {result.get('model', '')} • "
                f"SN {result.get('serial', '—')} • FW {result.get('firmware', '—')}\n"
                f"Protocols/features: {features}"
            )
            self._log(f"Communication test passed: {result.get('idn', '')}; {features}")

    def _device_error(self, device: str, operation: str, error: str) -> None:
        if operation == "replace_adapter":
            self.dashboard.cards[device].set_reconfiguring(False)
        elif operation == "test_communication":
            card = self.dashboard.cards[device]
            card.set_testing(False)
            card.update_state("fault")
            card.identity.setText(f"TEST FAILED: {error}")
        self._log(f"{device}/{operation}: {error}")

    def _set_device_state(self, device: str, state: str) -> None:
        self._device_states[device] = state
        label = self.toolbar_device_status.get(device)
        if label is not None:
            label.setText(f"● {device.title()}: {state.replace('_', ' ').upper()}")
            label.setProperty("deviceState", state)
            label.style().unpolish(label)
            label.style().polish(label)

    def _start_run(self, plan: object) -> None:
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
            self._run_controller.start(
                self._settings,
                self._repository.path,
                plan,  # type: ignore[arg-type]
                simulation=self._simulation,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Run not started", str(exc))
            return
        self.run_monitor.run_started(plan.actions)  # type: ignore[union-attr]
        for index in (1, 2, 3, 4, 7):
            self.tabs.setTabEnabled(index, False)
        self.tabs.setCurrentWidget(self.run_monitor)
        self._log("Run Engine started")

    def _run_event(self, name: str, data: object) -> None:
        payload = data if isinstance(data, dict) else {"data": data}
        self.run_monitor.append_event(name, payload)

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
            if self._run_controller.running:
                self._run_controller.request_emergency_stop(self._settings, simulation=self._simulation)
            for controller in self._controllers.values():
                controller.call("emergency_off")
            self._log("E-STOP sent to all instruments")

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

    def _log(self, message: str) -> None:
        self.log.appendPlainText(message)
        self.statusBar().showMessage(message, 8_000)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_workspace()
        self.anritsu_page._timer.stop()
        self._run_controller.close()
        for controller in self._controllers.values():
            controller.close()
        event.accept()
