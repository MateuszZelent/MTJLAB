"""Select a sweep series by fixing any combination of recorded setpoints."""

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CardWidget, FlowLayout

from .filter_choices import FilterComboBox


class SweepSeriesControls(CardWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 6)
        root.setSpacing(4)
        self.host = QWidget(self)
        self.flow = FlowLayout(self.host, needAni=False)
        self.flow.setContentsMargins(0, 0, 0, 0)
        self.flow.setHorizontalSpacing(10)
        self.flow.setVerticalSpacing(6)
        self.axis = FilterComboBox(self.host)
        self.axis.setAccessibleName("Browse spectrum series along parameter")
        self.axis.setMinimumWidth(210)
        self.axis.setMaximumWidth(330)
        self.axis.set_options(("All sweep parameters", None))
        self.axis.currentIndexChanged.connect(self._axis_changed)
        self.axis_host = self._field("Browse along", self.axis)
        self.flow.addWidget(self.axis_host)
        self.fixed = {}
        self.fields = {}
        root.addWidget(self.host)
        self.note = CaptionLabel(
            "Fix the other setpoints to browse one series. All values keeps repeated passes in acquisition order.", self
        )
        self.note.setWordWrap(True)
        root.addWidget(self.note)
        self.hide()

    def _field(self, label, combo):
        host = QWidget(self.host)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        caption = CaptionLabel(label, host)
        caption.setToolTip(label)
        layout.addWidget(caption)
        layout.addWidget(combo)
        return host

    @property
    def constraints(self):
        return tuple((key, combo.currentData()) for key, combo in self.fixed.items()
                     if key != self.axis.currentData() and combo.currentData() is not None)

    def set_catalogue(self, values, formatter):
        self.clear()
        self.axis.set_options(("All sweep parameters", None), tuple(values))
        for key, options in values.items():
            combo = FilterComboBox(self.host)
            combo.setMinimumWidth(180)
            combo.setMaximumWidth(330)
            combo.setAccessibleName(f"Fixed spectrum series parameter {key}")
            combo.set_options(("All values", None), options, lambda v, k=key: formatter(k, v))
            combo.currentIndexChanged.connect(self.changed)
            field = self._field(key, combo)
            self.flow.addWidget(field)
            self.fixed[key], self.fields[key] = combo, field
        self.setVisible(bool(values))
        self._sync_height()

    def clear(self):
        for field in self.fields.values():
            self.flow.removeWidget(field)
            field.hide()
            field.deleteLater()
        self.fixed.clear()
        self.fields.clear()
        self.axis.set_options(("All sweep parameters", None))
        self.hide()

    def reset(self):
        self.axis.blockSignals(True)
        self.axis.setCurrentIndex(0)
        self.axis.blockSignals(False)
        for combo in self.fixed.values():
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
            combo.setEnabled(True)

    def _axis_changed(self):
        axis = self.axis.currentData()
        for key, combo in self.fixed.items():
            if key == axis:
                blocked = combo.blockSignals(True)
                combo.setCurrentIndex(0)
                combo.blockSignals(blocked)
            combo.setEnabled(key != axis)
        self.changed.emit()

    def _sync_height(self):
        self.host.setMinimumHeight(self.flow.heightForWidth(max(200, self.width() - 20)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_height()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self, self._sync_height)
