"""Explicit per-channel state after a successful measurement."""
from PySide6.QtWidgets import QFormLayout, QHBoxLayout
from qfluentwidgets import BodyLabel, ComboBox, LineEdit, PrimaryPushButton, PushButton

from app.ui.recipes.fluent_dialog import FluentRecipeDialog


class FinalStateDialog(FluentRecipeDialog):
    def __init__(self, parent=None, *, initial=None, validate=None):
        super().__init__(parent)
        initial = initial or {}
        self._validate = validate
        self._zero_mode = False
        self._hold_voltage = str(initial.get("voltage", "0 V"))
        self.setWindowTitle("State after successful completion")
        self.setMinimumSize(680, 470)
        self.resize(780, 560)
        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=12)
        description = BodyLabel(
            "Choose the final state of one output channel. This block runs only after all measurements succeed. "
            "Stop, faults and E-STOP use emergency shutdown. Keeping OUTPUT ON requires that it is already ON in the recipe.", surface)
        description.setWordWrap(True)
        layout.addWidget(description)
        self.form = QFormLayout()
        self.form.setSpacing(12)
        self.device = ComboBox(surface)
        for label, value in (("MOKE Box", "moke_box"), ("Keithley 2600", "keithley"), ("Rigol DG1032Z", "rigol")):
            self.device.addItem(label, userData=value)
        self.channel = ComboBox(surface)
        self.output = ComboBox(surface)
        self.output.addItem("OFF / MOKE return to zero and disarm", userData="off")
        self.output.addItem("Hold voltage / keep confirmed OUTPUT ON", userData="hold")
        self.form.addRow("Device", self.device)
        self.form.addRow("Output channel", self.channel)
        self.form.addRow("After completion", self.output)
        self.fields = {}
        for key, label in (("voltage", "MOKE programming voltage"), ("level", "Keithley source level (mA or V)"),
                           ("frequency", "Rigol frequency"), ("high_level", "Rigol high level"), ("low_level", "Rigol low level")):
            edit = LineEdit(surface)
            edit.setPlaceholderText("Leave empty to keep the last planned value" if key != "voltage" else "Include units, for example 5 mV")
            edit.setText(str(initial.get(key, "")))
            self.fields[key] = edit
            self.form.addRow(label, edit)
        layout.addLayout(self.form)
        self.status = BodyLabel(surface)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch(1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = PushButton("Cancel", surface)
        self.save = PrimaryPushButton("Save final state", surface)
        footer.addWidget(cancel)
        footer.addWidget(self.save)
        layout.addLayout(footer)
        cancel.clicked.connect(self.reject)
        self.save.clicked.connect(self.accept)
        self.device.currentIndexChanged.connect(self._device_changed)
        self.channel.currentIndexChanged.connect(self._refresh)
        self.output.currentIndexChanged.connect(self._refresh)
        self.device.setCurrentIndex(max(0, self.device.findData(initial.get("device", "moke_box"))))
        self._device_changed()
        self.channel.setCurrentIndex(max(0, self.channel.findData(initial.get("channel", 0))))
        self.output.setCurrentIndex(max(0, self.output.findData(initial.get("output", "off"))))
        for key, edit in self.fields.items():
            edit.setText(str(initial.get(key, "")))
        self._refresh()

    def _device_changed(self, *_):
        self.channel.clear()
        device = self.device.currentData()
        channels = range(8) if device == "moke_box" else ("A", "B") if device == "keithley" else (1, 2)
        for channel in channels:
            label = f"VOUT {channel}" if device == "moke_box" else f"Channel {channel}"
            self.channel.addItem(label, userData=channel)
        self._refresh()

    def _refresh(self, *_):
        device = self.device.currentData()
        visible = {"moke_box": {"voltage"}, "keithley": {"level"}, "rigol": {"frequency", "high_level", "low_level"}}[device]
        for key, edit in self.fields.items():
            self.form.setRowVisible(edit, key in visible)
        zero = device == "moke_box" and self.output.currentData() == "off"
        self.setWindowTitle(f"MOKE Box VOUT {self.channel.currentData()} — final state" if device == "moke_box"
                            else "State after successful completion")
        self.output.setItemText(0, "Ramp to 0 V and disarm" if device == "moke_box" else "OUTPUT OFF")
        self.output.setItemText(1, "Hold requested voltage" if device == "moke_box" else "Keep confirmed OUTPUT ON")
        self.fields["voltage"].setEnabled(not zero)
        if zero:
            if not self._zero_mode:
                self._hold_voltage = self.fields["voltage"].text()
            self.fields["voltage"].setText("0 V")
        elif self._zero_mode:
            self.fields["voltage"].setText(self._hold_voltage)
        self._zero_mode = zero
        self.status.setText("MOKE OFF means the qualified zero target. Choose Hold to leave a nonzero voltage." if zero else
                            "Values are validated against the recipe configuration and station/DUT limits before saving.")

    def node_fields(self):
        device = self.device.currentData()
        result = {"device": device, "channel": self.channel.currentData(), "output": self.output.currentData()}
        keys = {"moke_box": ("voltage",), "keithley": ("level",), "rigol": ("frequency", "high_level", "low_level")}[device]
        for key in keys:
            value = self.fields[key].text().strip()
            if value:
                result[key] = value
        if device == "moke_box" and result["output"] == "off":
            result["voltage"] = "0 V"
        return result

    def accept(self):
        try:
            if self._validate is not None:
                self._validate(self.node_fields())
        except (RuntimeError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        super().accept()
