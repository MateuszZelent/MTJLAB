"""Fluent controls for the shared spectrum correction and plot workflow."""

from __future__ import annotations

import math

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    FlowLayout,
    FluentIcon,
    LineEdit,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    SpinBox,
    StrongBodyLabel,
    SwitchButton,
    TransparentToolButton,
)

from app.devices.anritsu_ms2830a import (
    ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS,
    ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS,
)
from app.domain.quantities import (
    DIMENSION_DB,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_POWER,
    DIMENSION_TIME,
    format_quantity_auto,
    parse_quantity,
)
from app.spectrum.processing import REFERENCE_OPERATIONS
from app.spectrum.stripe_correction import (
    AutomaticStripeSettings,
    FrequencyBand,
    StripeBands,
    parse_frequency_bands,
)
from app.ui.dialogs import StationDialog


class AveragingTargetControls(QWidget):
    """Choose an averaging length by count or by seconds."""

    def __init__(self, parent=None, *, initial_frames=200, allow_instrument=False):
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.backend = ComboBox(self)
        self.backend.setAccessibleName("Averaging source")
        self.backend.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.backend.setMinimumWidth(110)
        self.backend.addItem("PyLab Live", userData="application")
        if allow_instrument:
            self.backend.addItem("Anritsu · Lin Average", userData="instrument")
            layout.addWidget(self.backend, 0, 0, 1, 2)
        else:
            self.backend.hide()
        self.mode = ComboBox(self)
        self.mode.setAccessibleName("Averaging target mode")
        self.mode.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.mode.setMinimumWidth(84)
        self.mode.addItem("Frames", userData="frames")
        self.mode.addItem("Seconds", userData="seconds")
        self.frames = SpinBox(self)
        self.frames.setAccessibleName("Averaging frame count")
        self.frames.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.frames.setMinimumWidth(84)
        self.frames.setRange(1, 2_147_483_647)
        self.frames.setValue(initial_frames)
        self.seconds = SpinBox(self)
        self.seconds.setAccessibleName("Averaging duration in seconds")
        self.seconds.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.seconds.setMinimumWidth(84)
        self.seconds.setRange(1, 2_147_483)  # QTimer takes signed 32-bit milliseconds.
        self.seconds.setValue(60)
        self.seconds.setSuffix(" s")
        self._target_backend = "application"
        self._targets = {"application": (initial_frames, 60), "instrument": (initial_frames, 5)}
        layout.addWidget(self.mode, 1, 0)
        layout.addWidget(self.frames, 1, 1)
        layout.addWidget(self.seconds, 1, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        self.mode.currentIndexChanged.connect(self._sync_mode)
        self.backend.currentIndexChanged.connect(self._sync_mode)
        self._sync_mode()

    def _sync_mode(self):
        timed = self.mode.currentData() == "seconds"
        backend = self.backend.currentData()
        instrument = backend == "instrument"
        if self._target_backend != backend:
            self._targets[self._target_backend] = self.frames.value(), self.seconds.value()
            self._target_backend = backend
            if instrument:
                self.frames.setRange(ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS, ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS)
            else:
                self.frames.setRange(1, 2_147_483_647)
            frames, seconds = self._targets[backend]
            self.frames.setValue(frames)
            self.seconds.setValue(seconds)
        self.mode.setItemText(self.mode.findData("frames"), "Sweeps" if instrument else "Frames")
        self.frames.setAccessibleName("Averaging sweep count" if instrument else "Averaging frame count")
        self.frames.setToolTip("Complete sweeps per block" if instrument else "Live frames per average")
        self.seconds.setToolTip("Target based on the analyzer's nominal sweep time" if instrument else "Time window in seconds")
        self.frames.setVisible(not timed)
        self.seconds.setVisible(timed)

    def target(self) -> tuple[int, int | None]:
        if self.mode.currentData() == "seconds":
            return 0, self.seconds.value()
        return self.frames.value(), None


class StripeSwitch(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.toggle = SwitchButton(self)
        self.toggle.setOffText("Stripes")
        self.toggle.setOnText("Stripes")
        self.toggle.setAccessibleName("Enable Live stripe correction")
        self.toggle.indicator.setAccessibleName("Enable Live stripe correction")
        self.toggle.setToolTip("Detect and reduce narrow lines from a matching recorded background.")
        row.addWidget(self.toggle)
        self.settings = TransparentToolButton(FluentIcon.SETTING, self)
        self.settings.setAccessibleName("Open Stripes settings")
        self.settings.setToolTip("Stripes settings")
        row.addWidget(self.settings)


class StripeCorrectionControls(QWidget):
    """Inspect automatic Stripes and configure optional manual bands."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        description = CaptionLabel(
            "Automatic Stripes uses a matching background recorded with the signal of interest absent. "
            "With Subtract [dB], Corrected matches stripe levels to the reference. "
            "Reconstruct bands estimates the trace in stripe bands from neighboring frequencies, including fluctuating lines. "
            "Mask bands shows gaps at stripe frequencies and respects protected bands.", self)
        description.setWordWrap(True)
        layout.addWidget(description)
        grid = QGridLayout()
        self.mode = ComboBox(self)
        self.mode.addItem("Automatic from background", userData="automatic")
        self.mode.addItem("Manual bands", userData="manual")
        self.bands = PlainTextEdit(self)
        self.bands.setPlaceholderText("799.7 MHz .. 823.3 MHz")
        self.bands.setFixedHeight(74)
        self.protected = PlainTextEdit(self)
        self.protected.setPlaceholderText("Optional")
        self.protected.setFixedHeight(57)
        self.shoulder = LineEdit(self)
        self.shoulder.setText("5 MHz")
        self.view = ComboBox(self)
        for label, value in (("Corrected", "corrected"), ("Reconstruct bands", "reconstructed"), ("Mask bands", "masked"),
                             ("Input", "input"), ("Removed", "removed")):
            self.view.addItem(label, userData=value)
        self._manual_fields = []
        for row, (title, control) in enumerate((
            ("Mode", self.mode), ("Spectrum view", self.view),
            ("Stripe bands", self.bands), ("Shoulder on each side", self.shoulder),
            ("Protected bands", self.protected),
        )):
            control.setAccessibleName(title)
            label = BodyLabel(title, self)
            grid.addWidget(label, row, 0)
            grid.addWidget(control, row, 1)
            if control in (self.bands, self.shoulder):
                self._manual_fields.extend((label, control))
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        self.bands.setToolTip("Paste frequency pairs separated by semicolons or line breaks.")
        self.protected.setToolTip("Exclude these frequency bands from subtraction and from the baseline fit.")
        self.status = CaptionLabel("Stripe correction is off.", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.detected_bands = PlainTextEdit(self)
        self.detected_bands.setAccessibleName("Automatically detected stripe bands")
        self.detected_bands.setReadOnly(True)
        self.detected_bands.setFixedHeight(74)
        self.detected_bands.hide()
        self._detected: tuple[FrequencyBand, ...] = ()
        layout.addWidget(self.detected_bands)
        self.error = CaptionLabel(self)
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        actions = QHBoxLayout()
        self.background_setup = PushButton("Configure background…", self)
        self.disable = PushButton("Turn off", self)
        self.disable.setEnabled(False)
        self.apply = PrimaryPushButton("Apply", self)
        for action in (self.background_setup, self.disable, self.apply):
            actions.addWidget(action)
        layout.addLayout(actions)
        self.mode.currentIndexChanged.connect(self._sync_mode)
        self._sync_mode()

    def _sync_mode(self):
        manual = self.mode.currentData() == "manual"
        for widget in self._manual_fields:
            widget.setVisible(manual)
        self.detected_bands.setVisible(not manual and bool(self._detected))

    def set_detected_bands(self, bands: tuple[FrequencyBand, ...]) -> None:
        if self._detected == bands:
            return
        self._detected = bands
        self.detected_bands.setPlainText("\n".join(
            f"{format_quantity_auto(low, DIMENSION_FREQUENCY)} – {format_quantity_auto(high, DIMENSION_FREQUENCY)}"
            for low, high in bands
        ))
        self._sync_mode()

    def settings(self) -> AutomaticStripeSettings | StripeBands:
        protected = parse_frequency_bands(self.protected.toPlainText())
        if self.mode.currentData() == "automatic":
            return AutomaticStripeSettings(protected)
        return StripeBands(
            parse_frequency_bands(self.bands.toPlainText()),
            protected,
            parse_quantity(self.shoulder.text(), DIMENSION_FREQUENCY).si_value,
        )


class SpectrumCorrectionControls(CardWidget):
    """One correction choice applied before DSP in every preview."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("spectrumCorrectionControls")
        self.setProperty("stationSurface", "card")
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(4)
        strip = QWidget(self)
        row = FlowLayout(strip)
        self.strip_layout = row
        row.setContentsMargins(0, 0, 0, 0)
        row.setHorizontalSpacing(10)
        row.setVerticalSpacing(6)
        row.addWidget(StrongBodyLabel("Correction", strip))
        self.power_average = ComboBox(strip)
        self.power_average.setAccessibleName("Temporal power average before correction")
        self.power_average.setFixedWidth(110)
        for count in (1, 4, 8, 16, 32, 64):
            self.power_average.addItem("Avg: Off" if count == 1 else f"Avg: {count}", userData=count)
        self.power_average.setToolTip("Average received powers in W before Background or Reference. Use Filter settings for a custom count and reset gap.")
        row.addWidget(self.power_average)
        self.live_average = PushButton("Live average…", strip)
        self.live_average.setToolTip("Continuously average the most recent Live frames over a count or duration.")
        row.addWidget(self.live_average)
        self.show_live_frame = CheckBox("Live frame", strip)
        self.show_live_frame.setEnabled(False)
        row.addWidget(self.show_live_frame)
        self.stripes = StripeSwitch(strip)
        row.addWidget(self.stripes)
        self.background = CheckBox("Background", strip)
        self.background.setObjectName("spectrumFilter_background")
        self.background.setToolTip("Subtract a recorded background in linear W; retain positive and negative residuals.")
        self.configure_background = PushButton("Configure background…", strip)
        self.configure_background.setAccessibleName("Record a new background or load a saved background")
        self.reference = CheckBox("Reference", strip)
        self.reference.setAccessibleName("Enable reference mathematics")
        self.operation = ComboBox(strip)
        self.operation.setAccessibleName("Reference operation")
        for label, key in REFERENCE_OPERATIONS:
            self.operation.addItem(label, userData=key)
        self.operation.setFixedWidth(194)
        self.operation.setToolTip("Apply the selected operation in Spectrum and Spectrogram before the digital filters.")
        self.configure_reference = PushButton("Configure reference…", strip)
        self.configure_reference.setAccessibleName("Configure reference mathematics")
        for control in (self.background, self.configure_background, self.reference, self.configure_reference):
            row.addWidget(control)
        self.background_tools = PushButton("Quality…", strip)
        self.background_tools.setAccessibleName("Processing quality, reset average, background stability and drift models")
        row.addWidget(self.background_tools)
        root.addWidget(strip)
        self.summary = CaptionLabel("No correction · acquire a spectrum or start Live.", self)
        self.summary.setWordWrap(True)
        self.summary.setObjectName("muted")
        root.addWidget(self.summary)


def parse_plot_amplitude(text: str, unit: str) -> float:
    """Parse in the plotted domain, including signed W and derived units."""
    dimensions = {"dBm": DIMENSION_DBM, "dB": DIMENSION_DB,
                  "W": DIMENSION_POWER, "s": DIMENSION_TIME}
    if unit in dimensions:
        value = parse_quantity(text, dimensions[unit]).si_value
    elif unit in {"ratio", "linear ratio"}:
        cleaned = text.strip().replace(",", ".")
        if cleaned.endswith("linear ratio"):
            cleaned = cleaned.removesuffix("linear ratio").strip()
        elif cleaned.endswith("ratio"):
            cleaned = cleaned.removesuffix("ratio").strip()
        value = float(cleaned)
    elif unit == "mW²":
        # Reference multiplication is stored/displayed in mW², not in W.
        cleaned = text.strip()
        if not cleaned.endswith("mW²"):
            raise ValueError("Use the explicit plotted unit mW², e.g. 2e-8 mW².")
        value = float(cleaned.removesuffix("mW²").strip().replace(",", "."))
    else:
        raise ValueError(f"Unsupported plot unit: {unit}.")
    if not math.isfinite(value):
        raise ValueError("Axis limits must be finite.")
    return value


def format_plot_amplitude(value: float, unit: str) -> str:
    dimension = {"W": DIMENSION_POWER, "s": DIMENSION_TIME}.get(unit)
    if dimension:
        return format_quantity_auto(value, dimension)
    return f"{value:.9g} {unit}"


class PlotRangeController(QObject):
    """Keep user ranges stable across frames; reset Y when its unit changes."""

    changed = Signal()

    def __init__(self, view, parent: QObject, *, unit: str = "dBm", fit=None) -> None:
        super().__init__(parent)
        self.view = view
        self.unit = unit
        self._fit = fit or view.autoRange
        self.fixed_ranges: dict[str, tuple[float, float]] = {}
        self._restoring = False
        view.sigRangeChanged.connect(self._range_changed)

    def set_unit(self, unit: str) -> None:
        if unit == self.unit:
            return
        self.unit = unit
        self.fixed_ranges.pop("y", None)
        self.restore()
        self.changed.emit()

    def apply(self, axis: str, lower: str, upper: str) -> None:
        parser = (lambda text: parse_quantity(text, DIMENSION_FREQUENCY).si_value) if axis == "x" else (
            lambda text: parse_plot_amplitude(text, self.unit))
        limits = parser(lower), parser(upper)
        if not all(math.isfinite(value) for value in limits) or limits[0] >= limits[1]:
            raise ValueError("Minimum must be smaller than maximum. The previous range is preserved.")
        if axis == "x" and limits[0] < 0:
            raise ValueError("Frequency minimum must be non-negative.")
        self.fixed_ranges[axis] = limits
        self.restore()
        self.changed.emit()

    def release(self, axis: str) -> None:
        self.fixed_ranges.pop(axis, None)
        self.view.enableAutoRange(axis=axis, enable=True)
        self._fit()
        self.restore()
        self.changed.emit()

    def fit(self) -> None:
        self.fixed_ranges.clear()
        self.view.setMouseEnabled(x=True, y=True)
        self._fit()
        self.changed.emit()

    def restore(self) -> None:
        if self._restoring:
            return
        self._restoring = True
        try:
            for axis, limits in self.fixed_ranges.items():
                index = 0 if axis == "x" else 1
                if tuple(self.view.viewRange()[index]) != limits:
                    self.view.setRange(**{f"{axis}Range": limits}, padding=0)
            mouse = ["x" not in self.fixed_ranges, "y" not in self.fixed_ranges]
            if self.view.state["mouseEnabled"] != mouse:
                self.view.setMouseEnabled(x=mouse[0], y=mouse[1])
        finally:
            self._restoring = False

    def _range_changed(self, *_args) -> None:
        if not self._restoring:
            self.restore()


class PlotScaleDialog(StationDialog):
    """Display-only X/Y scale controls, separate from instrument settings."""

    def __init__(self, controller: PlotRangeController, parent: QWidget, *, spectrogram: bool = False) -> None:
        super().__init__(parent, resizable=True)
        self.controller = controller
        self.setObjectName("spectrumPlotScaleDialog")
        self.setWindowTitle("Spectrogram scales" if spectrogram else "Spectrum scales")
        self.setModal(False)
        self.resize(620, 340)
        surface = self.use_modal_shell_content().surface
        root = self.modal_content_layout(spacing=12)
        root.addWidget(StrongBodyLabel("Plot scales", surface))
        hint = CaptionLabel("Apply a fixed range or let each axis fit the data. These limits control the display.", surface)
        hint.setWordWrap(True)
        root.addWidget(hint)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        for column, label in enumerate(("Axis", "Minimum", "Maximum", "")):
            grid.addWidget(BodyLabel(label, surface), 0, column)
        self.editors = {}
        self.apply_buttons = {}
        self.auto_buttons = {}
        self.y_label = BodyLabel(surface)
        for row, axis in enumerate(("x", "y"), 1):
            label = BodyLabel("Frequency [Hz]", surface) if axis == "x" else self.y_label
            grid.addWidget(label, row, 0)
            low, high = LineEdit(surface), LineEdit(surface)
            low.setMinimumWidth(120)
            high.setMinimumWidth(120)
            low.setAccessibleName(f"{axis.upper()} minimum")
            high.setAccessibleName(f"{axis.upper()} maximum")
            self.editors[axis] = low, high
            grid.addWidget(low, row, 1)
            grid.addWidget(high, row, 2)
            actions = QWidget(surface)
            actions.setMinimumHeight(34)
            buttons = QHBoxLayout(actions)
            buttons.setContentsMargins(0, 0, 0, 0)
            buttons.setSpacing(6)
            apply_button = PushButton("Apply", actions)
            auto_button = PushButton("Auto", actions)
            apply_button.clicked.connect(lambda _=False, key=axis: self._apply(key))
            auto_button.clicked.connect(lambda _=False, key=axis: controller.release(key))
            self.apply_buttons[axis] = apply_button
            self.auto_buttons[axis] = auto_button
            buttons.addWidget(apply_button)
            buttons.addWidget(auto_button)
            grid.addWidget(actions, row, 3)
            grid.setRowMinimumHeight(row, 34)
            for editor in (low, high):
                editor.returnPressed.connect(lambda key=axis: self._apply(key))
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        root.addLayout(grid)
        root.addStretch(1)
        self.feedback = CaptionLabel(surface)
        self.feedback.setWordWrap(True)
        root.addWidget(self.feedback)
        buttons = QHBoxLayout()
        self.fit_button = PushButton("Fit both axes", surface)
        self.fit_button.clicked.connect(controller.fit)
        buttons.addWidget(self.fit_button)
        buttons.addStretch(1)
        close = PrimaryPushButton("Close", surface)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)
        self._spectrogram = spectrogram
        self._controller_disconnected = False
        controller.changed.connect(self._sync)

        def _disconnect_controller(_result: int = 0) -> None:
            if self._controller_disconnected:
                return
            self._controller_disconnected = True
            try:
                controller.changed.disconnect(self._sync)
            except (RuntimeError, TypeError):
                pass

        self.finished.connect(_disconnect_controller)
        self._sync()

    def _sync(self) -> None:
        self.y_label.setText(f"Time [{self.controller.unit}]" if self._spectrogram else f"Amplitude [{self.controller.unit}]")
        ranges = self.controller.view.viewRange()
        for index, axis in enumerate(("x", "y")):
            values = self.controller.fixed_ranges.get(axis, ranges[index])
            formatter = (lambda value: format_quantity_auto(value, DIMENSION_FREQUENCY)) if axis == "x" else (
                lambda value: format_plot_amplitude(value, self.controller.unit))
            for editor, value in zip(self.editors[axis], values, strict=True):
                editor.setText(formatter(value))
            self.auto_buttons[axis].setEnabled(axis in self.controller.fixed_ranges)

    def _apply(self, axis: str) -> None:
        try:
            lower, upper = self.editors[axis]
            self.controller.apply(axis, lower.text(), upper.text())
        except (TypeError, ValueError) as exc:
            self.feedback.setText(str(exc))
        else:
            self.feedback.setText(f"Fixed {axis.upper()} range applied.")
