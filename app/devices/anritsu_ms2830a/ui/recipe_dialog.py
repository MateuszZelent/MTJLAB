"""Anritsu-specific recipe node editors."""

# ruff: noqa: F401
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel,
    CardWidget, CheckBox, ComboBox, LineEdit, PrimaryPushButton, PushButton,
    ScrollArea, SegmentedWidget, SpinBox,
)
from app.ui.dialogs import StationMessageBox as QMessageBox

from app.devices.anritsu_ms2830a import AnritsuConfigurationSnapshot, SignalGeneratorSnapshot
from app.devices.anritsu_ms2830a.ui.page import (
    AnritsuAdvancedSpectrumPanel,
    AnritsuSpectrumConfigurationPanel,
)
from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_DBM, DIMENSION_FREQUENCY, parse_quantity
from app.recipes import RecipeNode
from app.settings.models import StationSettings
from app.ui.common import line_edit as _line
from app.ui.recipes.sweep_editor import SweepGeneratorDialog
from app.ui.recipes.fluent_dialog import FluentRecipeDialog


class AnritsuNodeEditorDialog(FluentRecipeDialog):
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
        self.setProperty("stationSurface", "page")
        self.plan_mode = True
        self.hardware_actions_enabled = False
        self._working_segments: dict[str, list[dict[str, object]]] = {}
        self.setWindowTitle("Anritsu MS2830A — configure sweep node")
        self.resize(1180, 760)
        self.setMinimumSize(680, 480)
        layout = QVBoxLayout(self)
        heading = BodyLabel("Anritsu MS2830A · Spectrum analyser")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        self.content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter.setChildrenCollapsible(False)
        left = CardWidget(self)
        left.setObjectName("recipeEditorParameters")
        left_layout = QVBoxLayout(left)
        parameter_tabs = QStackedWidget(left)
        parameter_routes = SegmentedWidget(left)
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
        parameter_tabs.addWidget(self.configuration_panel)
        parameter_tabs.addWidget(self.advanced_panel)
        parameter_routes.addItem("spectrum", "Spectrum setup")
        parameter_routes.addItem("advanced", "Bandwidth & input path")
        parameter_routes.setCurrentItem("spectrum")
        parameter_routes.currentItemChanged.connect(
            lambda route: parameter_tabs.setCurrentIndex(0 if route == "spectrum" else 1)
        )
        left_layout.addWidget(parameter_routes)
        left_layout.addWidget(parameter_tabs)
        self.content_splitter.addWidget(left)
        right = CardWidget(self)
        right.setObjectName("recipeEditorParameters")
        right_layout = QGridLayout(right)
        title = BodyLabel("Node role and controlled parameters")
        title.setObjectName("sectionTitle")
        right_layout.addWidget(title, 0, 0, 1, 2)
        self.node_role = ComboBox(self)
        self.node_role.addItem("Apply spectrum settings only", userData="configure")
        self.node_role.addItem(
            "Apply settings, then acquire spectrum", userData="acquire_spectrum"
        )
        self.node_role.addItem(
            "Apply settings, then acquire reference", userData="acquire_reference"
        )
        right_layout.addWidget(BodyLabel("Node role"), 1, 0)
        right_layout.addWidget(self.node_role, 1, 1)
        self.acquisition_hint = CaptionLabel(
            "Acquisition is stored as a separate child step so its role remains visible in the tree.",
            self,
        )
        self.acquisition_hint.setWordWrap(True)
        self.acquisition_hint.setObjectName("muted")
        right_layout.addWidget(self.acquisition_hint, 2, 0, 1, 2)
        self.average_count = SpinBox(self)
        self.average_count.setRange(1, 9999)
        self.average_count.setValue(1)
        self.average_count_label = BodyLabel("Average complete spectra")
        right_layout.addWidget(self.average_count_label, 3, 0)
        right_layout.addWidget(self.average_count, 3, 1)
        self.reference_operation = ComboBox(self)
        for label, value in (
            ("None — raw spectrum", "none"),
            ("Difference in dB", "difference_db"),
            ("Linear ratio", "ratio_linear"),
            ("Add power", "add_power"),
            ("Subtract power", "subtract_power"),
            ("Multiply linear", "multiply_linear"),
        ):
            self.reference_operation.addItem(label, userData=value)
        self.reference_operation_label = BodyLabel("Reference processing")
        right_layout.addWidget(self.reference_operation_label, 4, 0)
        right_layout.addWidget(self.reference_operation, 4, 1)
        self.parameter_selectors: dict[str, ComboBox] = {}
        for row, (parameter_id, label, sweepable) in enumerate(
            self.parameter_specs, start=5
        ):
            right_layout.addWidget(BodyLabel(label), row, 0)
            selector = ComboBox(self)
            selector.addItem("Unchanged", userData="unchanged")
            selector.addItem("Set", userData="set")
            if sweepable:
                selector.addItem("Sweep — ROI required", userData="sweep")
            selector.currentIndexChanged.connect(self._selection_changed)
            self.parameter_selectors[parameter_id] = selector
            right_layout.addWidget(selector, row, 1)
        operation_row = len(self.parameter_specs) + 5
        self.trace = ComboBox(self)
        self.trace.addItems(("TRAC1",))
        self.trace_label = BodyLabel("Trace")
        right_layout.addWidget(self.trace_label, operation_row + 1, 0)
        right_layout.addWidget(self.trace, operation_row + 1, 1)
        self.open_roi_button = PrimaryPushButton("Go to ROI…", self)
        self.open_roi_button.setEnabled(False)
        self.open_roi_button.clicked.connect(self._open_roi)
        right_layout.addWidget(
            self.open_roi_button, operation_row + 2, 0, 1, 2
        )
        note = BodyLabel(
            "The complete visible core spectrum snapshot is stored and applied. Set and "
            "Sweep expose explicit plan rows; advanced settings are applied only when "
            "selected. Spectrum acquisition is a separate Acquire spectrum once block "
            "placed inside the loop."
        )
        note.setObjectName("recipeHint")
        note.setWordWrap(True)
        right_layout.addWidget(note, operation_row + 3, 0, 1, 2)
        right_layout.setRowStretch(operation_row + 4, 1)
        right_scroll = ScrollArea(self)
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setWidget(right)
        self.content_splitter.addWidget(right_scroll)
        self.content_splitter.setSizes([570, 390])
        layout.addWidget(self.content_splitter, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = PushButton("Cancel", self)
        self.apply_button = PrimaryPushButton("Apply Anritsu node", self)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.apply_button)
        self.apply_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self.node_role.currentIndexChanged.connect(self._role_changed)
        layout.addLayout(footer)
        self._role_changed()

    def _role_changed(self) -> None:
        role = self.selected_node_role()
        acquisition = role != "configure"
        spectrum = role == "acquire_spectrum"
        for widget in (
            self.acquisition_hint,
            self.average_count_label,
            self.average_count,
            self.trace_label,
            self.trace,
        ):
            widget.setVisible(acquisition)
        self.reference_operation_label.setVisible(spectrum)
        self.reference_operation.setVisible(spectrum)
        self.apply_button.setText(
            {
                "configure": "Apply spectrum settings",
                "acquire_spectrum": "Apply settings + add spectrum",
                "acquire_reference": "Apply settings + add reference",
            }[role]
        )
        self.setWindowTitle(
            {
                "configure": "Anritsu MS2830A — Spectrum settings",
                "acquire_spectrum": "Anritsu MS2830A — settings and spectrum acquisition",
                "acquire_reference": "Anritsu MS2830A — settings and reference acquisition",
            }[role]
        )

    def selected_node_role(self) -> str:
        return str(self.node_role.currentData() or "configure")

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
        self.node_role.setCurrentIndex(
            self.node_role.findData("acquire_spectrum" if acquire_single else "configure")
        )
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
        try:
            snapshot = self.configuration_panel.configuration_snapshot()
            if snapshot.start_hz >= snapshot.stop_hz:
                raise ConfigurationError(
                    "Anritsu stop frequency must be greater than start frequency."
                )
            actions = self.planned_parameter_actions()
            if any(
                str(action.get("parameter_id", "")).startswith("advanced.")
                for action in actions
            ):
                self.advanced_panel.configuration()
        except Exception as exc:
            QMessageBox.warning(self, "Anritsu node", str(exc))
            return
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
        super().accept()


class AnritsuSignalGeneratorNodeEditorDialog(FluentRecipeDialog):
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
        output_policy: str = "unchanged",
    ) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        self.setWindowTitle("Anritsu MS2830A — signal generator sweep node")
        self.resize(620, 420)
        self.setMinimumSize(520, 360)
        self._working_segments: dict[str, list[dict[str, object]]] = {}
        layout = QVBoxLayout(self)
        heading = BodyLabel("Anritsu MS2830A · Signal generator")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        form = QGridLayout()
        self.frequency = LineEdit(self)
        self.frequency.setText(frequency)
        self.power = LineEdit(self)
        self.power.setText(power)
        self.parameter_selectors: dict[str, ComboBox] = {}
        for row, (parameter_id, label, _dimension) in enumerate(
            self.parameter_specs
        ):
            field = self.frequency if parameter_id == "sg.frequency" else self.power
            selector = ComboBox(self)
            selector.addItem("Unchanged", userData="unchanged")
            selector.addItem("Set", userData="set")
            selector.addItem("Sweep — ROI required", userData="sweep")
            selector.currentIndexChanged.connect(self._selection_changed)
            self.parameter_selectors[parameter_id] = selector
            form.addWidget(BodyLabel(label), row, 0)
            form.addWidget(field, row, 1)
            form.addWidget(selector, row, 2)
        self.output_policy = ComboBox(self)
        self.output_policy.addItem(
            "Keep RF OUTPUT OFF (safe default)", userData="unchanged"
        )
        self.output_policy.addItem(
            "RF OUTPUT ON for this block · OFF on exit", userData="on"
        )
        self.output_policy.addItem(
            "RF OUTPUT ON and keep confirmed ON", userData="on_keep"
        )
        self.output_policy.addItem(
            "Continue confirmed RF OUTPUT ON · live sweep", userData="continue"
        )
        self.output_policy.addItem("Force RF OUTPUT OFF", userData="off")
        output_index = self.output_policy.findData(output_policy)
        self.output_policy.setCurrentIndex(output_index if output_index >= 0 else 0)
        form.addWidget(BodyLabel("RF output"), 2, 0)
        form.addWidget(self.output_policy, 2, 1, 1, 2)
        layout.addLayout(form)
        self.open_roi_button = PrimaryPushButton("Edit ROI…", self)
        self.open_roi_button.setEnabled(False)
        self.open_roi_button.clicked.connect(self._open_roi)
        layout.addWidget(self.open_roi_button)
        note = BodyLabel(
            "The complete visible SG snapshot is stored and applied with RF OFF; "
            "Unchanged still uses the visible value, Set exposes an explicit row, and "
            "Sweep defines the ROI axis. Safe ON configures and verifies with RF OFF "
            "before energizing. Continuous mode is accepted only after this recipe has "
            "already configured and confirmed RF OUTPUT ON."
        )
        note.setObjectName("recipeHint")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = PushButton("Cancel", self)
        self.apply_button = PrimaryPushButton(
            "Apply signal generator settings · RF OFF", self
        )
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.apply_button)
        self.apply_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        layout.addLayout(footer)
        self.load_plan_actions(parameter_actions or [])

    def _selected_sweep_parameter(self) -> str | None:
        selected = [
            parameter_id
            for parameter_id, selector in self.parameter_selectors.items()
            if selector.currentData() == "sweep"
        ]
        return selected[0] if len(selected) == 1 else None

    def selected_output_policy(self) -> str:
        return str(self.output_policy.currentData() or "unchanged")

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
        super().accept()
