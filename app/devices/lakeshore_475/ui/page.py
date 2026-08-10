"""Read-only manual view for the Lake Shore Model 475."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import QButtonGroup, QFormLayout, QGridLayout, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, FluentIcon, IconWidget, PrimaryPushButton, PushButton, StrongBodyLabel, SubtitleLabel, TransparentPushButton, isDarkTheme

from app.devices.lakeshore_475.models import (
    FieldUnit,
    GaussmeterReading,
    GaussmeterSnapshot,
    MeasurementMode,
)
from app.domain.manual_metadata import ManualMetadataValue
from app.domain.quantities import (
    DIMENSION_FREQUENCY,
    DIMENSION_MAGNETIC_FIELD,
    DIMENSION_TIME,
    parse_quantity,
)
from app.settings.models import StationSettings
from app.ui.design_system import plot_theme, tokens_for
from app.ui.dialogs import StationDialog

if TYPE_CHECKING:
    from app.ui.workers import DeviceController


class _PlotNavigationBar(QWidget):
    """Small Fluent navigation bar for a pyqtgraph time-series plot."""

    def __init__(self, plot: pg.PlotWidget, parent: QWidget) -> None:
        super().__init__(parent)
        self.plot = plot
        self.toolbar_buttons: list[TransparentPushButton] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.reset_view = self._button(
            "Reset view", "Show the complete field history", self._reset_view
        )
        self.pan = self._button(
            "Pan", "Drag to move through the field history", self._use_pan
        )
        self.box_zoom = self._button(
            "Box zoom", "Drag a rectangle to zoom into the field history", self._use_box_zoom
        )
        self.zoom_out = self._button(
            "Zoom out", "Zoom out around the current view centre", self._zoom_out
        )
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for button in (self.pan, self.box_zoom):
            button.setCheckable(True)
            self.mode_group.addButton(button)
        self.pan.setChecked(True)
        self._use_pan()
        layout.addStretch(1)

    def _button(self, text: str, tooltip: str, callback: object) -> TransparentPushButton:
        button = TransparentPushButton(text, self)
        button.setObjectName("plotToolButton")
        button.setToolTip(tooltip)
        button.setAccessibleName(text)
        button.clicked.connect(callback)  # type: ignore[arg-type]
        self.toolbar_buttons.append(button)
        self.layout().addWidget(button)
        return button

    def _reset_view(self) -> None:
        self.plot.getViewBox().autoRange()

    def _use_pan(self) -> None:
        self.pan.setChecked(True)
        self.plot.getViewBox().setMouseMode(pg.ViewBox.PanMode)

    def _use_box_zoom(self) -> None:
        self.box_zoom.setChecked(True)
        self.plot.getViewBox().setMouseMode(pg.ViewBox.RectMode)

    def _zoom_out(self) -> None:
        self.plot.getViewBox().scaleBy((1.5, 1.5))


class LakeShoreLiveWindow(StationDialog):
    """Compact always-on-top view sharing the page's single VISA read loop."""

    read_requested = Signal()
    live_changed = Signal(bool)
    closed = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Lake Shore 475 — floating live")
        self.setObjectName("lakeshoreLiveWindow")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.resize(540, 430)
        self.setMinimumSize(380, 320)

        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=10)
        header = QHBoxLayout()
        copy = QVBoxLayout()
        copy.addWidget(StrongBodyLabel("Magnetic field · live"))
        note = CaptionLabel("Read-only view · shared instrument session")
        note.setObjectName("muted")
        copy.addWidget(note)
        header.addLayout(copy, 1)
        self.live_state = CaptionLabel("STOPPED", surface)
        self.live_state.setProperty("deviceState", "neutral")
        header.addWidget(self.live_state)
        layout.addLayout(header)

        readout = CardWidget(surface)
        readout_layout = QGridLayout(readout)
        readout_layout.setContentsMargins(16, 12, 16, 12)
        readout_layout.addWidget(BodyLabel("Field"), 0, 0)
        self.field = SubtitleLabel("— T")
        self.field.setObjectName("lakeshoreFloatingField")
        readout_layout.addWidget(self.field, 0, 1)
        readout_layout.addWidget(BodyLabel("Mode"), 1, 0)
        self.mode = BodyLabel("—")
        readout_layout.addWidget(self.mode, 1, 1)
        layout.addWidget(readout)

        controls = QHBoxLayout()
        self.read_now = PrimaryPushButton(FluentIcon.SYNC, "Read now", surface)
        self.live = CheckBox("Live", surface)
        controls.addWidget(self.read_now)
        controls.addWidget(self.live)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.plot = pg.PlotWidget(surface)
        self.plot.setLabel("left", "Field", units="T")
        self.plot.setLabel("bottom", "Elapsed time", units="s")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.field_curve = self.plot.plot()
        self.negative_peak_curve = self.plot.plot()
        self.positive_peak_curve = self.plot.plot()
        self.plot_navigation = _PlotNavigationBar(self.plot, surface)
        layout.addWidget(self.plot_navigation)
        layout.addWidget(self.plot, 1)
        self.status = CaptionLabel("No reading received yet.", surface)
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.close_button = PushButton("Close", surface)
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self._apply_plot_theme()

        self.read_now.clicked.connect(self.read_requested)
        self.live.toggled.connect(self.live_changed)

    def event(self, event: QEvent) -> bool:
        if event.type() in {QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange}:
            self._apply_plot_theme()
        return super().event(event)

    def _apply_plot_theme(self) -> None:
        palette = plot_theme(tokens_for("dark" if isDarkTheme() else "light"))
        self.plot.setBackground(palette.background)
        for axis_name in ("left", "bottom"):
            axis = self.plot.getAxis(axis_name)
            axis.setPen(pg.mkPen(palette.axes))
            axis.setTextPen(pg.mkPen(palette.axes))
        self.field_curve.setPen(pg.mkPen(palette.measurement, width=2))
        self.negative_peak_curve.setPen(pg.mkPen(palette.reference, width=1))
        self.positive_peak_curve.setPen(pg.mkPen(palette.measurement, width=1))

    def set_live(self, enabled: bool) -> None:
        self.live.blockSignals(True)
        self.live.setChecked(enabled)
        self.live.blockSignals(False)
        self.live_state.setText("LIVE" if enabled else "STOPPED")
        self.live_state.setProperty("deviceState", "verified" if enabled else "neutral")
        self.live_state.style().unpolish(self.live_state)
        self.live_state.style().polish(self.live_state)

    def set_reading(self, reading: GaussmeterReading) -> None:
        if reading.field_t is not None:
            value = f"{reading.field_t:+.8g} T"
        else:
            value = f"{reading.negative_peak_t:+.8g} / {reading.positive_peak_t:+.8g} T"
        self.field.setText(value)
        self.mode.setText(reading.mode.value.upper())
        self.status.setText(
            f"Updated {reading.timestamp_utc.astimezone().strftime('%H:%M:%S')} · "
            f"UNIT {reading.snapshot.unit_code} · RANGE {reading.snapshot.range_code}"
        )

    def set_history(self, history: tuple[GaussmeterReading, ...], window_seconds: int) -> None:
        if not history:
            self.field_curve.setData([], [])
            self.negative_peak_curve.setData([], [])
            self.positive_peak_curve.setData([], [])
            return
        latest = history[-1].timestamp_utc
        elapsed = [(item.timestamp_utc - latest).total_seconds() for item in history]
        self.field_curve.setData(
            elapsed,
            [item.field_t if item.field_t is not None else float("nan") for item in history],
        )
        self.negative_peak_curve.setData(
            elapsed,
            [item.negative_peak_t if item.negative_peak_t is not None else float("nan") for item in history],
        )
        self.positive_peak_curve.setData(
            elapsed,
            [item.positive_peak_t if item.positive_peak_t is not None else float("nan") for item in history],
        )
        self.plot.setXRange(-window_seconds, 0, padding=0)

    def set_status(self, message: str) -> None:
        self.status.setText(message)

    def closeEvent(self, event: object) -> None:
        self.closed.emit()
        super().closeEvent(event)  # type: ignore[arg-type]


class LakeShore475Page(QWidget):
    """Safe readout page; it contains no instrument configuration control."""

    status = Signal(str)

    def __init__(self, controller: DeviceController, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        self._controller, self._settings = controller, settings
        self._in_flight = False
        self._history: deque[GaussmeterReading] = deque()
        self._plot_dirty = False
        self._window_filter_installed = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._live_tick)
        self._plot_timer = QTimer(self)
        self._plot_timer.timeout.connect(self._refresh_plot_if_needed)
        self._live_window: LakeShoreLiveWindow | None = None
        self._build()
        controller.result.connect(self._result)
        controller.error.connect(self._error)
        controller.state_changed.connect(self._state)
        self.set_settings(settings)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 18)
        layout.setSpacing(12)
        self.hero_card = CardWidget(self)
        self.hero_card.setObjectName("lakeshoreHeroCard")
        hero_layout = QHBoxLayout(self.hero_card)
        hero_layout.setContentsMargins(20, 16, 20, 16)
        copy = QVBoxLayout()
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.device_icon = IconWidget(FluentIcon.PIN, self.hero_card)
        self.device_icon.setFixedSize(18, 18)
        title_row.addWidget(self.device_icon)
        title = StrongBodyLabel("Lake Shore 475", self.hero_card)
        title.setObjectName("pageTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        copy.addLayout(title_row)
        note = CaptionLabel("Read-only gaussmeter · live magnetic-field monitor", self.hero_card)
        note.setObjectName("muted")
        note.setWordWrap(True)
        note.setMinimumWidth(0)
        note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        copy.addWidget(note)
        self.resource = BodyLabel(parent=self.hero_card)
        self.resource.setWordWrap(True)
        self.resource.setMinimumWidth(0)
        self.resource.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        copy.addWidget(self.resource)
        hero_layout.addLayout(copy, 1)
        self.execution_badge = CaptionLabel("SWEEP CONTROLLED", self.hero_card)
        self.execution_badge.setObjectName("executionControlBadge")
        self.execution_badge.setProperty("deviceState", "verified")
        self.execution_badge.hide()
        hero_layout.addWidget(self.execution_badge)
        self.read_only_badge = CaptionLabel("READ-ONLY", self.hero_card)
        self.read_only_badge.setProperty("deviceState", "compliance")
        hero_layout.addWidget(self.read_only_badge)
        self.values_card = CardWidget(self)
        self.values_card.setObjectName("lakeshoreValuesCard")
        self.values_card.setMinimumWidth(0)
        form = QFormLayout(self.values_card)
        form.setContentsMargins(20, 12, 20, 12)
        form.setVerticalSpacing(8)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.field = SubtitleLabel("— T", self.values_card)
        self.frequency = BodyLabel("— Hz", self.values_card)
        self.peaks = BodyLabel("— / — T", self.values_card)
        self.mode = BodyLabel("—", self.values_card)
        self.configuration = CaptionLabel("—", self.values_card)
        form.addRow("Field", self.field)
        form.addRow("Frequency (RMS)", self.frequency)
        form.addRow("Negative / positive peak", self.peaks)
        form.addRow("Mode", self.mode)
        form.addRow("Unit · range · autorange · probe", self.configuration)
        self.live_card = CardWidget(self)
        self.live_card.setObjectName("lakeshoreLiveCard")
        live_layout = QVBoxLayout(self.live_card)
        live_layout.setContentsMargins(20, 16, 20, 16)
        live_layout.setSpacing(12)
        live_header = QHBoxLayout()
        live_copy = QVBoxLayout()
        live_copy.setSpacing(2)
        live_copy.addWidget(StrongBodyLabel("Live preview", self.live_card))
        self.live_description = CaptionLabel(
            "Sampling and drawing run independently to keep the trace responsive.",
            self.live_card,
        )
        self.live_description.setWordWrap(True)
        self.live_description.setMinimumWidth(0)
        self.live_description.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        live_copy.addWidget(self.live_description)
        live_header.addLayout(live_copy, 1)
        self.live_state = CaptionLabel("STOPPED", self.live_card)
        self.live_state.setProperty("deviceState", "neutral")
        live_header.addWidget(self.live_state)
        live_layout.addLayout(live_header)

        self.live_controls = QGridLayout()
        self.live_controls.setHorizontalSpacing(10)
        self.live_controls.setVerticalSpacing(8)
        self.read_now = PrimaryPushButton(FluentIcon.SYNC, "Read now", self.live_card)
        self.read_now.clicked.connect(self._read)
        self.live = CheckBox("Live preview", self.live_card)
        self.live.toggled.connect(self._live_changed)
        self.sample_interval = self._time_combo(
            (("2 Hz", 500), ("1 Hz", 1_000), ("0.5 Hz", 2_000), ("0.2 Hz", 5_000)),
            "Sampling",
        )
        self.sample_interval.currentIndexChanged.connect(self._sampling_changed)
        # Compatibility name retained for callers that inspected the previous control.
        self.interval = self.sample_interval
        self.refresh_interval = self._time_combo(
            (("100 ms", 100), ("250 ms", 250), ("500 ms", 500), ("1 s", 1_000)),
            "Plot refresh",
        )
        self.refresh_interval.setCurrentIndex(2)
        self.refresh_interval.currentIndexChanged.connect(self._refresh_changed)
        self.history_window = self._time_combo(
            (("1 min", 60), ("2 min", 120), ("5 min", 300), ("10 min", 600)),
            "Recording window",
        )
        self.history_window.currentIndexChanged.connect(self._history_window_changed)
        self.open_live_window_button = PushButton("Open floating window", self.live_card)
        self.open_live_window_button.setToolTip(
            "Open an always-on-top field readout and history plot using this page's live session."
        )
        self.open_live_window_button.clicked.connect(self._open_live_window)
        self.sampling_label = CaptionLabel("Sampling", self.live_card)
        self.refresh_label = CaptionLabel("Refresh", self.live_card)
        self.history_label = CaptionLabel("History", self.live_card)
        self._live_control_widgets = (
            self.read_now,
            self.live,
            self.sampling_label,
            self.sample_interval,
            self.refresh_label,
            self.refresh_interval,
            self.history_label,
            self.history_window,
            self.open_live_window_button,
        )
        live_layout.addLayout(self.live_controls)
        self.top_cards = QGridLayout()
        self.top_cards.setSpacing(12)
        layout.addLayout(self.top_cards)
        self._compact_layout: bool | None = None
        self._reflow(compact=True)
        layout.addWidget(self.live_card)

        self.plot_card = CardWidget(self)
        self.plot_card.setObjectName("lakeshorePlotCard")
        plot_layout = QVBoxLayout(self.plot_card)
        plot_layout.setContentsMargins(16, 12, 16, 16)
        plot_header = QHBoxLayout()
        plot_header.addWidget(StrongBodyLabel("Magnetic field history", self.plot_card))
        plot_header.addStretch(1)
        self.plot_span = CaptionLabel("Last 1 min · elapsed time", self.plot_card)
        self.plot_span.setWordWrap(True)
        self.plot_span.setMinimumWidth(0)
        self.plot_span.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        plot_header.addWidget(self.plot_span)
        plot_layout.addLayout(plot_header)
        self.history_plot = pg.PlotWidget(self.plot_card)
        self.history_plot.setObjectName("lakeshoreHistoryPlot")
        self.history_plot.setLabel("left", "Field", units="T")
        self.history_plot.setLabel("bottom", "Elapsed time", units="s")
        self.history_plot.showGrid(x=True, y=True, alpha=0.2)
        self.history_plot.setMinimumHeight(260)
        self.history_plot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._field_curve = self.history_plot.plot()
        self._negative_peak_curve = self.history_plot.plot()
        self._positive_peak_curve = self.history_plot.plot()
        self._apply_plot_theme()
        self.plot_navigation = _PlotNavigationBar(self.history_plot, self.plot_card)
        plot_layout.addWidget(self.plot_navigation)
        plot_layout.addWidget(self.history_plot, 1)
        layout.addWidget(self.plot_card, 1)
        self.banner = CaptionLabel("Connect the Lake Shore 475 to begin.", self)
        self.banner.setObjectName("muted")
        self.banner.setWordWrap(True)
        layout.addWidget(self.banner)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reflow_for_window_width(self.window().width())

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        window = self.window()
        if not self._window_filter_installed and window is not self:
            window.installEventFilter(self)
            self._window_filter_installed = True
        self._reflow_for_window_width(window.width())

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if watched is self.window() and event.type() == QEvent.Type.Resize:
            self._reflow_for_window_width(event.size().width())  # type: ignore[attr-defined]
        return super().eventFilter(watched, event)

    def _reflow_for_window_width(self, width: int) -> None:
        # With the 248 px station navigation and shell margins, windows below
        # this point provide less than ~850 px to a device page.
        self._reflow(compact=width < 1_120)

    def _reflow(self, *, compact: bool) -> None:
        if self._compact_layout == compact:
            return
        self._compact_layout = compact
        for widget in (self.hero_card, self.values_card):
            self.top_cards.removeWidget(widget)
        for widget in self._live_control_widgets:
            self.live_controls.removeWidget(widget)
        for column in range(9):
            self.top_cards.setColumnStretch(column, 0)
            self.live_controls.setColumnStretch(column, 0)
        if compact:
            self.top_cards.addWidget(self.hero_card, 0, 0)
            self.top_cards.addWidget(self.values_card, 1, 0)
            self.top_cards.setColumnStretch(0, 1)
            self.live_controls.addWidget(self.read_now, 0, 0)
            self.live_controls.addWidget(self.live, 0, 1, 1, 3)
            pairs = (
                (self.sampling_label, self.sample_interval),
                (self.refresh_label, self.refresh_interval),
                (self.history_label, self.history_window),
            )
            for row, (label, control) in enumerate(pairs, start=1):
                self.live_controls.addWidget(label, row, 0)
                self.live_controls.addWidget(control, row, 1, 1, 3)
            self.live_controls.addWidget(self.open_live_window_button, 4, 0, 1, 4)
            self.live_controls.setColumnStretch(1, 1)
        else:
            self.top_cards.addWidget(self.hero_card, 0, 0)
            self.top_cards.addWidget(self.values_card, 0, 1)
            self.top_cards.setColumnStretch(0, 2)
            self.top_cards.setColumnStretch(1, 3)
            for column, widget in enumerate(self._live_control_widgets):
                self.live_controls.addWidget(widget, 0, column)
            self.live_controls.setColumnStretch(8, 1)

    def _time_combo(self, choices: tuple[tuple[str, int], ...], accessible_name: str) -> ComboBox:
        combo = ComboBox(self)
        combo.setAccessibleName(accessible_name)
        for label, value in choices:
            combo.addItem(label, userData=value)
        combo.setMinimumWidth(92)
        return combo

    def event(self, event: QEvent) -> bool:
        if event.type() in {QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange}:
            self._apply_plot_theme()
        return super().event(event)

    def _apply_plot_theme(self) -> None:
        """Retheme the pyqtgraph trace without competing with Fluent widget styling."""

        palette = plot_theme(tokens_for("dark" if isDarkTheme() else "light"))
        self.history_plot.setBackground(palette.background)
        for axis_name in ("left", "bottom"):
            axis = self.history_plot.getAxis(axis_name)
            axis.setPen(pg.mkPen(palette.axes))
            axis.setTextPen(pg.mkPen(palette.axes))
        self._field_curve.setPen(pg.mkPen(palette.measurement, width=2))
        self._negative_peak_curve.setPen(pg.mkPen(palette.reference, width=1))
        self._positive_peak_curve.setPen(pg.mkPen(palette.measurement, width=1))

    def set_settings(self, settings: StationSettings) -> None:
        self._settings = settings
        profile = settings.lakeshore_gaussmeter
        self.resource.setText(profile.resource or "No VISA resource assigned")
        configured_ms = round(parse_quantity(profile.live_interval, DIMENSION_TIME).si_value * 1000)
        closest = min(
            range(self.sample_interval.count()),
            key=lambda index: abs(int(self.sample_interval.itemData(index)) - configured_ms),
        )
        self.sample_interval.setCurrentIndex(closest)
        self.read_now.setEnabled(profile.enabled and bool(profile.resource))
        self.open_live_window_button.setEnabled(profile.enabled and bool(profile.resource))

    def stop_live(self, reason: str) -> None:
        self.live.setChecked(False)
        self._timer.stop()
        self._plot_timer.stop()
        self.live_state.setText("STOPPED")
        self.live_state.setProperty("deviceState", "neutral")
        self.live_state.style().unpolish(self.live_state)
        self.live_state.style().polish(self.live_state)
        self.banner.setText(reason)
        if self._live_window is not None:
            self._live_window.set_live(False)
            self._live_window.set_status(reason)

    def _open_live_window(self) -> None:
        if self._live_window is None:
            window = LakeShoreLiveWindow(self)
            window.read_requested.connect(self._read)
            window.live_changed.connect(self.live.setChecked)
            window.closed.connect(self._live_window_closed)
            self._live_window = window
        self._sync_live_window()
        self._live_window.show()
        self._live_window.raise_()
        self._live_window.activateWindow()

    def _live_window_closed(self) -> None:
        if self._live_window is not None:
            self._live_window.hide()

    def _sync_live_window(self) -> None:
        if self._live_window is None:
            return
        self._live_window.set_live(self.live.isChecked())
        self._live_window.set_history(
            tuple(self._history), self._selected_value(self.history_window)
        )

    def _read(self) -> None:
        if self._in_flight:
            return
        self._in_flight = True
        self.read_now.setEnabled(False)
        self._controller.call("read_measurement")

    def _live_tick(self) -> None:
        self._read()

    def _live_changed(self, enabled: bool) -> None:
        if enabled:
            self._timer.start(self._selected_value(self.sample_interval))
            self._plot_timer.start(self._selected_value(self.refresh_interval))
            self.live_state.setText("LIVE")
            self.live_state.setProperty("deviceState", "verified")
            self._read()
        else:
            self._timer.stop()
            self._plot_timer.stop()
            self.live_state.setText("STOPPED")
            self.live_state.setProperty("deviceState", "neutral")
            self._refresh_plot_if_needed()
        self.live_state.style().unpolish(self.live_state)
        self.live_state.style().polish(self.live_state)
        self._sync_live_window()

    @staticmethod
    def _selected_value(combo: ComboBox) -> int:
        return int(combo.currentData())

    def _sampling_changed(self, _index: int) -> None:
        if self._timer.isActive():
            self._timer.setInterval(self._selected_value(self.sample_interval))

    def _refresh_changed(self, _index: int) -> None:
        if self._plot_timer.isActive():
            self._plot_timer.setInterval(self._selected_value(self.refresh_interval))

    def _history_window_changed(self, _index: int) -> None:
        minutes = self._selected_value(self.history_window) // 60
        self.plot_span.setText(f"Last {minutes} min · elapsed time")
        self._prune_history(datetime.now(timezone.utc))
        self._plot_dirty = True
        self._refresh_plot_if_needed()

    def _result(self, operation: str, result: object) -> None:
        if operation != "read_measurement" or not isinstance(result, GaussmeterReading):
            return
        self._in_flight = False
        self.read_now.setEnabled(True)
        self._show_reading(result)

    def _show_reading(self, result: GaussmeterReading) -> None:
        """Render one confirmed reading without changing command availability."""
        self.field.setText("— T" if result.field_t is None else f"{result.field_t:+.8g} T")
        self.frequency.setText("— Hz" if result.frequency_hz is None else f"{result.frequency_hz:.8g} Hz")
        self.peaks.setText("— / — T" if result.negative_peak_t is None else f"{result.negative_peak_t:+.8g} / {result.positive_peak_t:+.8g} T")
        self.mode.setText(result.mode.value.upper())
        snap = result.snapshot
        self.configuration.setText(f"{snap.unit.value} · {snap.range_code} · {'on' if snap.autorange_enabled else 'off'} · {snap.probe_type_code}")
        self._history.append(result)
        self._prune_history(result.timestamp_utc)
        self._plot_dirty = True
        if self._live_window is not None:
            self._live_window.set_reading(result)
        if not self.live.isChecked():
            self._refresh_plot_if_needed()
        self.banner.setText(f"Updated {result.timestamp_utc.astimezone().strftime('%H:%M:%S')}")

    def manual_metadata_values(self) -> tuple[ManualMetadataValue, ...]:
        """Return the latest confirmed Lake Shore field reading."""

        if not self._history:
            return ()
        reading = self._history[-1]
        values: list[ManualMetadataValue] = []

        def add(
            key: str,
            label: str,
            dimension: str,
            unit: str,
            value: float | None,
        ) -> None:
            if value is None or not math.isfinite(float(value)):
                return
            values.append(
                ManualMetadataValue(
                    key=key,
                    device="Lake Shore 475",
                    label=label,
                    dimension=dimension,
                    unit=unit,
                    value_si=float(value),
                    source="last confirmed Lake Shore readback",
                )
            )

        add(
            "lakeshore_gaussmeter.field_t",
            "Lake Shore · field",
            DIMENSION_MAGNETIC_FIELD,
            "T",
            reading.field_t,
        )
        add(
            "lakeshore_gaussmeter.frequency_hz",
            "Lake Shore · RMS frequency",
            DIMENSION_FREQUENCY,
            "Hz",
            reading.frequency_hz,
        )
        add(
            "lakeshore_gaussmeter.negative_peak_t",
            "Lake Shore · negative peak",
            DIMENSION_MAGNETIC_FIELD,
            "T",
            reading.negative_peak_t,
        )
        add(
            "lakeshore_gaussmeter.positive_peak_t",
            "Lake Shore · positive peak",
            DIMENSION_MAGNETIC_FIELD,
            "T",
            reading.positive_peak_t,
        )
        return tuple(values)

    def apply_execution_event(
        self,
        event_name: str,
        event: Mapping[str, object],
        device_state: Mapping[str, object],
        _output_status: Mapping[str, str],
    ) -> None:
        """Render each runner-owned gaussmeter measurement and history point."""
        if event_name == "action_started" and event.get("kind") == "measure_lakeshore_field":
            self.live_state.setText("SWEEP MEASURING")
            self.banner.setText(
                "Run Engine is waiting for a complete Lake Shore measurement."
            )
            return
        if event_name != "action_finished" or event.get("kind") != "measure_lakeshore_field":
            return
        record = device_state.get("measurement")
        actual = record.get("actual") if isinstance(record, Mapping) else None
        if not isinstance(actual, Mapping):
            return
        try:
            mode = MeasurementMode(str(actual.get("mode", "")))
            unit = FieldUnit(str(actual.get("unit", "")))
            timestamp = datetime.fromisoformat(str(actual.get("timestamp_utc", "")))
            snapshot = GaussmeterSnapshot(
                mode_code=str(actual.get("mode_code", "")),
                mode=mode,
                unit_code=str(actual.get("unit_code", "")),
                unit=unit,
                range_code=str(actual.get("range_code", "")),
                autorange_enabled=bool(actual.get("autorange_enabled", False)),
                probe_type_code=str(actual.get("probe_type_code", "")),
                timestamp_utc=timestamp,
            )
            reading = GaussmeterReading(
                mode=mode,
                unit=unit,
                snapshot=snapshot,
                timestamp_utc=timestamp,
                field_t=self._execution_number(actual.get("field_t")),
                frequency_hz=self._execution_number(actual.get("frequency_hz")),
                negative_peak_t=self._execution_number(actual.get("negative_peak_t")),
                positive_peak_t=self._execution_number(actual.get("positive_peak_t")),
            )
        except (TypeError, ValueError):
            return
        self._show_reading(reading)
        self.live_state.setText("SWEEP UPDATED")
        self.banner.setText(
            "Run Engine confirmed the Lake Shore measurement and exposed it to the UI."
        )

    @staticmethod
    def _execution_number(value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    def set_execution_controlled(self, controlled: bool) -> None:
        self.execution_badge.setVisible(controlled)
        if not controlled and not self._timer.isActive():
            self.live_state.setText("STOPPED")

    def _prune_history(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._selected_value(self.history_window))
        while self._history and self._history[0].timestamp_utc < cutoff:
            self._history.popleft()

    def _refresh_plot_if_needed(self) -> None:
        if not self._plot_dirty:
            return
        self._update_history_plot()
        self._plot_dirty = False

    def _update_history_plot(self) -> None:
        if not self._history:
            self._field_curve.setData([], [])
            self._negative_peak_curve.setData([], [])
            self._positive_peak_curve.setData([], [])
            return
        latest = self._history[-1].timestamp_utc
        elapsed = [(reading.timestamp_utc - latest).total_seconds() for reading in self._history]
        field = [reading.field_t if reading.field_t is not None else float("nan") for reading in self._history]
        negative = [reading.negative_peak_t if reading.negative_peak_t is not None else float("nan") for reading in self._history]
        positive = [reading.positive_peak_t if reading.positive_peak_t is not None else float("nan") for reading in self._history]
        self._field_curve.setData(elapsed, field)
        self._negative_peak_curve.setData(elapsed, negative)
        self._positive_peak_curve.setData(elapsed, positive)
        window_seconds = self._selected_value(self.history_window)
        self.history_plot.setXRange(-window_seconds, 0, padding=0)
        self._sync_live_window()

    def _error(self, operation: str, message: str) -> None:
        if operation == "read_measurement":
            self._in_flight = False
            self.read_now.setEnabled(True)
            self.stop_live(f"Read failed: {message}")
            self.status.emit(f"Lake Shore read failed: {message}")

    def _state(self, state: str) -> None:
        if state == "disconnected":
            self._in_flight = False
            self.stop_live("Live readout stopped: reconnect Lake Shore 475 before reading again.")

    def closeEvent(self, event: object) -> None:
        self.stop_live("Live readout stopped because the Lake Shore page closed.")
        if self._live_window is not None:
            self._live_window.close()
        super().closeEvent(event)  # type: ignore[arg-type]
