"""Read-only manual view for the Lake Shore Model 475."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

import pyqtgraph as pg
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget

from app.devices.lakeshore_gaussmeter.models import GaussmeterReading
from app.domain.quantities import DIMENSION_TIME, parse_quantity
from app.settings.models import StationSettings

if TYPE_CHECKING:
    from app.ui.workers import DeviceController


class LakeShore475Page(QWidget):
    """Safe readout page; it contains no instrument configuration control."""

    status = Signal(str)

    def __init__(self, controller: DeviceController, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller, self._settings = controller, settings
        self._in_flight = False
        self._history: deque[GaussmeterReading] = deque(maxlen=600)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._live_tick)
        self._build()
        controller.result.connect(self._result)
        controller.error.connect(self._error)
        controller.state_changed.connect(self._state)
        self.set_settings(settings)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        hero = QFrame()
        hero.setObjectName("lakeshoreHero")
        hero_layout = QHBoxLayout(hero)
        copy = QVBoxLayout()
        title = QLabel("Lake Shore 475")
        title.setObjectName("pageTitle")
        copy.addWidget(title)
        note = QLabel("Read-only gaussmeter · no configuration commands")
        note.setObjectName("muted")
        copy.addWidget(note)
        self.resource = QLabel()
        copy.addWidget(self.resource)
        hero_layout.addLayout(copy, 1)
        badge = QLabel("READ-ONLY")
        badge.setObjectName("mokeProtocolBadge")
        hero_layout.addWidget(badge)
        layout.addWidget(hero)
        values = QFrame()
        form = QFormLayout(values)
        self.field = QLabel("— T")
        self.frequency = QLabel("— Hz")
        self.peaks = QLabel("— / — T")
        self.mode = QLabel("—")
        self.configuration = QLabel("—")
        form.addRow("Field", self.field)
        form.addRow("Frequency (RMS)", self.frequency)
        form.addRow("Negative / positive peak", self.peaks)
        form.addRow("Mode", self.mode)
        form.addRow("Unit · range · autorange · probe", self.configuration)
        layout.addWidget(values)
        self.history_plot = pg.PlotWidget()
        self.history_plot.setObjectName("lakeshoreHistoryPlot")
        self.history_plot.setLabel("left", "Field", units="T")
        self.history_plot.setLabel("bottom", "Sample")
        self.history_plot.showGrid(x=True, y=True, alpha=0.2)
        self._field_curve = self.history_plot.plot(pen=pg.mkPen("#4fa3ff", width=2))
        self._negative_peak_curve = self.history_plot.plot(pen=pg.mkPen("#d76b82", width=1))
        self._positive_peak_curve = self.history_plot.plot(pen=pg.mkPen("#7fc97f", width=1))
        layout.addWidget(self.history_plot, 1)
        controls = QHBoxLayout()
        self.read_now = QPushButton("Read now")
        self.read_now.setObjectName("primaryButton")
        self.read_now.clicked.connect(self._read)
        self.live = QCheckBox("Live readout")
        self.live.toggled.connect(self._live_changed)
        self.interval = QSpinBox()
        self.interval.setRange(500, 60_000)
        self.interval.setSuffix(" ms")
        self.interval.valueChanged.connect(lambda value: self._timer.setInterval(value))
        controls.addWidget(self.read_now)
        controls.addWidget(self.live)
        controls.addWidget(self.interval)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.banner = QLabel("Connect the Lake Shore 475 to begin.")
        self.banner.setObjectName("muted")
        self.banner.setWordWrap(True)
        layout.addWidget(self.banner)
        layout.addStretch(1)

    def set_settings(self, settings: StationSettings) -> None:
        self._settings = settings
        profile = settings.lakeshore_gaussmeter
        self.resource.setText(profile.resource or "No VISA resource assigned")
        self.interval.setValue(
            round(parse_quantity(profile.live_interval, DIMENSION_TIME).si_value * 1000)
        )
        self.read_now.setEnabled(profile.enabled and bool(profile.resource))

    def stop_live(self, reason: str) -> None:
        self.live.setChecked(False)
        self._timer.stop()
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
            self._timer.start(self.interval.value())
            self._read()
        else:
            self._timer.stop()

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
        self._update_history_plot()
        self.banner.setText(f"Updated {result.timestamp_utc.astimezone().strftime('%H:%M:%S')}")

    def _update_history_plot(self) -> None:
        indices = list(range(len(self._history)))
        field = [reading.field_t if reading.field_t is not None else float("nan") for reading in self._history]
        negative = [reading.negative_peak_t if reading.negative_peak_t is not None else float("nan") for reading in self._history]
        positive = [reading.positive_peak_t if reading.positive_peak_t is not None else float("nan") for reading in self._history]
        self._field_curve.setData(indices, field)
        self._negative_peak_curve.setData(indices, negative)
        self._positive_peak_curve.setData(indices, positive)

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
