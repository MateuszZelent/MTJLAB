"""Operator annotation of saved points; no instrument access."""

from PySide6.QtWidgets import QFormLayout, QHBoxLayout
from qfluentwidgets import BodyLabel, LineEdit, SpinBox, TextEdit, PushButton, PrimaryPushButton

from app.ui.dialogs import StationDialog
from app.devices.keithley_2600.characterization.observations import save_observation


class ObservationDialog(StationDialog):
    def __init__(self, curve, author="", parent=None):
        super().__init__(parent, resizable=True)
        self.curve = curve
        self.saved_path = None
        self.setWindowTitle("Annotate saved characterization")
        self.setModal(True)
        self.resize(640, 560)
        layout = self.modal_content_layout(spacing=12)
        surface = self.modal_shell.surface
        label = BodyLabel(f"Field item {curve.index + 1}, B = {curve.current_a:g} A. Select source indices (zero-based).", surface)
        label.setWordWrap(True)
        layout.addWidget(label)
        form = QFormLayout()
        self.first = SpinBox(surface)
        self.last = SpinBox(surface)
        for control in (self.first, self.last):
            control.setRange(0, len(curve.dataset.points) - 1)
        self.last.setValue(len(curve.dataset.points) - 1)
        self.author = LineEdit(surface)
        self.author.setText(author)
        self.description = TextEdit(surface)
        self.description.setPlaceholderText("Describe what is visible in the measured data")
        self.hypothesis = LineEdit(surface)
        self.hypothesis.setPlaceholderText("Optional mechanism hypothesis — unverified")
        for title, control in (("First point", self.first), ("Last point", self.last),
                               ("Author", self.author), ("Observation", self.description),
                               ("Hypothesis", self.hypothesis)):
            form.addRow(title, control)
        layout.addLayout(form)
        self.error = BodyLabel("", surface)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = PushButton("Cancel", surface)
        cancel.clicked.connect(self.reject)
        save = PrimaryPushButton("Save observation", surface)
        save.clicked.connect(self._save)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    def _save(self):
        try:
            self.saved_path = save_observation(self.curve, self.first.value(), self.last.value(),
                author=self.author.text(), description=self.description.toPlainText(), hypothesis=self.hypothesis.text())
        except (ValueError, OSError) as exc:
            self.error.setText(str(exc))
            return
        self.accept()
