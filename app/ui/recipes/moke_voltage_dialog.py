"""Explicit fixed MOKE operating point, independent of baseline acquisition."""
from app.domain.quantities import DIMENSION_VOLTAGE, parse_quantity
from app.safety.moke_box import MokeVoltagePlan, control_profile_from_settings
from app.ui.recipes.fluent_dialog import FluentRecipeDialog
from qfluentwidgets import BodyLabel, ComboBox, LineEdit, PrimaryPushButton, PushButton
from PySide6.QtWidgets import QHBoxLayout


class MokeVoltageSetDialog(FluentRecipeDialog):
    def __init__(self, settings, parent=None, *, channel=None, voltage="0 mV", allow_sweep=False):
        super().__init__(parent)
        self.settings = settings
        self.simulation = (settings.moke_box.endpoint or "").startswith("SIM::MOKE")
        self.setWindowTitle("Set MOKE voltage before measurement")
        self.setMinimumSize(620, 420)
        self.resize(760, 470)
        layout = self.modal_content_layout(spacing=12)
        self.mode = ComboBox(self)
        self.mode.addItem("Fixed voltage — keep for following measurements", userData="fixed")
        if allow_sweep:
            self.mode.addItem("Sweep voltage — define ROI points", userData="sweep")
            layout.addWidget(BodyLabel("Operation"))
            layout.addWidget(self.mode)
        else:
            self.mode.hide()
        layout.addWidget(BodyLabel("Set and confirm this voltage, then keep it for the following measurements."))
        self.channel = ComboBox(self)
        for candidate in range(8):
            self.channel.addItem(f"VOUT {candidate}", userData=candidate)
            try:
                control_profile_from_settings(settings, simulation=self.simulation, channel=candidate)
            except RuntimeError:
                self.channel.setItemEnabled(candidate, False)
        self.channel.setCurrentIndex(settings.moke_box.voltage_control.channel if channel is None else channel)
        layout.addWidget(BodyLabel("Output channel"))
        layout.addWidget(self.channel)
        self.voltage = LineEdit(self)
        self.voltage.setText(voltage)
        layout.addWidget(BodyLabel("Voltage — include units, for example 5 mV"))
        layout.addWidget(self.voltage)
        self.status = BodyLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        cancel = PushButton("Cancel", self)
        self.save = PrimaryPushButton("Save operating point", self)
        cancel.setMinimumWidth(100)
        self.save.setMinimumWidth(200)
        footer.addStretch(1)
        footer.addWidget(cancel)
        footer.addWidget(self.save)
        layout.addLayout(footer)
        cancel.clicked.connect(self.reject)
        self.save.clicked.connect(self.accept)
        self.channel.currentIndexChanged.connect(self.refresh)
        self.voltage.textChanged.connect(self.refresh)
        self.mode.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def validated_data(self):
        channel = self.channel.currentData()
        profile = control_profile_from_settings(self.settings, simulation=self.simulation, channel=channel)
        value = parse_quantity(self.voltage.text(), DIMENSION_VOLTAGE).si_value
        plan = MokeVoltagePlan(profile.fingerprint, channel, profile.minimum_v, profile.maximum_v, (value,))
        plan.validate(profile)
        return {"channel": channel, "voltage": self.voltage.text().strip()}, profile, plan.applied_voltage(value)

    def refresh(self, *_):
        sweep = self.mode.currentData() == "sweep"
        self.voltage.setEnabled(not sweep)
        self.save.setText("Configure sweep ROI…" if sweep else "Save operating point")
        try:
            if sweep:
                profile = control_profile_from_settings(self.settings, simulation=self.simulation, channel=self.channel.currentData())
                self.status.setText(f"Station limits: {profile.minimum_v:g}…{profile.maximum_v:g} V. Define voltage points in the next window.")
                self.save.setEnabled(True)
                return True
            _, profile, applied = self.validated_data()
        except RuntimeError as exc:
            self.status.setText(str(exc))
            self.save.setEnabled(False)
            return False
        self.status.setText(f"Station limits: {profile.minimum_v:g}…{profile.maximum_v:g} V. DAC target: {applied:g} V.")
        self.save.setEnabled(True)
        return True

    def accept(self):
        if self.refresh():
            super().accept()
