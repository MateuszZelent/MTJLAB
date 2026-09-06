"""Read-only manual diagnostics for the reconstructed MOKE Box protocol."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, CaptionLabel, CardWidget, CheckBox, ComboBox, PrimaryPushButton, PushButton, SpinBox, StrongBodyLabel, TitleLabel, isDarkTheme
from app.ui.dialogs import StationDialog

from app.devices.moke_box.models import MokeHallVoltageReading, hall_field_from_voltage
from app.domain.manual_metadata import ManualMetadataValue
from app.domain.quantities import (
    DIMENSION_MAGNETIC_FIELD,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    parse_quantity,
)
from app.settings.models import StationSettings
from app.ui.design_system import plot_theme, tokens_for
from app.ui.widgets import FluentTabView
from app.ui.workers import DeviceController


class MokeHallLiveWindow(StationDialog):
    """Modeless, always-on-top read-only view backed by the page's single timer."""

    read_requested = Signal()
    live_changed = Signal(bool)
    interval_changed = Signal(int)
    closed = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("MOKE Box — Hall live")
        self.setObjectName("mokeHallLiveWindow")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(False)
        self.setMinimumWidth(320)

        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=10)
        heading = StrongBodyLabel("Hall 1 · live readout")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        note = CaptionLabel("Read-only AD7734 channel 0 · no VOUT, gain or field command")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.readout_card = CardWidget(surface)
        self.readout_card.setObjectName("mokeLiveReadout")
        panel_layout = QGridLayout(self.readout_card)
        panel_layout.addWidget(BodyLabel("Hall voltage"), 0, 0)
        self.voltage = BodyLabel("— V")
        self.voltage.setObjectName("mokeLiveVoltage")
        panel_layout.addWidget(self.voltage, 0, 1)
        panel_layout.addWidget(BodyLabel("Derived field"), 1, 0)
        self.field = BodyLabel("— mT")
        self.field.setObjectName("mokeLiveField")
        panel_layout.addWidget(self.field, 1, 1)
        layout.addWidget(self.readout_card)

        controls = QHBoxLayout()
        self.read_now = PrimaryPushButton("Read now", surface)
        self.live = CheckBox("Live", surface)
        self.interval = SpinBox(surface)
        self.interval.setRange(500, 60_000)
        self.interval.setValue(1_000)
        self.interval.setSuffix(" ms")
        self.interval.setToolTip("Minimum 500 ms avoids overlapping TCP reads.")
        controls.addWidget(self.read_now)
        controls.addWidget(self.live)
        controls.addWidget(self.interval)
        layout.addLayout(controls)
        self.status = BodyLabel("Open the MOKE Box connection to begin.", surface)
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.close_button = PushButton("Close", surface)
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)

        self.read_now.clicked.connect(self.read_requested)
        self.live.toggled.connect(self.live_changed)
        self.interval.valueChanged.connect(self.interval_changed)

    def set_live(self, enabled: bool, interval_ms: int) -> None:
        self.live.blockSignals(True)
        self.live.setChecked(enabled)
        self.live.blockSignals(False)
        self.interval.blockSignals(True)
        self.interval.setValue(interval_ms)
        self.interval.blockSignals(False)

    def set_reading(self, reading: MokeHallVoltageReading) -> None:
        field_t = hall_field_from_voltage(reading.voltage_v)
        self.voltage.setText(f"{reading.voltage_v:+.6f} V")
        self.field.setText(f"{field_t * 1_000:+.3f} mT")
        self.status.setText(
            f"Updated {reading.timestamp_utc.astimezone().strftime('%H:%M:%S')} · "
            f"AD7734 0x{reading.raw_codes[0]:06X}"
        )

    def set_status(self, message: str) -> None:
        self.status.setText(message)

    def closeEvent(self, event: object) -> None:
        self.closed.emit()
        super().closeEvent(event)  # type: ignore[arg-type]


class MokeBoxPage(QWidget):
    """Display every confirmed MOKE measurement path without actuator controls."""

    status = Signal(str)

    def __init__(
        self,
        controller: DeviceController,
        settings: StationSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        self._controller = controller
        self._settings = settings
        self._pending_operation: str | None = None
        self.vout_values: dict[int, QLabel] = {}
        self._last_vouts: dict[int, float] = {}
        self.field_values: dict[str, QLabel] = {}
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(1_000)
        self._live_timer.timeout.connect(self._request_live_hall_read)
        self._plot_timer = QTimer(self)
        self._plot_timer.setInterval(500)
        self._plot_timer.timeout.connect(self._refresh_plot_if_needed)
        self._history: deque[MokeHallVoltageReading] = deque()
        self._plot_dirty = False
        self._hall_live_window: MokeHallLiveWindow | None = None
        self._build()
        controller.result.connect(self._result)
        controller.error.connect(self._error)
        controller.state_changed.connect(self._state_changed)
        self.set_settings(settings)
        self._state_changed("disconnected")

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(12)

        self.hero_card = CardWidget(self)
        self.hero_card.setObjectName("mokeHero")
        hero_layout = QHBoxLayout(self.hero_card)
        hero_layout.setContentsMargins(20, 16, 20, 16)
        heading = QVBoxLayout()
        title = TitleLabel("MOKE Box")
        title.setObjectName("pageTitle")
        subtitle = CaptionLabel("Binary TCP diagnostics · confirmed four-byte records")
        subtitle.setObjectName("muted")
        self.endpoint = BodyLabel()
        self.endpoint.setObjectName("mokeEndpoint")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        heading.addWidget(self.endpoint)
        hero_layout.addLayout(heading, 1)
        self.execution_badge = CaptionLabel("SWEEP CONTROLLED", self.hero_card)
        self.execution_badge.setObjectName("executionControlBadge")
        self.execution_badge.setProperty("deviceState", "verified")
        self.execution_badge.hide()
        hero_layout.addWidget(self.execution_badge)
        self.protocol_badge = BodyLabel("READ-ONLY")
        self.protocol_badge.setObjectName("mokeProtocolBadge")
        hero_layout.addWidget(self.protocol_badge)
        outer.addWidget(self.hero_card)

        self.safety_note = BodyLabel()
        self.safety_note.setObjectName("mokeSafetyNote")
        self.safety_note.setWordWrap(True)
        outer.addWidget(self.safety_note)

        self.views = FluentTabView(self)
        self.views.setObjectName("mokeViews")
        self.views.addTab(self._build_vout_view(), "VOUT 0–7")
        self.views.addTab(self._build_field_view(), "Hall field")
        outer.addWidget(self.views, 1)

    def _build_vout_view(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        self.vout_card = CardWidget(page)
        layout.addWidget(self.vout_card)
        content = QVBoxLayout(self.vout_card)
        content.setContentsMargins(20, 16, 20, 16)
        header = QHBoxLayout()
        copy = QVBoxLayout()
        title = StrongBodyLabel("Eight-channel DAC readback")
        title.setObjectName("sectionTitle")
        hint = CaptionLabel("One read-only request returns channels D0…D7 and validates every checksum.")
        hint.setObjectName("muted")
        copy.addWidget(title)
        copy.addWidget(hint)
        header.addLayout(copy, 1)
        self.read_vouts_button = PrimaryPushButton("Read all VOUT")
        self.read_vouts_button.setToolTip(
            "Send the documented 18 00 00 18 readback frame and receive exactly 32 bytes."
        )
        self.read_vouts_button.clicked.connect(self._read_vouts)
        header.addWidget(self.read_vouts_button)
        content.addLayout(header)

        rack = QGridLayout()
        rack.setSpacing(10)
        for channel in range(8):
            card = CardWidget(page)
            card.setObjectName("mokeValueCard")
            card_layout = QVBoxLayout(card)
            channel_label = BodyLabel(f"VOUT {channel}")
            channel_label.setObjectName("mokeChannelLabel")
            value = BodyLabel("— V")
            value.setObjectName("mokeValue")
            value.setToolTip(f"Last validated MOKE AD5362 readback for channel {channel}.")
            card_layout.addWidget(channel_label)
            card_layout.addWidget(value)
            rack.addWidget(card, channel // 4, channel % 4)
            self.vout_values[channel] = value
        content.addLayout(rack)
        content.addStretch(1)
        return page

    def _build_field_view(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        self.hall_card = CardWidget(page)
        layout.addWidget(self.hall_card)
        content = QVBoxLayout(self.hall_card)
        content.setContentsMargins(20, 16, 20, 16)
        header = QHBoxLayout()
        copy = QVBoxLayout()
        title = StrongBodyLabel("Hall voltage (read-only)")
        title.setObjectName("sectionTitle")
        hint = BodyLabel(
            "Reads the physically verified MainBox AD7734 Hall-1 channel 0. "
            "The magnetic-field value is only a derived estimate from the confirmed base polynomial."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        copy.addWidget(title)
        copy.addWidget(hint)
        header.addLayout(copy, 1)
        self.field_samples = SpinBox()
        self.field_samples.setRange(1, 1)
        self.field_samples.setValue(1)
        self.field_samples.setToolTip(
            "One physical Hall-1 sample per request is verified on the connected MOKE Box."
        )
        self.field_samples.hide()
        self.read_fields_button = PrimaryPushButton("Get Hall voltage (V)")
        self.read_fields_button.setToolTip(
            "Sends Send Data(N), then reads Hall 1 (channel 0) and Hall 2 (channel 2). "
            "It does not change VOUT, gain, or magnetic field."
        )
        self.read_fields_button.clicked.connect(self._read_fields)
        header.addWidget(self.read_fields_button)
        content.addLayout(header)

        live_controls = QGridLayout()
        live_controls.setHorizontalSpacing(10)
        live_controls.setVerticalSpacing(8)
        self.live_hall = CheckBox("Live Hall")
        self.live_hall.setToolTip(
            "Poll one Hall-1 sample at a fixed interval. A new TCP request is not queued while the prior read is pending."
        )
        self.live_hall.toggled.connect(self._set_live_enabled)
        self.sample_interval = self._time_combo(
            (("2 Hz", 500), ("1 Hz", 1_000), ("0.5 Hz", 2_000), ("0.2 Hz", 5_000)),
            "Sampling frequency",
        )
        self.sample_interval.setCurrentIndex(1)
        self.sample_interval.currentIndexChanged.connect(self._sampling_changed)
        self.live_interval = self.sample_interval
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
        self.open_live_window_button = PushButton("Open floating Hall live")
        self.open_live_window_button.setToolTip(
            "Open a compact always-on-top Hall readout that remains available while you work in other tabs."
        )
        self.open_live_window_button.clicked.connect(self._open_hall_live_window)
        live_controls.addWidget(self.live_hall, 0, 0)
        live_controls.setColumnStretch(1, 1)
        live_controls.addWidget(self.open_live_window_button, 0, 2, 1, 4)
        live_controls.addWidget(CaptionLabel("Sampling", page), 1, 0)
        live_controls.addWidget(self.sample_interval, 1, 1)
        live_controls.addWidget(CaptionLabel("Refresh", page), 1, 2)
        live_controls.addWidget(self.refresh_interval, 1, 3)
        live_controls.addWidget(CaptionLabel("History", page), 1, 4)
        live_controls.addWidget(self.history_window, 1, 5)
        content.addLayout(live_controls)

        cards = QGridLayout()
        for column, key in enumerate(("hall1", "hall2")):
            card = CardWidget(page)
            card.setObjectName("mokeFieldCard")
            card_layout = QGridLayout(card)
            heading = BodyLabel(
                "Hall 1 · longitudinal (verified)"
                if key == "hall1"
                else "Hall 2 · transversal (not yet qualified)"
            )
            heading.setObjectName("mokeChannelLabel")
            card_layout.addWidget(heading, 0, 0, 1, 2)
            for row, (suffix, label) in enumerate(
                (("voltage", "Hall voltage"), ("stddev", "Std. deviation"), ("field", "Derived field")),
                start=1,
            ):
                card_layout.addWidget(BodyLabel(label), row, 0)
                value = BodyLabel("—")
                value.setObjectName("mokeFieldValue" if suffix == "field" else "mokeMetricValue")
                card_layout.addWidget(value, row, 1)
                self.field_values[f"{key}_{suffix}"] = value
            cards.addWidget(card, 0, column)
        content.addLayout(cards)
        self.field_status = BodyLabel("Connect MOKE Box, then read one Hall-voltage sample.")
        self.field_status.setObjectName("mokeHallStatus")
        self.field_status.setWordWrap(True)
        content.addWidget(self.field_status)
        self.field_timestamp = BodyLabel("No Hall acquisition yet")
        self.field_timestamp.setObjectName("muted")
        content.addWidget(self.field_timestamp)
        plot_header = QHBoxLayout()
        plot_header.addWidget(StrongBodyLabel("Hall 1 voltage history", page))
        plot_header.addStretch(1)
        self.plot_span = CaptionLabel("Last 1 min · elapsed time", page)
        plot_header.addWidget(self.plot_span)
        content.addLayout(plot_header)
        self.history_plot = pg.PlotWidget(page)
        self.history_plot.setObjectName("mokeHallHistoryPlot")
        self.history_plot.setLabel("left", "Hall voltage", units="V")
        self.history_plot.setLabel("bottom", "Elapsed time", units="s")
        self.history_plot.showGrid(x=True, y=True, alpha=0.2)
        self.history_plot.setMinimumHeight(250)
        self._voltage_curve = self.history_plot.plot()
        self._apply_plot_theme()
        content.addWidget(self.history_plot, 1)
        return page

    def _time_combo(
        self, choices: tuple[tuple[str, int], ...], accessible_name: str
    ) -> ComboBox:
        combo = ComboBox(self)
        combo.setAccessibleName(accessible_name)
        for label, value in choices:
            combo.addItem(label, userData=value)
        combo.setMinimumWidth(92)
        return combo

    @staticmethod
    def _selected_value(combo: ComboBox) -> int:
        return int(combo.currentData())

    def event(self, event: QEvent) -> bool:
        if event.type() in {
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
        } and hasattr(self, "history_plot"):
            self._apply_plot_theme()
        return super().event(event)

    def _apply_plot_theme(self) -> None:
        palette = plot_theme(tokens_for("dark" if isDarkTheme() else "light"))
        self.history_plot.setBackground(palette.background)
        for axis_name in ("left", "bottom"):
            axis = self.history_plot.getAxis(axis_name)
            axis.setPen(pg.mkPen(palette.axes))
            axis.setTextPen(pg.mkPen(palette.axes))
        self._voltage_curve.setPen(pg.mkPen(palette.measurement, width=2))

    def set_settings(self, settings: StationSettings) -> None:
        self._settings = settings
        profile = settings.moke_box
        for combo, configured, in_seconds in (
            (self.sample_interval, profile.live_interval, False),
            (self.refresh_interval, profile.plot_refresh_interval, False),
            (self.history_window, profile.history_window, True),
        ):
            si_seconds = parse_quantity(configured, DIMENSION_TIME).si_value
            configured_val = round(si_seconds if in_seconds else si_seconds * 1000)
            closest = min(
                range(combo.count()),
                key=lambda index: abs(int(combo.itemData(index)) - configured_val),
            )
            combo.setCurrentIndex(closest)
        self.endpoint.setText(f"Endpoint  {profile.endpoint or 'not configured'}")
        if not profile.enabled or not profile.protocol_qualified or not profile.endpoint:
            self.safety_note.setText(
                "Configuration incomplete. Set the TCP endpoint and explicitly qualify the reconstructed protocol in Station settings. "
                "No connection will be opened until then."
            )
        else:
            self.safety_note.setText(
                "Read-only profile: the page can read VOUT and the qualified Hall-1 voltage. It never sends gain or VOUT-setting commands. "
                "MOKE field-off is not yet qualified, so E-STOP closes the TCP session and reports UNKNOWN."
            )

    def _read_vouts(self) -> None:
        self._start("read_vouts", "Reading VOUT…")
        self._controller.call("read_vouts")

    def _read_fields(self) -> None:
        self._request_hall_read(live=False)

    def _request_live_hall_read(self) -> None:
        if self.live_hall.isChecked():
            self._request_hall_read(live=True)

    def _request_hall_read(self, *, live: bool) -> None:
        if self._pending_operation is not None:
            return
        if not self.read_fields_button.isEnabled():
            if live:
                self.stop_live("Live Hall stopped: MOKE Box is not connected and verified.")
            return
        samples = self.field_samples.value()
        self.field_status.setText("Live Hall: reading…" if live else "Reading Hall-voltage sample…")
        self._start("read_hall_voltage", "Reading Hall voltage…")
        self._controller.call("read_hall_voltage", {"count": samples})

    def _set_live_enabled(self, enabled: bool) -> None:
        if enabled:
            if not self.read_fields_button.isEnabled() or self._pending_operation is not None:
                self.live_hall.blockSignals(True)
                self.live_hall.setChecked(False)
                self.live_hall.blockSignals(False)
                self.field_status.setText("Connect and verify MOKE Box before starting live Hall readout.")
                self._sync_live_window()
                return
            self._live_timer.setInterval(self._selected_value(self.sample_interval))
            self._live_timer.start()
            self._plot_timer.start(self._selected_value(self.refresh_interval))
            self._request_live_hall_read()
            self.field_status.setText("Live Hall readout is active.")
        else:
            self._live_timer.stop()
            self._plot_timer.stop()
            if self._pending_operation is None:
                self.field_status.setText("Live Hall readout stopped.")
        self._sync_live_window()

    def _sampling_changed(self, _index: int) -> None:
        if self._live_timer.isActive():
            self._live_timer.setInterval(self._selected_value(self.sample_interval))
        self._sync_live_window()

    def _refresh_changed(self, _index: int) -> None:
        if self._plot_timer.isActive():
            self._plot_timer.setInterval(self._selected_value(self.refresh_interval))

    def _history_window_changed(self, _index: int) -> None:
        minutes = self._selected_value(self.history_window) // 60
        self.plot_span.setText(f"Last {minutes} min · elapsed time")
        self._prune_history(datetime.now(timezone.utc))
        self._plot_dirty = True
        self._refresh_plot_if_needed()

    def _set_live_interval(self, interval_ms: int) -> None:
        closest = min(
            range(self.sample_interval.count()),
            key=lambda index: abs(int(self.sample_interval.itemData(index)) - interval_ms),
        )
        self.sample_interval.setCurrentIndex(closest)

    def _open_hall_live_window(self) -> None:
        if self._hall_live_window is None:
            window = MokeHallLiveWindow(self)
            window.read_requested.connect(self._read_fields)
            window.live_changed.connect(self.live_hall.setChecked)
            window.interval_changed.connect(self._set_live_interval)
            window.closed.connect(self._live_window_closed)
            self._hall_live_window = window
        self._sync_live_window()
        self._hall_live_window.show()
        self._hall_live_window.raise_()
        self._hall_live_window.activateWindow()

    def _live_window_closed(self) -> None:
        # Closing the separate window only hides that view; it does not alter
        # a deliberately enabled live readout on the main MOKE page.
        if self._hall_live_window is not None:
            self._hall_live_window.hide()

    def _sync_live_window(self) -> None:
        if self._hall_live_window is not None:
            self._hall_live_window.set_live(
                self.live_hall.isChecked(), self._selected_value(self.sample_interval)
            )

    def stop_live(self, reason: str = "Live Hall readout stopped.") -> None:
        self._live_timer.stop()
        self._plot_timer.stop()
        self.live_hall.blockSignals(True)
        self.live_hall.setChecked(False)
        self.live_hall.blockSignals(False)
        self.field_status.setText(reason)
        if self._hall_live_window is not None:
            self._hall_live_window.set_status(reason)
        self._sync_live_window()

    def _start(self, operation: str, message: str) -> None:
        if self._pending_operation is not None:
            return
        self._pending_operation = operation
        self._set_measurement_controls(False)
        self.status.emit(f"MOKE Box: {message}")

    def _result(self, operation: str, result: object) -> None:
        if operation == "connect":
            self.status.emit("MOKE Box connected and verified by read-only VOUT response")
            return
        if operation == "read_vouts" and isinstance(result, dict):
            for channel, value in result.items():
                if channel in self.vout_values:
                    numeric = float(value)
                    self._last_vouts[int(channel)] = numeric
                    self.vout_values[channel].setText(f"{numeric:+.6f} V")
            self.status.emit("MOKE Box: eight VOUT channels validated")
        elif operation == "read_hall_voltage" and isinstance(result, MokeHallVoltageReading):
            self._show_hall_reading(result)
            self.status.emit("MOKE Box: Hall voltage measurement completed")
        if operation == self._pending_operation:
            self._pending_operation = None
            self._set_measurement_controls(True)

    def _show_hall_reading(self, result: MokeHallVoltageReading) -> None:
        self.field_values["hall1_field"].setText(
            f"{hall_field_from_voltage(result.voltage_v):+.9f} T"
        )
        self.field_values["hall1_voltage"].setText(f"{result.voltage_v:+.6f} V")
        self.field_values["hall1_stddev"].setText(f"{result.stddev_v:.6g} V")
        for suffix in ("field", "voltage", "stddev"):
            self.field_values[f"hall2_{suffix}"].setText("—")
        self.field_timestamp.setText(
            f"{result.samples} sample · raw AD7734 0x{result.raw_codes[0]:06X} · "
            f"{result.timestamp_utc.isoformat(timespec='seconds')}"
        )
        self.field_status.setText(
            "Live Hall readout is active." if self.live_hall.isChecked() else
            "Hall 1 voltage received from MainBox AD7734 channel 0. "
            "Hall 2 is intentionally not shown until its physical response is qualified."
        )
        self._history.append(result)
        self._prune_history(result.timestamp_utc)
        self._plot_dirty = True
        if not self.live_hall.isChecked():
            self._refresh_plot_if_needed()
        if self._hall_live_window is not None:
            self._hall_live_window.set_reading(result)

    def manual_metadata_values(self) -> tuple[ManualMetadataValue, ...]:
        """Return confirmed read-only MOKE Hall and VOUT values."""

        values: list[ManualMetadataValue] = []

        def add(
            key: str,
            label: str,
            unit: str,
            value: float,
            *,
            source: str,
            dimension: str | None,
        ) -> None:
            if not math.isfinite(float(value)):
                return
            values.append(
                ManualMetadataValue(
                    key=key,
                    device="MOKE Box",
                    label=label,
                    dimension=dimension,
                    unit=unit,
                    value_si=float(value),
                    source=source,
                )
            )

        for channel, value in sorted(self._last_vouts.items()):
            add(
                f"moke_box.vout.{channel}_v",
                f"MOKE VOUT {channel}",
                "V",
                value,
                dimension=DIMENSION_VOLTAGE,
                source="last confirmed MOKE VOUT readback",
            )
        if self._history:
            reading = self._history[-1]
            add(
                "moke_box.hall1.voltage_v",
                "MOKE Hall 1 · voltage",
                "V",
                reading.voltage_v,
                dimension=DIMENSION_VOLTAGE,
                source="last confirmed MOKE Hall readback",
            )
            add(
                "moke_box.hall1.stddev_v",
                "MOKE Hall 1 · standard deviation",
                "V",
                reading.stddev_v,
                dimension=DIMENSION_VOLTAGE,
                source="last confirmed MOKE Hall readback",
            )
            add(
                "moke_box.hall1.field_t",
                "MOKE Hall 1 · derived field",
                "T",
                hall_field_from_voltage(reading.voltage_v),
                dimension=DIMENSION_MAGNETIC_FIELD,
                source="derived from last confirmed MOKE Hall readback",
            )
        return tuple(values)

    def apply_execution_event(
        self,
        event_name: str,
        event: Mapping[str, object],
        device_state: Mapping[str, object],
        _output_status: Mapping[str, str],
    ) -> None:
        """Render runner-confirmed Hall readings in the normal page and plot."""
        if event_name == "action_started" and event.get("kind") == "measure_moke_hall":
            self.field_status.setText(
                "Run Engine is reading the Hall channel; waiting for a validated response."
            )
            return
        if event_name != "action_finished" or event.get("kind") != "measure_moke_hall":
            return
        record = device_state.get("hall_readback")
        actual = record.get("actual") if isinstance(record, Mapping) else None
        if not isinstance(actual, Mapping):
            return
        voltage = self._execution_number(actual.get("voltage_v"))
        stddev = self._execution_number(actual.get("stddev_v"))
        raw = actual.get("raw_ad7734")
        samples = actual.get("samples", 1)
        if voltage is None or stddev is None or not isinstance(raw, int):
            return
        timestamp = self._execution_timestamp(actual.get("timestamp_utc"))
        reading = MokeHallVoltageReading(
            voltage_v=voltage,
            stddev_v=stddev,
            samples=int(samples) if isinstance(samples, int) and samples > 0 else 1,
            raw_codes=(raw,),
            timestamp_utc=timestamp,
        )
        self._show_hall_reading(reading)
        self.field_status.setText(
            "Run Engine confirmed the Hall measurement and exposed it to the UI."
        )

    @staticmethod
    def _execution_number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    @staticmethod
    def _execution_timestamp(value: object) -> datetime:
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return datetime.now(timezone.utc)

    def set_execution_controlled(self, controlled: bool) -> None:
        self.execution_badge.setVisible(controlled)

    def _prune_history(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._selected_value(self.history_window))
        while self._history and self._history[0].timestamp_utc < cutoff:
            self._history.popleft()

    def _refresh_plot_if_needed(self) -> None:
        if not self._plot_dirty:
            return
        if not self._history:
            self._voltage_curve.setData([], [])
        else:
            latest = self._history[-1].timestamp_utc
            elapsed = [
                (reading.timestamp_utc - latest).total_seconds()
                for reading in self._history
            ]
            self._voltage_curve.setData(
                elapsed, [reading.voltage_v for reading in self._history]
            )
        self.history_plot.setXRange(
            -self._selected_value(self.history_window), 0, padding=0
        )
        self._plot_dirty = False

    def _error(self, operation: str, error: str) -> None:
        if operation == self._pending_operation:
            self._pending_operation = None
            self._set_measurement_controls(False)
        if operation == "read_hall_voltage":
            self.stop_live(
                "Hall-voltage read failed. The TCP session was closed for safety; reconnect before retrying. "
                f"Details: {error}"
            )
        self.status.emit(f"MOKE Box {operation} failed: {error}")

    def _state_changed(self, state: str) -> None:
        self._set_measurement_controls(state == "verified" and self._pending_operation is None)
        if state == "verified" and self._pending_operation is None:
            self.field_status.setText("Ready. Start with one Hall-voltage sample.")
        elif state == "disconnected":
            self.field_status.setText("Connect MOKE Box to enable the Hall-voltage read.")
        elif state in {"fault", "unknown"}:
            self.field_status.setText("The previous MOKE session is no longer usable. Reconnect before reading Hall voltage.")
        if state in {"fault", "unknown", "disconnected"}:
            self._pending_operation = None
            self.stop_live("Live Hall readout stopped: reconnect MOKE Box before reading again.")

    def _set_measurement_controls(self, enabled: bool) -> None:
        for control in (self.read_vouts_button, self.read_fields_button):
            control.setEnabled(enabled)
        self.live_hall.setEnabled(enabled)
        self.open_live_window_button.setEnabled(enabled)
        if self._hall_live_window is not None:
            self._hall_live_window.read_now.setEnabled(enabled)
            self._hall_live_window.live.setEnabled(enabled)

    def closeEvent(self, event: object) -> None:
        self.stop_live("Live Hall readout stopped because the MOKE page closed.")
        if self._hall_live_window is not None:
            self._hall_live_window.close()
        super().closeEvent(event)  # type: ignore[arg-type]
