"""Offline overlays using the same power and correction pipeline as Live."""

from dataclasses import dataclass

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, CardWidget, CheckBox, ComboBox, FlowLayout, PushButton

from app.spectrum.comparison_view import convert_power_view, display_state
from app.spectrum.display_model import SpectrumDisplayTrace
from .processing import RecordedBaselineEvidence, ResultProcessing, ResultSpectrumProcessor
from .baseline_controls import BaselineSampleCombo, baseline_sample_label


@dataclass(frozen=True, slots=True)
class SpectrumView:
    show_analysis: bool = True
    show_raw: bool = False
    show_background: bool = False
    show_reference: bool = False
    background_index: int | None = None
    reference_index: int | None = None
    unit: str = "auto"
    background_sweep: int | None = None
    reference_sweep: int | None = None
    show_background_spectrum: bool = False
    show_reference_spectrum: bool = False

    @property
    def active(self):
        return (not self.show_analysis or self.show_raw or self.show_background or self.show_reference
                or self.show_background_spectrum or self.show_reference_spectrum or self.unit != "auto")


@dataclass(frozen=True, slots=True)
class SpectrumViewPayload:
    traces: tuple[SpectrumDisplayTrace, ...]
    notes: tuple[str, ...]
    baselines: tuple = ()


def _build_view(session, checkpoint, frequencies, values, unit, points, processing, view, cancelled):
    def check():
        if cancelled is not None and cancelled():
            raise InterruptedError("Result comparison cancelled.")
    check()
    traces, notes, baselines = [], [], []
    def add(key, label, result):
        baselines.extend(baseline for baseline in result.baselines if baseline not in baselines)
        notes.extend(note for note in result.notes if note not in notes)
        traces.append(SpectrumDisplayTrace(key, label, result.frequencies_hz, result.values,
                                          result.unit, checkpoint, (result.method,)))
    if view.show_analysis:
        processor = ResultSpectrumProcessor(session.path, processing, points, reader=session, cancelled=cancelled)
        add("analysis", "Analysis" if processing.active else "Raw", processor.process(checkpoint, frequencies, values, unit))
    if view.show_raw and (processing.active or not view.show_analysis):
        traces.append(SpectrumDisplayTrace("raw", "Raw", tuple(frequencies), tuple(values), unit, checkpoint, ("recorded raw",)))
    for enabled, key, index, sweep, operation, label in (
        (view.show_background, "background_difference", view.background_index, view.background_sweep, "subtract_power_signed", "Raw − background"),
        (view.show_reference, "reference_difference", view.reference_index, view.reference_sweep, "subtract_reference_signed", "Raw − reference"),
    ):
        if not enabled:
            continue
        check()
        processor = ResultSpectrumProcessor(session.path, ResultProcessing(operation, index, reference_sweep=sweep), points,
                                            reader=session, cancelled=cancelled)
        add(key, label, processor.process(checkpoint, frequencies, values, unit))
    for purpose, enabled, index, sweep, operation in (
        ("background", view.show_background_spectrum, view.background_index, view.background_sweep, "subtract_power_signed"),
        ("reference", view.show_reference_spectrum, view.reference_index, view.reference_sweep, "subtract_reference_signed"),
    ):
        if not enabled:
            continue
        check()
        processor = ResultSpectrumProcessor(session.path, ResultProcessing(operation, index, reference_sweep=sweep),
            points, reader=session, cancelled=cancelled)
        reference, compatibility_notes = processor.resolve_reference(checkpoint, frequencies, unit)
        evidence = RecordedBaselineEvidence.from_reference(reference)
        if evidence not in baselines:
            baselines.append(evidence)
        notes.extend(note for note in compatibility_notes if note not in notes)
        notes.append(f"{purpose.title()} {reference.index}: {baseline_sample_label(reference)}.")
        traces.append(SpectrumDisplayTrace(f"{purpose}_spectrum", f"{purpose.title()} spectrum",
            reference.frequencies_hz, reference.powers_dbm, "dBm", checkpoint,
            (f"Recorded {purpose} {reference.index} · {baseline_sample_label(reference)}",)))
    if not traces:
        traces.append(SpectrumDisplayTrace("raw", "Raw", tuple(frequencies), tuple(values), unit, checkpoint, ("recorded raw",)))
    units = {trace.unit for trace in traces}
    target_unit = view.unit
    if len(units) > 1:
        if not units <= {"dBm", "W"}:
            raise ValueError("Overlay traces must share a physical dimension. Disable power comparisons for a dB ratio or non-power analysis.")
        if target_unit == "auto":
            target_unit = "W"
            notes.append("Power overlays share W so signed residuals and raw power use one axis.")
    state, note = convert_power_view(display_state(traces, checkpoint), target_unit)
    if note:
        notes.append(note)
    check()
    return SpectrumViewPayload(state.traces, tuple(notes), tuple(baselines))


def read_private_view(session, point, processing, view, *, cancelled=None):
    point = session.point(point.index)
    trace = session.spectrum(session.path, point.index)
    if trace is None:
        raise ValueError("No raw spectrum at this checkpoint.")
    return _build_view(session, point.index, trace.frequencies_hz, trace.powers_dbm, "dBm",
                       (point,), processing, view, cancelled)


def read_public_view(session, spectrum, trace_index, points, processing, view, *, cancelled=None):
    return _build_view(session, spectrum.checkpoint, spectrum.x_values,
                       spectrum.traces[trace_index].values, spectrum.y_unit, points,
                       processing, view, cancelled)


def read_baseline_view(reference, processing, view, *, cancelled=None):
    """Inspect a mean or an original repeat without treating it as a checkpoint."""
    from app.spectrum.preview_processing import SpectrumPreviewProcessor
    from app.spectrum.display_processing import clean_display_spectrum
    if processing.operation != "none" or view.show_background or view.show_reference:
        raise ValueError("Choose a measurement checkpoint for background/reference correction and comparisons.")
    if processing.parameters.temporal_average_frames > 1:
        raise ValueError("This is an already averaged recorded baseline. Same-point power averaging needs a measurement checkpoint's individual source sweeps.")
    if cancelled is not None and cancelled():
        raise InterruptedError("Baseline view cancelled.")
    cleaned, _ = SpectrumPreviewProcessor().process(reference.powers_dbm,
        frequencies_hz=reference.frequencies_hz, unit="dBm", modes=processing.modes,
        parameters=processing.parameters, cleaner=clean_display_spectrum)
    trace = SpectrumDisplayTrace("baseline", f"{reference.purpose.title()} spectrum",
        tuple(reference.frequencies_hz), cleaned.values, cleaned.unit, reference.index,
        (f"Recorded {reference.purpose} {reference.index} · {baseline_sample_label(reference)}", cleaned.method))
    state, note = convert_power_view(display_state((trace,), reference.index), view.unit)
    evidence = RecordedBaselineEvidence.from_reference(reference)
    details = f"Recorded {reference.purpose} {reference.index} · {baseline_sample_label(reference)} · {reference.acquired_at_utc or 'time unavailable'}"
    return SpectrumViewPayload(state.traces, (details,) + cleaned.notes + ((note,) if note else ()), (evidence,))


class SpectrumViewControls(CardWidget):
    changed = Signal(object)
    tools_requested = Signal()
    floating_requested = Signal()
    peaks_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._references = {}
        self._baseline_view = False
        self._sample_collections = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 6)
        self.strip = QWidget(self)
        self.flow = FlowLayout(self.strip, needAni=False)
        self.flow.setContentsMargins(0, 0, 0, 0)
        self.flow.setHorizontalSpacing(8)
        self.flow.setVerticalSpacing(6)
        self.flow.addWidget(CaptionLabel("Compare / view", self.strip))
        self.boxes = {}
        for key, label in (("show_analysis", "Analysis"), ("show_raw", "Raw"),
                           ("show_background", "Raw − BG"), ("show_reference", "Raw − Ref"),
                           ("show_background_spectrum", "Background spectrum"), ("show_reference_spectrum", "Reference spectrum")):
            box = CheckBox(label, self.strip)
            box.setChecked(key == "show_analysis")
            box.toggled.connect(self._changed)
            self.flow.addWidget(box)
            self.boxes[key] = box
        self.baselines = {}
        self.samples = {}
        for purpose in ("background", "reference"):
            combo = ComboBox(self.strip)
            combo.setFixedWidth(165)
            combo.setAccessibleName(f"Overlay recorded {purpose}")
            combo.addItem(f"Automatic {purpose}", userData=None)
            combo.currentIndexChanged.connect(self._changed)
            self.flow.addWidget(combo)
            self.baselines[purpose] = combo
            sample = BaselineSampleCombo(self.strip)
            sample.setAccessibleName(f"{purpose.title()} mean or individual repeat for comparison")
            sample.currentIndexChanged.connect(self._changed)
            self.flow.addWidget(sample)
            self.samples[purpose] = sample
        self.unit = ComboBox(self.strip)
        for label, key in (("Auto units", "auto"), ("W — linear power", "W"), ("dBm — log power", "dBm")):
            self.unit.addItem(label, userData=key)
        self.unit.setToolTip("dBm is logarithmic absolute power. Non-positive residuals appear as gaps; select W to retain their signs.")
        self.unit.currentIndexChanged.connect(self._changed)
        self.flow.addWidget(self.unit)
        for label, signal in (("Axes / markers / band…", self.tools_requested),
                              ("Peak table / tracking…", self.peaks_requested),
                              ("Floating window", self.floating_requested)):
            button = PushButton(label, self.strip)
            button.clicked.connect(signal.emit)
            self.flow.addWidget(button)
        self.reset = PushButton("Reset view", self.strip)
        self.reset.clicked.connect(self.reset_view)
        self.flow.addWidget(self.reset)
        root.addWidget(self.strip)
        note = CaptionLabel("Analysis uses Post-processing. Other curves use unfiltered RAW and the explicit mean/repeat choices here. Background spectrum / Reference spectrum show the baseline itself.", self)
        note.setWordWrap(True)
        root.addWidget(note)
        self._sync_baselines()

    @property
    def state(self):
        return SpectrumView(**{key: box.isChecked() for key, box in self.boxes.items()},
            background_index=self.baselines["background"].currentData(),
            reference_index=self.baselines["reference"].currentData(), unit=self.unit.currentData(),
            background_sweep=self.samples["background"].currentData(),
            reference_sweep=self.samples["reference"].currentData())

    def set_references(self, references):
        self._references = {reference.index: reference for reference in references}
        for purpose, combo in self.baselines.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(f"Automatic {purpose}", userData=None)
            for reference in self._references.values():
                if reference.purpose == purpose:
                    combo.addItem(f"{purpose.title()} {reference.index} · {reference.average_count} sweep(s)", userData=reference.index)
            combo.blockSignals(False)
        for key, box in self.boxes.items():
            box.blockSignals(True)
            box.setChecked(key == "show_analysis")
            box.blockSignals(False)
        self.unit.blockSignals(True)
        self.unit.setCurrentIndex(0)
        self.unit.blockSignals(False)
        for purpose, sample in self.samples.items():
            sample.set_reference(None)
        self._sample_collections = {purpose: None for purpose in self.baselines}
        self._sync_baselines()

    def _sync_baselines(self):
        for purpose, combo in self.baselines.items():
            enabled = not self._baseline_view and (self.boxes[f"show_{purpose}"].isChecked()
                                                   or self.boxes[f"show_{purpose}_spectrum"].isChecked())
            combo.setEnabled(enabled)
            self.samples[purpose].setEnabled(enabled and combo.currentData() in self._references)
        for box in self.boxes.values():
            box.setEnabled(not self._baseline_view)
        self.reset.setEnabled(self.state.active)

    def reset_view(self):
        for key, box in self.boxes.items():
            box.blockSignals(True)
            box.setChecked(key == "show_analysis")
            box.blockSignals(False)
        for control in (*self.baselines.values(), self.unit):
            control.blockSignals(True)
            control.setCurrentIndex(0)
            control.blockSignals(False)
        self._changed()

    def _changed(self, *_args):
        for purpose, combo in self.baselines.items():
            index = combo.currentData()
            if index != self._sample_collections.get(purpose):
                self.samples[purpose].set_reference(self._references.get(index))
                self._sample_collections[purpose] = index
        self._sync_baselines()
        self.changed.emit(self.state)

    def set_baseline_view(self, enabled):
        self._baseline_view = enabled
        self._sync_baselines()

    def _sync_height(self):
        self.strip.setMinimumHeight(self.flow.heightForWidth(max(200, self.width() - 20)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_height()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self, self._sync_height)
