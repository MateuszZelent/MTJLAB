"""Read-only manual view for the Lake Shore Model 475."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QTimer, Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, PrimaryPushButton, StrongBodyLabel, SubtitleLabel, isDarkTheme

from app.devices.lakeshore_475.models import GaussmeterReading
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.settings.models import StationSettings
from app.ui.design_system import plot_theme, tokens_for

if TYPE_CHECKING:
    from app.ui.workers import DeviceController


class LakeShore475Page(QWidget):
    """Safe readout page; it contains no instrument configuration control."""

    status = Signal(str)

    def __init__(self, controller: DeviceController, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller, self._settings = controller, settings
        self._in_flight = False
        self._history: deque[GaussmeterReading] = deque()
        self._plot_dirty = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._live_tick)
        self._plot_timer = QTimer(self)
        self._plot_timer.timeout.connect(self._refresh_plot_if_needed)
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
        title = StrongBodyLabel("Lake Shore 475", self.hero_card)
        title.setObjectName("pageTitle")
        copy.addWidget(title)
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
        badge = CaptionLabel("READ-ONLY", self.hero_card)
        badge.setProperty("deviceState", "compliance")
        hero_layout.addWidget(badge)
        self.values_card = CardWidget(self)
        self.values_card.setObjectName("lakeshoreValuesCard")
        self.values_card.setMinimumWidth(0)
        form = QFormLayout(self.values_card)
        form.setContentsMargins(20, 12, 20, 12)
        form.setVerticalSpacing(8)
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
        live_copy.addWidget(CaptionLabel("Sampling and drawing run independently to keep the trace responsive.", self.live_card))
        live_header.addLayout(live_copy, 1)
        self.live_state = CaptionLabel("STOPPED", self.live_card)
        self.live_state.setProperty("deviceState", "neutral")
        live_header.addWidget(self.live_state)
        live_layout.addLayout(live_header)

        controls = QHBoxLayout()
        controls.setSpacing(12)
        self.read_now = PrimaryPushButton("Read now", self.live_card)
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
        controls.addWidget(self.read_now)
        controls.addWidget(self.live)
        controls.addSpacing(8)
        controls.addWidget(CaptionLabel("Sampling", self.live_card))
        controls.addWidget(self.sample_interval)
        controls.addWidget(CaptionLabel("Refresh", self.live_card))
        controls.addWidget(self.refresh_interval)
        controls.addWidget(CaptionLabel("History", self.live_card))
        controls.addWidget(self.history_window)
        controls.addStretch(1)
        live_layout.addLayout(controls)
        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        top_row.addWidget(self.hero_card, 2)
        top_row.addWidget(self.values_card, 3)
        layout.addLayout(top_row)
        layout.addWidget(self.live_card)

        self.plot_card = CardWidget(self)
        self.plot_card.setObjectName("lakeshorePlotCard")
        plot_layout = QVBoxLayout(self.plot_card)
        plot_layout.setContentsMargins(16, 12, 16, 16)
        plot_header = QHBoxLayout()
        plot_header.addWidget(StrongBodyLabel("Magnetic field history", self.plot_card))
        plot_header.addStretch(1)
        self.plot_span = CaptionLabel("Last 1 min · elapsed time", self.plot_card)
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
        plot_layout.addWidget(self.history_plot, 1)
        layout.addWidget(self.plot_card, 1)
        self.banner = CaptionLabel("Connect the Lake Shore 475 to begin.", self)
        self.banner.setObjectName("muted")
        self.banner.setWordWrap(True)
        layout.addWidget(self.banner)

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

    def stop_live(self, reason: str) -> None:
        self.live.setChecked(False)
        self._timer.stop()
        self._plot_timer.stop()
        self.live_state.setText("STOPPED")
        self.live_state.setProperty("deviceState", "neutral")
        self.live_state.style().unpolish(self.live_state)
        self.live_state.style().polish(self.live_state)
        self.banner.setText(reason)

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
        self.field.setText("— T" if result.field_t is None else f"{result.field_t:+.8g} T")
        self.frequency.setText("— Hz" if result.frequency_hz is None else f"{result.frequency_hz:.8g} Hz")
        self.peaks.setText("— / — T" if result.negative_peak_t is None else f"{result.negative_peak_t:+.8g} / {result.positive_peak_t:+.8g} T")
        self.mode.setText(result.mode.value.upper())
        snap = result.snapshot
        self.configuration.setText(f"{snap.unit.value} · {snap.range_code} · {'on' if snap.autorange_enabled else 'off'} · {snap.probe_type_code}")
        self._history.append(result)
        self._prune_history(result.timestamp_utc)
        self._plot_dirty = True
        if not self.live.isChecked():
            self._refresh_plot_if_needed()
        self.banner.setText(f"Updated {result.timestamp_utc.astimezone().strftime('%H:%M:%S')}")

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

    def _error(self, operation: str, message: str) -> None:
        if operation == "read_measurement":
            self._in_flight = False
            self.read_now.setEnabled(True)
            self.stop_live(f"Read failed: {message}")
            self.status.emit(f"Lake Shore read failed: {message}")

    def _state(self, state: str) -> None:
        if state == "disconnected":
            self._in_flight = False
            self._timer.stop()
