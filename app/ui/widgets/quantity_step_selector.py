"""Explicit voltage step shared by MOKE control surfaces."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ComboBox

from app.domain.quantities import DIMENSION_VOLTAGE, parse_quantity


class VoltageStepSelector(QWidget):
    step_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(CaptionLabel("Step", self))
        self.combo = ComboBox(self)
        self.combo.setAccessibleName("Voltage arrow step")
        self.combo.setToolTip("Step for voltage arrow keys and +/− controls, independent of written precision. Changing the step does not change the voltage.")
        for text in ("0.1 mV", "0.5 mV", "1 mV", "2 mV", "5 mV", "10 mV", "20 mV", "50 mV", "100 mV"):
            self.combo.addItem(text)
        self.combo.setCurrentText("1 mV")
        self.combo.setMinimumWidth(100)
        layout.addWidget(self.combo)
        self.combo.currentTextChanged.connect(self.step_changed)

    def step_text(self):
        return self.combo.currentText()

    def set_step_text(self, text):
        if parse_quantity(text, DIMENSION_VOLTAGE).si_value <= 0:
            raise ValueError("Voltage step must be positive.")
        if self.combo.findText(text) < 0:
            self.combo.addItem(text)
        blocked = self.combo.blockSignals(True)
        self.combo.setCurrentText(text)
        self.combo.blockSignals(blocked)
