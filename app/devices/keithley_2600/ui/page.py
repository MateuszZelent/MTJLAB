"""Manual-control and recipe-editing UI for the Keithley 2600 module."""

# ruff: noqa: F401
from __future__ import annotations

import math
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

import pyqtgraph as pg
from PySide6.QtCore import QMimeData, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QCloseEvent, QDrag, QIcon, QKeySequence, QPainter, QPalette, QPixmap, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QFrame, QGridLayout,
    QBoxLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QPlainTextEdit,
    QProgressBar, QPushButton, QSplitter, QSpinBox,
    QSizePolicy, QStyledItemDelegate, QStyle,
    QTableWidgetItem, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel, CardWidget, CheckBox, ComboBox, PrimaryPushButton, PushButton,
    FluentIcon, ScrollArea, SpinBox, StrongBodyLabel, TableWidget, TitleLabel,
    TransparentToolButton,
)

from app.devices.keithley_2600 import (
    KeithleyChannelConfigurationReadback, KeithleyConfigurationReadback,
    KeithleyRampRequest, KeithleySourceRequest, build_keithley_ramp_levels,
)
from app.domain.errors import ConfigurationError
from app.domain.quantities import (
    DIMENSION_CURRENT, DIMENSION_RESISTANCE, DIMENSION_TIME, DIMENSION_VOLTAGE,
    format_quantity_auto, parse_quantity,
)
from app.recipes import RecipeNode, replace_recipe_node
from app.safety.quick_controls import quick_control_safety_bounds
from app.recipes.parameter_registry import SWEEP_DIMENSIONS
from app.safety.keithley import validate_keithley_source
from app.settings.models import StationSettings
from app.ui.common import line_edit as _line
from app.ui.dialogs import StationDialog, StationMessageBox as QMessageBox
from app.ui.widgets import LimitField, NotificationBanner, SpectrumPlotWidget
from app.ui.recipes.fluent_dialog import FluentRecipeDialog
from app.ui.workers import DeviceController


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


class KeithleyConfigurationPanel(CardWidget):
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
        title = StrongBodyLabel("Source and measurement configuration")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.form = QFormLayout()
        self.form.setVerticalSpacing(4)
        self.form.setHorizontalSpacing(8)
        self.channel = ComboBox()
        self.channel.addItems(["A", "B"])
        self.channel.setCurrentText("B")
        self.mode = ComboBox()
        self.mode.addItems(["current", "voltage", "measure_only"])
        self.level = _line("1 mA")
        self.compliance = _line("67 mV")
        self.nplc = _line("1")
        self.settle = _line("100 ms")
        self.sense_mode = ComboBox()
        self.sense_mode.addItems(["2wire", "4wire"])
        self.source_autorange = CheckBox("Source autorange", self)
        self.source_autorange.setChecked(True)
        self.source_range = _line("AUTO")
        self.measure_voltage_autorange = CheckBox("Measure V autorange", self)
        self.measure_voltage_autorange.setChecked(True)
        self.measure_voltage_range = _line("AUTO")
        self.measure_current_autorange = CheckBox("Measure I autorange", self)
        self.measure_current_autorange.setChecked(True)
        self.measure_current_range = _line("AUTO")
        self.level_field = self._bounded("level", self.level)
        self.compliance_field = self._bounded("compliance", self.compliance)
        self.nplc_field = self._bounded("nplc", self.nplc)
        self.source_range_field = self._bounded("source_range", self.source_range)
        for label, widget in (
            ("Channel", self.channel),
            ("Source mode", self.mode),
            ("Source current", self.level_field),
            ("Voltage limit (compliance)", self.compliance_field),
            ("NPLC", self.nplc_field),
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
            note = BodyLabel(
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
        if key in {"source_range", "measure_voltage_range", "measure_current_range"}:
            field.edit_button.hide()
            field.setToolTip(
                "Disable autorange and enter the requested instrument range directly. "
                "The displayed maximum is the immutable 2602A hardware ceiling."
            )
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
            return limits.point_settle_time.min, limits.point_settle_time.max
        if mode == "measure_only" and key in {"level", "compliance", "source_range"}:
            return "N/A", "N/A"
        if key == "level":
            bound = quick_control_safety_bounds(self._settings)[
                f"keithley.{self.channel.currentText()}.{mode}"
            ]
            return bound.minimum_text, bound.maximum_text
        if key == "compliance":
            value = (
                limits.voltage_compliance
                if mode == "current"
                else limits.current_compliance
            )
            return value.min, value.max
        if key == "source_range":
            return "> 0", "3 A" if mode == "current" else "40 V"
        if key == "measure_voltage_range":
            return "> 0", "40 V"
        if key == "measure_current_range":
            return "> 0", "3 A"
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
                "Voltage limit (compliance)"
            )
            self.form.labelForField(self.source_range_field).setText("Current source range")
        elif mode == "voltage":
            self.form.labelForField(self.level_field).setText("Source voltage")
            self.form.labelForField(self.compliance_field).setText(
                "Current limit (compliance)"
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


class KeithleyNodeEditorDialog(FluentRecipeDialog):
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
        self.setProperty("stationSurface", "page")
        self._settings = settings
        self._loaded_segments_by_parameter: dict[
            str, list[dict[str, object]]
        ] = {}
        self.setWindowTitle("Keithley 2600 — configure sweep node")
        self.resize(1120, 720)
        layout = QVBoxLayout(self)
        heading = BodyLabel("Keithley 2600")
        heading.setObjectName("recipePageTitle")
        layout.addWidget(heading)
        self.configuration_panel = KeithleyConfigurationPanel(
            settings, self, plan_mode=True
        )
        workspace = QSplitter(Qt.Orientation.Horizontal)
        configuration_scroll = ScrollArea()
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
        parameter_card = CardWidget(self)
        parameter_card.setObjectName("recipeEditorParameters")
        parameter_layout = QGridLayout(parameter_card)
        parameter_layout.setContentsMargins(10, 10, 10, 10)
        selection_title = BodyLabel("Select what this node controls")
        selection_title.setObjectName("sectionTitle")
        parameter_layout.addWidget(selection_title, 0, 0, 1, 2)
        parameter_layout.addWidget(BodyLabel("Parameter"), 1, 0)
        parameter_layout.addWidget(BodyLabel("Action"), 1, 1)
        self.parameter_selectors: dict[str, ComboBox] = {}
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
            parameter_layout.addWidget(BodyLabel(label), row, 0)
            selector = ComboBox(self)
            selector.setProperty("parameterId", parameter_id)
            selector.addItem("Unchanged", userData="unchanged")
            selector.addItem("Set", userData="set")
            if sweepable:
                selector.addItem("Sweep — ROI required", userData="sweep")
            parameter_layout.addWidget(selector, row, 1)
            self.parameter_selectors[parameter_id] = selector
        output_row = 2 + len(definitions)
        parameter_layout.addWidget(BodyLabel("Output state"), output_row, 0)
        self.output_policy = ComboBox(self)
        self.output_policy.addItem("Unchanged", userData="unchanged")
        self.output_policy.addItem("OUTPUT ON at start", userData="on")
        self.output_policy.addItem("OUTPUT OFF", userData="off")
        parameter_layout.addWidget(self.output_policy, output_row, 1)
        self.open_roi_button = PrimaryPushButton("Go to ROI…", self)
        self.open_roi_button.setEnabled(False)
        self.open_roi_button.setToolTip(
            "Open the interval and point editor for the single parameter marked Sweep."
        )
        parameter_layout.addWidget(self.open_roi_button, output_row + 1, 0, 1, 2)
        self.roi_status = BodyLabel(
            "Mark one parameter as Sweep to define an ROI."
        )
        self.roi_status.setObjectName("muted")
        self.roi_status.setWordWrap(True)
        parameter_layout.addWidget(self.roi_status, output_row + 2, 0, 1, 2)
        parameter_note = BodyLabel(
            "The complete visible Keithley snapshot is stored and applied with OUTPUT OFF. "
            "Set marks a value as an explicit plan row; Sweep turns one value into the ROI "
            "axis. Unchanged still uses the visible snapshot value. OUTPUT is only a plan "
            "declaration; this window never energizes the instrument."
        )
        parameter_note.setObjectName("muted")
        parameter_note.setWordWrap(True)
        parameter_layout.addWidget(parameter_note, output_row + 3, 0, 1, 2)
        parameter_scroll = ScrollArea()
        parameter_scroll.setWidgetResizable(True)
        parameter_scroll.setFrameShape(QFrame.Shape.NoFrame)
        parameter_scroll.setWidget(parameter_card)
        workspace.addWidget(parameter_scroll)
        workspace.setStretchFactor(0, 3)
        workspace.setStretchFactor(1, 2)
        workspace.setSizes([660, 430])
        layout.addWidget(workspace, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = PushButton("Cancel", self)
        self.validate_button = PushButton("Validate", self)
        self.apply_button = PrimaryPushButton("Apply selected actions", self)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.validate_button)
        footer.addWidget(self.apply_button)
        layout.addLayout(footer)
        self.apply_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self.validate_button.clicked.connect(self._validate)
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
                "Select exactly one Sweep axis in this node."
            )
            return
        if not sweep_ids:
            self.roi_status.setText(
                "Mark one parameter as Sweep to define an ROI."
            )
            return
        parameter_id = sweep_ids[0]
        segments = self._loaded_segments_by_parameter.get(parameter_id)
        self.roi_status.setText(
            f"ROI saved: {len(segments)} segment(s)."
            if segments
            else "No ROI has been defined yet."
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
        # The generic ROI dialog is still being migrated out of RecipePage.
        # This delayed import avoids a module-level cycle during compatibility.
        from app.ui.recipes.sweep_editor import SweepGeneratorDialog

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
                "Select Set or Sweep for at least one parameter, or choose an OUTPUT action.",
            )
            return
        if self._validate():
            super().accept()


class _KeithleyReadbackDialog(StationDialog):
    """Modal, read-only comparison of both Keithley channel configurations."""

    assign_requested = Signal(str, str)

    def __init__(
        self,
        readback: KeithleyConfigurationReadback,
        configured: dict[str, KeithleyConfigurationSnapshot],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("keithleyReadbackDialog")
        self.setWindowTitle("Keithley 2600 — settings read from device")
        self.setModal(True)
        self.setMinimumSize(760, 500)
        self.resize(860, 540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = StrongBodyLabel("Hardware configuration snapshot", self)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        note = BodyLabel(
            "Read-only TSP queries were used for both channels. No setting or OUTPUT "
            "state was changed. Settling time belongs to the application and is not "
            "stored in the Keithley.",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        output_on = any(channel.output_enabled for channel in readback.channels)
        output_status = StrongBodyLabel(
            "Warning: at least one hardware OUTPUT is ON."
            if output_on
            else "Hardware readback: OUTPUT A OFF · OUTPUT B OFF",
            self,
        )
        output_status.setObjectName(
            "keithleyReadbackOutputOn" if output_on else "sectionTitle"
        )
        layout.addWidget(output_status)

        self.table = TableWidget(self)
        self.table.setObjectName("keithleyReadbackTable")
        self.table.setAccessibleName(
            "Keithley hardware settings read from channels A and B"
        )
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            [
                "Parameter",
                "Device A",
                "Current form A",
                "",
                "Device B",
                "Current form B",
                "",
            ]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        values = {
            channel.channel: self._channel_values(channel)
            for channel in readback.channels
        }
        expected = {
            channel: self._snapshot_values(snapshot)
            for channel, snapshot in configured.items()
        }
        rows = (
            "OUTPUT state",
            "OUTPUT OFF mode",
            "Source mode",
            "Source level",
            "Compliance limit",
            "Source autorange",
            "Active source range",
            "Sense mode",
            "NPLC",
            "Measure V autorange",
            "Active measure V range",
            "Measure I autorange",
            "Active measure I range",
        )
        self.table.setRowCount(len(rows))
        self._status_cells: dict[tuple[str, str], QTableWidgetItem] = {}
        for row, parameter in enumerate(rows):
            self.table.setItem(row, 0, QTableWidgetItem(parameter))
            for channel, value_column, status_column, button_column in (
                ("A", 1, 2, 3),
                ("B", 4, 5, 6),
            ):
                value = values[channel][parameter]
                matches = self._values_match(
                    parameter, values[channel], expected[channel]
                )
                value_item = QTableWidgetItem(value)
                configured_value = expected[channel].get(parameter)
                status_item = QTableWidgetItem(
                    "MATCH" if matches else (configured_value or "Not controlled by form")
                )
                colour = QColor("#168a45" if matches else "#c43b3b")
                value_item.setForeground(colour)
                status_item.setForeground(colour)
                self.table.setItem(row, value_column, value_item)
                self.table.setItem(row, status_column, status_item)
                self._status_cells[(channel, parameter)] = status_item
                if parameter not in {"OUTPUT state", "OUTPUT OFF mode"}:
                    assign = PushButton("Assign", self.table)
                    assign.setAccessibleName(
                        f"Assign {parameter} from Keithley channel {channel}"
                    )
                    assign.clicked.connect(
                        lambda _checked=False, ch=channel, key=parameter: (
                            self._assign(ch, key)
                        )
                    )
                    self.table.setCellWidget(row, button_column, assign)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.table.resizeRowsToContents()
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        assign_all = PrimaryPushButton("Assign all", self)
        assign_all.setToolTip(
            "Copy all configurable A/B values to the form. Safety limits and OUTPUT "
            "states are not changed."
        )
        assign_all.clicked.connect(
            self._assign_all
        )
        footer.addWidget(assign_all)
        footer.addStretch(1)
        close = PushButton("Close", self)
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)

    def _assign(self, channel: str, parameter: str) -> None:
        self.assign_requested.emit(channel, parameter)
        if not getattr(self.parent(), "_last_assignment_succeeded", False):
            return
        source_group = {
            "Source mode",
            "Source level",
            "Compliance limit",
            "Source autorange",
            "Active source range",
        }
        parameters = source_group if parameter in source_group else {parameter}
        for assigned_parameter in parameters:
            item = self._status_cells[(channel, assigned_parameter)]
            item.setText("MATCH")
            item.setForeground(QColor("#168a45"))

    def _assign_all(self) -> None:
        self.assign_requested.emit("ALL", "ALL")
        if not getattr(self.parent(), "_last_assignment_succeeded", False):
            return
        for (channel, parameter), item in self._status_cells.items():
            if parameter in {"OUTPUT state", "OUTPUT OFF mode"}:
                continue
            item.setText("MATCH")
            item.setForeground(QColor("#168a45"))

    @classmethod
    def _snapshot_values(cls, snapshot: KeithleyConfigurationSnapshot) -> dict[str, str]:
        source_dimension = (
            DIMENSION_CURRENT if snapshot.source_mode == "current" else DIMENSION_VOLTAGE
        )
        compliance_dimension = (
            DIMENSION_VOLTAGE if snapshot.source_mode == "current" else DIMENSION_CURRENT
        )

        def quantity(text: str, dimension: str) -> str:
            if text.strip().upper() == "AUTO":
                return "AUTO"
            try:
                return format_quantity_auto(parse_quantity(text, dimension).si_value, dimension)
            except Exception:
                return text.strip()

        return {
            "Source mode": snapshot.source_mode.upper(),
            "Source level": quantity(snapshot.source_level, source_dimension),
            "Compliance limit": quantity(snapshot.compliance, compliance_dimension),
            "Source autorange": "ON" if snapshot.source_autorange else "OFF",
            "Active source range": (
                "AUTO (device selects active range)"
                if snapshot.source_autorange
                else quantity(snapshot.source_range, source_dimension)
            ),
            "Sense mode": "2-wire" if snapshot.sense_mode == "2wire" else "4-wire",
            "NPLC": snapshot.nplc.strip(),
            "Measure V autorange": "ON" if snapshot.measure_voltage_autorange else "OFF",
            "Active measure V range": (
                "AUTO (device selects active range)"
                if snapshot.measure_voltage_autorange
                else quantity(snapshot.measure_voltage_range, DIMENSION_VOLTAGE)
            ),
            "Measure I autorange": "ON" if snapshot.measure_current_autorange else "OFF",
            "Active measure I range": (
                "AUTO (device selects active range)"
                if snapshot.measure_current_autorange
                else quantity(snapshot.measure_current_range, DIMENSION_CURRENT)
            ),
        }

    @staticmethod
    def _values_match(
        parameter: str,
        device: dict[str, str],
        configured: dict[str, str],
    ) -> bool:
        range_to_autorange = {
            "Active source range": "Source autorange",
            "Active measure V range": "Measure V autorange",
            "Active measure I range": "Measure I autorange",
        }
        autorange_parameter = range_to_autorange.get(parameter)
        if autorange_parameter is not None and configured[autorange_parameter] == "ON":
            return device[autorange_parameter] == "ON"
        return configured.get(parameter) == device[parameter]

    @staticmethod
    def _channel_values(
        channel: KeithleyChannelConfigurationReadback,
    ) -> dict[str, str]:
        source_dimension = (
            DIMENSION_CURRENT
            if channel.source_mode == "current"
            else DIMENSION_VOLTAGE
        )
        compliance_dimension = (
            DIMENSION_VOLTAGE
            if channel.source_mode == "current"
            else DIMENSION_CURRENT
        )
        off_mode = {
            "normal": "NORMAL",
            "high_impedance": "HIGH-Z",
            "zero": "ZERO",
        }[channel.output_off_mode]
        return {
            "OUTPUT state": "ON" if channel.output_enabled else "OFF",
            "OUTPUT OFF mode": off_mode,
            "Source mode": channel.source_mode.upper(),
            "Source level": format_quantity_auto(
                channel.source_level_si, source_dimension
            ),
            "Compliance limit": format_quantity_auto(
                channel.compliance_si, compliance_dimension
            ),
            "Source autorange": "ON" if channel.source_autorange else "OFF",
            "Active source range": format_quantity_auto(
                channel.source_range_si, source_dimension
            ),
            "Sense mode": "2-wire" if channel.sense_mode == "2wire" else "4-wire",
            "NPLC": f"{channel.nplc:.9g}",
            "Measure V autorange": (
                "ON" if channel.measure_voltage_autorange else "OFF"
            ),
            "Active measure V range": format_quantity_auto(
                channel.measure_voltage_range_v, DIMENSION_VOLTAGE
            ),
            "Measure I autorange": (
                "ON" if channel.measure_current_autorange else "OFF"
            ),
            "Active measure I range": format_quantity_auto(
                channel.measure_current_range_a, DIMENSION_CURRENT
            ),
        }


class _KeithleyFloatingPanelWindow(StationDialog):
    """Non-modal host for one live Keithley panel."""

    closed = Signal()

    def __init__(self, title: str, panel: QWidget, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Keithley 2600 — {title}")
        self.setObjectName("keithleyFloatingPanelWindow")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.setMinimumSize(460, 230)
        self.resize(760, 360 if "plot" in title.lower() else 270)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(panel)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed.emit()
        super().closeEvent(event)


class KeithleyPage(QWidget):
    status = Signal(str)
    quick_controls_requested = Signal()
    settings_assignment_requested = Signal(object)
    settings_defaults_requested = Signal(object)
    _MANUAL_RAMP_ENABLED = False

    def __init__(self, controller: DeviceController, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._station_settings = settings
        self._limit_fields: dict[str, LimitField] = {}
        self._output_states = {"A": False, "B": False}
        self._output_state_known = {"A": False, "B": False}
        self._pending_channels: dict[str, str] = {}
        self._pending_output_enabled: dict[str, bool] = {}
        self._pending_config_modes: dict[str, str] = {}
        self._configured_channels: set[str] = set()
        self._auto_enable_channel: str | None = None
        self._measure_pending = False
        self._ramp_pending = False
        self._readback_pending = False
        self._device_state_value = "DISCONNECTED"
        self._live_next_channel = "A"
        self._history_started_at = time.monotonic()
        self._history_window_s = 30.0
        self._measurement_history: dict[str, list[dict[str, float]]] = {"A": [], "B": []}
        self.history_widgets: dict[str, dict[str, object]] = {}
        self.last_update_labels: dict[str, CaptionLabel] = {}
        self._panel_slots: dict[str, QWidget] = {}
        self._panel_titles: dict[str, str] = {}
        self._panel_widgets: dict[str, QWidget] = {}
        self._panel_float_buttons: dict[str, TransparentToolButton] = {}
        self._floating_panels: dict[str, _KeithleyFloatingPanelWindow] = {}
        self._readback_dialog: _KeithleyReadbackDialog | None = None
        self._last_assignment_succeeded = False
        self._loading_form_snapshot = False
        self._panel_placeholders: dict[str, CardWidget] = {}
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(1000)
        self._live_timer.timeout.connect(self._request_live_measurement)
        layout = QVBoxLayout(self)
        self.banner = NotificationBanner()
        layout.addWidget(self.banner)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        self.hero_card = CardWidget()
        hero = self.hero_card
        hero.setObjectName("keithleyHero")
        hero_layout = QHBoxLayout(hero)
        title = TitleLabel("Keithley 2600 — Dual-channel SMU")
        title.setObjectName("keithleyPageTitle")
        hero_layout.addWidget(title)
        hero_layout.addStretch(1)
        self.quick_controls_button = PushButton("Quick controls...", self.hero_card)
        self.quick_controls_button.clicked.connect(self.quick_controls_requested)
        hero_layout.addWidget(self.quick_controls_button)
        live_title = StrongBodyLabel("Live", hero)
        live_title.setObjectName("sectionTitle")
        self.live_channel_a = CheckBox("A", hero)
        self.live_channel_b = CheckBox("B", hero)
        for channel, checkbox in (
            ("A", self.live_channel_a),
            ("B", self.live_channel_b),
        ):
            checkbox.setToolTip(
                f"Continuously measure channel {channel}. This is read-only and never "
                "enables an output."
            )
        interval_label = BodyLabel("Interval", hero)
        self.live_interval = SpinBox(hero)
        self.live_interval.setRange(100, 60_000)
        self.live_interval.setSingleStep(100)
        self.live_interval.setValue(1000)
        self.live_interval.setSuffix(" ms")
        self.live_interval.setFixedWidth(132)
        self.live_interval.setToolTip(
            "Time between measurement requests. When A and B are selected they are "
            "measured alternately, so each channel updates about every two intervals."
        )
        self.live_timing = CaptionLabel(hero)
        self.live_timing.setObjectName("keithleyLiveTiming")
        self.live_timing.setMinimumWidth(118)
        hero_layout.addWidget(live_title)
        hero_layout.addWidget(self.live_channel_a)
        hero_layout.addWidget(self.live_channel_b)
        hero_layout.addWidget(interval_label)
        hero_layout.addWidget(self.live_interval)
        hero_layout.addWidget(self.live_timing)
        hero.setMaximumHeight(60)
        layout.addWidget(hero)
        channel_grid = QGridLayout()
        channel_grid.setSpacing(12)
        self.channel_cards: dict[str, dict[str, QLabel | QFrame]] = {}
        for column, channel_name in enumerate(("A", "B")):
            panel = self._build_channel_card(channel_name)
            channel_grid.addWidget(
                self._register_detachable_panel(
                    f"channel_{channel_name}", panel, f"Channel {channel_name}"
                ),
                0,
                column,
            )
        layout.addLayout(channel_grid)
        source_tab = QWidget()
        source_layout = QVBoxLayout(source_tab)
        source_layout.setContentsMargins(8, 6, 8, 6)
        source_layout.setSpacing(6)
        buttons = QHBoxLayout()
        self.apply_configuration_button = PrimaryPushButton(
            "Apply & verify settings · OUTPUT OFF"
        )
        self.read_configuration_button = PushButton("Read from device…")
        measure = PushButton("Measure selected channel")
        self.measure_selected_button = measure
        self.output_toggle = PushButton("OUTPUT OFF")
        self.output_toggle.setCheckable(True)
        self.output_toggle.setObjectName("outputOffButton")
        self.output_toggle.setVisible(False)
        buttons.addWidget(self.apply_configuration_button)
        buttons.addWidget(self.read_configuration_button)
        buttons.addWidget(measure)
        source_layout.addLayout(buttons)
        self.configuration_panel = KeithleyConfigurationPanel(settings, source_tab)
        source_layout.addWidget(self.configuration_panel)
        self.channel = self.configuration_panel.channel
        self.mode = self.configuration_panel.mode
        self.level = self.configuration_panel.level
        self.compliance = self.configuration_panel.compliance
        self.nplc = self.configuration_panel.nplc
        # NPLC is an advanced acquisition parameter. Manual control uses the
        # channel default (normally 1 PLC); recipe editors can still expose it.
        self.configuration_panel.form.setRowVisible(
            self.configuration_panel.nplc_field, False
        )
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
        self.max_abs_power = _line(
            self._station_settings.keithley.safety.channels[
                self.channel.currentText()
            ].lab_limits.max_abs_power
        )
        self.max_abs_power_field = self._keithley_bounded(
            "max_abs_power", self.max_abs_power
        )
        self.keithley_form.addRow(
            "Maximum source × compliance power", self.max_abs_power_field
        )
        workflow = CardWidget()
        workflow.setObjectName("keithleyOutputWorkflow")
        workflow_layout = QVBoxLayout(workflow)
        workflow_layout.setContentsMargins(7, 5, 7, 5)
        self.output_readiness = BodyLabel()
        self.output_readiness.setWordWrap(True)
        self.output_readiness.setObjectName("keithleyInterlockStatus")
        self.output_readiness.setToolTip(
            "OUTPUT interlock status. All checks must pass before a channel can be enabled."
        )
        workflow_layout.addWidget(self.output_readiness)
        self.output_guidance = CaptionLabel(workflow)
        self.output_guidance.setObjectName("muted")
        self.output_guidance.setWordWrap(True)
        workflow_layout.addWidget(self.output_guidance)
        source_layout.addWidget(workflow)
        ramp_panel = CardWidget()
        ramp_panel.setObjectName("keithleyRampPanel")
        self.manual_ramp_panel = ramp_panel
        ramp_layout = QVBoxLayout(ramp_panel)
        ramp_layout.setContentsMargins(7, 5, 7, 5)
        ramp_title = StrongBodyLabel("Manual source ramp — OUTPUT must already be ON")
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
            ramp_form.addWidget(BodyLabel(label), 0, column)
            ramp_form.addWidget(widget, 1, column)
        ramp_layout.addLayout(ramp_form)
        ramp_actions = QHBoxLayout()
        self.ramp_preview_button = PushButton("Preview ramp")
        self.ramp_execute_button = PrimaryPushButton("Ramp to target")
        self.ramp_preview = BodyLabel("Preview the ramp before execution.")
        self.ramp_preview.setWordWrap(True)
        self.ramp_preview.setObjectName("muted")
        ramp_actions.addWidget(self.ramp_preview_button)
        ramp_actions.addWidget(self.ramp_execute_button)
        ramp_actions.addWidget(self.ramp_preview, 1)
        ramp_layout.addLayout(ramp_actions)
        source_layout.addWidget(ramp_panel)
        ramp_panel.setVisible(self._MANUAL_RAMP_ENABLED)
        self.readout = BodyLabel()
        self.readout.hide()
        source_layout.addStretch(1)
        source_scroll = self._scroll_widget(source_tab)
        source_tab.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.source_scroll = source_scroll
        source_scroll.setObjectName("keithleyControlPanel")
        source_scroll.setMinimumWidth(640)
        history_tab = QWidget()
        self.history_layout = QHBoxLayout(history_tab)
        self.history_layout.setContentsMargins(6, 0, 0, 0)
        self.history_layout.setSpacing(8)
        for channel_name in ("A", "B"):
            panel = self._build_keithley_history_panel(channel_name)
            self.history_layout.addWidget(
                self._register_detachable_panel(
                    f"plot_{channel_name}", panel, f"Plot for channel {channel_name}"
                ),
                1,
            )
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setObjectName("keithleyWorkspace")
        self.workspace_splitter.addWidget(source_scroll)
        self.workspace_splitter.addWidget(history_tab)
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 5)
        self.workspace_splitter.setSizes([760, 1040])
        self.workspace_splitter.setChildrenCollapsible(False)
        self._workspace_compact: bool | None = None
        layout.addWidget(self.workspace_splitter, 1)
        self.apply_configuration_button.clicked.connect(self.configure)
        self.read_configuration_button.clicked.connect(
            self.read_configuration_from_device
        )
        measure.clicked.connect(self.request_measurement)
        self.output_toggle.toggled.connect(self._output_toggled)
        self.ramp_preview_button.clicked.connect(self._preview_manual_ramp)
        self.ramp_execute_button.clicked.connect(self._execute_manual_ramp)
        self.live_channel_a.toggled.connect(self._live_selection_changed)
        self.live_channel_b.toggled.connect(self._live_selection_changed)
        self.live_interval.valueChanged.connect(self._live_interval_changed)
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
        self._channel_form_snapshots: dict[str, KeithleyConfigurationSnapshot] = {
            channel: self._default_form_snapshot(channel)
            for channel in ("A", "B")
        }
        self._load_form_snapshot(self._channel_form_snapshots[self._active_channel])
        self.channel.currentTextChanged.connect(self._channel_changed)
        self.mode.currentTextChanged.connect(self._mode_changed)
        self.channel.currentTextChanged.connect(self._selected_channel_changed)
        for editor in (
            self.level,
            self.compliance,
            self.nplc,
            self.settle,
            self.source_range,
            self.measure_voltage_range,
            self.measure_current_range,
        ):
            editor.editingFinished.connect(self._persist_form_defaults)
        self.sense_mode.currentIndexChanged.connect(self._persist_form_defaults)
        self.source_autorange.toggled.connect(
            lambda enabled: self._autorange_changed(
                enabled, self.source_range, "source range"
            )
        )
        self.measure_voltage_autorange.toggled.connect(
            lambda enabled: self._autorange_changed(
                enabled, self.measure_voltage_range, "voltage measurement range"
            )
        )
        self.measure_current_autorange.toggled.connect(
            lambda enabled: self._autorange_changed(
                enabled, self.measure_current_range, "current measurement range"
            )
        )
        self._selected_channel_changed(self.channel.currentText())
        self._update_source_mode_ui()
        self._update_output_readiness()
        self._update_ramp_defaults()
        self._update_live_controls()
        self._install_keithley_help(
            measure=measure,
            output_toggle=self.output_toggle,
        )

    @staticmethod
    def _scroll_widget(content: QWidget) -> ScrollArea:
        content.setMinimumWidth(0)
        content.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        compact = event.size().width() < 1360
        if self._workspace_compact == compact:
            return
        self._workspace_compact = compact
        self.source_scroll.setMinimumWidth(0 if compact else 640)
        self.keithley_form.setRowWrapPolicy(
            QFormLayout.RowWrapPolicy.WrapAllRows
            if compact
            else QFormLayout.RowWrapPolicy.DontWrapRows
        )
        self.workspace_splitter.setOrientation(
            Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal
        )
        self.history_layout.setDirection(
            QBoxLayout.Direction.TopToBottom
            if compact
            else QBoxLayout.Direction.LeftToRight
        )
        self.workspace_splitter.setSizes(
            [900, 760] if compact else [760, 1040]
        )

    def _register_detachable_panel(
        self, key: str, panel: QWidget, title: str
    ) -> QWidget:
        slot = QWidget(self)
        slot.setObjectName(f"keithleyPanelSlot_{key}")
        slot_layout = QVBoxLayout(slot)
        slot_layout.setContentsMargins(0, 0, 0, 0)
        slot_layout.setSpacing(0)
        slot_layout.addWidget(panel)
        self._panel_slots[key] = slot
        self._panel_titles[key] = title
        self._panel_widgets[key] = panel
        return slot

    def _panel_float_button(self, key: str, parent: QWidget) -> TransparentToolButton:
        button = TransparentToolButton(FluentIcon.FULL_SCREEN, parent)
        button.setObjectName(f"keithleyFloatButton_{key}")
        button.setFixedSize(28, 28)
        button.setToolTip("Open this panel in a separate floating window")
        button.setAccessibleName("Open panel in a floating window")
        button.clicked.connect(
            lambda _checked=False, panel_key=key: self._toggle_panel_floating(panel_key)
        )
        self._panel_float_buttons[key] = button
        return button

    def _toggle_panel_floating(self, key: str) -> None:
        if key in self._floating_panels:
            self._dock_panel(key)
        else:
            self._detach_panel(key)

    def _detach_panel(self, key: str) -> None:
        floating = self._floating_panels.get(key)
        if floating is not None:
            floating.show()
            floating.raise_()
            floating.activateWindow()
            return
        panel = self._panel_widgets[key]
        slot = self._panel_slots[key]
        slot.layout().removeWidget(panel)
        placeholder = CardWidget(slot)
        placeholder.setObjectName("keithleyFloatingPlaceholder")
        placeholder_layout = QVBoxLayout(placeholder)
        placeholder_layout.setContentsMargins(14, 12, 14, 12)
        placeholder_layout.addWidget(StrongBodyLabel(self._panel_titles[key], placeholder))
        note = BodyLabel("This panel is open in a separate window.", placeholder)
        note.setObjectName("muted")
        placeholder_layout.addWidget(note)
        restore = PushButton("Restore panel", placeholder)
        restore.clicked.connect(
            lambda _checked=False, panel_key=key: self._dock_panel(panel_key)
        )
        placeholder_layout.addWidget(restore)
        placeholder_layout.addStretch(1)
        slot.layout().addWidget(placeholder)
        self._panel_placeholders[key] = placeholder

        floating = _KeithleyFloatingPanelWindow(
            self._panel_titles[key], panel, self
        )
        floating.closed.connect(
            lambda panel_key=key: self._dock_panel(panel_key, close_window=False)
        )
        self._floating_panels[key] = floating
        button = self._panel_float_buttons[key]
        button.setIcon(FluentIcon.BACK_TO_WINDOW)
        button.setToolTip("Restore this panel to the Keithley page")
        button.setAccessibleName("Restore panel to the Keithley page")
        floating.show()
        floating.raise_()
        floating.activateWindow()

    def _dock_panel(self, key: str, *, close_window: bool = True) -> None:
        floating = self._floating_panels.pop(key, None)
        if floating is None:
            return
        panel = self._panel_widgets[key]
        if floating.layout() is not None:
            floating.layout().removeWidget(panel)
        placeholder = self._panel_placeholders.pop(key, None)
        if placeholder is not None:
            self._panel_slots[key].layout().removeWidget(placeholder)
            placeholder.deleteLater()
        self._panel_slots[key].layout().addWidget(panel)
        button = self._panel_float_buttons[key]
        button.setIcon(FluentIcon.FULL_SCREEN)
        button.setToolTip("Open this panel in a separate floating window")
        button.setAccessibleName("Open panel in a floating window")
        if close_window:
            floating.close()
        floating.deleteLater()

    def _build_keithley_history_panel(self, channel: str) -> CardWidget:
        panel = CardWidget()
        panel.setObjectName("keithleyChannelCard")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(7, 6, 7, 6)
        panel_layout.setSpacing(4)
        header = QHBoxLayout()
        title = StrongBodyLabel(f"CHANNEL {channel} — rolling 30 s history")
        title.setObjectName("keithleyHistoryTitle")
        last_update = CaptionLabel("No measurements yet", panel)
        last_update.setObjectName("keithleyLastUpdate")
        self.last_update_labels[channel] = last_update
        metric = ComboBox()
        metric.addItem("DC resistance |V/I|", userData=("resistance", "Resistance", "Ω"))
        metric.addItem("Voltage", userData=("voltage", "Voltage", "V"))
        metric.addItem("Current", userData=("current", "Current", "A"))
        metric.addItem("Power V×I", userData=("power", "Power", "W"))
        clear = PushButton("Clear history")
        clear.setProperty("compact", True)
        metric.setFixedHeight(28)
        clear.setFixedHeight(28)
        header.addWidget(title)
        header.addWidget(last_update)
        header.addStretch(1)
        header.addWidget(metric)
        header.addWidget(clear)
        header.addWidget(self._panel_float_button(f"plot_{channel}", panel))
        panel_layout.addLayout(header)
        note = BodyLabel("ROLLING 30 s  •  DC resistance |V/I|  •  not complex impedance")
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
        if not isinstance(plot, SpectrumPlotWidget) or not isinstance(metric, ComboBox):
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
            show_points=True,
        )
        # The history keeps elapsed seconds from the start of this page.  Move
        # the visible viewport with the rolling retention window; otherwise
        # points collected after 30 s remain in the data model but drift out of
        # the original 0â€“30 s view.
        latest_elapsed_s = history[-1]["elapsed_s"] if history else 0.0
        plot.plot.setXRange(
            max(0.0, latest_elapsed_s - self._history_window_s),
            max(self._history_window_s, latest_elapsed_s),
            padding=0,
        )

    def _clear_keithley_history(self, channel: str) -> None:
        self._measurement_history[channel].clear()
        self.last_update_labels[channel].setText("No measurements yet")
        plot = self.history_widgets[channel]["plot"]
        if isinstance(plot, SpectrumPlotWidget):
            plot.clear()
        self.status.emit(f"Keithley CH {channel} measurement history cleared")

    def _build_channel_card(self, channel: str) -> CardWidget:
        card = CardWidget()
        card.setObjectName("keithleyChannelCard")
        card.setProperty("selected", False)
        card.setMaximumHeight(154)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(8, 6, 8, 6)
        card_layout.setSpacing(4)
        header = QHBoxLayout()
        name = StrongBodyLabel(f"CHANNEL {channel}")
        name.setObjectName("keithleyCardTitle")
        led = BodyLabel("●")
        led.setObjectName("keithleyOutputLed")
        output = BodyLabel("OUTPUT OFF")
        output.setObjectName("keithleyOutputState")
        header.addWidget(name)
        header.addStretch(1)
        header.addWidget(led)
        header.addWidget(output)
        header.addWidget(self._panel_float_button(f"channel_{channel}", card))
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
            tile = CardWidget()
            tile.setObjectName("keithleyMeterTile")
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(7, 3, 7, 3)
            tile_layout.setSpacing(1)
            caption_label = CaptionLabel(caption)
            caption_label.setObjectName("muted")
            value = StrongBodyLabel(f"— {unit}")
            value.setObjectName("keithleyMeterValue")
            tile_layout.addWidget(caption_label)
            tile_layout.addWidget(value)
            meters.addWidget(tile, 0, index)
            values[key] = value
        card_layout.addLayout(meters)
        footer = QHBoxLayout()
        compliance = BodyLabel("COMPLIANCE: clear")
        compliance.setObjectName("keithleyComplianceClear")
        select = PushButton(f"Select CH {channel}")
        select.setProperty("compact", True)
        select.clicked.connect(lambda _checked=False, ch=channel: self.channel.setCurrentText(ch))
        measure = PushButton(f"Measure CH {channel}")
        measure.setProperty("compact", True)
        measure.clicked.connect(lambda _checked=False, ch=channel: self.request_measurement(ch))
        output_on_action = PushButton("OUTPUT ON")
        output_on_action.setCheckable(True)
        output_off_action = PushButton("OUTPUT OFF")
        for button in (output_on_action, output_off_action):
            button.setProperty("compact", True)
        output_on_action.setObjectName("outputOnButton")
        output_off_action.setObjectName("outputOffButton")
        output_on_action.clicked.connect(
            lambda _checked=False, ch=channel: self._request_channel_output(ch, True)
        )
        output_off_action.clicked.connect(
            lambda _checked=False, ch=channel: self._request_channel_output(ch, False)
        )
        footer.addWidget(compliance)
        footer.addStretch(1)
        footer.addWidget(select)
        footer.addWidget(measure)
        footer.addWidget(output_on_action)
        footer.addWidget(output_off_action)
        for button in (select, measure, output_on_action, output_off_action):
            button.setFixedHeight(28)
        card_layout.addLayout(footer)
        self.channel_cards[channel] = {
            "card": card,
            "led": led,
            "output": output,
            "compliance": compliance,
            "select": select,
            "measure": measure,
            "output_on_action": output_on_action,
            "output_off_action": output_off_action,
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
            self.mode: ("Source mode", "Current forces the Source current and uses Voltage limit (compliance) as its protection. Voltage forces the Source voltage and uses Current limit (compliance). Measure only does not program or enable a source."),
            self.level: ("Source value", "The quantity Keithley actively tries to force. In Current mode this is current; in Voltage mode this is voltage. MIN/MAX are the configured laboratory range for this programmed value."),
            self.compliance: ("Opposite-quantity safety limit", "The protection limit shown directly below the source setpoint. Current mode exposes Voltage limit (compliance); Voltage mode exposes Current limit (compliance). Reaching it means the requested source value cannot be maintained."),
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
            output_toggle: ("OUTPUT ON/OFF", "ON validates the visible values against MIN/MAX, configures with OUTPUT OFF, verifies readback and then energizes the terminals. OFF disables the selected output immediately and verifies readback."),
            self.live_channel_a: ("Live channel A", "Continuously requests I/V readings from channel A. It never enables an output, but it does generate instrument traffic."),
            self.live_channel_b: ("Live channel B", "Continuously requests I/V readings from channel B. It never enables an output, but it does generate instrument traffic."),
            self.live_interval: ("Live request interval", "Time between read requests. With both channels selected, A and B alternate, so each channel updates approximately every two request intervals."),
            self.live_timing: ("Effective live timing", "Shows both the request interval and the effective update period for each selected channel."),
            self.ramp_target: ("Ramp target", "Final Current or Voltage source level. It is validated against the channel laboratory limits and active DUT envelope before any command is sent."),
            self.ramp_step: ("Maximum ramp step", "Largest allowed change between adjacent source points. The adapter rejects values above the configured ramp_current_step_max or ramp_voltage_step_max."),
            self.ramp_settle: ("Ramp dwell", "Time allowed after every source step before the atomic I/V safety measurement."),
            self.ramp_deadline: ("Ramp deadline", "Maximum wall-clock time for the complete operation. Timeout triggers a best-effort OFF of both SMU outputs."),
            self.ramp_preview_button: ("Preview ramp", "Calculates the finite point sequence without contacting the instrument. Execution still queries the actual starting source level."),
            self.ramp_execute_button: ("Ramp active output", "Changes an already enabled source through bounded points. It never turns an output on; every point measures I/V and trips OFF on failure."),
            self.apply_configuration_button: ("Apply and verify with OUTPUT OFF", "Validates every visible value and unit, forces the selected channel OUTPUT OFF, writes the complete source and measurement configuration, and reads every instrument-programmable parameter back. Software settling time is validated locally. This action never enables OUTPUT."),
            self.read_configuration_button: ("Read settings from device", "Queries the complete active configuration of channels A and B and opens a read-only comparison. Only print(...) TSP queries are sent; no setting or OUTPUT state is changed."),
        }
        for widget, (title, description) in help_items.items():
            self._set_help(widget, title, description)

        for channel, card in self.channel_cards.items():
            self._set_help(card["card"], f"Channel {channel} overview", "Live overview of this channel. Voltage and current are direct readings; resistance and power are derived from the latest I/V pair.")
            self._set_help(card["led"], f"Channel {channel} output LED", "The indicator is lit only for confirmed OUTPUT ON. Grey means OUTPUT OFF, unknown, or disconnected; use the adjacent text to distinguish those states.")
            self._set_help(card["output"], f"Channel {channel} output state", "Shows the last state confirmed by a successful connect, configure, enable, ramp-off or compliance-stop operation.")
            self._set_help(card["voltage"], "Measured voltage", "Direct voltage reading returned by Keithley for this channel.")
            self._set_help(card["current"], "Measured current", "Direct current reading returned by Keithley for this channel.")
            self._set_help(card["resistance"], "Derived resistance", "Calculated as |V/I| from the latest reading. It is not a dedicated resistance measurement and becomes infinity when current is effectively zero.")
            self._set_help(card["power"], "Derived power", "Calculated as V × I from the latest reading. Sign describes source/load direction; magnitude describes electrical power.")
            self._set_help(card["compliance"], "Compliance indicator", "ACTIVE means the measured opposite quantity reached the programmed compliance threshold. The safety policy may immediately disable outputs.")
            self._set_help(card["select"], f"Select channel {channel}", "Makes this channel active in the configuration form without changing its electrical output.")
            self._set_help(card["measure"], f"Measure channel {channel}", "Requests one voltage/current reading for this channel without enabling its output.")
            self._set_help(card["output_on_action"], f"Channel {channel} OUTPUT ON", "Validates and confirms the visible source settings, configures with OUTPUT OFF, verifies readback and only then energizes this channel.")
            self._set_help(card["output_off_action"], f"Channel {channel} OUTPUT OFF", "Disables this channel immediately and verifies the hardware readback. OUTPUT OFF is never blocked by audit health.")
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

    def _device_is_output_ready(self) -> bool:
        return self._device_state_value in {"VERIFIED", "OUTPUT OFF", "OUTPUT ON"}

    def _output_prerequisites(
        self, channel: str | None = None
    ) -> tuple[bool, list[str]]:
        channel = channel or self.channel.currentText()
        safety = self._station_settings.keithley.safety
        checks = [
            (safety.allow_output_enable, "Keithley output permission enabled"),
            (safety.channels[channel].enabled, f"channel {channel} enabled"),
            (self._device_is_output_ready(), "device connected and verified"),
        ]
        return all(value for value, _label in checks), [
            f"{'✓' if value else '✕'} {label}" for value, label in checks
        ]

    def _output_readiness_guidance(self, channel: str) -> str:
        steps: list[str] = []
        if not self._station_settings.keithley.connection.resource:
            steps.append(
                "configure and save the Keithley VISA resource in Discovery or Settings"
            )
        if not self._device_is_output_ready():
            steps.append(
                "use Instrument connection at the top of this page and click Connect"
            )
        if not self._station_settings.keithley.safety.channels[channel].enabled:
            steps.append(f"enable channel {channel} in station settings")
        if not self._station_settings.keithley.safety.allow_output_enable:
            steps.append("enable Keithley output permission in station settings")
        if steps:
            return "To enable OUTPUT: " + "; then ".join(steps) + "."
        return (
            "Ready. OUTPUT ON will configure with terminals OFF, enable the selected "
            "channel, and verify the instrument readback."
        )

    def _update_output_readiness(self) -> None:
        if not hasattr(self, "output_readiness"):
            return
        selected_channel = self.channel.currentText()
        ready, checks = self._output_prerequisites(selected_channel)
        self.output_readiness.setText("Output readiness: " + " • ".join(checks))
        self.output_guidance.setText(
            self._output_readiness_guidance(selected_channel)
        )
        self.output_toggle.setEnabled(ready or self._output_states[self.channel.currentText()])
        configure_pending = "configure" in self._pending_channels
        configuration_mutation_pending = (
            self._auto_enable_channel is not None
            or any(
                operation in self._pending_channels
                for operation in (
                    "configure",
                    "set_output",
                    "ramp_to_zero",
                    "ramp_to_level",
                )
            )
        )
        configuration_busy = configuration_mutation_pending or self._readback_pending
        self.apply_configuration_button.setText(
            "Applying & verifying…"
            if configure_pending
            else "Apply & verify settings · OUTPUT OFF"
        )
        self.apply_configuration_button.setEnabled(
            self._device_is_output_ready() and not configuration_busy
        )
        self.read_configuration_button.setText(
            "Reading device…"
            if self._readback_pending
            else "Read from device…"
        )
        self.read_configuration_button.setEnabled(
            self._device_is_output_ready()
            and not configuration_busy
            and not self._measure_pending
        )
        for channel, card in self.channel_cards.items():
            pending_enable = (
                self._auto_enable_channel == channel
                or self._readback_pending
                or any(
                    self._pending_channels.get(operation) == channel
                    for operation in ("configure", "set_output")
                )
            )
            channel_ready, channel_checks = self._output_prerequisites(channel)
            missing = "; ".join(
                item for item in channel_checks if item.startswith("✕")
            )
            card["output_on_action"].setEnabled(
                channel_ready and not pending_enable
            )
            card["output_on_action"].setToolTip(
                (
                    "Ready: configure OFF → enable with readback."
                    if channel_ready
                    else "Click for the blocking condition: " + missing
                )
            )
            # A confirmed OFF or disconnected channel has no OFF action to
            # perform. An uncertain connected state still exposes best-effort
            # OFF regardless of profile, RBAC or audit health.
            confirmed_off = (
                self._output_state_known[channel]
                and not self._output_states[channel]
            )
            disconnected = self._device_state_value == "DISCONNECTED"
            card["output_off_action"].setEnabled(
                not pending_enable and not confirmed_off and not disconnected
            )

    def _device_state_changed(self, state: str) -> None:
        normalized = state.upper()
        self._device_state_value = normalized.replace("_", " ")
        if normalized == "DISCONNECTED":
            self._live_timer.stop()
            for checkbox in (self.live_channel_a, self.live_channel_b):
                checkbox.setChecked(False)
            self._configured_channels.clear()
            self._output_states = {"A": False, "B": False}
            self._output_state_known = {"A": False, "B": False}
            self.output_toggle.blockSignals(True)
            self.output_toggle.setChecked(False)
            self.output_toggle.blockSignals(False)
            self._style_output_toggle(False)
            for channel in ("A", "B"):
                widgets = self.channel_cards[channel]
                widgets["output"].setText("OUTPUT UNKNOWN")
                widgets["output"].setProperty("outputState", "neutral")
                widgets["led"].setProperty("outputState", "neutral")
                widgets["output_on_action"].setChecked(False)
                widgets["output_on_action"].setProperty(
                    "controlState", "available"
                )
                for widget in (
                    widgets["output"],
                    widgets["led"],
                    widgets["output_on_action"],
                ):
                    widget.style().unpolish(widget)
                    widget.style().polish(widget)
        elif normalized == "VERIFIED":
            # Connection qualification explicitly forces and verifies both outputs OFF.
            self._set_channel_output("A", False)
            self._set_channel_output("B", False)
        elif normalized in {"FAULT", "UNKNOWN"}:
            self._output_state_known = {"A": False, "B": False}
            for channel in ("A", "B"):
                widgets = self.channel_cards[channel]
                widgets["output"].setText("OUTPUT UNKNOWN")
                widgets["output"].setProperty("outputState", "neutral")
                widgets["led"].setProperty("outputState", "neutral")
                widgets["output_on_action"].setChecked(False)
                widgets["output_on_action"].setProperty(
                    "controlState", "available"
                )
                for widget in (
                    widgets["output"],
                    widgets["led"],
                    widgets["output_on_action"],
                ):
                    widget.style().unpolish(widget)
                    widget.style().polish(widget)
        self._update_live_controls()
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
        path_connected = bool(
            getattr(measurement, "measurement_path_connected", True)
        )
        # Keep the thaTEC-compatible derived V/I view available even when the
        # HIGH-Z off-mode relay is open. The UI labels that state as FLOATING;
        # it does not claim this is a physically valid connected-DUT value.
        resistance = abs(voltage / current) if abs(current) > 1e-15 else math.inf
        widgets = self.channel_cards[channel]
        widgets["voltage"].setText(self._engineering(voltage, "V"))
        widgets["current"].setText(self._engineering(current, "A"))
        widgets["power"].setText(self._engineering(power, "W"))
        widgets["resistance"].setText(
            self._engineering(resistance, "Ω")
            if math.isfinite(resistance)
            else "∞ Ω"
        )
        compliance = bool(getattr(measurement, "compliance_detected", False))
        widgets["compliance"].setText(
            "PATH: HIGH-Z / FLOATING"
            if not path_connected
            else ("COMPLIANCE: ACTIVE" if compliance else "COMPLIANCE: clear")
        )
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
        self.last_update_labels[channel].setText(
            f"{channel}: updated {time.strftime('%H:%M:%S')} • "
            f"t={elapsed:.1f} s • {len(history)} pts"
            + (" • HIGH-Z / floating" if not path_connected else "")
        )

    def _set_channel_output(self, channel: str, enabled: bool) -> None:
        self._output_states[channel] = enabled
        self._output_state_known[channel] = True
        widgets = self.channel_cards[channel]
        widgets["output"].setText("OUTPUT ON" if enabled else "OUTPUT OFF")
        widgets["output"].setProperty("outputState", "active" if enabled else "neutral")
        widgets["led"].setProperty("outputState", "active" if enabled else "neutral")
        widgets["output_on_action"].setChecked(enabled)
        widgets["output_on_action"].setProperty(
            "controlState", "energized" if enabled else "available"
        )
        for widget in (
            widgets["output"],
            widgets["led"],
            widgets["output_on_action"],
        ):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        widgets["output_on_action"].setText("OUTPUT ON")
        widgets["output_off_action"].setText("OUTPUT OFF")
        if channel == self.channel.currentText():
            self.output_toggle.blockSignals(True)
            self.output_toggle.setChecked(enabled)
            self.output_toggle.blockSignals(False)
            self._style_output_toggle(enabled)
        self._update_ramp_defaults()
        self._update_output_readiness()
        self._update_live_controls()

    def request_measurement(self, channel: str | None = None) -> None:
        if self._measure_pending:
            return
        selected = channel or self.channel.currentText()
        self._pending_channels["measure"] = selected
        self._measure_pending = True
        self._controller.call("measure", selected)

    def _request_channel_output(self, channel: str, enabled: bool) -> None:
        self.channel.setCurrentText(channel)
        if enabled and self._output_states[channel]:
            return
        action = self.channel_cards[channel][
            "output_on_action" if enabled else "output_off_action"
        ]
        action.setText("ENABLING…" if enabled else "DISABLING…")
        action.setEnabled(False)
        if enabled:
            self._output_toggled(True)
        else:
            self.request_output_off(channel)

    def request_output_off(self, channel: str | None = None) -> None:
        channel = channel or self.channel.currentText()
        self.channel.setCurrentText(channel)
        self._pending_channels["set_output"] = channel
        self._pending_output_enabled[channel] = False
        self.output_toggle.setText("DISABLING…")
        self.output_toggle.setEnabled(False)
        self._update_output_readiness()
        self._controller.call("set_output", (channel, False))

    def _selected_live_channels(self) -> list[str]:
        return [
            channel
            for channel, checkbox in (
                ("A", self.live_channel_a),
                ("B", self.live_channel_b),
            )
            if checkbox.isChecked()
            and self._station_settings.keithley.safety.channels[channel].enabled
        ]

    def _live_interval_changed(self, interval_ms: int) -> None:
        self._live_timer.setInterval(interval_ms)
        self._update_live_timing()

    def _live_selection_changed(self, _enabled: bool) -> None:
        selected = self._selected_live_channels()
        self._update_live_timing()
        if not selected:
            self._live_timer.stop()
            return
        if not self._device_is_output_ready():
            self._live_timer.stop()
            self.banner.show_message(
                "Live measurement requires a connected and verified Keithley. "
                "Use Instrument connection at the top of this page.",
                timeout_ms=12_000,
            )
            return
        if self._live_next_channel not in selected:
            self._live_next_channel = selected[0]
        self._live_timer.setInterval(self.live_interval.value())
        if not self._live_timer.isActive():
            self._request_live_measurement()
            self._live_timer.start()

    def _update_live_controls(self) -> None:
        connected = self._device_is_output_ready()
        high_impedance_off = (
            self._station_settings.keithley.safety.output_off_mode
            == "high_impedance"
        )
        for channel, checkbox in (
            ("A", self.live_channel_a),
            ("B", self.live_channel_b),
        ):
            channel_enabled = (
                self._station_settings.keithley.safety.channels[channel].enabled
            )
            measurement_path_available = (
                not high_impedance_off or self._output_states[channel]
            )
            checkbox.setEnabled(connected and channel_enabled)
            measure_button = self.channel_cards[channel]["measure"]
            measure_button.setEnabled(connected and channel_enabled)
            if not channel_enabled and checkbox.isChecked():
                checkbox.setChecked(False)
            if not connected:
                checkbox.setToolTip(
                    f"Connect and verify Keithley before starting Live channel {channel}."
                )
            elif not channel_enabled:
                checkbox.setToolTip(
                    f"Channel {channel} is disabled in the station profile."
                )
            elif not measurement_path_available:
                checkbox.setToolTip(
                    f"Channel {channel} is OUTPUT OFF in HIGH-Z mode, so its relay is "
                    "open and the reading may float. Live never enables OUTPUT "
                    "automatically."
                )
                measure_button.setToolTip(
                    f"Read channel {channel} without changing OUTPUT. In HIGH-Z with "
                    "OUTPUT OFF the relay is open, so the result may float."
                )
            else:
                checkbox.setToolTip(
                    f"Continuously measure channel {channel}. This is read-only and "
                    "never enables an output."
                )
                measure_button.setToolTip(
                    f"Read channel {channel} without changing either OUTPUT state."
                )
        selected = self.channel.currentText()
        if hasattr(self, "measure_selected_button"):
            self.measure_selected_button.setEnabled(
                self.channel_cards[selected]["measure"].isEnabled()
            )
        self.live_interval.setEnabled(connected)
        self._update_live_timing()

    def _update_live_timing(self) -> None:
        interval_s = self.live_interval.value() / 1000.0
        interval_text = format_quantity_auto(
            interval_s, DIMENSION_TIME, precision=4
        )
        selected = self._selected_live_channels()
        if not selected:
            if (
                self._device_is_output_ready()
                and self._station_settings.keithley.safety.output_off_mode
                == "high_impedance"
                and not any(self._output_states.values())
            ):
                self.live_timing.setText("Stopped • HIGH-Z relay open; readings may float")
                self.live_timing.setToolTip(
                    "HIGH-Z opens both output relays. Live remains available and never "
                    "changes OUTPUT, but OFF-state readings are not connected-DUT values."
                )
                return
            self.live_timing.setText(f"Stopped • {interval_text}")
            self.live_timing.setToolTip(
                f"Live measurement is stopped. Request interval: {interval_text}."
            )
            return
        channels = " + ".join(selected)
        effective_s = interval_s * len(selected)
        effective_text = format_quantity_auto(
            effective_s, DIMENSION_TIME, precision=4
        )
        self.live_timing.setText(
            f"{channels} • each ≈ {effective_text}"
        )
        self.live_timing.setToolTip(
            f"Live {channels}: one request every {interval_text}; "
            f"each selected channel updates approximately every {effective_text}."
        )

    def _request_live_measurement(self) -> None:
        if self._measure_pending or self._ramp_pending:
            return
        selected = self._selected_live_channels()
        if not selected:
            self._live_timer.stop()
            self.status.emit("Keithley live readout stopped: no selected channels")
            return
        channel = (
            self._live_next_channel
            if self._live_next_channel in selected
            else selected[0]
        )
        next_index = (selected.index(channel) + 1) % len(selected)
        self._live_next_channel = selected[next_index]
        self.request_measurement(channel)

    def _remember_source_values(self) -> None:
        self._source_value_cache[(self._active_channel, self._active_mode)] = (
            self.level.text(),
            self.compliance.text(),
            self.source_range.text(),
        )
        if hasattr(self, "_channel_form_snapshots"):
            self._channel_form_snapshots[self._active_channel] = (
                self._capture_form_snapshot(
                    channel=self._active_channel, mode=self._active_mode
                )
            )

    def _capture_form_snapshot(
        self, *, channel: str, mode: str
    ) -> KeithleyConfigurationSnapshot:
        return KeithleyConfigurationSnapshot(
            channel=channel,
            source_mode=mode,
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

    def _default_form_snapshot(self, channel: str) -> KeithleyConfigurationSnapshot:
        defaults = self._station_settings.keithley.safety.channels[channel].defaults
        mode = str(defaults.get("source_mode", "measure_only"))
        source_key = "source_current" if mode == "current" else "source_voltage"
        compliance_key = (
            "voltage_compliance" if mode == "current" else "current_compliance"
        )
        fallback_level, fallback_compliance, _ = self._default_source_values(channel, mode)
        sense = str(defaults.get("sense_mode", "2wire")).replace("-", "").lower()
        return KeithleyConfigurationSnapshot(
            channel=channel,
            source_mode=mode,
            source_level=str(defaults.get(source_key, fallback_level)),
            compliance=str(defaults.get(compliance_key, fallback_compliance)),
            nplc=str(defaults.get("nplc", 1)),
            settling_time=str(defaults.get("settling_time", "100 ms")),
            sense_mode=sense,
            source_autorange=bool(defaults.get("source_autorange", True)),
            source_range=str(defaults.get("source_range", "AUTO")),
            measure_voltage_autorange=bool(
                defaults.get("measure_voltage_autorange", True)
            ),
            measure_voltage_range=str(defaults.get("measure_voltage_range", "AUTO")),
            measure_current_autorange=bool(
                defaults.get("measure_current_autorange", True)
            ),
            measure_current_range=str(defaults.get("measure_current_range", "AUTO")),
        )

    def _load_form_snapshot(self, snapshot: KeithleyConfigurationSnapshot) -> None:
        self._loading_form_snapshot = True
        try:
            self.mode.blockSignals(True)
            self.mode.setCurrentText(snapshot.source_mode)
            self.mode.blockSignals(False)
            self._active_mode = snapshot.source_mode
            self.level.setText(snapshot.source_level)
            self.compliance.setText(snapshot.compliance)
            self.nplc.setText(snapshot.nplc)
            self.sense_mode.setCurrentText(snapshot.sense_mode)
            self.source_autorange.setChecked(snapshot.source_autorange)
            self.source_range.setText(snapshot.source_range)
            self.measure_voltage_autorange.setChecked(snapshot.measure_voltage_autorange)
            self.measure_voltage_range.setText(snapshot.measure_voltage_range)
            self.measure_current_autorange.setChecked(snapshot.measure_current_autorange)
            self.measure_current_range.setText(snapshot.measure_current_range)
        finally:
            self._loading_form_snapshot = False
        self._source_value_cache[(snapshot.channel, snapshot.source_mode)] = (
            snapshot.source_level,
            snapshot.compliance,
            snapshot.source_range,
        )
        self._refresh_keithley_limits()
        self._update_source_mode_ui()

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
        self.max_abs_power.setText(
            self._station_settings.keithley.safety.channels[
                channel
            ].lab_limits.max_abs_power
        )
        snapshot = self._channel_form_snapshots.get(channel)
        if snapshot is not None:
            self._load_form_snapshot(snapshot)
        else:
            self._refresh_keithley_limits()
            self._load_source_values()
            self._update_source_mode_ui()

    def _mode_changed(self, mode: str) -> None:
        self._remember_source_values()
        self._active_mode = mode
        self._refresh_keithley_limits()
        self._load_source_values()
        self._update_source_mode_ui()
        self._persist_form_defaults()

    def _persist_form_defaults(self, *_args: object) -> None:
        if self._loading_form_snapshot:
            return
        self._remember_source_values()
        self.settings_defaults_requested.emit(dict(self._channel_form_snapshots))

    def _autorange_changed(
        self, enabled: bool, range_editor: QLineEdit, label: str
    ) -> None:
        if self._loading_form_snapshot:
            return
        range_editor.setEnabled(not enabled)
        if enabled:
            range_editor.setText("AUTO")
            self._persist_form_defaults()
            return
        self.banner.show_message(
            f"Autorange disabled: enter an explicit {label} with a unit. "
            "The draft will be validated when you press SAVE SETTINGS, "
            "Apply/verify, or OUTPUT ON.",
            timeout_ms=8_000,
        )
        range_editor.setFocus()

    def _update_source_mode_ui(self) -> None:
        mode = self.mode.currentText()
        source_visible = mode != "measure_only"
        for widget in (self.level_field, self.compliance_field, self.source_autorange, self.source_range_field):
            self.keithley_form.setRowVisible(widget, source_visible)
        if mode == "current":
            self.keithley_form.labelForField(self.level_field).setText("Source current")
            self.keithley_form.labelForField(self.compliance_field).setText("Voltage limit (compliance)")
            self.keithley_form.labelForField(self.source_range_field).setText("Current source range")
        elif mode == "voltage":
            self.keithley_form.labelForField(self.level_field).setText("Source voltage")
            self.keithley_form.labelForField(self.compliance_field).setText("Current limit (compliance)")
            self.keithley_form.labelForField(self.source_range_field).setText("Voltage source range")
        self.source_range.setEnabled(not self.source_autorange.isChecked())
        self.measure_voltage_range.setEnabled(
            not self.measure_voltage_autorange.isChecked()
        )
        self.measure_current_range.setEnabled(
            not self.measure_current_autorange.isChecked()
        )
        self._update_output_readiness()
        self._update_ramp_defaults(reset_values=True)

    def _update_ramp_defaults(self, *, reset_values: bool = False) -> None:
        if not self._MANUAL_RAMP_ENABLED:
            self.ramp_preview_button.setEnabled(False)
            self.ramp_execute_button.setEnabled(False)
            return
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
        if not self._MANUAL_RAMP_ENABLED:
            self.banner.show_message("Manual source ramp is disabled in the thaTEC workflow.")
            return
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
        if not self._MANUAL_RAMP_ENABLED:
            self.banner.show_message("Manual source ramp is disabled in the thaTEC workflow.")
            return
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
        if key == "max_abs_power":
            return "> 0", limits.max_abs_power
        if key == "nplc":
            return 0.001, 25
        if key == "settle":
            return limits.point_settle_time.min, limits.point_settle_time.max
        if mode == "measure_only" and key in {"level", "compliance", "source_range"}:
            return "N/A", "N/A"
        if key == "level":
            bound = quick_control_safety_bounds(self._station_settings)[
                f"keithley.{self.channel.currentText()}.{mode}"
            ]
            return bound.minimum_text, bound.maximum_text
        if key == "compliance":
            value = limits.voltage_compliance if mode == "current" else limits.current_compliance
            return value.min, value.max
        if key == "source_range":
            return "> 0", "3 A" if mode == "current" else "40 V"
        if key == "measure_voltage_range":
            return "> 0", "40 V"
        if key == "measure_current_range":
            return "> 0", "3 A"
        return "NOT SET", "NOT SET"

    def _keithley_bounded(self, key: str, editor: QWidget) -> LimitField:
        field = LimitField(editor, *self._keithley_limit_values(key))
        field.setProperty("limitKey", key)
        for badge in (field.minimum, field.maximum):
            badge.setMinimumWidth(68)
            badge.setProperty("keithleyCompact", True)
        field.edit_button.setFixedSize(48, 28)
        field.edit_button.setText("Edit")
        if key in {"source_range", "measure_voltage_range", "measure_current_range"}:
            field.edit_button.hide()
            field.setToolTip(
                "Disable autorange and enter the requested instrument range directly. "
                "The displayed maximum is the immutable 2602A hardware ceiling."
            )
        self._limit_fields[key] = field
        return field

    def _refresh_keithley_limits(self, *_args: object) -> None:
        for key, field in self._limit_fields.items():
            field.set_limits(*self._keithley_limit_values(key))
            field.validate_and_clamp()

    def set_settings(self, settings: StationSettings) -> None:
        # Settings can be refreshed after an unrelated explicit save. The manual
        # form is an active user draft and must not be replaced by persisted
        # defaults unless the page is being constructed or the user explicitly
        # imports device values.
        self._remember_source_values()
        snapshots = dict(self._channel_form_snapshots)
        active_channel = self.channel.currentText()
        self._station_settings = settings
        self.configuration_panel.set_settings(settings)
        self.max_abs_power.setText(
            settings.keithley.safety.channels[
                active_channel
            ].lab_limits.max_abs_power
        )
        self._channel_form_snapshots = snapshots
        self._active_channel = active_channel
        self._load_form_snapshot(self._channel_form_snapshots[active_channel])
        self._refresh_keithley_limits()
        self._update_output_readiness()
        self._update_ramp_defaults(reset_values=True)

    def configure(self) -> None:
        if self._readback_pending or self._auto_enable_channel is not None or any(
            operation in self._pending_channels
            for operation in (
                "configure",
                "set_output",
                "ramp_to_zero",
                "ramp_to_level",
            )
        ):
            return
        if not self._device_is_output_ready():
            self.banner.show_message(
                "Connect and verify Keithley before applying settings. No command was sent."
            )
            return
        try:
            request = self._source_request()
        except Exception as exc:
            self.banner.show_message(f"Invalid Keithley settings: {exc}")
            return
        # This is the explicit, non-energizing configuration path. It must
        # never inherit an OUTPUT-ON continuation from another UI action.
        self._auto_enable_channel = None
        self._pending_channels["configure"] = self.channel.currentText()
        self._pending_config_modes[self.channel.currentText()] = request.mode
        self._update_output_readiness()
        self.status.emit(
            f"Keithley CH {request.channel}: applying and verifying settings with OUTPUT OFF"
        )
        self._controller.call("configure", request)

    def read_configuration_from_device(self) -> None:
        mutation_pending = self._auto_enable_channel is not None or any(
            operation in self._pending_channels
            for operation in (
                "configure",
                "set_output",
                "ramp_to_zero",
                "ramp_to_level",
            )
        )
        if self._readback_pending or mutation_pending:
            return
        if not self._device_is_output_ready():
            self.banner.show_message(
                "Connect and verify Keithley before reading device settings."
            )
            return
        self._readback_pending = True
        self._update_output_readiness()
        self.status.emit(
            "Keithley: reading complete channel A/B configuration (queries only)"
        )
        self._controller.call("read_configuration")

    def _show_configuration_readback(
        self,
        readback: KeithleyConfigurationReadback,
    ) -> None:
        self._remember_source_values()
        dialog = _KeithleyReadbackDialog(
            readback, dict(self._channel_form_snapshots), self
        )
        dialog.assign_requested.connect(
            lambda channel, parameter: self._assign_configuration_readback(
                readback, channel, parameter
            )
        )
        self._readback_dialog = dialog
        dialog.exec()

    def _assign_configuration_readback(
        self,
        readback: KeithleyConfigurationReadback,
        channel: str,
        parameter: str,
    ) -> None:
        channels = {
            item.channel: item for item in readback.channels
        }
        targets = ("A", "B") if channel == "ALL" else (channel,)
        for target in targets:
            hardware = channels[target]
            snapshot = self._channel_form_snapshots[target]
            source_dimension = (
                DIMENSION_CURRENT
                if hardware.source_mode == "current"
                else DIMENSION_VOLTAGE
            )
            compliance_dimension = (
                DIMENSION_VOLTAGE
                if hardware.source_mode == "current"
                else DIMENSION_CURRENT
            )
            changes: dict[str, object] = {}
            assignments = {
                "Source mode": ("source_mode", hardware.source_mode),
                "Source level": (
                    "source_level",
                    format_quantity_auto(hardware.source_level_si, source_dimension),
                ),
                "Compliance limit": (
                    "compliance",
                    format_quantity_auto(hardware.compliance_si, compliance_dimension),
                ),
                "Source autorange": ("source_autorange", hardware.source_autorange),
                "Active source range": (
                    "source_range",
                    "AUTO" if hardware.source_autorange else format_quantity_auto(
                        hardware.source_range_si, source_dimension
                    ),
                ),
                "Sense mode": ("sense_mode", hardware.sense_mode),
                "NPLC": ("nplc", f"{hardware.nplc:.9g}"),
                "Measure V autorange": (
                    "measure_voltage_autorange",
                    hardware.measure_voltage_autorange,
                ),
                "Active measure V range": (
                    "measure_voltage_range",
                    "AUTO" if hardware.measure_voltage_autorange else format_quantity_auto(
                        hardware.measure_voltage_range_v, DIMENSION_VOLTAGE
                    ),
                ),
                "Measure I autorange": (
                    "measure_current_autorange",
                    hardware.measure_current_autorange,
                ),
                "Active measure I range": (
                    "measure_current_range",
                    "AUTO" if hardware.measure_current_autorange else format_quantity_auto(
                        hardware.measure_current_range_a, DIMENSION_CURRENT
                    ),
                ),
            }
            source_group = {
                "Source mode",
                "Source level",
                "Compliance limit",
                "Source autorange",
                "Active source range",
            }
            if parameter == "ALL":
                selected = assignments
            elif parameter in source_group:
                selected = {key: assignments[key] for key in source_group}
            else:
                selected = {parameter: assignments[parameter]}
            dependent_autorange = {
                "Active source range": "Source autorange",
                "Active measure V range": "Measure V autorange",
                "Active measure I range": "Measure I autorange",
            }.get(parameter)
            if dependent_autorange is not None:
                selected[dependent_autorange] = assignments[dependent_autorange]
            for field_name, value in selected.values():
                changes[field_name] = value
            self._channel_form_snapshots[target] = replace(snapshot, **changes)

        active = self.channel.currentText()
        self._active_channel = active
        self._load_form_snapshot(self._channel_form_snapshots[active])
        self._last_assignment_succeeded = False
        self.settings_assignment_requested.emit(dict(self._channel_form_snapshots))

    def readback_assignment_completed(self, succeeded: bool) -> None:
        self._last_assignment_succeeded = succeeded

    def _source_request(self) -> KeithleySourceRequest:
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
            source_range_si=self._manual_range(self.source_range.text(), level_dimension, self.source_autorange.isChecked()),
            measure_voltage_autorange=self.measure_voltage_autorange.isChecked(),
            measure_voltage_range_si=self._manual_range(self.measure_voltage_range.text(), DIMENSION_VOLTAGE, self.measure_voltage_autorange.isChecked()),
            measure_current_autorange=self.measure_current_autorange.isChecked(),
            measure_current_range_si=self._manual_range(self.measure_current_range.text(), DIMENSION_CURRENT, self.measure_current_autorange.isChecked()),
        )
        # First safety layer: reject unit mistakes and out-of-profile values in
        # the UI before any request reaches the worker or VISA session. The
        # adapter repeats the same validation as the independent second layer.
        validate_keithley_source(
            self._station_settings.keithley.safety.channels[request.channel],
            request,
        )
        return request

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
                self.request_output_off()
            return
        ready, checks = self._output_prerequisites()
        if not ready:
            self._show_output_blocked(
                "Complete the missing readiness checks:\n"
                + "\n".join(item for item in checks if item.startswith("✕"))
            )
            self._reset_output_toggle()
            return
        try:
            request = self._source_request()
        except Exception as exc:
            self._show_output_blocked(f"Invalid Keithley settings:\n{exc}")
            self._reset_output_toggle()
            return
        if request.mode == "measure_only":
            self._show_output_blocked(
                "Select Current or Voltage source mode before enabling OUTPUT."
            )
            self._reset_output_toggle()
            return
        self._auto_enable_channel = channel
        self._pending_channels["configure"] = channel
        self._pending_config_modes[channel] = request.mode
        self.output_toggle.setText("ENABLING…")
        self.output_toggle.setEnabled(False)
        self._update_output_readiness()
        self.status.emit(f"Keithley CH {channel}: validating and configuring before OUTPUT ON")
        self._controller.call("configure", request)

    def _show_output_blocked(self, detail: str) -> None:
        """Explain every local OUTPUT-ON rejection before any VISA dispatch.

        The adapter remains responsible for the final device-side safety check.
        This message covers only UI-side validation/preconditions, where no
        command has yet been submitted to the worker or instrument.
        """
        message = f"{detail}\n\nNo command was sent to Keithley."
        self.banner.show_message(f"OUTPUT ON blocked: {detail}", timeout_ms=15_000)
        self.status.emit(f"Keithley OUTPUT ON blocked: {detail}")
        QMessageBox.warning(self, "Keithley OUTPUT ON blocked", message)

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
        if operation == "read_configuration" and isinstance(
            result, KeithleyConfigurationReadback
        ):
            self._readback_pending = False
            for channel in result.channels:
                self._set_channel_output(channel.channel, channel.output_enabled)
            self._update_output_readiness()
            self.status.emit(
                "Keithley: channel A/B settings read using queries only"
            )
            self._show_configuration_readback(result)
        elif operation == "measure" and hasattr(result, "current_a"):
            measurement = result
            self._measure_pending = False
            if hasattr(measurement, "output_enabled"):
                self._set_channel_output(
                    str(measurement.channel), bool(measurement.output_enabled)
                )
            self._update_channel_measurement(measurement)
            self.readout.setText(
                f"I: {measurement.current_a * 1e3:.8g} mA   "
                f"V: {measurement.voltage_v * 1e3:.8g} mV   P: {measurement.power_w * 1e6:.8g} µW"
                + ("   COMPLIANCE" if measurement.compliance_detected else "")
                + (
                    "   HIGH-Z / FLOATING — derived V/I shown for thaTEC compatibility"
                    if not getattr(measurement, "measurement_path_connected", True)
                    else ""
                )
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
            self._update_output_readiness()
            if self._auto_enable_channel == channel:
                self._pending_channels["set_output"] = channel
                self._pending_output_enabled[channel] = True
                self.status.emit(f"Keithley CH {channel}: configuration verified; enabling OUTPUT")
                self._update_output_readiness()
                self._controller.call("set_output", (channel, True))
            else:
                self.banner.show_message(
                    f"Keithley CH {channel}: all instrument settings applied and "
                    "verified by readback; software settling time validated locally. "
                    "OUTPUT remains OFF.",
                    severity="success",
                    timeout_ms=8_000,
                )
                self.status.emit(f"Keithley CH {channel} configured while OUTPUT is OFF")
        elif operation == "set_output":
            channel = self._pending_channels.pop("set_output", self.channel.currentText())
            requested_enabled = self._pending_output_enabled.pop(channel, bool(result))
            actual_enabled = result if isinstance(result, bool) else requested_enabled
            self._auto_enable_channel = None
            self._set_channel_output(channel, actual_enabled)
            self.status.emit(
                f"Keithley CH {channel} OUTPUT {'ON' if actual_enabled else 'OFF'}"
            )
        elif operation == "ramp_to_zero":
            channel = self._pending_channels.pop("ramp_to_zero", self.channel.currentText())
            self._set_channel_output(channel, False)
            self._auto_enable_channel = None
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
        if operation == "read_configuration":
            self._readback_pending = False
            self._update_output_readiness()
        if operation == "measure":
            self._measure_pending = False
        if operation == "configure":
            channel = self._pending_channels.pop("configure", self.channel.currentText())
            self._pending_config_modes.pop(channel, None)
        if operation == "set_output":
            channel = self._pending_channels.get("set_output", self.channel.currentText())
            self._pending_output_enabled.pop(channel, None)
        if operation == "ramp_to_level":
            channel = self._pending_channels.pop("ramp_to_level", self.channel.currentText())
            self._ramp_pending = False
            self.ramp_execute_button.setText("Ramp to target")
            self._set_channel_output(channel, False)
            self._update_ramp_defaults()
        if operation in {"configure", "set_output", "ramp_to_zero"}:
            self._auto_enable_channel = None
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
            "read_configuration",
            "measure",
            "set_output",
            "ramp_to_zero",
            "ramp_to_level",
        }:
            QMessageBox.warning(self, "Keithley", error)
