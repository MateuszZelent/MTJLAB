"""Rigol-specific recipe node editor."""

# ruff: noqa: F401
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CardWidget, CheckBox, ComboBox, PrimaryPushButton, PushButton, ScrollArea,
)
from app.ui.dialogs import StationMessageBox as QMessageBox

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
        heading = BodyLabel("Rigol DG1032Z · Carrier and output")
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        description = BodyLabel(
            "The complete carrier snapshot is stored in the recipe. Select one of "
            "Frequency, HighL or LowL as a local ROI axis when required."
        )
        description.setWordWrap(True)
        description.setObjectName("recipeHint")
        layout.addWidget(description)
        content = QSplitter(Qt.Orientation.Horizontal)
        carrier = CardWidget(self)
        form = QFormLayout(carrier)
        self.channel = ComboBox(self)
        self.channel.addItem("Channel 1", userData=1)
        self.channel.addItem("Channel 2", userData=2)
        channel_index = self.channel.findData(snapshot.channel)
        self.channel.setCurrentIndex(channel_index if channel_index >= 0 else 0)
        form.addRow("Channel", self.channel)
        self.waveform = ComboBox(self)
        self.waveform.addItems(("SIN", "SQU", "RAMP", "PULS", "NOIS", "USER", "DC"))
        self.waveform.setCurrentText(snapshot.waveform)
        self.time_mode = ComboBox(self)
        self.time_mode.addItems(("Frequency", "Period"))
        self.frequency = _line(snapshot.frequency)
        try:
            frequency_hz = parse_quantity(snapshot.frequency, DIMENSION_FREQUENCY).si_value
            period_text = f"{1 / frequency_hz:.12g} s" if frequency_hz > 0 else "1 ms"
        except Exception:
            period_text = "1 ms"
        self.period = _line(period_text)
        self.level_mode = ComboBox(self)
        self.level_mode.addItems(("High Level / Low Level", "Amplitude / Offset"))
        self.high_level = _line(snapshot.high_level)
        self.low_level = _line(snapshot.low_level)
        try:
            high_v = parse_quantity(snapshot.high_level, DIMENSION_VOLTAGE).si_value
            low_v = parse_quantity(snapshot.low_level, DIMENSION_VOLTAGE).si_value
            amplitude_text = self._format_voltage(high_v - low_v)
            offset_text = self._format_voltage((high_v + low_v) / 2)
        except Exception:
            amplitude_text, offset_text = "2 mV", "0 V"
        self.vpp = _line(amplitude_text)
        self.offset = _line(offset_text)
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
            ("Time representation", self.time_mode),
            ("Frequency", self.frequency),
            ("Period", self.period),
            ("Voltage entry mode", self.level_mode),
            ("HighL", self.high_level),
            ("LowL", self.low_level),
            ("Amplitude (Vpp)", self.vpp),
            ("Offset / DC level", self.offset),
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
        self.carrier_form = form
        carrier_scroll = ScrollArea(self)
        carrier_scroll.setWidgetResizable(True)
        carrier_scroll.setFrameShape(QFrame.Shape.NoFrame)
        carrier_scroll.setWidget(carrier)
        content.addWidget(carrier_scroll)
        actions_frame = CardWidget(self)
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
        note = BodyLabel(
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
        self.waveform.currentTextChanged.connect(self._waveform_changed)
        self.time_mode.currentTextChanged.connect(self._dynamic_controls_changed)
        self.level_mode.currentTextChanged.connect(self._dynamic_controls_changed)
        self.frequency.editingFinished.connect(self._sync_period_from_frequency)
        self.period.editingFinished.connect(self._sync_frequency_from_period)
        self.high_level.editingFinished.connect(self._sync_vpp_offset_from_levels)
        self.low_level.editingFinished.connect(self._sync_vpp_offset_from_levels)
        self.vpp.editingFinished.connect(self._sync_levels_from_vpp_offset)
        self.offset.editingFinished.connect(self._sync_levels_from_vpp_offset)
        layout.addLayout(footer)
        self.load_plan_actions(parameter_actions or [])
        self._selection_changed()
        self._waveform_changed(self.waveform.currentText())

    def _sync_dc_level(self) -> None:
        if self.waveform.currentText() == "DC":
            self.high_level.setText(self.offset.text())
            self.low_level.setText(self.offset.text())

    @staticmethod
    def _format_voltage(value_v: float) -> str:
        if 0 < abs(value_v) < 1:
            return f"{value_v * 1e3:.12g} mV"
        return f"{value_v:.12g} V"

    def _sync_vpp_offset_from_levels(self) -> None:
        try:
            high = parse_quantity(self.high_level.text(), DIMENSION_VOLTAGE).si_value
            low = parse_quantity(self.low_level.text(), DIMENSION_VOLTAGE).si_value
        except Exception:
            return
        self.vpp.setText(self._format_voltage(high - low))
        self.offset.setText(self._format_voltage((high + low) / 2))

    def _sync_levels_from_vpp_offset(self) -> None:
        try:
            offset = parse_quantity(self.offset.text(), DIMENSION_VOLTAGE).si_value
            amplitude = (
                0.0
                if self.waveform.currentText() == "DC"
                else parse_quantity(self.vpp.text(), DIMENSION_VOLTAGE).si_value
            )
        except Exception:
            return
        self.high_level.setText(self._format_voltage(offset + amplitude / 2))
        self.low_level.setText(self._format_voltage(offset - amplitude / 2))

    def _sync_period_from_frequency(self) -> None:
        try:
            frequency = parse_quantity(self.frequency.text(), DIMENSION_FREQUENCY).si_value
            if frequency > 0:
                self.period.setText(f"{1 / frequency:.12g} s")
        except Exception:
            return

    def _sync_frequency_from_period(self) -> None:
        try:
            period = parse_quantity(self.period.text(), DIMENSION_TIME).si_value
            if period > 0:
                self.frequency.setText(f"{1 / period:.12g} Hz")
        except Exception:
            return

    def _dynamic_controls_changed(self, *_args: object) -> None:
        self._waveform_changed(self.waveform.currentText())

    def _waveform_changed(self, waveform: str) -> None:
        is_dc = waveform == "DC"
        has_time = waveform not in {"DC", "NOIS"}
        high_low_mode = self.level_mode.currentText() == "High Level / Low Level"
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
            self.duty: waveform == "SQU",
            self.symmetry: waveform == "RAMP",
            self.pulse_width: waveform == "PULS",
            self.pulse_leading: waveform == "PULS",
            self.pulse_trailing: waveform == "PULS",
        }
        for widget, visible in visibility.items():
            self.carrier_form.setRowVisible(widget, visible)
        high_label = self.carrier_form.labelForField(self.high_level)
        if high_label is not None:
            high_label.setText("DC level" if is_dc else "HighL")
        if is_dc:
            self._sync_dc_level()
        for selector in self.parameter_selectors.values():
            selector.setEnabled(not is_dc)
            if is_dc:
                selector.setCurrentIndex(selector.findData("set"))

    def selected_channel(self) -> int:
        return int(self.channel.currentData())

    def selected_output_policy(self) -> str:
        return str(self.output_policy.currentData())

    def configuration_snapshot(self) -> RigolConfigurationSnapshot:
        if self.waveform.currentText() == "DC":
            high_level = self.offset.text().strip()
        elif self.level_mode.currentText() == "Amplitude / Offset":
            self._sync_levels_from_vpp_offset()
            high_level = self.high_level.text().strip()
        else:
            high_level = self.high_level.text().strip()
        low_level = (
            high_level
            if self.waveform.currentText() == "DC"
            else self.low_level.text().strip()
        )
        return RigolConfigurationSnapshot(
            channel=self.selected_channel(),
            waveform=self.waveform.currentText(),
            frequency=self.frequency.text().strip(),
            high_level=high_level,
            low_level=low_level,
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
        if self.level_mode.currentText() == "Amplitude / Offset":
            self._sync_levels_from_vpp_offset()
        high_level = self.high_level.text().strip()
        values = {
            "carrier.frequency": self.frequency.text().strip(),
            "carrier.high_level": high_level,
            "carrier.low_level": (
                high_level
                if self.waveform.currentText() == "DC"
                else self.low_level.text().strip()
            ),
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
