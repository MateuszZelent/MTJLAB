"""Manual-control and recipe-editing UI for the Keithley 2600 module."""

# ruff: noqa: F401
from __future__ import annotations

import math
import time
from copy import deepcopy
from dataclasses import astuple, dataclass, replace
from typing import Any
from uuid import uuid4

import pyqtgraph as pg
from PySide6.QtCore import QMimeData, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QDrag, QIcon, QKeySequence, QPainter, QPalette, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QSpinBox,
    QStyledItemDelegate, QStyle, QTabWidget, QTableWidget,
    QTableWidgetItem, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    CaptionLabel, CardWidget, CheckBox, ComboBox, PrimaryPushButton, PushButton,
    StrongBodyLabel, TitleLabel,
)

from app.devices.keithley_2600 import (
    KeithleyRampRequest, KeithleySourceRequest, build_keithley_ramp_levels,
)
from app.domain.errors import ConfigurationError
from app.domain.quantities import (
    DIMENSION_CURRENT, DIMENSION_RESISTANCE, DIMENSION_TIME, DIMENSION_VOLTAGE,
    format_quantity_auto, parse_quantity,
)
from app.recipes import RecipeNode, replace_recipe_node
from app.recipes.parameter_registry import SWEEP_DIMENSIONS
from app.safety.keithley import validate_keithley_source
from app.settings.models import StationSettings
from app.ui.common import line_edit as _line
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
            parameter_layout.addWidget(QLabel(label), row, 0)
            selector = ComboBox(self)
            selector.setProperty("parameterId", parameter_id)
            selector.addItem("Bez zmian", userData="unchanged")
            selector.addItem("Ustaw", userData="set")
            if sweepable:
                selector.addItem("Sweep — ROI wymagane", userData="sweep")
            parameter_layout.addWidget(selector, row, 1)
            self.parameter_selectors[parameter_id] = selector
        output_row = 2 + len(definitions)
        parameter_layout.addWidget(QLabel("Output state"), output_row, 0)
        self.output_policy = ComboBox(self)
        self.output_policy.addItem("Bez zmian", userData="unchanged")
        self.output_policy.addItem("OUTPUT ON na początku", userData="on")
        self.output_policy.addItem("OUTPUT OFF", userData="off")
        parameter_layout.addWidget(self.output_policy, output_row, 1)
        self.open_roi_button = PrimaryPushButton("Przejdź do ROI…", self)
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
        self.hero_card = CardWidget()
        hero = self.hero_card
        hero.setObjectName("keithleyHero")
        hero_layout = QHBoxLayout(hero)
        title = TitleLabel("Keithley 2600 — Dual-channel SMU")
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
        self.device_state = StrongBodyLabel("DISCONNECTED")
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
        workflow = CardWidget()
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
        ramp_panel = CardWidget()
        ramp_panel.setObjectName("keithleyRampPanel")
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
            ramp_form.addWidget(QLabel(label), 0, column)
            ramp_form.addWidget(widget, 1, column)
        ramp_layout.addLayout(ramp_form)
        ramp_actions = QHBoxLayout()
        self.ramp_preview_button = PushButton("Preview ramp")
        self.ramp_execute_button = PrimaryPushButton("Ramp to target")
        self.ramp_preview = QLabel("Preview the ramp before execution.")
        self.ramp_preview.setWordWrap(True)
        self.ramp_preview.setObjectName("muted")
        ramp_actions.addWidget(self.ramp_preview_button)
        ramp_actions.addWidget(self.ramp_execute_button)
        ramp_actions.addWidget(self.ramp_preview, 1)
        ramp_layout.addLayout(ramp_actions)
        source_layout.addWidget(ramp_panel)
        buttons = QHBoxLayout()
        measure = PushButton("Measure selected channel")
        self.output_toggle = PushButton("OUTPUT OFF")
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

    def _build_keithley_history_panel(self, channel: str) -> CardWidget:
        panel = CardWidget()
        panel.setObjectName("keithleyChannelCard")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(7, 6, 7, 6)
        panel_layout.setSpacing(4)
        header = QHBoxLayout()
        title = StrongBodyLabel(f"CHANNEL {channel} — rolling 30 s history")
        title.setObjectName("keithleyHistoryTitle")
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
        )

    def _clear_keithley_history(self, channel: str) -> None:
        self._measurement_history[channel].clear()
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
        compliance = QLabel("COMPLIANCE: clear")
        compliance.setObjectName("keithleyComplianceClear")
        select = PushButton(f"Select CH {channel}")
        select.setProperty("compact", True)
        select.clicked.connect(lambda _checked=False, ch=channel: self.channel.setCurrentText(ch))
        measure = PushButton(f"Measure CH {channel}")
        measure.setProperty("compact", True)
        measure.clicked.connect(lambda _checked=False, ch=channel: self.request_measurement(ch))
        output_action = PushButton("OUTPUT OFF")
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
        semantic_state = {
            "VERIFIED": "verified",
            "OUTPUT_OFF": "verified",
            "OUTPUT_ON": "active",
            "COMPLIANCE": "compliance",
            "FAULT": "fault",
            "UNKNOWN": "fault",
        }.get(normalized, "neutral")
        for widget in (self.device_led, self.device_state):
            widget.setProperty("deviceState", semantic_state)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
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
                widgets["led"].setProperty("outputState", "neutral")
                widgets["led"].style().unpolish(widgets["led"])
                widgets["led"].style().polish(widgets["led"])
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
        widgets["led"].setProperty("outputState", "active" if enabled else "off")
        widgets["led"].style().unpolish(widgets["led"])
        widgets["led"].style().polish(widgets["led"])
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
