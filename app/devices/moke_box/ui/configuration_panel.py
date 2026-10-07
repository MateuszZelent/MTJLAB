"""MOKE version of Keithley's source configuration block.

The form spacing, wrapping, bounded editor and range popup use the same
controls and layout as KeithleyConfigurationPanel.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QSizePolicy, QVBoxLayout
from qfluentwidgets import CaptionLabel, CardWidget, ComboBox, StrongBodyLabel

from app.domain.quantities import DIMENSION_VOLTAGE, parse_quantity
from app.domain.errors import SafetyViolation
from app.safety.moke_box import MokeVoltagePlan
from app.ui.common import line_edit
from app.ui.widgets import LimitEditDialog, LimitField
from app.ui.widgets.quantity_step_selector import VoltageStepSelector


class MokeVoltageConfigurationPanel(CardWidget):
    range_changed = Signal(str, str)
    validation_failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mokeConfigurationPanel")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.minimum_text, self.maximum_text = "-0.5 V", "0.5 V"
        self.profile = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        title = StrongBodyLabel("Source and measurement configuration")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.form = QFormLayout()
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.form.setVerticalSpacing(4)
        self.form.setHorizontalSpacing(8)
        self.channel = ComboBox()
        for channel in range(8):
            self.channel.addItem(f"VOUT {channel}", userData=channel)
        self.channel.setCurrentIndex(2)
        self.channel.setToolTip("Select the output configuration draft. Selecting a channel does not change its voltage.")
        self.level = line_edit("0 mV")
        self.level.setProperty("precisionStep", "1 mV")
        self.level.setToolTip("Up/Down use the selected Step (default 1 mV). Live OFF requires Apply voltage.")
        self.level.setAccessibleName("Programming voltage")
        self.voltage_step = VoltageStepSelector(self)
        self.level_field = LimitField(self.level, self.minimum_text, self.maximum_text,
                                     range_mode=True, editor_accessory=self.voltage_step)
        self.level_field.setProperty("limitKey", "level")
        self.level.setMinimumWidth(140)
        for badge in (self.level_field.minimum, self.level_field.maximum):
            badge.setMinimumWidth(88)
            badge.setProperty("keithleyCompact", True)
        self.level_field.edit_requested.connect(self.edit_limits)
        self.calculated_field = StrongBodyLabel("Calculated field: no active calibration", self)
        self.calculated_field.setWordWrap(True)
        self.calculated_field.hide()  # The workflow hosts this readout in the adjacent field column.
        for label, widget in (
            ("Configure channel (not OUTPUT)", self.channel),
            ("Source voltage", self.level_field),
        ):
            self.form.addRow(label, widget)
        layout.addLayout(self.form)
        self.profile_summary = CaptionLabel("Connect a qualified MOKE output profile.", self)
        self.profile_summary.setWordWrap(True)
        layout.addWidget(self.profile_summary)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "form"):
            self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows if self.width() < 720 else QFormLayout.RowWrapPolicy.WrapLongRows)

    def _validate_operator_limits(self, minimum, maximum):
        lo = parse_quantity(minimum, DIMENSION_VOLTAGE).si_value
        hi = parse_quantity(maximum, DIMENSION_VOLTAGE).si_value
        if lo >= hi:
            raise ValueError("Operator minimum must be smaller than maximum.")
        if self.profile is not None:
            if not self.profile.minimum_v <= lo < hi <= self.profile.maximum_v:
                raise SafetyViolation(f"Operator limits must stay within the VOUT {self.profile.channel} station range "
                                 f"[{self.profile.minimum_v:g}, {self.profile.maximum_v:g}] V. "
                                 "Edit the approved station profile in Settings to change that range.")
            MokeVoltagePlan(self.profile.fingerprint, self.profile.channel, lo, hi, (lo,)).validate(self.profile)

    def set_operator_limits(self, minimum, maximum):
        self._validate_operator_limits(minimum, maximum)
        self.minimum_text, self.maximum_text = minimum, maximum
        self.level_field.set_limits(minimum, maximum)
        self.range_changed.emit(minimum, maximum)

    def edit_limits(self):
        limits = (f" Station VOUT {self.profile.channel}: {self.profile.minimum_v:g} to {self.profile.maximum_v:g} V."
                  if self.profile is not None else "")
        dialog = LimitEditDialog("MOKE source voltage", self.minimum_text, self.maximum_text,
                                 guidance="Set operator min/max in V or mV within the qualified station range. "
                                          "Editing this range sends no hardware command." + limits,
                                 validate=self._validate_operator_limits, parent=self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        try:
            self.set_operator_limits(dialog.minimum.text(), dialog.maximum.text())
        except (RuntimeError, ValueError) as exc:
            self.validation_failed.emit(str(exc))
