"""Shared acquisition processing controls for standalone and managed sweep steps."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CheckBox, ComboBox, LineEdit, PushButton

from app.recipes.spectrum_processing import parse_processing, processing_mapping
from app.ui.dialogs import StationFileDialog


class RecipeSpectrumOptions(QWidget):
    changed = Signal()

    def __init__(self, parent=None, *, reference_only=False, fields=None):
        super().__init__(parent)
        self._reference_only = reference_only
        self.source_unit = "dBm"
        _modes, self._parameters = parse_processing(None)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.source = ComboBox(self)
        for label, value in (
            ("Acquire at this recipe step", "acquire"),
            ("Load saved reference", "reference"),
            ("Load saved background", "background"),
        ):
            self.source.addItem(label, userData=value)
        root.addWidget(self.source)
        self.file_row = QWidget(self)
        row = QHBoxLayout(self.file_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.path = LineEdit(self.file_row)
        self.path.setPlaceholderText("Reference/background HDF5 file")
        self.browse = PushButton("Browse…", self.file_row)
        row.addWidget(self.path, 1)
        row.addWidget(self.browse)
        root.addWidget(self.file_row)
        self.filters_row = QWidget(self)
        row = QHBoxLayout(self.filters_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.filters = {}
        for key, label in (
            ("narrow_reject", "Narrow peaks"),
            ("emi_reject", "EMI lines"),
            ("denoise", "Denoise"),
        ):
            checkbox = CheckBox(label, self.filters_row)
            checkbox.toggled.connect(self.changed)
            row.addWidget(checkbox)
            self.filters[key] = checkbox
        root.addWidget(self.filters_row)
        self.parameters = PushButton("Filter settings and signal protection…", self)
        root.addWidget(self.parameters)
        self.note = BodyLabel(self)
        self.note.setWordWrap(True)
        root.addWidget(self.note)
        self.source.currentIndexChanged.connect(self._refresh)
        self.source.currentIndexChanged.connect(self.changed)
        self.path.textChanged.connect(self.changed)
        self.browse.clicked.connect(self._browse)
        self.parameters.clicked.connect(self._edit_parameters)
        self.load_fields(fields or {})

    def load_fields(self, fields):
        modes, self._parameters = parse_processing(fields.get("processing"))
        for key, checkbox in self.filters.items():
            checkbox.setChecked(key in modes)
        self.path.setText(str(fields.get("source_file", "")))
        kind = fields.get("file_kind", "reference") if fields.get("source_file") else "acquire"
        self.source.setCurrentIndex(self.source.findData(kind))
        self._refresh()

    def set_reference_only(self, reference_only):
        self._reference_only = reference_only
        self._refresh()

    def _refresh(self, *_args):
        self.source.setVisible(self._reference_only)
        self.file_row.setVisible(self._reference_only and self.source.currentData() != "acquire")
        self.filters_row.setVisible(not self._reference_only)
        self.parameters.setVisible(not self._reference_only)
        self.note.setText(
            "Reference/background is taken at this point in the recipe. Set the source operating point in earlier steps; outputs need not be OFF. Imported files must match analyzer settings."
            if self._reference_only
            else "Add Acquire reference before this step to record or load background/reference after configuring the analyzer. Each point averages its own sweeps, then applies correction and filters. RAW remains archived. EMI needs enough sweeps within this point; settings are independent of manual Live."
        )

    def node_fields(self):
        if self._reference_only:
            if self.source.currentData() == "acquire":
                return {}
            if not self.path.text().strip():
                raise ValueError("Choose a saved reference/background file.")
            return {"source_file": self.path.text().strip(), "file_kind": self.source.currentData()}
        modes = [key for key, box in self.filters.items() if box.isChecked()]
        if not modes and self._parameters == parse_processing(None)[1]:
            return {}
        return {"processing": processing_mapping(modes, self._parameters)}

    def _browse(self):
        path, _ = StationFileDialog.getOpenFileName(
            self, "Load reference/background", "", "HDF5 (*.h5 *.hdf5)"
        )
        if path:
            self.path.setText(path)

    def _edit_parameters(self):
        from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import (
            SpectrumAnalysisSettingsDialog,
        )

        dialog = SpectrumAnalysisSettingsDialog(
            self,
            current_parameters=self._parameters,
            section="filters",
            allow_temporal_average=False,
            source_unit=self.source_unit,
        )
        dialog.parameters_applied.connect(self._parameters_applied)
        dialog.exec()
        dialog.deleteLater()

    def _parameters_applied(self, parameters):
        self._parameters = parameters
        self.changed.emit()
