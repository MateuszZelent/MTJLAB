"""Rigol-specific recipe node editor."""

# ruff: noqa: F401
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    CheckBox, ComboBox, PrimaryPushButton, PushButton,
)

from app.devices.rigol_dg1000z.ui.page import RigolConfigurationSnapshot
from app.devices.rigol_dg1000z import RigolChannelConfig
from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_FREQUENCY, DIMENSION_RESISTANCE, DIMENSION_TIME, DIMENSION_VOLTAGE, parse_quantity
from app.recipes import RecipeNode
from app.safety.rigol_current import validate_rigol_waveform
from app.settings.models import StationSettings
from app.ui.common import line_edit as _line
from app.ui.recipes import SweepGeneratorDialog
from app.ui.recipes.fluent_dialog import FluentRecipeDialog


class RigolNodeEditorDialog(FluentRecipeDialog):
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
        self.setProperty("stationSurface", "page")
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
        self.channel = ComboBox(self)
        self.channel.addItem("Channel 1", userData=1)
        self.channel.addItem("Channel 2", userData=2)
        channel_index = self.channel.findData(snapshot.channel)
        self.channel.setCurrentIndex(channel_index if channel_index >= 0 else 0)
        form.addRow("Channel", self.channel)
        self.waveform = ComboBox(self)
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
        self.output_polarity = ComboBox(self)
        self.output_polarity.addItems(("NORM", "INV"))
        self.output_polarity.setCurrentText(snapshot.output_polarity)
        self.output_mode = ComboBox(self)
        self.output_mode.addItems(("NORM", "GAT"))
        self.output_mode.setCurrentText(snapshot.output_mode)
        self.gate_polarity = ComboBox(self)
        self.gate_polarity.addItems(("NORM", "INV"))
        self.gate_polarity.setCurrentText(snapshot.gate_polarity)
        self.sync_enabled = CheckBox("SYNC enabled", self)
        self.sync_enabled.setChecked(snapshot.sync_enabled)
        self.sync_polarity = ComboBox(self)
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
        self.parameter_selectors: dict[str, ComboBox] = {}
        for parameter_id, label in (
            ("carrier.frequency", "Frequency"),
            ("carrier.high_level", "HighL"),
            ("carrier.low_level", "LowL"),
        ):
            selector = ComboBox(self)
            selector.addItem("Set fixed", userData="set")
            selector.addItem("Sweep — ROI required", userData="sweep")
            selector.currentIndexChanged.connect(self._selection_changed)
            self.parameter_selectors[parameter_id] = selector
            actions_layout.addRow(label, selector)
        self.open_roi_button = PrimaryPushButton("Edit selected ROI…", self)
        self.open_roi_button.setEnabled(False)
        self.open_roi_button.clicked.connect(self._open_roi)
        actions_layout.addRow(self.open_roi_button)
        self.output_policy = ComboBox(self)
        self.output_policy.addItem("Leave OUTPUT unchanged", userData="unchanged")
        self.output_policy.addItem("Switch OUTPUT ON", userData="on")
        self.output_policy.addItem("Switch OUTPUT OFF", userData="off")
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
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.apply_button = PrimaryPushButton("Apply Rigol configuration", self)
        self.cancel_button = PushButton("Cancel", self)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.apply_button)
        self.apply_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        layout.addLayout(footer)
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


