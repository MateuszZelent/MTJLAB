"""Fluent configuration modal for Anritsu spectrum cleanup and peak analysis."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    DoubleSpinBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    SpinBox,
    StrongBodyLabel,
    SubtitleLabel,
    isDarkTheme,
)

from app.spectrum import SpectrumAnalysisParameters
from app.domain.quantities import DIMENSION_FREQUENCY, format_quantity_auto, parse_quantity
from app.ui.dialogs import StationDialog


def _create_separator(parent: QWidget) -> QWidget:
    """Subtle horizontal separator matching theme borders."""
    sep = QWidget(parent)
    sep.setFixedHeight(1)
    color = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.07)"
    sep.setStyleSheet(f"background-color: {color}; border: none;")
    return sep


def _create_setting_row(
    title: str,
    description: str,
    control: QWidget,
    parent: QWidget,
) -> QWidget:
    """Responsive setting row: title and description on the left, fixed control on the right."""
    row = QWidget(parent)
    row_layout = QHBoxLayout(row)
    row_layout.setContentsMargins(16, 6, 16, 6)
    row_layout.setSpacing(16)

    text_layout = QVBoxLayout()
    text_layout.setSpacing(2)
    title_label = BodyLabel(title, row)
    desc_label = CaptionLabel(description, row)
    desc_label.setObjectName("muted")
    desc_label.setWordWrap(True)
    text_layout.addWidget(title_label)
    text_layout.addWidget(desc_label)

    row_layout.addLayout(text_layout, 1)
    row_layout.addWidget(
        control, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    )
    return row


class SpectrumAnalysisSettingsDialog(StationDialog):
    """Modal dialog for tuning edge-preserving denoise, EMI suppression, and peak detection."""

    parameters_applied = Signal(object)
    closed = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        current_parameters: SpectrumAnalysisParameters | None = None,
    ) -> None:
        super().__init__(
            parent,
            resizable=True,
            modal_shell_outer_margins=(0, 0, 0, 0),
            modal_shell_backdrop_margins=(4, 4, 4, 4),
            modal_shell_surface_margins=(20, 16, 20, 16),
        )
        self.setObjectName("spectrumAnalysisSettingsDialog")
        self.setProperty("stationSurface", "raised")
        self.setWindowTitle("Spectrum analysis & cleanup parameters")
        self.setModal(True)
        self.setMinimumSize(580, 500)
        self.resize(720, 680)

        self._initial_parameters = current_parameters or SpectrumAnalysisParameters()
        surface = self.use_modal_shell_content().surface

        root_layout = self.modal_content_layout(spacing=10)
        root_layout.setContentsMargins(0, 0, 0, 0)

        # ── Header (Pinned at top) ──────────────────────────────────────────
        header_widget = QWidget(surface)
        header_layout = QVBoxLayout(header_widget)
        header_layout.setContentsMargins(4, 0, 4, 0)
        header_layout.setSpacing(4)

        heading = SubtitleLabel("Digital Signal Processing Settings", header_widget)
        header_layout.addWidget(heading)

        explanation = CaptionLabel(
            "Configure digital filtering and peak discovery for live display analysis. "
            "Raw instrument measurement data in HDF5 archives and hardware readbacks remain completely unaffected.",
            header_widget,
        )
        explanation.setObjectName("muted")
        explanation.setWordWrap(True)
        header_layout.addWidget(explanation)
        root_layout.addWidget(header_widget)

        # ── Scrollable Settings Body ─────────────────────────────────────────
        scroll = ScrollArea(surface)
        scroll.setObjectName("analysisSettingsScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        scroll_content = QWidget(scroll)
        scroll_content.setProperty("stationSurface", "surface")
        cards_layout = QVBoxLayout(scroll_content)
        cards_layout.setContentsMargins(0, 4, 6, 4)
        cards_layout.setSpacing(10)

        narrow_card = CardWidget(scroll_content)
        narrow_layout = QVBoxLayout(narrow_card)
        narrow_layout.setContentsMargins(0, 10, 0, 10)
        narrow_layout.setSpacing(4)
        narrow_header = QWidget(narrow_card)
        narrow_header_layout = QVBoxLayout(narrow_header)
        narrow_header_layout.setContentsMargins(16, 0, 16, 4)
        narrow_header_layout.addWidget(StrongBodyLabel("Narrow-peak rejection", narrow_header))
        hint = CaptionLabel(
            "Replaces isolated positive and negative spikes by local background. Width is measured "
            "at half local prominence in the selected trace. A narrow real signal can also be removed; "
            "protect its full band including tails and an RBW margin.", narrow_header)
        hint.setWordWrap(True)
        narrow_header_layout.addWidget(hint)
        narrow_layout.addWidget(narrow_header)
        self.narrow_width = LineEdit(narrow_card)
        self.narrow_width.setFixedWidth(150)
        self.narrow_width.setAccessibleName("Maximum narrow-peak width")
        self.narrow_width.setText(format_quantity_auto(self._initial_parameters.narrow_max_width_hz, DIMENSION_FREQUENCY))
        narrow_layout.addWidget(_create_setting_row("Maximum peak width", "Explicit frequency, e.g. 6 MHz.", self.narrow_width, narrow_card))
        self.narrow_threshold = DoubleSpinBox(narrow_card)
        self.narrow_threshold.setRange(3, 30)
        self.narrow_threshold.setSingleStep(.5)
        self.narrow_threshold.setDecimals(1)
        self.narrow_threshold.setSuffix(" ×")
        self.narrow_threshold.setFixedWidth(150)
        self.narrow_threshold.setValue(self._initial_parameters.narrow_threshold_sigma)
        narrow_layout.addWidget(_create_setting_row("Outlier threshold", "Robust local noise scale; higher means fewer replacements.", self.narrow_threshold, narrow_card))
        self.narrow_protect = CheckBox("Protect band", narrow_card)
        self.narrow_protect.setFixedWidth(150)
        regions = self._initial_parameters.narrow_protected_regions_hz
        self.narrow_protect.setChecked(bool(regions))
        narrow_layout.addWidget(_create_setting_row("Keep signal band unchanged", "Whole spikes touching this band are preserved.", self.narrow_protect, narrow_card))
        self.narrow_protected_start = LineEdit(narrow_card)
        self.narrow_protected_stop = LineEdit(narrow_card)
        for control, value, name in ((self.narrow_protected_start, regions[0][0] if regions else 1e9, "Protected band start"),
                                     (self.narrow_protected_stop, regions[0][1] if regions else 2e9, "Protected band stop")):
            control.setFixedWidth(150)
            control.setText(format_quantity_auto(value, DIMENSION_FREQUENCY))
            control.setAccessibleName(name)
            control.setEnabled(bool(regions))
            self.narrow_protect.toggled.connect(control.setEnabled)
        narrow_layout.addWidget(_create_setting_row("Protected band start", "Frequency including lower signal tails and margin.", self.narrow_protected_start, narrow_card))
        narrow_layout.addWidget(_create_setting_row("Protected band stop", "Frequency including upper signal tails and margin.", self.narrow_protected_stop, narrow_card))
        cards_layout.addWidget(narrow_card)

        # ── Card 1: Denoise (Bilateral Filter) ──────────────────────────────
        denoise_card = CardWidget(scroll_content)
        denoise_card.setProperty("stationSurface", "card")
        denoise_layout = QVBoxLayout(denoise_card)
        denoise_layout.setContentsMargins(0, 10, 0, 10)
        denoise_layout.setSpacing(4)

        denoise_header = QWidget(denoise_card)
        denoise_header_layout = QVBoxLayout(denoise_header)
        denoise_header_layout.setContentsMargins(16, 0, 16, 4)
        denoise_header_layout.setSpacing(2)
        denoise_title = StrongBodyLabel(
            "Edge-Preserving Denoise (Bilateral Filter)", denoise_header
        )
        denoise_header_layout.addWidget(denoise_title)
        denoise_hint = CaptionLabel(
            "Smooths baseline noise fluctuations across bins while preserving sharp resonance peaks and carrier slopes.",
            denoise_header,
        )
        denoise_hint.setObjectName("muted")
        denoise_hint.setWordWrap(True)
        denoise_header_layout.addWidget(denoise_hint)
        denoise_layout.addWidget(denoise_header)
        denoise_layout.addWidget(_create_separator(denoise_card))

        self.denoise_window = SpinBox(denoise_card)
        self.denoise_window.setRange(3, 51)
        self.denoise_window.setSingleStep(2)
        self.denoise_window.setSuffix(" bins")
        self.denoise_window.setFixedWidth(150)
        self.denoise_window.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.denoise_window.setValue(self._initial_parameters.denoise_window)
        self.denoise_window.setToolTip(
            "Filter kernel size in frequency bins (must be an odd integer between 3 and 51)."
        )
        denoise_layout.addWidget(
            _create_setting_row(
                "Filter window width",
                "Odd kernel width in frequency bins (3–51). Wider windows yield stronger baseline smoothing.",
                self.denoise_window,
                denoise_card,
            )
        )
        cards_layout.addWidget(denoise_card)

        # ── Card 2: Stationary-Line Rejection (EMI / Clock Spurs) ──────────
        emi_card = CardWidget(scroll_content)
        emi_card.setProperty("stationSurface", "card")
        emi_layout = QVBoxLayout(emi_card)
        emi_layout.setContentsMargins(0, 10, 0, 10)
        emi_layout.setSpacing(4)

        emi_header = QWidget(emi_card)
        emi_header_layout = QVBoxLayout(emi_header)
        emi_header_layout.setContentsMargins(16, 0, 16, 4)
        emi_header_layout.setSpacing(2)
        emi_title = StrongBodyLabel(
            "Stationary-Line Rejection (EMI / Clock Spurs)", emi_header
        )
        emi_header_layout.addWidget(emi_title)
        emi_hint = CaptionLabel(
            "Identifies persistent, temporally static spikes across historical frames and removes them via linear interpolation.",
            emi_header,
        )
        emi_hint.setObjectName("muted")
        emi_hint.setWordWrap(True)
        emi_header_layout.addWidget(emi_hint)
        emi_layout.addWidget(emi_header)
        emi_layout.addWidget(_create_separator(emi_card))

        self.emi_threshold = DoubleSpinBox(emi_card)
        self.emi_threshold.setRange(1.0, 50.0)
        self.emi_threshold.setSingleStep(0.5)
        self.emi_threshold.setDecimals(1)
        self.emi_threshold.setSuffix(" dB")
        self.emi_threshold.setFixedWidth(150)
        self.emi_threshold.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.emi_threshold.setValue(self._initial_parameters.emi_threshold_db)
        self.emi_threshold.setToolTip(
            "Minimum peak elevation above local rolling noise floor (dB) to consider a spur candidate."
        )
        emi_layout.addWidget(
            _create_setting_row(
                "Elevation threshold",
                "Minimum peak height in dB above the local rolling noise floor to consider an interference candidate.",
                self.emi_threshold,
                emi_card,
            )
        )
        emi_layout.addWidget(_create_separator(emi_card))

        self.emi_max_std = DoubleSpinBox(emi_card)
        self.emi_max_std.setRange(0.05, 5.0)
        self.emi_max_std.setSingleStep(0.05)
        self.emi_max_std.setDecimals(2)
        self.emi_max_std.setSuffix(" dB")
        self.emi_max_std.setFixedWidth(150)
        self.emi_max_std.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.emi_max_std.setValue(self._initial_parameters.emi_max_std_db)
        self.emi_max_std.setToolTip(
            "Maximum power standard deviation across historical frames. Lower values restrict rejection to static carriers."
        )
        emi_layout.addWidget(
            _create_setting_row(
                "Maximum temporal variation (σ)",
                "Standard deviation upper bound across historical frames. Tighter thresholds ensure only truly static carriers are rejected.",
                self.emi_max_std,
                emi_card,
            )
        )
        emi_layout.addWidget(_create_separator(emi_card))

        self.emi_min_frames = SpinBox(emi_card)
        self.emi_min_frames.setRange(3, 24)
        self.emi_min_frames.setSingleStep(1)
        self.emi_min_frames.setSuffix(" frames")
        self.emi_min_frames.setFixedWidth(150)
        self.emi_min_frames.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.emi_min_frames.setValue(self._initial_parameters.emi_min_frames)
        self.emi_min_frames.setToolTip(
            "Minimum number of rolling history frames required before interference rejection engages."
        )
        emi_layout.addWidget(
            _create_setting_row(
                "Minimum history frames",
                "Number of rolling acquisition frames required before stationary-line suppression is engaged.",
                self.emi_min_frames,
                emi_card,
            )
        )
        cards_layout.addWidget(emi_card)

        # ── Card 3: Peak Detection & Fitting ────────────────────────────────
        peak_card = CardWidget(scroll_content)
        peak_card.setProperty("stationSurface", "card")
        peak_layout = QVBoxLayout(peak_card)
        peak_layout.setContentsMargins(0, 10, 0, 10)
        peak_layout.setSpacing(4)

        peak_header = QWidget(peak_card)
        peak_header_layout = QVBoxLayout(peak_header)
        peak_header_layout.setContentsMargins(16, 0, 16, 4)
        peak_header_layout.setSpacing(2)
        peak_title = StrongBodyLabel(
            "Peak Detection & Analytical Fitting", peak_header
        )
        peak_header_layout.addWidget(peak_title)
        peak_hint = CaptionLabel(
            "Local extrema discovery thresholds, peak prominence constraints, and optional sub-bin line-shape modeling.",
            peak_header,
        )
        peak_hint.setObjectName("muted")
        peak_hint.setWordWrap(True)
        peak_header_layout.addWidget(peak_hint)
        peak_layout.addWidget(peak_header)
        peak_layout.addWidget(_create_separator(peak_card))

        self.peak_snr = DoubleSpinBox(peak_card)
        self.peak_snr.setRange(1.0, 50.0)
        self.peak_snr.setSingleStep(0.5)
        self.peak_snr.setDecimals(1)
        self.peak_snr.setSuffix(" dB")
        self.peak_snr.setFixedWidth(150)
        self.peak_snr.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.peak_snr.setValue(self._initial_parameters.peak_min_snr_db)
        self.peak_snr.setToolTip(
            "Minimum signal-to-noise ratio in dB above estimated noise floor required to register a peak."
        )
        peak_layout.addWidget(
            _create_setting_row(
                "Minimum SNR",
                "Signal-to-noise ratio threshold in dB required above the estimated local noise floor.",
                self.peak_snr,
                peak_card,
            )
        )
        peak_layout.addWidget(_create_separator(peak_card))

        self.peak_prominence = DoubleSpinBox(peak_card)
        self.peak_prominence.setRange(0.5, 50.0)
        self.peak_prominence.setSingleStep(0.5)
        self.peak_prominence.setDecimals(1)
        self.peak_prominence.setSuffix(" dB")
        self.peak_prominence.setFixedWidth(150)
        self.peak_prominence.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.peak_prominence.setValue(self._initial_parameters.peak_min_prominence_db)
        self.peak_prominence.setToolTip(
            "Minimum peak prominence in dB relative to surrounding spectral valleys."
        )
        peak_layout.addWidget(
            _create_setting_row(
                "Minimum prominence",
                "Required vertical drop in dB on both sides before a higher peak is reached.",
                self.peak_prominence,
                peak_card,
            )
        )
        peak_layout.addWidget(_create_separator(peak_card))

        self.peak_max_count = SpinBox(peak_card)
        self.peak_max_count.setRange(1, 100)
        self.peak_max_count.setSingleStep(5)
        self.peak_max_count.setSuffix(" peaks")
        self.peak_max_count.setFixedWidth(150)
        self.peak_max_count.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.peak_max_count.setValue(self._initial_parameters.peak_max_count)
        self.peak_max_count.setToolTip(
            "Maximum number of strongest peaks retained in the table and marker overlays."
        )
        peak_layout.addWidget(
            _create_setting_row(
                "Maximum peak count",
                "Maximum number of strongest candidate peaks retained for display, markers, and tabular tracking.",
                self.peak_max_count,
                peak_card,
            )
        )
        peak_layout.addWidget(_create_separator(peak_card))

        self.peak_fit_models = CheckBox("Fit models", peak_card)
        self.peak_fit_models.setFixedWidth(150)
        self.peak_fit_models.setChecked(self._initial_parameters.peak_fit_models)
        self.peak_fit_models.setToolTip(
            "Fit analytical line shapes to extract precise center frequency, FWHM, and Q factor."
        )
        peak_layout.addWidget(
            _create_setting_row(
                "Analytical sub-bin fitting",
                "Fit Gaussian and Lorentzian curves around peak extrema to report sub-bin center frequency and FWHM.",
                self.peak_fit_models,
                peak_card,
            )
        )
        cards_layout.addWidget(peak_card)

        cards_layout.addStretch(1)
        scroll.setWidget(scroll_content)
        root_layout.addWidget(scroll, 1)

        # ── Action Bar (Pinned at bottom) ───────────────────────────────────
        bottom_container = QWidget(surface)
        bottom_layout = QVBoxLayout(bottom_container)
        bottom_layout.setContentsMargins(4, 4, 4, 0)
        bottom_layout.setSpacing(10)

        bottom_layout.addWidget(_create_separator(bottom_container))
        self.validation_error = CaptionLabel("", bottom_container)
        self.validation_error.setWordWrap(True)
        self.validation_error.hide()
        bottom_layout.addWidget(self.validation_error)
        for editor in (self.narrow_width, self.narrow_protected_start, self.narrow_protected_stop):
            editor.textChanged.connect(lambda _text: self.validation_error.hide())
        self.narrow_protect.toggled.connect(lambda _checked: self.validation_error.hide())

        buttons_row = QHBoxLayout()
        buttons_row.setContentsMargins(0, 0, 0, 0)

        self.reset_button = PushButton("Reset to defaults", bottom_container)
        self.reset_button.setToolTip("Restore factory recommended DSP parameters.")
        buttons_row.addWidget(self.reset_button)

        buttons_row.addStretch(1)

        self.cancel_button = PushButton("Cancel", bottom_container)
        buttons_row.addWidget(self.cancel_button)

        self.apply_button = PrimaryPushButton("Apply parameters", bottom_container)
        buttons_row.addWidget(self.apply_button)

        bottom_layout.addLayout(buttons_row)
        root_layout.addWidget(bottom_container)

        # ── Connections ──────────────────────────────────────────────────────
        self.reset_button.clicked.connect(self.reset_to_defaults)
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button.clicked.connect(self._apply)

    def _sanitize_window(self, val: int) -> int:
        val = max(3, min(51, int(val)))
        return val if val % 2 == 1 else val + 1

    def reset_to_defaults(self) -> None:
        defaults = SpectrumAnalysisParameters()
        self.denoise_window.setValue(defaults.denoise_window)
        self.emi_threshold.setValue(defaults.emi_threshold_db)
        self.emi_max_std.setValue(defaults.emi_max_std_db)
        self.emi_min_frames.setValue(defaults.emi_min_frames)
        self.peak_snr.setValue(defaults.peak_min_snr_db)
        self.peak_prominence.setValue(defaults.peak_min_prominence_db)
        self.peak_max_count.setValue(defaults.peak_max_count)
        self.peak_fit_models.setChecked(defaults.peak_fit_models)
        self.narrow_width.setText(format_quantity_auto(defaults.narrow_max_width_hz, DIMENSION_FREQUENCY))
        self.narrow_threshold.setValue(defaults.narrow_threshold_sigma)
        self.narrow_protect.setChecked(False)
        self.validation_error.hide()

    def get_parameters(self) -> SpectrumAnalysisParameters:
        win = self._sanitize_window(self.denoise_window.value())
        width_hz = parse_quantity(self.narrow_width.text(), DIMENSION_FREQUENCY).si_value
        if width_hz <= 0:
            raise ValueError("Maximum peak width must be positive, e.g. 6 MHz.")
        regions = ()
        if self.narrow_protect.isChecked():
            lower = parse_quantity(self.narrow_protected_start.text(), DIMENSION_FREQUENCY).si_value
            upper = parse_quantity(self.narrow_protected_stop.text(), DIMENSION_FREQUENCY).si_value
            if not 0 <= lower < upper:
                raise ValueError("Protected band start must be nonnegative and smaller than stop.")
            regions = ((lower, upper), *self._initial_parameters.narrow_protected_regions_hz[1:])
        return replace(self._initial_parameters,
            denoise_window=win,
            emi_threshold_db=float(self.emi_threshold.value()),
            emi_max_std_db=float(self.emi_max_std.value()),
            emi_min_frames=int(self.emi_min_frames.value()),
            peak_min_snr_db=float(self.peak_snr.value()),
            peak_min_prominence_db=float(self.peak_prominence.value()),
            peak_max_count=int(self.peak_max_count.value()),
            peak_fit_models=bool(self.peak_fit_models.isChecked()),
            narrow_max_width_hz=width_hz,
            narrow_threshold_sigma=float(self.narrow_threshold.value()),
            narrow_protected_regions_hz=regions,
        )

    def _apply(self) -> None:
        try:
            params = self.get_parameters()
        except ValueError as exc:
            self.validation_error.setText(str(exc))
            self.validation_error.show()
            return
        self.parameters_applied.emit(params)
        self.accept()

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)
        self.closed.emit()
