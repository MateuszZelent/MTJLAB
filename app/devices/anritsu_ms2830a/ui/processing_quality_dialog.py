"""Processing evidence and existing background tools in one Fluent dialog."""

from datetime import UTC, datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox, PushButton, ScrollArea, StrongBodyLabel

from app.domain.quantities import DIMENSION_POWER, format_quantity_auto
from app.ui.dialogs import StationDialog


class ProcessingQualityDialog(StationDialog):
    def __init__(self, page):
        super().__init__(page, resizable=True)
        self.page = page
        self.workspace = page.correction_workspace
        self.setWindowTitle("Spectrum processing quality")
        self.setMinimumSize(440, 380)
        self.resize(660, 620)
        outer = self.modal_content_layout(spacing=12)
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(scroll)
        root = QVBoxLayout(content)
        root.setSpacing(12)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        root.addWidget(StrongBodyLabel("Power average → correction → display filters", self))
        self.evidence = BodyLabel(self)
        self.evidence.setWordWrap(True)
        root.addWidget(self.evidence)
        reset = PushButton("Reset average for a new operating point", self)
        reset.clicked.connect(page._reset_preview_average)
        root.addWidget(reset)
        root.addWidget(StrongBodyLabel("Background model", self))
        self.model = ComboBox(self)
        self.model.setAccessibleName("Background drift model for all previews")
        self.model.currentIndexChanged.connect(self._select_model)
        root.addWidget(self.model)
        self.status = BodyLabel(self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.buttons = []
        for label, callback in (
            ("Inspect background stability…", self.workspace._open_reference_diagnostics),
            ("Train drift model…", self.workspace._open_model_training),
            ("Validate on held-out recordings…", self.workspace._open_model_validation),
            ("Load a saved drift model…", self.workspace._load_interference),
        ):
            button = PushButton(label, self)
            button.clicked.connect(lambda _checked=False, action=callback: action(self.page))
            self.buttons.append(button)
            root.addWidget(button)
        note = BodyLabel("Diagnostics report stability and correlation; they do not certify sweep independence. "
                         "A drift model requires a matching background and independently qualified signal-free control bands. "
                         "The source operating point remains under your control.", self)
        note.setWordWrap(True)
        root.addWidget(note)
        root.addStretch()
        actions = QHBoxLayout()
        close = PushButton("Close", self)
        close.clicked.connect(self.accept)
        actions.addStretch()
        actions.addWidget(close)
        outer.addLayout(actions)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.finished.connect(self._timer.stop)
        self.refresh()

    def _select_model(self, index):
        if index >= 0:
            self.workspace.interference_mode.setCurrentIndex(index)

    def refresh(self):
        workspace = self.workspace
        stats = self.page._preview_statistics
        profile = workspace._profile
        lines = ["Preview standard uncertainty: not qualified."]
        if stats is not None:
            lines.insert(0, f"Power average: {stats.frame_count}/{stats.target_frames} received frames over {stats.duration_s:.3g} s.")
            if stats.median_temporal_scatter_w is not None:
                scatter = format_quantity_auto(stats.median_temporal_scatter_w, DIMENSION_POWER)
                lines.append(f"Median temporal scatter: {scatter} (before averaging; descriptive, not a standard error).")
            if stats.reference_mean_uncertainty_w is not None:
                value = format_quantity_auto(stats.reference_mean_uncertainty_w, DIMENSION_POWER)
                lines.append(f"Archived reference contribution: {value}; shared by all signal frames.")
        if profile is not None:
            age = max(0., datetime.now(UTC).timestamp() - profile.completed_at_s)
            lines.append(f"Background: {profile.sweep_count} sweeps; signal absence "
                         f"{'qualified' if profile.signal_free_qualified else 'unqualified'}. "
                         f"Age: {age:.0f} s. Result is relative to the recorded operating point.")
        bands = self.page._analysis_parameters.narrow_protected_regions_hz
        lines.append(f"User-protected signal bands: {len(bands)}. Outlier scales are not detection confidence.")
        calibration = workspace.interference_mode.currentData()
        if calibration is not None:
            lines.append(f"The selected background model also protects {int(calibration.protected_mask.sum())} signal bins.")
        self.evidence.setText("\n\n".join(lines))
        source = workspace.interference_mode
        self.model.blockSignals(True)
        labels = [source.itemText(i) for i in range(source.count())]
        if [self.model.itemText(i) for i in range(self.model.count())] != labels:
            self.model.clear()
            self.model.addItems(labels)
        self.model.setCurrentIndex(source.currentIndex())
        self.model.blockSignals(False)
        idle = not workspace.running and not workspace._profile_io_busy
        self.model.setEnabled(idle and profile is not None)
        for button in self.buttons:
            button.setEnabled(idle)
        self.buttons[-1].setEnabled(idle and profile is not None)
        self.status.setText(workspace.interference_status.text())
