"""Lazy, explicit stored-mean / individual-repeat choices for recorded baselines."""

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CardWidget, ComboBox, FlowLayout, PushButton

from .filter_choices import FilterComboBox


def baseline_sample_label(reference):
    if reference.selected_sweep is not None:
        return f"repeat {reference.selected_sweep + 1}/{reference.collection_average_count}"
    return f"stored mean · {reference.average_count} sweep(s)"


class BaselineSampleCombo(FilterComboBox):
    """Virtual choices avoid creating thousands of Qt items for timed backgrounds."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(220)
        self.setAccessibleName("Stored baseline mean or individual repeat")
        self.set_reference(None)

    def set_reference(self, reference, selected=None):
        blocked = self.blockSignals(True)
        count = len(reference.source_sweep_indices) if reference is not None else 0
        mean_label = (f"Stored mean · {reference.average_count} sweep(s)" if reference is not None
                      else "Stored mean — choose collection")
        self.set_options((mean_label, None), tuple(range(count)),
                         lambda ordinal: f"Individual repeat {ordinal + 1} / {count}")
        self.setCurrentIndex(max(0, self.findData(selected)))
        self.setToolTip("The stored mean averages linear power. Individual repeats use the original recorded raw dBm."
            if count else "Individual repeats are unavailable here. Select an explicit collection with recorded source sweeps; no repeats are reconstructed from its mean.")
        self.blockSignals(blocked)


class RecordedBaselineControls(CardWidget):
    show_requested = Signal(int, object)
    measurement_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.references = {}
        self.viewing = False
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 6)
        self.strip = QWidget(self)
        self.flow = FlowLayout(self.strip, needAni=False)
        self.flow.setContentsMargins(0, 0, 0, 0)
        self.flow.setHorizontalSpacing(8)
        self.flow.setVerticalSpacing(6)
        self.flow.addWidget(CaptionLabel("Recorded background / reference", self.strip))
        self.collection = ComboBox(self.strip)
        self.collection.setFixedWidth(230)
        self.collection.setAccessibleName("Recorded baseline collection to view")
        self.flow.addWidget(self.collection)
        self.sample = BaselineSampleCombo(self.strip)
        self.flow.addWidget(self.sample)
        self.show_button = PushButton("Show baseline spectrum", self.strip)
        self.show_button.clicked.connect(self._show)
        self.flow.addWidget(self.show_button)
        self.measurement_button = PushButton("Return to measurement", self.strip)
        self.measurement_button.clicked.connect(self.measurement_requested.emit)
        self.flow.addWidget(self.measurement_button)
        self.measurement_button.hide()
        root.addWidget(self.strip)
        self.note = CaptionLabel("", self)
        self.note.setWordWrap(True)
        root.addWidget(self.note)
        self.collection.currentIndexChanged.connect(self._collection_changed)
        self.sample.currentIndexChanged.connect(self._sample_changed)
        self.set_references(())

    def set_references(self, references):
        self.viewing = False
        self.references = {reference.index: reference for reference in references}
        blocked = self.collection.blockSignals(True)
        self.collection.clear()
        for reference in self.references.values():
            self.collection.addItem(f"{reference.purpose.title()} {reference.index} · {reference.average_count} sweep(s)",
                                    userData=reference.index)
        self.collection.blockSignals(blocked)
        self.show_button.setEnabled(bool(self.references))
        self.setVisible(bool(self.references))
        self._collection_changed()

    def select(self, index, sweep=None):
        blocked = self.collection.blockSignals(True)
        self.collection.setCurrentIndex(self.collection.findData(index))
        self.collection.blockSignals(blocked)
        self.sample.set_reference(self.references.get(index), sweep)
        self._note()

    def set_viewing(self, viewing):
        self.viewing = viewing
        self.measurement_button.setVisible(viewing)

    def _collection_changed(self, *_args):
        self.sample.set_reference(self.references.get(self.collection.currentData()))
        self._note()
        if self.viewing:
            self._show()

    def _sample_changed(self, *_args):
        self._note()
        if self.viewing:
            self._show()

    def _note(self):
        reference = self.references.get(self.collection.currentData())
        if reference is None:
            self.note.setText("No recorded background/reference in this archive.")
        elif reference.source_sweep_indices:
            self.note.setText("View the stored mean or an original repeat. These viewing choices do not change the separate baseline selected for subtraction below.")
        else:
            self.note.setText("Only the stored baseline is available; this archive does not contain its individual repeats.")

    def _show(self):
        index = self.collection.currentData()
        if index is not None:
            self.show_requested.emit(index, self.sample.currentData())

    def _sync_height(self):
        self.strip.setMinimumHeight(self.flow.heightForWidth(max(200, self.width() - 20)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_height()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self, self._sync_height)
