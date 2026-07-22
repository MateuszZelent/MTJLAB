"""Manual-control UI for the Rigol DG1000Z module."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout, QFrame, QGridLayout, QHBoxLayout,
    QPushButton, QSplitter,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel, CardWidget, CheckBox, ComboBox, PrimaryPushButton,
    PushButton, ScrollArea, SpinBox, StrongBodyLabel, TitleLabel,
)

from app.devices.rigol_dg1000z import (
    RigolBurstConfig, RigolChannelConfig, RigolCounterConfig, RigolCounterReading, RigolFrequencySweepConfig,
    RigolModulationConfig, RigolOutputConfig,
)
from app.domain.quantities import (
    DIMENSION_FREQUENCY, DIMENSION_TIME, DIMENSION_VOLTAGE,
    format_quantity_auto, parse_quantity,
)
from app.safety.rigol_current import validate_rigol_frequency_sweep, validate_rigol_waveform
from app.safety.quick_controls import quick_control_safety_bounds
from app.settings.models import StationSettings
from app.ui.common import line_edit as _line
from app.ui.dialogs import StationMessageBox as QMessageBox
from app.ui.widgets import FluentTabView, LimitField, NotificationBanner, SpectrumPlotWidget
from app.ui.workers import DeviceController


@dataclass(frozen=True, slots=True)
class RigolConfigurationSnapshot:
    """Qt-independent carrier configuration shared by manual and plan editors."""

    channel: int = 1
    waveform: str = "SIN"
    time_mode: str = "Frequency"
    frequency: str = "1 kHz"
    level_mode: str = "Amplitude / Offset"
    high_level: str = "1 mV"
    low_level: str = "-1 mV"
    output_load: str = "HIGHZ"
    phase_deg: str = "0"
    square_duty_percent: str = "50"
    ramp_symmetry_percent: str = "50"
    pulse_width: str = "100 us"
    pulse_leading: str = "10 ns"
    pulse_trailing: str = "10 ns"
    output_polarity: str = "NORM"
    output_mode: str = "NORM"
    gate_polarity: str = "NORM"
    sync_enabled: bool = False
    sync_polarity: str = "NORM"
    sync_delay: str = "0 s"


@dataclass(frozen=True, slots=True)
class _RigolUiOperation:
    """Correlate an asynchronous controller completion with its UI request."""

    request_id: int
    operation: str
    channel: int | None
    purpose: str
    payload: object
    requested_output: bool | None = None


class RigolPage(QWidget):
    status = Signal(str)
    quick_controls_requested = Signal()
    quick_setpoint_requested = Signal(str, str)

    LEVEL_MODE_AMPLITUDE_OFFSET = "Amplitude + Offset"
    LEVEL_MODE_HIGH_LOW = "High Level + Low Level (asymmetric)"

    def __init__(self, controller: DeviceController, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._station_settings = settings
        self._limit_fields: dict[QWidget, LimitField] = {}
        self._pending_output_enable = False
        self._pending_output_channel: int | None = None
        self._pending_output_request_id: int | None = None
        self._pending_output_stage: str | None = None
        self._pending_output_config: RigolOutputConfig | None = None
        self._next_ui_request_id = 0
        self._queued_ui_operations: dict[str, deque[_RigolUiOperation]] = defaultdict(deque)
        self._issuing_ui_operation: _RigolUiOperation | None = None
        self._issuing_ui_operation_was_queued = False
        self._confirmed_advanced_states: dict[int, dict[str, bool | None]] = {
            channel: {"modulation": None, "sweep": None, "burst": None}
            for channel in (1, 2)
        }
        self._confirmed_carrier_configs: dict[int, RigolChannelConfig | None] = {
            1: None,
            2: None,
        }
        self._pending_advanced_requests: dict[tuple[int, str], int] = {}
        self._device_state_value = "disconnected"
        self._output_states = {1: False, 2: False}
        self._output_state_known = {1: False, 2: False}
        self._execution_readbacks: dict[int, dict[str, object]] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        self.hero_card = CardWidget()
        header = self.hero_card
        header.setObjectName("rigolHero")
        header_layout = QHBoxLayout(header)
        heading = QVBoxLayout()
        title = TitleLabel("Rigol DG1032Z")
        title.setObjectName("pageTitle")
        subtitle = CaptionLabel("Function generator · channel control and safe output activation")
        subtitle.setObjectName("muted")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header_layout.addLayout(heading, 1)
        self.execution_badge = CaptionLabel("SWEEP CONTROLLED", self.hero_card)
        self.execution_badge.setObjectName("executionControlBadge")
        self.execution_badge.setProperty("deviceState", "verified")
        self.execution_badge.hide()
        header_layout.addWidget(self.execution_badge)
        self.quick_controls_button = PushButton("Quick controls...", self.hero_card)
        self.quick_controls_button.clicked.connect(self.quick_controls_requested)
        header_layout.addWidget(self.quick_controls_button)

        self.device_led = BodyLabel("●")
        self.device_led.setObjectName("rigolLed")
        self.device_state = StrongBodyLabel("DISCONNECTED")
        self.device_state.setObjectName("rigolState")
        state_box = QVBoxLayout()
        state_line = QHBoxLayout()
        state_line.addWidget(self.device_led)
        state_line.addWidget(self.device_state)
        state_box.addLayout(state_line)
        self.capability_badge = CaptionLabel("Capabilities: awaiting identification")
        self.capability_badge.setObjectName("rigolBadge")
        state_box.addWidget(self.capability_badge)
        header_layout.addLayout(state_box)
        layout.addWidget(header)
        self.banner = NotificationBanner()
        layout.addWidget(self.banner)

        self.channel = ComboBox()
        self.channel.addItems(["1", "2"])
        self.waveform = ComboBox()
        self.waveform.addItems(["SIN", "SQU", "RAMP", "PULS", "NOIS", "USER", "DC"])
        self.waveform.setCurrentText("SIN")
        self.time_mode = ComboBox()
        self.time_mode.addItems(["Frequency", "Period"])
        self.frequency = _line("1 kHz")
        self.period = _line("1 ms")
        self.level_mode = ComboBox()
        self.level_mode.addItems(
            [self.LEVEL_MODE_AMPLITUDE_OFFSET, self.LEVEL_MODE_HIGH_LOW]
        )
        self.level_mode.setCurrentText(self.LEVEL_MODE_AMPLITUDE_OFFSET)
        self.level_mode_hint = CaptionLabel(
            "Choose how voltage is entered. High/Low allows an asymmetric waveform "
            "directly; Amplitude/Offset describes the same levels as Vpp and center."
        )
        self.level_mode_hint.setWordWrap(True)
        self.level_mode_hint.setObjectName("muted")
        self.high_level = _line("1 mV")
        self.low_level = _line("-1 mV")
        self.vpp = _line("2 mV")
        self.offset = _line("0 V")
        self.load = _line("HIGHZ")
        self.output_polarity = ComboBox()
        self.output_polarity.addItems(["NORM", "INV"])
        self.output_mode = ComboBox()
        self.output_mode.addItems(["NORM", "GAT"])
        self.gate_polarity = ComboBox()
        self.gate_polarity.addItems(["NORM", "INV"])
        self.sync_enabled = CheckBox("SYNC enabled", self)
        self.sync_polarity = ComboBox()
        self.sync_polarity.addItems(["NORM", "INV"])
        self.sync_delay = _line("0 s")
        self.phase = _line("0")
        self.duty = _line("50")
        self.ramp_symmetry = _line("50")
        self.pulse_width = _line("100 us")
        self.pulse_leading = _line("10 ns")
        self.pulse_trailing = _line("10 ns")
        self._level_syncing = False
        self._time_syncing = False
        self._visible_form_channel = 1
        self._channel_form_snapshots: dict[int, RigolConfigurationSnapshot] = {
            1: RigolConfigurationSnapshot(channel=1),
            2: RigolConfigurationSnapshot(channel=2),
        }

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.control_tabs = FluentTabView(self)
        self.control_tabs.setObjectName("rigolControlTabs")

        configure = PrimaryPushButton("Validate and apply waveform")
        self.waveform_apply_button = configure
        self.basic_scroll = self._form_page(
            "Basic parameters",
            "For a standard sine wave, change only Frequency and Amplitude. Other fields already contain safe defaults.",
            (
                ("Channel", self.channel),
                ("Waveform", self.waveform),
                ("Time representation", self.time_mode),
                ("Frequency", self._bounded(self.frequency, "frequency")),
                ("Period", self.period),
                ("Voltage entry mode", self.level_mode),
                ("", self.level_mode_hint),
                ("High Level", self._bounded(self.high_level, "high_level")),
                ("Low Level", self._bounded(self.low_level, "low_level")),
                ("Amplitude (Vpp)", self._bounded(self.vpp, "amplitude_vpp")),
                ("Offset / DC level", self._bounded(self.offset, "offset")),
                ("Phase [deg]", self.phase),
            ),
            (),
        )
        self.basic_form = self.basic_scroll.widget().findChild(QFormLayout)
        shape_apply = PrimaryPushButton("Apply shape parameters")
        self.shape_scroll = self._form_page(
            "Waveform shape",
            "Only parameters applicable to the selected waveform are shown.",
            (
                ("Duty [%] · SQU", self.duty),
                ("Symmetry [%] · RAMP", self.ramp_symmetry),
                ("Pulse width · PULS", self.pulse_width),
                ("Pulse leading edge · PULS", self.pulse_leading),
                ("Pulse trailing edge · PULS", self.pulse_trailing),
            ),
            (shape_apply,),
        )
        self.shape_form = self.shape_scroll.widget().findChild(QFormLayout)

        configure_output = PrimaryPushButton("Apply output path")
        self.sync_phases_button = PushButton("Synchronize CH1/CH2 phases")
        self.sync_phases_button.setEnabled(False)
        self.output_on = PushButton("OUTPUT ON")
        self.output_on.setCheckable(False)
        self.output_off = PushButton("OUTPUT OFF")
        self.output_on.setEnabled(False)
        self.output_off.setEnabled(False)
        self.output_action_bar = CardWidget(self)
        self.output_action_bar.setObjectName("rigolOutputActionBar")
        output_action_layout = QHBoxLayout(self.output_action_bar)
        output_action_layout.setContentsMargins(12, 8, 12, 8)
        self.output_action_context = StrongBodyLabel("Physical output · CH1")
        self.output_action_context.setObjectName("sectionTitle")
        self.output_channel_state = StrongBodyLabel("CH1 OUTPUT UNKNOWN")
        self.output_channel_state.setProperty("outputState", "neutral")
        output_action_note = CaptionLabel(
            "OFF is always available. ON validates the visible waveform first."
        )
        output_action_note.setObjectName("muted")
        output_action_layout.addWidget(self.output_action_context)
        output_action_layout.addWidget(self.output_channel_state)
        output_action_layout.addWidget(output_action_note, 1)
        output_action_layout.addWidget(self.output_on)
        output_action_layout.addWidget(self.output_off)
        # Keep de-energising controls visible independently of the selected
        # configuration tab; operators must not hunt for OUTPUT OFF.
        layout.insertWidget(2, self.output_action_bar)
        self.output_scroll = self._form_page(
            "Output path and SYNC",
            "OUTPUT ON validates and applies the visible channel settings, confirms "
            "the output is OFF, then enables the selected output.",
            (
                ("Generator load setting", self.load),
                ("Output polarity", self.output_polarity),
                ("Output mode", self.output_mode),
                ("Gate polarity", self.gate_polarity),
                ("", self.sync_enabled),
                ("SYNC polarity", self.sync_polarity),
                ("SYNC delay", self.sync_delay),
            ),
            (
                configure_output,
                self.sync_phases_button,
            ),
        )

        self.advanced = FluentTabView(self)
        self.advanced.setObjectName("rigolAdvancedTabs")
        self.advanced.addTab(self._modulation_tab(), "Modulation")
        self.advanced.addTab(self._sweep_tab(), "Sweep")
        self.advanced.addTab(self._burst_tab(), "Burst")
        self.advanced.addTab(self._counter_tab(), "Counter")
        for index in range(self.advanced.count()):
            self.advanced.setTabEnabled(index, False)

        self.control_tabs.addTab(self.basic_scroll, "Basic")
        self.control_tabs.addTab(self.shape_scroll, "Shape")
        self.control_tabs.addTab(self.output_scroll, "Output")
        self.control_tabs.addTab(self.advanced, "Advanced")
        splitter.addWidget(self.control_tabs)

        insight = QWidget()
        insight_layout = QVBoxLayout(insight)
        insight_layout.setContentsMargins(10, 0, 0, 0)
        self.preview_title = StrongBodyLabel("Waveform preview")
        self.preview_title.setObjectName("sectionTitle")
        insight_layout.addWidget(self.preview_title)
        self.preview_plot = SpectrumPlotWidget(legend=False)
        self.preview_plot.set_labels(x="Normalized period", x_unit="", y="Voltage", y_unit="V")
        self.preview_plot.setMinimumHeight(260)
        insight_layout.addWidget(self.preview_plot, 1)

        self.safety_card = CardWidget()
        safety = self.safety_card
        safety.setObjectName("rigolSafetyCard")
        safety_layout = QVBoxLayout(safety)
        safety_title = StrongBodyLabel("Load safety")
        safety_title.setObjectName("sectionTitle")
        safety_layout.addWidget(safety_title)
        self.estimate = BodyLabel("Estimated current: —")
        self.estimate.setObjectName("muted")
        self.estimate.setWordWrap(True)
        safety_layout.addWidget(self.estimate)
        warning = BodyLabel("⚠ This estimate is not a measurement. Verify DUT impedance and configured limits before OUTPUT ON.")
        warning.setObjectName("rigolWarning")
        warning.setWordWrap(True)
        safety_layout.addWidget(warning)
        insight_layout.addWidget(safety)
        splitter.addWidget(insight)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([620, 500])
        layout.addWidget(splitter, 1)

        configure.clicked.connect(self.configure)
        shape_apply.clicked.connect(self.configure)
        configure_output.clicked.connect(self.configure_output)
        self.sync_phases_button.clicked.connect(lambda: self._controller.call("synchronize_phases"))
        self.high_level.editingFinished.connect(self._sync_vpp_offset_from_levels)
        self.low_level.editingFinished.connect(self._sync_vpp_offset_from_levels)
        self.vpp.editingFinished.connect(self._sync_levels_from_vpp_offset)
        self.offset.editingFinished.connect(self._sync_levels_from_vpp_offset)
        self.frequency.editingFinished.connect(self._sync_period_from_frequency)
        self.period.editingFinished.connect(self._sync_frequency_from_period)
        self.frequency.editingFinished.connect(self._submit_active_frequency)
        self.period.editingFinished.connect(self._submit_active_frequency)
        self.high_level.editingFinished.connect(
            lambda: self._submit_active_voltage("high_level", self.high_level)
        )
        self.low_level.editingFinished.connect(
            lambda: self._submit_active_voltage("low_level", self.low_level)
        )
        self.vpp.editingFinished.connect(
            lambda: self._submit_active_voltage("amplitude", self.vpp)
        )
        self.offset.editingFinished.connect(
            lambda: self._submit_active_voltage("offset", self.offset)
        )
        self.output_on.clicked.connect(lambda: self.request_output(True))
        self.output_off.clicked.connect(lambda: self.request_output(False))
        controller.request.connect(self._controller_request_queued)
        controller.result.connect(self._result)
        controller.error.connect(self._error)
        controller.state_changed.connect(self._device_state_changed)
        self.waveform.currentTextChanged.connect(self._update_dynamic_controls)
        self.level_mode.currentTextChanged.connect(self._update_dynamic_controls)
        self.time_mode.currentTextChanged.connect(self._update_dynamic_controls)
        self.waveform.currentTextChanged.connect(self._update_preview)
        self.channel.currentTextChanged.connect(self._update_preview)
        self.channel.currentTextChanged.connect(self._selected_output_channel_changed)
        self.channel.currentTextChanged.connect(self._refresh_rigol_limits)
        for field in (self.frequency, self.period, self.high_level, self.low_level, self.vpp, self.offset, self.duty, self.ramp_symmetry, self.pulse_width):
            field.textChanged.connect(self._update_preview)
        self.load_settings_defaults()
        self._sync_vpp_offset_from_levels()
        self._sync_period_from_frequency()
        self._update_dynamic_controls()
        self._update_preview()
        self._refresh_confirmed_advanced_controls()
        self._refresh_rigol_output_controls()
        self._install_rigol_help(
            configure=configure,
            shape_apply=shape_apply,
            configure_output=configure_output,
            output_on=self.output_on,
            output_off=self.output_off,
        )

    def _active_output_selected(self) -> bool:
        channel = int(self.channel.currentText())
        return self._output_state_known[channel] and self._output_states[channel]

    def _submit_active_frequency(self) -> None:
        if self.waveform.currentText() in {"DC", "NOIS"}:
            return
        channel = self.channel.currentText()
        try:
            value = parse_quantity(self.frequency.text(), DIMENSION_FREQUENCY)
        except Exception as exc:
            self.banner.show_message(
                f"Rigol CH{channel}: invalid frequency: {exc}",
                severity="error",
                timeout_ms=12_000,
            )
            return
        if self._confirmed_carrier_configs[int(channel)] is None:
            self.configure()
            return
        self.quick_setpoint_requested.emit(
            f"rigol.{channel}.frequency",
            format_quantity_auto(value.si_value, DIMENSION_FREQUENCY),
        )

    def _submit_active_voltage(self, field: str, editor: QWidget) -> None:
        channel = self.channel.currentText()
        try:
            value = parse_quantity(editor.text(), DIMENSION_VOLTAGE)  # type: ignore[attr-defined]
        except Exception as exc:
            self.banner.show_message(
                f"Rigol CH{channel}: invalid voltage: {exc}",
                severity="error",
                timeout_ms=12_000,
            )
            return
        if self._confirmed_carrier_configs[int(channel)] is None:
            self.configure()
            return
        self.quick_setpoint_requested.emit(
            f"rigol.{channel}.{field}",
            format_quantity_auto(value.si_value, DIMENSION_VOLTAGE),
        )

    def quick_setpoint_state_changed(self, target: str, state: str, detail: str) -> None:
        if not target.startswith("rigol."):
            return
        if state == "rejected":
            self.banner.show_message(
                f"Active Rigol change rejected: {detail}",
                severity="error",
                timeout_ms=15_000,
            )
        elif state == "applied":
            self.status.emit(f"Rigol active setpoint verified: {detail}")

    def quick_setpoint_value_read(self, target: str, value_si: float) -> None:
        parts = target.split(".")
        if len(parts) != 3 or parts[0] != "rigol" or parts[1] != self.channel.currentText():
            return
        field = parts[2]
        if field == "frequency":
            self.frequency.setText(format_quantity_auto(value_si, DIMENSION_FREQUENCY))
            self._sync_period_from_frequency()
            self._record_visible_quick_readback(int(parts[1]))
            return
        editors = {
            "high_level": self.high_level,
            "low_level": self.low_level,
            "amplitude": self.vpp,
            "offset": self.offset,
        }
        editor = editors.get(field)
        if editor is None:
            return
        editor.setText(format_quantity_auto(value_si, DIMENSION_VOLTAGE))
        if field in {"high_level", "low_level"}:
            self._sync_vpp_offset_from_levels()
        else:
            self._sync_levels_from_vpp_offset()
        self._record_visible_quick_readback(int(parts[1]))

    def apply_execution_readback(
        self,
        channel: int,
        *,
        frequency_hz: float | None = None,
        high_level_v: float | None = None,
        low_level_v: float | None = None,
        output_state: str | None = None,
    ) -> None:
        """Render runner-confirmed values without issuing a manual command.

        Recipe execution owns a separate VISA session, so this method is a
        one-way display projection.  It intentionally does not call a page
        controller, mutate station settings or re-enable any manual control.
        """

        if channel not in {1, 2}:
            return
        if output_state == "on":
            self._set_rigol_channel_output(channel, True)
        elif output_state == "off":
            self._set_rigol_channel_output(channel, False)
        elif output_state == "unknown":
            self._output_state_known[channel] = False
            self._refresh_rigol_output_controls()
        if channel != int(self.channel.currentText()):
            return
        if frequency_hz is not None:
            self.frequency.setText(
                format_quantity_auto(frequency_hz, DIMENSION_FREQUENCY)
            )
            self._sync_period_from_frequency()
        if high_level_v is not None:
            self.high_level.setText(
                format_quantity_auto(high_level_v, DIMENSION_VOLTAGE)
            )
        if low_level_v is not None:
            self.low_level.setText(
                format_quantity_auto(low_level_v, DIMENSION_VOLTAGE)
            )
        if high_level_v is not None or low_level_v is not None:
            self._sync_vpp_offset_from_levels()
        self._record_visible_quick_readback(channel)

    def apply_execution_event(
        self,
        event_name: str,
        event: Mapping[str, object],
        device_state: Mapping[str, object],
        output_status: Mapping[str, str],
    ) -> None:
        """Project all confirmed Rigol channels from one runner snapshot."""
        for channel in (1, 2):
            record = device_state.get(f"channel_{channel}")
            actual = record.get("actual") if isinstance(record, Mapping) else None
            if isinstance(actual, Mapping):
                self._execution_readbacks[channel] = dict(actual)
            state = output_status.get(f"rigol.{channel}")
            if state == "on":
                self._set_rigol_channel_output(channel, True)
            elif state == "off":
                self._set_rigol_channel_output(channel, False)
            elif state == "unknown":
                self._output_state_known[channel] = False

        if event_name in {"action_started", "manual_stage_waiting"}:
            channel_hint = event.get("channel")
            if channel_hint in {1, 2, "1", "2"}:
                self.channel.setCurrentText(str(channel_hint))
            setpoints = event.get("setpoints_si")
            if isinstance(setpoints, Mapping):
                for target in setpoints:
                    parts = str(target).split(".")
                    if len(parts) >= 3 and parts[0] == "rigol" and parts[1] in {"1", "2"}:
                        self.channel.setCurrentText(parts[1])
                        break
        self._render_execution_channel(int(self.channel.currentText()))

    def _render_execution_channel(self, channel: int) -> None:
        actual = self._execution_readbacks.get(channel, {})
        self.apply_execution_readback(
            channel,
            frequency_hz=self._execution_number(actual.get("frequency_hz")),
            high_level_v=self._execution_number(actual.get("high_level_v")),
            low_level_v=self._execution_number(actual.get("low_level_v")),
        )

    @staticmethod
    def _execution_number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    def set_execution_controlled(self, controlled: bool) -> None:
        self.execution_badge.setVisible(controlled)

    def _record_visible_quick_readback(self, channel: int) -> None:
        try:
            config = self._visible_channel_config()
        except Exception:
            self._confirmed_carrier_configs[channel] = None
            return
        if config.channel == channel:
            self._confirmed_carrier_configs[channel] = config

    @staticmethod
    def _set_help(widget: QWidget, title: str, text: str) -> None:
        help_text = f"<b>{title}</b><br>{text}"
        widget.setToolTip(help_text)
        widget.setToolTipDuration(20_000)
        widget.setWhatsThis(help_text)
        widget.setAccessibleDescription(f"{title}. {text}")

    def _install_rigol_help(
        self,
        *,
        configure: QPushButton,
        shape_apply: QPushButton,
        configure_output: QPushButton,
        output_on: QPushButton,
        output_off: QPushButton,
    ) -> None:
        help_items = {
            self.channel: ("Channel", "Selects physical output CH1 or CH2. Each channel has independent settings and safety limits."),
            self.waveform: ("Waveform", "SIN is sine, SQU square, RAMP triangular/ramp, PULS pulse, NOIS noise, USER selects the arbitrary waveform already stored in the generator, and DC is a constant voltage. USER is deliberately programmed in Frequency mode; sample-rate playback and sample upload are not represented by this form."),
            self.time_mode: ("Time representation", "Choose whether the same repetition rate is entered as frequency or period. The application converts one into the other."),
            self.frequency: ("Frequency", "Number of waveform cycles per second. For a standard sine wave this and Amplitude are normally the only values you change."),
            self.period: ("Period", "Duration of one complete waveform cycle. Period equals 1/frequency."),
            self.level_mode: ("Voltage entry mode", "High Level + Low Level programs asymmetric upper and lower voltages directly. Amplitude + Offset programs Vpp and vertical center. Rigol converts between these equivalent representations."),
            self.high_level: ("HighL", "Highest programmed waveform voltage. This is a generator setting/read-back, not a measured DUT voltage."),
            self.low_level: ("LowL", "Lowest programmed waveform voltage. Vpp = HighL − LowL."),
            self.vpp: ("Amplitude (Vpp)", "Peak-to-peak voltage: the difference between maximum and minimum level. A 2 mVpp sine at 0 V offset spans −1 mV to +1 mV."),
            self.offset: ("Offset / DC level", "Moves the waveform vertically around zero. In DC mode this is the constant programmed output voltage."),
            self.phase: ("Phase", "Starting angular position of the waveform in degrees. The safe default is 0°. It matters mainly when comparing or synchronizing channels."),
            self.duty: ("Square duty cycle", "Percentage of each period for which a square wave remains at HighL. 50% gives equal high and low durations."),
            self.ramp_symmetry: ("Ramp symmetry", "Percentage of the period spent on the rising part of a ramp. 50% produces a symmetric triangle."),
            self.pulse_width: ("Pulse width", "Time for which a pulse remains at its active/high level. It must fit within the selected period."),
            self.pulse_leading: ("Leading-edge time", "Programmed transition time from LowL to HighL for a pulse."),
            self.pulse_trailing: ("Trailing-edge time", "Programmed transition time from HighL to LowL for a pulse."),
            self.load: ("Load setting", "Expected external load used by the generator to calculate displayed voltage. HIGHZ means a high-impedance load. This does not measure the real DUT impedance."),
            self.output_polarity: ("Output polarity", "NORM preserves the waveform; INV inverts it around the configured offset."),
            self.output_mode: ("Output mode", "NORM continuously follows the selected waveform. GAT makes output behavior depend on an external gate signal."),
            self.gate_polarity: ("Gate polarity", "Selects which external gate level is considered active when gated output mode is used."),
            self.sync_enabled: ("SYNC output", "Enables the rear-panel synchronization signal associated with this channel. It is a timing reference, not the analog waveform output."),
            self.sync_polarity: ("SYNC polarity", "Selects normal or inverted polarity for the SYNC timing signal."),
            self.sync_delay: ("SYNC delay", "Time shift applied between the waveform timing and its SYNC output."),
            self.mod_enabled: ("Modulation", "Modulation varies a carrier parameter using another signal. Leave disabled for an ordinary sine, square, ramp or pulse."),
            self.mod_type: ("Modulation type", "AM varies amplitude, FM frequency, PM phase; ASK/FSK/PSK switch between discrete states; PWM varies pulse width."),
            self.mod_source: ("Modulation source", "INT uses the generator's internal modulating waveform. EXT uses a signal connected to the rear Mod/Trig connector."),
            self.mod_rate: ("Modulation rate", "Repetition frequency of the internal modulating signal or digital state changes."),
            self.mod_parameter: ("Modulation parameter", "Type-dependent amount, such as AM depth, FM deviation or PM deviation. Its meaning changes with Modulation type."),
            self.mod_shape: ("Internal modulation shape", "Waveform used internally to vary the carrier when Source is INT."),
            self.mod_polarity: ("Modulation polarity", "Defines the logical polarity for supported digital modulation types such as ASK/FSK/PSK."),
            self.sweep_enabled: ("Frequency sweep", "Automatically changes frequency from Start to Stop. Leave disabled for a fixed-frequency signal."),
            self.sweep_start: ("Sweep start", "Frequency at the beginning of the sweep."),
            self.sweep_stop: ("Sweep stop", "Frequency at the end of the sweep. It may be above or below Start."),
            self.sweep_duration: ("Sweep time", "Time used to traverse from the start frequency to the stop frequency."),
            self.sweep_start_hold: ("Start hold", "Time spent at the start frequency before the sweep begins."),
            self.sweep_stop_hold: ("Stop hold", "Time spent at the stop frequency after the sweep reaches it."),
            self.sweep_return_time: ("Return time", "Time used to return from Stop to Start before the next sweep cycle."),
            self.sweep_spacing: ("Sweep spacing", "LIN changes frequency linearly, LOG logarithmically, and STEP advances through discrete frequency points."),
            self.sweep_steps: ("Sweep steps", "Number of discrete points used by STEP sweep mode."),
            self.sweep_trigger: ("Sweep trigger source", "INT starts sweeps internally, EXT waits for the rear trigger input, and MAN waits for the Trigger sweep button."),
            self.sweep_trigger_slope: ("Trigger slope", "Selects rising/positive or falling/negative edge of an external trigger."),
            self.sweep_trigger_out: ("Trigger output", "Emits a timing signal so another instrument can synchronize with the sweep."),
            self.burst_enabled: ("Burst", "Outputs a limited group of cycles after a trigger, or follows an external gate. Leave disabled for continuous output."),
            self.burst_mode: ("Burst mode", "TRIG outputs the configured number of cycles, INF continues after an external or manual trigger, and GAT outputs while the external gate has the active level."),
            self.burst_cycles: ("Burst cycles", "Number of complete carrier cycles emitted for each trigger in TRIG mode."),
            self.burst_phase: ("Burst start phase", "Carrier phase at which each triggered burst begins."),
            self.burst_period: ("Burst period", "Interval between internally triggered bursts. It is not the carrier waveform period."),
            self.burst_delay: ("Burst delay", "Delay from the accepted trigger to the start of the burst."),
            self.burst_trigger: ("Burst trigger source", "INT generates triggers internally, EXT uses the rear input, and MAN uses the Trigger burst button."),
            self.burst_trigger_slope: ("Burst trigger slope", "Selects the active edge of the external burst trigger."),
            self.burst_trigger_out: ("Burst trigger output", "Provides a trigger timing signal for synchronizing other instruments."),
            self.burst_gate_polarity: ("Burst gate polarity", "Selects which level at the external gate input allows waveform output in GAT mode."),
            self.burst_idle: ("Burst idle level", "Determines the output level between bursts: first point, top, center or bottom of the waveform."),
            self.sync_phases_button: ("Synchronize phases", "Aligns the phase reference of CH1 and CH2. It does not enable either output."),
            configure: ("Apply waveform safely", "Validates limits, forces the selected output OFF, writes only parameters relevant to the selected waveform and verifies read-back."),
            shape_apply: ("Apply shape", "Applies the waveform together with its duty, symmetry or pulse-edge parameters while OUTPUT remains OFF."),
            configure_output: ("Apply output path", "Configures load, polarity, gate and SYNC settings while OUTPUT remains OFF."),
            output_on: (
                "OUTPUT ON",
                "Validates and applies the visible channel settings, confirms OUTPUT OFF "
                "and then energizes the physical BNC output.",
            ),
            output_off: ("OUTPUT OFF", "Immediately requests the selected physical output to be disabled."),
        }
        for widget, (title, description) in help_items.items():
            self._set_help(widget, title, description)

        tab_help = {
            0: "Basic waveform selection, frequency/period and voltage levels. Start here for ordinary signals.",
            1: "Parameters specific to square, ramp and pulse shapes. This tab appears only when relevant.",
            2: "Physical output path, load model, polarity, SYNC and protected OUTPUT controls.",
            3: "Optional modulation, frequency sweep and burst functions. Leave these disabled for normal continuous output.",
        }
        for index, description in tab_help.items():
            self.control_tabs.setTabToolTip(index, description)
        self.advanced.setTabToolTip(0, "Vary carrier amplitude, frequency, phase or digital state with an internal or external signal.")
        self.advanced.setTabToolTip(1, "Automatically move carrier frequency between Start and Stop.")
        self.advanced.setTabToolTip(2, "Generate finite cycle groups or externally gated waveform segments.")

    @staticmethod
    def _form_page(
        title: str,
        description: str,
        rows: tuple[tuple[str, QWidget], ...],
        actions: tuple[QPushButton, ...],
    ) -> ScrollArea:
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = CardWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(18, 18, 18, 24)
        content_layout.setSpacing(12)
        heading = StrongBodyLabel(title)
        heading.setObjectName("sectionTitle")
        content_layout.addWidget(heading)
        help_text = CaptionLabel(description)
        help_text.setObjectName("muted")
        help_text.setWordWrap(True)
        content_layout.addWidget(help_text)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setVerticalSpacing(12)
        for label, widget in rows:
            form.addRow(label, widget)
        content_layout.addLayout(form)
        action_grid = QGridLayout()
        for index, button in enumerate(actions):
            action_grid.addWidget(button, index // 2, index % 2)
        content_layout.addLayout(action_grid)
        content_layout.addStretch(1)
        scroll.setWidget(content)
        return scroll

    def _rigol_limit_values(self, key: str) -> tuple[object, object]:
        limits = self._station_settings.rigol.safety.channels[self.channel.currentText()].lab_limits
        quick_field = {
            "frequency": "frequency",
            "amplitude_vpp": "amplitude",
            "offset": "offset",
        }.get(key)
        if quick_field is not None:
            bound = quick_control_safety_bounds(self._station_settings)[
                f"rigol.{self.channel.currentText()}.{quick_field}"
            ]
            return bound.minimum_text, bound.maximum_text
        value = getattr(limits, key)
        if not value.enabled:
            return "HARDWARE", "HARDWARE"
        return value.min, value.max

    def _bounded(self, editor: QWidget, limit_key: str) -> LimitField:
        minimum, maximum = self._rigol_limit_values(limit_key)
        field = LimitField(editor, minimum, maximum)
        field.setProperty("limitKey", limit_key)
        self._limit_fields[editor] = field
        return field

    def _row_widget(self, editor: QWidget) -> QWidget:
        return self._limit_fields.get(editor, editor)

    def _refresh_rigol_limits(self, *_args: object) -> None:
        for field in self._limit_fields.values():
            key = str(field.property("limitKey"))
            field.set_limits(*self._rigol_limit_values(key))

    def set_settings(self, settings: StationSettings) -> None:
        self._station_settings = settings
        # A completion queued under the previous station profile must never
        # resume an OUTPUT-ON chain after limits/permissions have changed.
        self._clear_pending_output()
        self._pending_advanced_requests.clear()
        self._confirmed_carrier_configs = {1: None, 2: None}
        for channel in (1, 2):
            for mode in ("modulation", "sweep", "burst"):
                self._confirmed_advanced_states[channel][mode] = None
        self._refresh_rigol_limits()
        self._refresh_confirmed_advanced_controls()

    @staticmethod
    def _snapshot_from_defaults(
        channel: int, defaults: Mapping[str, object]
    ) -> RigolConfigurationSnapshot:
        fallback = RigolConfigurationSnapshot(channel=channel)
        return RigolConfigurationSnapshot(
            channel=channel,
            waveform=str(defaults.get("waveform", fallback.waveform)),
            time_mode=str(defaults.get("time_mode", fallback.time_mode)),
            frequency=str(defaults.get("frequency", fallback.frequency)),
            level_mode=str(defaults.get("level_mode", fallback.level_mode)),
            high_level=str(defaults.get("high_level", fallback.high_level)),
            low_level=str(defaults.get("low_level", fallback.low_level)),
            output_load=str(
                defaults.get(
                    "output_load", defaults.get("output_load_setting", fallback.output_load)
                )
            ),
            phase_deg=str(defaults.get("phase_deg", fallback.phase_deg)),
            square_duty_percent=str(
                defaults.get("square_duty_percent", fallback.square_duty_percent)
            ),
            ramp_symmetry_percent=str(
                defaults.get("ramp_symmetry_percent", fallback.ramp_symmetry_percent)
            ),
            pulse_width=str(defaults.get("pulse_width", fallback.pulse_width)),
            pulse_leading=str(defaults.get("pulse_leading", fallback.pulse_leading)),
            pulse_trailing=str(defaults.get("pulse_trailing", fallback.pulse_trailing)),
            output_polarity=str(
                defaults.get("output_polarity", fallback.output_polarity)
            ),
            output_mode=str(defaults.get("output_mode", fallback.output_mode)),
            gate_polarity=str(defaults.get("gate_polarity", fallback.gate_polarity)),
            sync_enabled=bool(defaults.get("sync_enabled", fallback.sync_enabled)),
            sync_polarity=str(defaults.get("sync_polarity", fallback.sync_polarity)),
            sync_delay=str(defaults.get("sync_delay", fallback.sync_delay)),
        )

    def load_settings_defaults(self) -> None:
        """Restore both persisted channel forms without issuing device commands."""

        self._channel_form_snapshots = {
            channel: self._snapshot_from_defaults(
                channel,
                self._station_settings.rigol.safety.channels[str(channel)].defaults,
            )
            for channel in (1, 2)
        }
        self._visible_form_channel = int(self.channel.currentText())
        self._load_basic_snapshot(
            self._channel_form_snapshots[self._visible_form_channel]
        )

    def configuration_snapshots(self) -> dict[int, RigolConfigurationSnapshot]:
        """Return complete visible/cached forms for explicit persistence."""

        snapshots = dict(self._channel_form_snapshots)
        channel = int(self.channel.currentText())
        snapshots[channel] = replace(self.configuration_snapshot(), channel=channel)
        return snapshots

    def configuration_snapshot(self) -> RigolConfigurationSnapshot:
        """Return the visible carrier state without communicating with hardware."""

        high, low = self._effective_levels()
        return RigolConfigurationSnapshot(
            channel=int(self.channel.currentText()),
            waveform=self.waveform.currentText(),
            time_mode=self.time_mode.currentText(),
            frequency=self.frequency.text().strip(),
            level_mode=(
                "High Level / Low Level"
                if self.level_mode.currentText() == self.LEVEL_MODE_HIGH_LOW
                else "Amplitude / Offset"
            ),
            high_level=self._format_voltage(high),
            low_level=self._format_voltage(low),
            output_load=self.load.text().strip(),
            phase_deg=self.phase.text().strip(),
            square_duty_percent=self.duty.text().strip(),
            ramp_symmetry_percent=self.ramp_symmetry.text().strip(),
            pulse_width=self.pulse_width.text().strip(),
            pulse_leading=self.pulse_leading.text().strip(),
            pulse_trailing=self.pulse_trailing.text().strip(),
            output_polarity=self.output_polarity.currentText(),
            output_mode=self.output_mode.currentText(),
            gate_polarity=self.gate_polarity.currentText(),
            sync_enabled=self.sync_enabled.isChecked(),
            sync_polarity=self.sync_polarity.currentText(),
            sync_delay=self.sync_delay.text().strip(),
        )

    def configuration_snapshot_for(
        self, channel: int | None = None
    ) -> RigolConfigurationSnapshot:
        """Return the best confirmed/basic form snapshot for one channel."""

        channel = channel or int(self.channel.currentText())
        if channel == int(self.channel.currentText()):
            return self.configuration_snapshot()
        cached = self._channel_form_snapshots.get(channel)
        if cached is not None:
            return cached
        carrier = self._confirmed_carrier_configs.get(channel)
        if carrier is None:
            return replace(self.configuration_snapshot(), channel=channel)
        visible = self.configuration_snapshot()
        return RigolConfigurationSnapshot(
            channel=channel,
            waveform=carrier.waveform,
            time_mode=visible.time_mode,
            frequency=format_quantity_auto(
                carrier.frequency_hz, DIMENSION_FREQUENCY
            ),
            level_mode=visible.level_mode,
            high_level=self._format_voltage(carrier.high_level_v),
            low_level=self._format_voltage(carrier.low_level_v),
            output_load=str(carrier.output_load),
            phase_deg=f"{carrier.phase_deg:.12g}",
            square_duty_percent=f"{carrier.square_duty_percent or 50:.12g}",
            ramp_symmetry_percent=f"{carrier.ramp_symmetry_percent or 50:.12g}",
            pulse_width=(
                format_quantity_auto(carrier.pulse_width_s, DIMENSION_TIME)
                if carrier.pulse_width_s is not None
                else "100 us"
            ),
            pulse_leading=(
                format_quantity_auto(carrier.pulse_leading_s, DIMENSION_TIME)
                if carrier.pulse_leading_s is not None
                else "10 ns"
            ),
            pulse_trailing=(
                format_quantity_auto(carrier.pulse_trailing_s, DIMENSION_TIME)
                if carrier.pulse_trailing_s is not None
                else "10 ns"
            ),
            # Output settings are not confirmed/cached independently by the page.
            # Preserve the visible form values rather than inventing another
            # channel's hardware state.
            output_polarity=visible.output_polarity,
            output_mode=visible.output_mode,
            gate_polarity=visible.gate_polarity,
            sync_enabled=visible.sync_enabled,
            sync_polarity=visible.sync_polarity,
            sync_delay=visible.sync_delay,
        )

    @staticmethod
    def _format_voltage(value_v: float) -> str:
        if 0 < abs(value_v) < 1:
            return f"{value_v * 1e3:.12g} mV"
        return f"{value_v:.12g} V"

    def _new_ui_operation(
        self,
        operation: str,
        payload: object,
        *,
        purpose: str,
        request_id: int | None = None,
    ) -> _RigolUiOperation:
        if request_id is None:
            self._next_ui_request_id += 1
            request_id = self._next_ui_request_id
        channel: int | None = None
        requested_output: bool | None = None
        if operation == "set_output":
            try:
                raw_channel, raw_enabled = payload  # type: ignore[misc]
                channel = int(raw_channel)
                requested_output = bool(raw_enabled)
            except (TypeError, ValueError):
                pass
        else:
            raw_channel = getattr(payload, "channel", None)
            if raw_channel is not None:
                try:
                    channel = int(raw_channel)
                except (TypeError, ValueError):
                    pass
        return _RigolUiOperation(
            request_id=request_id,
            operation=operation,
            channel=channel,
            purpose=purpose,
            payload=payload,
            requested_output=requested_output,
        )

    def _dispatch_ui_operation(
        self,
        operation: str,
        payload: object,
        *,
        purpose: str,
        request_id: int | None = None,
    ) -> _RigolUiOperation:
        request = self._new_ui_operation(
            operation,
            payload,
            purpose=purpose,
            request_id=request_id,
        )
        self._issue_ui_operation(request)
        return request

    def _issue_ui_operation(self, request: _RigolUiOperation) -> None:
        self._issuing_ui_operation = request
        self._issuing_ui_operation_was_queued = False
        try:
            self._controller.call(request.operation, request.payload)
        finally:
            self._issuing_ui_operation = None
            self._issuing_ui_operation_was_queued = False

    def _controller_request_queued(self, operation: str, payload: object) -> None:
        """Record the worker FIFO position, including calls made outside this page."""

        issuing = self._issuing_ui_operation
        if issuing is not None and issuing.operation == operation:
            request = issuing
            self._issuing_ui_operation_was_queued = True
        else:
            request = self._new_ui_operation(
                operation,
                payload,
                purpose="observed_external_request",
            )
        self._queued_ui_operations[operation].append(request)

    def _completion_request(self, operation: str) -> _RigolUiOperation | None:
        # A GUI-thread operation guard reports its error synchronously and does
        # not emit controller.request. Associate that failure with the call
        # currently being issued instead of consuming an older worker request.
        issuing = self._issuing_ui_operation
        if (
            issuing is not None
            and issuing.operation == operation
            and not self._issuing_ui_operation_was_queued
        ):
            return issuing
        queue = self._queued_ui_operations.get(operation)
        if not queue:
            return None
        request = queue.popleft()
        if not queue:
            self._queued_ui_operations.pop(operation, None)
        return request

    def _set_pending_output(
        self,
        request: _RigolUiOperation,
        *,
        enable: bool,
        stage: str,
    ) -> None:
        self._pending_output_enable = enable
        self._pending_output_channel = request.channel
        self._pending_output_request_id = request.request_id
        self._pending_output_stage = stage
        self._refresh_rigol_output_controls()

    def _clear_pending_output(self, *, request_id: int | None = None) -> bool:
        if (
            request_id is not None
            and self._pending_output_request_id != request_id
        ):
            return False
        self._pending_output_enable = False
        self._pending_output_channel = None
        self._pending_output_request_id = None
        self._pending_output_stage = None
        self._pending_output_config = None
        self._refresh_rigol_output_controls()
        return True

    def _pending_output_matches(
        self,
        request: _RigolUiOperation | None,
        *,
        stage: str | None = None,
    ) -> bool:
        if request is None or self._pending_output_request_id is None:
            return False
        return (
            request.request_id == self._pending_output_request_id
            and request.channel == self._pending_output_channel
            and (stage is None or self._pending_output_stage == stage)
        )

    def _advanced_controls(self) -> tuple[tuple[str, CheckBox, QPushButton], ...]:
        controls: list[tuple[str, CheckBox, QPushButton]] = []
        for name, checkbox_name, button_name in (
            ("modulation", "mod_enabled", "mod_apply_button"),
            ("sweep", "sweep_enabled", "sweep_apply_button"),
            ("burst", "burst_enabled", "burst_apply_button"),
        ):
            checkbox = getattr(self, checkbox_name, None)
            button = getattr(self, button_name, None)
            if isinstance(checkbox, CheckBox) and isinstance(button, QPushButton):
                controls.append((name, checkbox, button))
        return tuple(controls)

    def _refresh_confirmed_advanced_controls(self) -> None:
        if not hasattr(self, "channel"):
            return
        channel = int(self.channel.currentText())
        states = self._confirmed_advanced_states[channel]
        for name, checkbox, apply_button in self._advanced_controls():
            confirmed = states[name]
            pending = (channel, name) in self._pending_advanced_requests
            checkbox.blockSignals(True)
            checkbox.setChecked(confirmed is True)
            checkbox.blockSignals(False)
            checkbox.setEnabled(not pending)
            apply_button.setEnabled(not pending)
            semantic = "pending" if pending else "unknown" if confirmed is None else "confirmed"
            checkbox.setProperty("confirmationState", semantic)
            if pending:
                checkbox.setToolTip(
                    f"CH{channel} {name}: waiting for hardware readback."
                )
            elif confirmed is None:
                checkbox.setToolTip(
                    f"CH{channel} {name}: hardware state has not been confirmed in this session."
                )
            else:
                checkbox.setToolTip(
                    f"CH{channel} {name}: hardware confirmed "
                    f"{'ON' if confirmed else 'OFF'}."
                )
            checkbox.style().unpolish(checkbox)
            checkbox.style().polish(checkbox)
        self._refresh_manual_trigger_controls()

    def _refresh_manual_trigger_controls(self, *_args: object) -> None:
        if not all(
            hasattr(self, name)
            for name in (
                "channel",
                "sweep_trigger_button",
                "burst_trigger_button",
            )
        ):
            return
        channel = int(self.channel.currentText())
        output_on = (
            self._output_state_known[channel] and self._output_states[channel]
        )
        states = self._confirmed_advanced_states[channel]
        sweep_ready = (
            output_on
            and states["sweep"] is True
            and self.sweep_trigger.currentText() == "MAN"
        )
        burst_ready = (
            output_on
            and states["burst"] is True
            and self.burst_mode.currentText() in {"TRIG", "INF"}
            and self.burst_trigger.currentText() == "MAN"
        )
        self.sweep_trigger_button.setEnabled(sweep_ready)
        self.burst_trigger_button.setEnabled(burst_ready)
        self.sweep_trigger_button.setToolTip(
            "Ready: sweep is hardware-confirmed, source is MAN, and OUTPUT is ON."
            if sweep_ready
            else "Requires confirmed sweep ON, trigger source MAN, and physical OUTPUT ON."
        )
        self.burst_trigger_button.setToolTip(
            "Ready: triggered burst is hardware-confirmed, source is MAN, and OUTPUT is ON."
            if burst_ready
            else "Requires confirmed burst ON in TRIG or INF mode, source MAN, and physical OUTPUT ON."
        )

    def _queue_advanced_configuration(
        self,
        operation: str,
        mode: str,
        config: object,
    ) -> None:
        channel = int(getattr(config, "channel"))
        key = (channel, mode)
        if key in self._pending_advanced_requests:
            return
        request = self._new_ui_operation(
            operation,
            config,
            purpose=f"advanced_{mode}",
        )
        self._pending_advanced_requests[key] = request.request_id
        self._refresh_confirmed_advanced_controls()
        self._issue_ui_operation(request)

    def _complete_advanced_configuration(
        self,
        request: _RigolUiOperation | None,
        *,
        succeeded: bool,
    ) -> None:
        if request is None or request.channel not in (1, 2):
            return
        mode_for_operation = {
            "configure_modulation": "modulation",
            "configure_sweep": "sweep",
            "configure_burst": "burst",
        }
        mode = mode_for_operation.get(request.operation)
        if mode is None:
            return
        key = (request.channel, mode)
        if self._pending_advanced_requests.get(key) == request.request_id:
            self._pending_advanced_requests.pop(key, None)
        if succeeded and hasattr(request.payload, "enabled"):
            self._confirmed_advanced_states[request.channel][mode] = bool(
                getattr(request.payload, "enabled")
            )
        self._refresh_confirmed_advanced_controls()

    def set_capabilities(self, capabilities: object) -> None:
        supports = getattr(capabilities, "supports", lambda _feature: False)
        features = (
            ("modulation", "MOD"),
            ("frequency_sweep", "SWEEP"),
            ("burst", "BURST"),
            ("phase_sync", "PHASE SYNC"),
            ("counter", "COUNTER"),
            ("harmonics", "HARMONICS (guarded)"),
            ("waveform_sum", "SUM (guarded)"),
        )
        supported = [label for feature, label in features if supports(feature)]
        for index, feature in enumerate(("modulation", "frequency_sweep", "burst", "counter")):
            self.advanced.setTabEnabled(index, bool(supports(feature)))
        self.sync_phases_button.setEnabled(bool(supports("phase_sync")))
        self.capability_badge.setText("Capabilities: " + (" · ".join(supported) if supported else "no extensions"))

    def _device_state_changed(self, state: str) -> None:
        normalized = str(state).strip().lower()
        self._device_state_value = normalized
        self.device_state.setText(normalized.replace("_", " ").upper())
        semantic_state = {
            "verified": "verified",
            "output_off": "verified",
            "output_on": "active",
            "fault": "fault",
            "unknown": "fault",
        }.get(normalized, "neutral")
        for widget in (self.device_led, self.device_state):
            widget.setProperty("deviceState", semantic_state)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        if normalized in {"verified", "output_off"}:
            for channel in (1, 2):
                self._output_states[channel] = False
                self._output_state_known[channel] = True
        elif normalized == "output_on":
            # The controller state confirms that at least one physical output
            # is active, but it does not identify CH1/CH2.  Never paint the
            # currently selected channel as OFF on the strength of that
            # aggregate state alone; wait for the channel-specific result.
            if self._pending_output_channel is not None:
                self._output_state_known[self._pending_output_channel] = False
            elif not any(
                self._output_state_known[channel] and self._output_states[channel]
                for channel in (1, 2)
            ):
                for channel in (1, 2):
                    self._output_state_known[channel] = False
        elif normalized in {"disconnected", "fault", "unknown"}:
            for channel in (1, 2):
                self._output_state_known[channel] = False
                self._confirmed_carrier_configs[channel] = None
                for mode in ("modulation", "sweep", "burst"):
                    self._confirmed_advanced_states[channel][mode] = None
            # Never let a late configure completion resume an energising
            # transaction after a connection or fault transition.
            self._pending_output_enable = False
            self._pending_output_channel = None
            self._pending_output_request_id = None
            self._pending_output_stage = None
            self._pending_output_config = None
            self._pending_advanced_requests.clear()
            self._refresh_confirmed_advanced_controls()
        self._refresh_rigol_output_controls()

    def _selected_output_channel_changed(self, value: str) -> None:
        previous_channel = self._visible_form_channel
        next_channel = int(value)
        if previous_channel != next_channel:
            self._channel_form_snapshots[previous_channel] = replace(
                self.configuration_snapshot(), channel=previous_channel
            )
            self._visible_form_channel = next_channel
            self._load_basic_snapshot(self._channel_form_snapshots[next_channel])
        self.output_action_context.setText(f"Physical output · CH{value}")
        self._refresh_confirmed_advanced_controls()
        self._refresh_rigol_output_controls()
        if self.execution_badge.isVisible():
            self._render_execution_channel(int(value))

    def _load_basic_snapshot(self, snapshot: RigolConfigurationSnapshot) -> None:
        """Display one channel's cached form state without issuing device commands."""

        self.waveform.setCurrentText(snapshot.waveform)
        self.time_mode.setCurrentText(snapshot.time_mode)
        self.frequency.setText(snapshot.frequency)
        self.level_mode.setCurrentText(
            self.LEVEL_MODE_HIGH_LOW
            if snapshot.level_mode == "High Level / Low Level"
            else self.LEVEL_MODE_AMPLITUDE_OFFSET
        )
        self.high_level.setText(snapshot.high_level)
        self.low_level.setText(snapshot.low_level)
        self.load.setText(snapshot.output_load)
        self.phase.setText(snapshot.phase_deg)
        self.duty.setText(snapshot.square_duty_percent)
        self.ramp_symmetry.setText(snapshot.ramp_symmetry_percent)
        self.pulse_width.setText(snapshot.pulse_width)
        self.pulse_leading.setText(snapshot.pulse_leading)
        self.pulse_trailing.setText(snapshot.pulse_trailing)
        self.output_polarity.setCurrentText(snapshot.output_polarity)
        self.output_mode.setCurrentText(snapshot.output_mode)
        self.gate_polarity.setCurrentText(snapshot.gate_polarity)
        self.sync_enabled.setChecked(snapshot.sync_enabled)
        self.sync_polarity.setCurrentText(snapshot.sync_polarity)
        self.sync_delay.setText(snapshot.sync_delay)
        self._sync_period_from_frequency()
        self._sync_vpp_offset_from_levels()
        self._update_dynamic_controls()

    def _set_rigol_channel_output(self, channel: int, enabled: bool) -> None:
        self._output_states[channel] = enabled
        self._output_state_known[channel] = True
        self._refresh_rigol_output_controls()

    def _refresh_rigol_output_controls(self) -> None:
        channel = int(self.channel.currentText())
        known = self._output_state_known[channel]
        enabled = self._output_states[channel] if known else False
        connected = self._device_state_value in {
            "connected",
            "verified",
            "output_off",
            "output_on",
            "compliance",
        }
        can_send_off = connected or self._device_state_value in {"fault", "unknown"}
        pending = self._pending_output_channel is not None
        pending_enable = pending and self._pending_output_enable
        self.output_on.setChecked(enabled)
        self.output_on.setProperty(
            "controlState", "energized" if enabled else "available"
        )
        state_text = "ON" if enabled else "OFF" if known else "UNKNOWN"
        self.output_channel_state.setText(f"CH{channel} OUTPUT {state_text}")
        self.output_channel_state.setProperty(
            "outputState", "active" if enabled else "neutral"
        )
        # Unknown state blocks energising but retains a best-effort OFF action.
        self.output_on.setEnabled(connected and known and not pending)
        self.output_off.setEnabled(
            can_send_off
            and (enabled or not known or pending_enable)
            and not (pending and not self._pending_output_enable)
        )
        self.output_on.setToolTip(
            f"CH{channel} is confirmed OUTPUT ON."
            if enabled
            else f"Enable CH{channel} after validation and confirmed readback."
        )
        self.output_off.setToolTip(
            "Cancel the pending OUTPUT ON sequence and request a confirmed OFF."
            if pending_enable
            else f"Disable CH{channel} and confirm hardware readback."
            if self.output_off.isEnabled()
            else f"CH{channel} is already confirmed OUTPUT OFF."
        )
        for widget in (self.output_on, self.output_channel_state):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self._refresh_manual_trigger_controls()

    def _update_dynamic_controls(self, *_args: object) -> None:
        waveform = self.waveform.currentText()
        is_dc = waveform == "DC"
        has_time = waveform not in {"DC", "NOIS"}
        high_low_mode = self.level_mode.currentText() == self.LEVEL_MODE_HIGH_LOW

        visibility = {
            self.time_mode: has_time,
            self.frequency: has_time and self.time_mode.currentText() == "Frequency",
            self.period: has_time and self.time_mode.currentText() == "Period",
            self.level_mode: not is_dc,
            self.level_mode_hint: not is_dc,
            self.high_level: not is_dc and high_low_mode,
            self.low_level: not is_dc and high_low_mode,
            self.vpp: not is_dc and not high_low_mode,
            self.offset: is_dc or not high_low_mode,
            self.phase: waveform not in {"DC", "NOIS"},
        }
        for widget, visible in visibility.items():
            self.basic_form.setRowVisible(self._row_widget(widget), visible)

        shape_visibility = {
            self.duty: waveform == "SQU",
            self.ramp_symmetry: waveform == "RAMP",
            self.pulse_width: waveform == "PULS",
            self.pulse_leading: waveform == "PULS",
            self.pulse_trailing: waveform == "PULS",
        }
        for widget, visible in shape_visibility.items():
            self.shape_form.setRowVisible(widget, visible)
        self.control_tabs.setTabVisible(1, any(shape_visibility.values()))
        self.control_tabs.setTabVisible(3, True)
        for index in (0, 1, 2):
            self.advanced.setTabVisible(index, not is_dc)
        self.advanced.setTabVisible(3, True)
        if is_dc and self.advanced.currentIndex() in {0, 1, 2}:
            self.advanced.setCurrentIndex(3)
        if is_dc and self.control_tabs.currentIndex() == 1:
            self.control_tabs.setCurrentIndex(0)
        self._update_preview()

    def _effective_levels(self) -> tuple[float, float]:
        if self.waveform.currentText() == "DC":
            value = parse_quantity(self.offset.text(), DIMENSION_VOLTAGE).si_value
            return value, value
        if self.level_mode.currentText() == self.LEVEL_MODE_AMPLITUDE_OFFSET:
            vpp = parse_quantity(self.vpp.text(), DIMENSION_VOLTAGE).si_value
            offset = parse_quantity(self.offset.text(), DIMENSION_VOLTAGE).si_value
            return offset + vpp / 2, offset - vpp / 2
        return (
            parse_quantity(self.high_level.text(), DIMENSION_VOLTAGE).si_value,
            parse_quantity(self.low_level.text(), DIMENSION_VOLTAGE).si_value,
        )

    def _update_preview(self, *_args: object) -> None:
        try:
            high, low = self._effective_levels()
        except Exception:
            high, low = 1e-3, -1e-3
        if high < low:
            high, low = low, high
        amplitude = (high - low) / 2
        center = (high + low) / 2
        waveform = self.waveform.currentText()
        self.preview_title.setText(
            "Waveform preview unavailable for device USER memory"
            if waveform == "USER"
            else "Waveform preview"
        )
        duty = self._bounded_number(self.duty.text(), 50.0, 0.01, 99.99) / 100
        symmetry = self._bounded_number(self.ramp_symmetry.text(), 50.0, 0.01, 99.99) / 100
        try:
            frequency = parse_quantity(self.frequency.text(), DIMENSION_FREQUENCY).si_value
            width = parse_quantity(self.pulse_width.text(), DIMENSION_TIME).si_value
            pulse_duty = min(max(frequency * width, 0.001), 0.999)
        except Exception:
            pulse_duty = 0.2
        x_values: list[float] = []
        y_values: list[float] = []
        for index in range(241):
            x = index / 240
            if waveform == "SIN":
                value = center + amplitude * math.sin(2 * math.pi * x)
            elif waveform == "SQU":
                value = high if x % 1 < duty else low
            elif waveform == "RAMP":
                if x <= symmetry:
                    value = low + (high - low) * x / symmetry
                else:
                    value = high - (high - low) * (x - symmetry) / (1 - symmetry)
            elif waveform == "PULS":
                value = high if x % 1 < pulse_duty else low
            elif waveform == "NOIS":
                noise = 0.58 * math.sin(2 * math.pi * 37 * x) + 0.28 * math.sin(2 * math.pi * 83 * x + 0.7)
                value = center + amplitude * max(-1.0, min(1.0, noise))
            elif waveform == "DC":
                value = high
            else:
                value = center
            x_values.append(x)
            y_values.append(value)
        self.preview_plot.set_trace("Waveform", x_values, y_values, color="#2196f3", primary=True)
        if waveform == "DC":
            title = f"CH{self.channel.currentText()} · DC · Offset {high:.6g} V"
        else:
            title = f"CH{self.channel.currentText()} · {waveform} · HighL {high:.6g} V · LowL {low:.6g} V"
        self.preview_plot.set_title(title)

    @staticmethod
    def _bounded_number(text: str, fallback: float, minimum: float, maximum: float) -> float:
        try:
            value = float(text.replace(",", "."))
        except ValueError:
            value = fallback
        return min(max(value, minimum), maximum)

    def _sync_vpp_offset_from_levels(self) -> None:
        if self._level_syncing:
            return
        try:
            high = parse_quantity(self.high_level.text(), DIMENSION_VOLTAGE).si_value
            low = parse_quantity(self.low_level.text(), DIMENSION_VOLTAGE).si_value
        except Exception:
            return
        self._level_syncing = True
        try:
            self.vpp.setText(self._format_voltage(high - low))
            self.offset.setText(self._format_voltage((high + low) / 2))
        finally:
            self._level_syncing = False
        self._update_preview()

    def _sync_levels_from_vpp_offset(self) -> None:
        if self._level_syncing:
            return
        try:
            offset = parse_quantity(self.offset.text(), DIMENSION_VOLTAGE).si_value
            vpp = (
                0.0
                if self.waveform.currentText() == "DC"
                else parse_quantity(self.vpp.text(), DIMENSION_VOLTAGE).si_value
            )
        except Exception:
            return
        self._level_syncing = True
        try:
            self.high_level.setText(self._format_voltage(offset + vpp / 2))
            self.low_level.setText(self._format_voltage(offset - vpp / 2))
        finally:
            self._level_syncing = False
        self._update_preview()

    def _sync_period_from_frequency(self) -> None:
        if self._time_syncing:
            return
        try:
            frequency = parse_quantity(self.frequency.text(), DIMENSION_FREQUENCY).si_value
            if frequency <= 0:
                return
        except Exception:
            return
        self._time_syncing = True
        try:
            period = 1 / frequency
            if period < 1e-6:
                self.period.setText(f"{period * 1e9:.12g} ns")
            elif period < 1e-3:
                self.period.setText(f"{period * 1e6:.12g} us")
            elif period < 1:
                self.period.setText(f"{period * 1e3:.12g} ms")
            else:
                self.period.setText(f"{period:.12g} s")
        finally:
            self._time_syncing = False

    def _sync_frequency_from_period(self) -> None:
        if self._time_syncing:
            return
        try:
            period = parse_quantity(self.period.text(), DIMENSION_TIME).si_value
            if period <= 0:
                return
        except Exception:
            return
        self._time_syncing = True
        try:
            frequency = 1 / period
            if frequency >= 1e6:
                self.frequency.setText(f"{frequency / 1e6:.12g} MHz")
            elif frequency >= 1e3:
                self.frequency.setText(f"{frequency / 1e3:.12g} kHz")
            else:
                self.frequency.setText(f"{frequency:.12g} Hz")
        finally:
            self._time_syncing = False

    def _modulation_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.mod_enabled = CheckBox("Modulation enabled", self)
        self.mod_type = ComboBox()
        self.mod_type.addItems(["AM", "FM", "PM", "ASK", "FSK", "PSK", "PWM"])
        self.mod_source = ComboBox()
        self.mod_source.addItems(["INT", "EXT"])
        self.mod_rate = _line("1 kHz")
        self.mod_parameter = _line("50")
        self.mod_shape = ComboBox()
        self.mod_shape.addItems(
            ["SIN", "SQU", "TRI", "RAMP", "NRAMP", "NOIS", "USER"]
        )
        self.mod_polarity = ComboBox()
        self.mod_polarity.addItems(["POS", "NEG"])
        apply = PrimaryPushButton("Apply modulation while OUTPUT is OFF")
        self.mod_apply_button = apply
        for label, widget in (
            ("State", self.mod_enabled),
            ("Typ", self.mod_type),
            ("Source", self.mod_source),
                ("Rate / freq.", self._bounded(self.mod_rate, "modulation_rate")),
            ("Type parameter", self.mod_parameter),
            ("Internal shape", self.mod_shape),
            ("Polarity", self.mod_polarity),
            ("", apply),
        ):
            form.addRow(label, widget)
        self.modulation_form = form
        self.mod_type.currentTextChanged.connect(self._update_modulation_parameter_ui)
        self.mod_source.currentTextChanged.connect(
            lambda _value: self._update_modulation_parameter_ui(self.mod_type.currentText())
        )
        self._update_modulation_parameter_ui(self.mod_type.currentText())
        apply.clicked.connect(self.configure_modulation)
        self._set_help(apply, "Apply modulation", "Validates and applies modulation settings while the physical output remains OFF.")
        return self._scroll_widget(tab)

    def _update_modulation_parameter_ui(self, kind: str) -> None:
        labels = {
            "AM": "Depth [%]",
            "FM": "Frequency deviation",
            "PM": "Phase deviation [deg]",
            "ASK": "Alternate amplitude (Vpp)",
            "FSK": "Alternate frequency",
            "PSK": "Alternate phase [deg]",
            "PWM": "Duty deviation [%]",
        }
        defaults = {
            "AM": "50",
            "FM": "100 Hz",
            "PM": "90",
            "ASK": "1 V",
            "FSK": "2 kHz",
            "PSK": "180",
            "PWM": "10",
        }
        label = self.modulation_form.labelForField(self.mod_parameter)
        if label is not None:
            label.setText(labels.get(kind, "Parameter"))
        if not self.mod_parameter.hasFocus():
            self.mod_parameter.setText(defaults.get(kind, "0"))
        internal = self.mod_source.currentText() == "INT"
        self.mod_rate.setEnabled(internal)
        self.mod_shape.setEnabled(internal and kind in {"AM", "FM", "PM", "PWM"})
        self.mod_polarity.setEnabled(kind in {"ASK", "FSK", "PSK"})

    def _sweep_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.sweep_enabled = CheckBox("Sweep enabled", self)
        self.sweep_start = _line("100 Hz")
        self.sweep_stop = _line("1 kHz")
        self.sweep_duration = _line("1 s")
        self.sweep_start_hold = _line("0 s")
        self.sweep_stop_hold = _line("0 s")
        self.sweep_return_time = _line("0 s")
        self.sweep_spacing = ComboBox()
        self.sweep_spacing.addItems(["LIN", "LOG", "STEP"])
        self.sweep_steps = SpinBox(self)
        self.sweep_steps.setRange(2, 1024)
        self.sweep_steps.setValue(10)
        self.sweep_trigger = ComboBox()
        self.sweep_trigger.addItems(["INT", "EXT", "MAN"])
        self.sweep_trigger_slope = ComboBox()
        self.sweep_trigger_slope.addItems(["POS", "NEG"])
        self.sweep_trigger_out = ComboBox()
        self.sweep_trigger_out.addItems(["OFF", "POS", "NEG"])
        apply = PrimaryPushButton("Apply sweep while OUTPUT is OFF")
        self.sweep_apply_button = apply
        trigger = PushButton("Trigger sweep")
        self.sweep_trigger_button = trigger
        for label, widget in (
            ("State", self.sweep_enabled),
            ("Start", self._bounded(self.sweep_start, "frequency")),
            ("Stop", self._bounded(self.sweep_stop, "frequency")),
            ("Time", self._bounded(self.sweep_duration, "sweep_duration")),
            ("Hold start", self.sweep_start_hold),
            ("Hold stop", self.sweep_stop_hold),
            ("Return time", self.sweep_return_time),
            ("Spacing", self.sweep_spacing),
            ("Steps", self._bounded(self.sweep_steps, "sweep_steps")),
            ("Trigger source", self.sweep_trigger),
            ("Trigger slope", self.sweep_trigger_slope),
            ("Trigger output", self.sweep_trigger_out),
            ("", apply),
            ("", trigger),
        ):
            form.addRow(label, widget)
        apply.clicked.connect(self.configure_sweep)
        trigger.clicked.connect(
            lambda: self._dispatch_ui_operation(
                "trigger_sweep",
                int(self.channel.currentText()),
                purpose="manual_sweep_trigger",
            )
        )
        self.sweep_trigger.currentTextChanged.connect(
            self._refresh_manual_trigger_controls
        )
        self._set_help(apply, "Apply sweep", "Validates and programs the sweep while the physical output remains OFF.")
        self._set_help(trigger, "Manual sweep trigger", "Starts one sweep when Trigger source is MAN. It does not bypass output safety interlocks.")
        return self._scroll_widget(tab)

    def _burst_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.burst_enabled = CheckBox("Burst enabled", self)
        self.burst_mode = ComboBox()
        self.burst_mode.addItems(["TRIG", "INF", "GAT"])
        self.burst_cycles = SpinBox(self)
        self.burst_cycles.setRange(1, 1_000_000)
        self.burst_cycles.setValue(1)
        self.burst_phase = _line("0")
        self.burst_period = _line("1 ms")
        self.burst_delay = _line("0 s")
        self.burst_trigger = ComboBox()
        self.burst_trigger.addItems(["INT", "EXT", "MAN"])
        self.burst_trigger_slope = ComboBox()
        self.burst_trigger_slope.addItems(["POS", "NEG"])
        self.burst_trigger_out = ComboBox()
        self.burst_trigger_out.addItems(["OFF", "POS", "NEG"])
        self.burst_gate_polarity = ComboBox()
        self.burst_gate_polarity.addItems(["NORM", "INV"])
        self.burst_idle = ComboBox()
        self.burst_idle.addItems(["FPT", "TOP", "CENTER", "BOTTOM"])
        apply = PrimaryPushButton("Apply burst while OUTPUT is OFF")
        self.burst_apply_button = apply
        trigger = PushButton("Trigger burst")
        self.burst_trigger_button = trigger
        for label, widget in (
            ("State", self.burst_enabled),
            ("Mode", self.burst_mode),
            ("Cycles", self._bounded(self.burst_cycles, "burst_cycles")),
            ("Phase [deg]", self.burst_phase),
            ("Period", self._bounded(self.burst_period, "burst_period")),
            ("Delay", self.burst_delay),
            ("Trigger source", self.burst_trigger),
            ("Trigger slope", self.burst_trigger_slope),
            ("Trigger output", self.burst_trigger_out),
            ("Gate polarity", self.burst_gate_polarity),
            ("Idle", self.burst_idle),
            ("", apply),
            ("", trigger),
        ):
            form.addRow(label, widget)
        apply.clicked.connect(self.configure_burst)
        trigger.clicked.connect(
            lambda: self._dispatch_ui_operation(
                "trigger_burst",
                int(self.channel.currentText()),
                purpose="manual_burst_trigger",
            )
        )
        self.burst_trigger.currentTextChanged.connect(
            self._refresh_manual_trigger_controls
        )
        self.burst_mode.currentTextChanged.connect(
            self._refresh_manual_trigger_controls
        )
        self.burst_mode.currentTextChanged.connect(
            lambda mode: self.burst_cycles.setEnabled(mode == "TRIG")
        )
        self._set_help(apply, "Apply burst", "Validates and programs burst settings while the physical output remains OFF.")
        self._set_help(trigger, "Manual burst trigger", "Emits one configured burst when Trigger source is MAN.")
        return self._scroll_widget(tab)

    def _counter_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.counter_state = ComboBox()
        self.counter_state.addItems(["ON", "RUN", "STOP", "SINGLE", "OFF"])
        self.counter_coupling = ComboBox()
        self.counter_coupling.addItems(["AC", "DC"])
        self.counter_gate = ComboBox()
        self.counter_gate.addItems(["AUTO", "USER1", "USER2", "USER3", "USER4", "USER5", "USER6"])
        self.counter_hf_rejection = CheckBox("High-frequency rejection", self)
        self.counter_level = _line("0 V")
        self.counter_sensitivity = _line("25")
        self.counter_readout = BodyLabel("No counter measurement yet")
        self.counter_readout.setWordWrap(True)
        apply = PrimaryPushButton("Apply counter settings")
        read = PushButton("Read counter")
        for label, widget in (
            ("State", self.counter_state),
            ("Input coupling", self.counter_coupling),
            ("Gate time", self.counter_gate),
            ("", self.counter_hf_rejection),
            ("Trigger level", self.counter_level),
            ("Sensitivity [%]", self.counter_sensitivity),
            ("", apply),
            ("", read),
            ("Measurement", self.counter_readout),
        ):
            form.addRow(label, widget)
        apply.clicked.connect(self.configure_counter)
        read.clicked.connect(lambda: self._controller.call("read_counter"))
        self._set_help(apply, "Apply counter", "Configures only the rear-panel frequency counter; analog outputs are not changed.")
        self._set_help(read, "Read counter", "Reads frequency, period, duty cycle and positive/negative pulse width.")
        return self._scroll_widget(tab)

    def configure_counter(self) -> None:
        try:
            config = RigolCounterConfig(
                state=self.counter_state.currentText(),  # type: ignore[arg-type]
                coupling=self.counter_coupling.currentText(),  # type: ignore[arg-type]
                gate_time=self.counter_gate.currentText(),  # type: ignore[arg-type]
                high_frequency_rejection=self.counter_hf_rejection.isChecked(),
                trigger_level_v=parse_quantity(self.counter_level.text(), DIMENSION_VOLTAGE).si_value,
                sensitivity_percent=float(self.counter_sensitivity.text().replace(",", ".")),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Rigol counter", str(exc))
            return
        self._controller.call("configure_counter", config)

    @staticmethod
    def _scroll_widget(content: QWidget) -> ScrollArea:
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        return scroll

    def _visible_channel_config(self) -> RigolChannelConfig:
        high_level, low_level = self._effective_levels()
        frequency_hz = (
            1.0
            if self.waveform.currentText() in {"DC", "NOIS"}
            else parse_quantity(self.frequency.text(), DIMENSION_FREQUENCY).si_value
        )
        config = RigolChannelConfig(
            channel=int(self.channel.currentText()),
            waveform=self.waveform.currentText(),
            frequency_hz=frequency_hz,
            high_level_v=high_level,
            low_level_v=low_level,
            output_load=self.load.text().strip(),
            phase_deg=float(self.phase.text().replace(",", ".")),
            square_duty_percent=float(self.duty.text().replace(",", ".")) if self.waveform.currentText() == "SQU" else None,
            ramp_symmetry_percent=float(self.ramp_symmetry.text().replace(",", ".")) if self.waveform.currentText() == "RAMP" else None,
            pulse_width_s=parse_quantity(self.pulse_width.text(), DIMENSION_TIME).si_value if self.waveform.currentText() == "PULS" else None,
            pulse_leading_s=parse_quantity(self.pulse_leading.text(), DIMENSION_TIME).si_value if self.waveform.currentText() == "PULS" else None,
            pulse_trailing_s=parse_quantity(self.pulse_trailing.text(), DIMENSION_TIME).si_value if self.waveform.currentText() == "PULS" else None,
        )
        validate_rigol_waveform(
            channel=self._station_settings.rigol.safety.channels[str(config.channel)],
            safety=self._station_settings.rigol.safety,
            waveform=config.waveform,
            frequency=config.frequency_hz,
            high_level=config.high_level_v,
            low_level=config.low_level_v,
            output_load=config.output_load,
        )
        return config

    def configure(self, _checked: bool = False) -> None:
        del _checked
        try:
            config = self._visible_channel_config()
        except Exception as exc:
            self.banner.show_message(f"Invalid waveform settings: {exc}")
            return
        self._dispatch_ui_operation(
            "configure", config, purpose="manual_configure"
        )

    def configure_output(self) -> None:
        try:
            config = self._visible_output_config()
        except Exception as exc:
            QMessageBox.warning(self, "Output Rigol", str(exc))
            return
        self._dispatch_ui_operation(
            "configure_output", config, purpose="configure_output_path"
        )

    def _visible_output_config(self) -> RigolOutputConfig:
        return RigolOutputConfig(
            channel=int(self.channel.currentText()),
            output_load=self.load.text().strip(),
            polarity=self.output_polarity.currentText(),  # type: ignore[arg-type]
            mode=self.output_mode.currentText(),  # type: ignore[arg-type]
            gate_polarity=self.gate_polarity.currentText(),  # type: ignore[arg-type]
            sync_enabled=self.sync_enabled.isChecked(),
            sync_polarity=self.sync_polarity.currentText(),  # type: ignore[arg-type]
            sync_delay_s=parse_quantity(
                self.sync_delay.text(), DIMENSION_TIME
            ).si_value,
        )

    def configure_modulation(self) -> None:
        if not self.mod_enabled.isChecked():
            config = RigolModulationConfig(
                channel=int(self.channel.currentText()),
                enabled=False,
                modulation_type=self.mod_type.currentText(),  # type: ignore[arg-type]
            )
            self._queue_advanced_configuration(
                "configure_modulation", "modulation", config
            )
            return
        try:
            kind = self.mod_type.currentText()
            if kind in {"FM", "FSK"}:
                parameter = parse_quantity(
                    self.mod_parameter.text(), DIMENSION_FREQUENCY
                ).si_value
            elif kind == "ASK":
                parameter = parse_quantity(
                    self.mod_parameter.text(), DIMENSION_VOLTAGE
                ).si_value
            else:
                parameter = float(self.mod_parameter.text().replace(",", "."))
            config = RigolModulationConfig(
                channel=int(self.channel.currentText()),
                enabled=self.mod_enabled.isChecked(),
                modulation_type=kind,  # type: ignore[arg-type]
                source=self.mod_source.currentText(),  # type: ignore[arg-type]
                rate_hz=(
                    parse_quantity(self.mod_rate.text(), DIMENSION_FREQUENCY).si_value
                    if self.mod_source.currentText() == "INT"
                    else 1.0
                ),
                parameter=parameter,
                internal_shape=self.mod_shape.currentText(),  # type: ignore[arg-type]
                polarity=self.mod_polarity.currentText(),  # type: ignore[arg-type]
            )
        except Exception as exc:
            self._refresh_confirmed_advanced_controls()
            QMessageBox.warning(self, "Rigol modulation", str(exc))
            return
        self._queue_advanced_configuration(
            "configure_modulation", "modulation", config
        )

    def configure_sweep(self) -> None:
        if not self.sweep_enabled.isChecked():
            config = RigolFrequencySweepConfig(
                channel=int(self.channel.currentText()),
                enabled=False,
                start_hz=1.0,
                stop_hz=2.0,
                duration_s=1.0,
                steps=2,
            )
            self._queue_advanced_configuration(
                "configure_sweep", "sweep", config
            )
            return
        try:
            config = RigolFrequencySweepConfig(
                channel=int(self.channel.currentText()),
                enabled=self.sweep_enabled.isChecked(),
                start_hz=parse_quantity(self.sweep_start.text(), DIMENSION_FREQUENCY).si_value,
                stop_hz=parse_quantity(self.sweep_stop.text(), DIMENSION_FREQUENCY).si_value,
                duration_s=parse_quantity(self.sweep_duration.text(), "time").si_value,
                spacing=self.sweep_spacing.currentText(),  # type: ignore[arg-type]
                steps=self.sweep_steps.value(),
                start_hold_s=parse_quantity(self.sweep_start_hold.text(), DIMENSION_TIME).si_value,
                stop_hold_s=parse_quantity(self.sweep_stop_hold.text(), DIMENSION_TIME).si_value,
                return_time_s=parse_quantity(self.sweep_return_time.text(), DIMENSION_TIME).si_value,
                trigger_source=self.sweep_trigger.currentText(),  # type: ignore[arg-type]
                trigger_slope=self.sweep_trigger_slope.currentText(),  # type: ignore[arg-type]
                trigger_output=self.sweep_trigger_out.currentText(),
            )
            validate_rigol_frequency_sweep(
                channel=self._station_settings.rigol.safety.channels[self.channel.currentText()],
                start_hz=config.start_hz,
                stop_hz=config.stop_hz,
                duration_s=config.duration_s,
                steps=config.steps,
                start_hold_s=config.start_hold_s,
                stop_hold_s=config.stop_hold_s,
                return_time_s=config.return_time_s,
            )
        except Exception as exc:
            self._refresh_confirmed_advanced_controls()
            QMessageBox.warning(self, "Sweep Rigol", str(exc))
            return
        self._queue_advanced_configuration("configure_sweep", "sweep", config)

    def configure_burst(self) -> None:
        if not self.burst_enabled.isChecked():
            config = RigolBurstConfig(
                channel=int(self.channel.currentText()),
                enabled=False,
            )
            self._queue_advanced_configuration(
                "configure_burst", "burst", config
            )
            return
        try:
            config = RigolBurstConfig(
                channel=int(self.channel.currentText()),
                enabled=self.burst_enabled.isChecked(),
                mode=self.burst_mode.currentText(),  # type: ignore[arg-type]
                cycles=self.burst_cycles.value(),
                phase_deg=float(self.burst_phase.text().replace(",", ".")),
                period_s=parse_quantity(self.burst_period.text(), "time").si_value,
                delay_s=parse_quantity(self.burst_delay.text(), "time").si_value,
                trigger_source=self.burst_trigger.currentText(),  # type: ignore[arg-type]
                trigger_slope=self.burst_trigger_slope.currentText(),  # type: ignore[arg-type]
                trigger_output=self.burst_trigger_out.currentText(),
                gate_polarity=self.burst_gate_polarity.currentText(),  # type: ignore[arg-type]
                idle=self.burst_idle.currentText(),  # type: ignore[arg-type]
            )
        except Exception as exc:
            self._refresh_confirmed_advanced_controls()
            QMessageBox.warning(self, "Burst Rigol", str(exc))
            return
        self._queue_advanced_configuration("configure_burst", "burst", config)

    def request_output(self, enabled: bool) -> None:
        channel = int(self.channel.currentText())
        if (
            self._output_state_known[channel]
            and self._output_states[channel] == enabled
            and not (
                not enabled
                and self._pending_output_request_id is not None
                and self._pending_output_enable
            )
        ):
            return
        if not enabled:
            # OFF cancels any not-yet-energised ON chain.  The in-flight
            # configure may still complete, but its request id can no longer
            # authorize a later OUTPUT ON.
            self._clear_pending_output()
            request = self._new_ui_operation(
                "set_output",
                (channel, False),
                purpose="output_disable",
            )
            self._set_pending_output(request, enable=False, stage="set_output")
            self._issue_ui_operation(request)
            return
        if self._pending_output_request_id is not None:
            return
        try:
            config = self._visible_channel_config()
            output_config = self._visible_output_config()
        except Exception as exc:
            QMessageBox.warning(
                self,
                f"Rigol CH{channel} OUTPUT ON blocked",
                str(exc).strip()
                or "The visible Rigol settings could not be validated.",
            )
            return
        carrier_confirmed = self._confirmed_carrier_configs[channel] == config
        request = self._new_ui_operation(
            "configure_output" if carrier_confirmed else "configure",
            output_config if carrier_confirmed else config,
            purpose=(
                "output_enable_output_path"
                if carrier_confirmed
                else "output_enable_validation"
            ),
        )
        stage = "configure_output" if carrier_confirmed else "configure"
        self._set_pending_output(request, enable=True, stage=stage)
        self._pending_output_config = output_config
        self.status.emit(
            f"Rigol CH{channel}: "
            + (
                "applying and validating output path before OUTPUT ON"
                if carrier_confirmed
                else "applying and validating visible settings before OUTPUT ON"
            )
        )
        self._issue_ui_operation(request)

    def _result(self, operation: str, result: object) -> None:
        request = self._completion_request(operation)
        if operation == "configure" and hasattr(result, "peak_absolute_current_a"):
            estimate = result
            self.estimate.setText(
                "Estimated load current (not measured): "
                f"{estimate.peak_absolute_current_a * 1e3:.6g} mA; "
                f"estimated DUT power: {estimate.peak_estimated_dut_power_w * 1e3:.6g} mW; "
                f"Vth High/Low: {estimate.open_circuit_high_v:.6g} / {estimate.open_circuit_low_v:.6g} V"
            )
            channel = (
                request.channel
                if request is not None and request.channel in (1, 2)
                else int(self.channel.currentText())
            )
            if request is not None and isinstance(
                request.payload, RigolChannelConfig
            ):
                self._confirmed_carrier_configs[channel] = request.payload
            self._set_rigol_channel_output(channel, False)
            for mode in ("modulation", "sweep", "burst"):
                self._confirmed_advanced_states[channel][mode] = False
            self._refresh_confirmed_advanced_controls()
            if self._pending_output_matches(request, stage="configure"):
                self.status.emit(
                    f"Rigol CH{channel}: carrier valid; validating output path"
                )
                output_request = self._new_ui_operation(
                    "configure_output",
                    self._pending_output_config,
                    purpose="output_enable_output_path",
                    request_id=request.request_id,
                )
                self._set_pending_output(
                    output_request, enable=True, stage="configure_output"
                )
                self._issue_ui_operation(output_request)
            else:
                self.status.emit("Rigol configured while OUTPUT is OFF")
        elif operation in {"configure_modulation", "configure_sweep", "configure_burst"}:
            self._complete_advanced_configuration(request, succeeded=True)
            if request is not None and request.channel in (1, 2):
                self._set_rigol_channel_output(request.channel, False)
            self.status.emit(f"Rigol: {operation} configured while OUTPUT is OFF")
        elif operation == "configure_output":
            channel = (
                request.channel
                if request is not None and request.channel in (1, 2)
                else int(self.channel.currentText())
            )
            self._set_rigol_channel_output(channel, False)
            if self._pending_output_matches(request, stage="configure_output"):
                self.status.emit(
                    f"Rigol CH{channel}: output path valid; enabling OUTPUT"
                )
                output_request = self._new_ui_operation(
                    "set_output",
                    (channel, True),
                    purpose="output_enable",
                    request_id=request.request_id,
                )
                self._set_pending_output(
                    output_request, enable=True, stage="set_output"
                )
                self._issue_ui_operation(output_request)
            else:
                self.status.emit("Rigol: output path confirmed while OUTPUT is OFF")
        elif operation == "set_output":
            channel = (
                request.channel
                if request is not None and request.channel in (1, 2)
                else int(self.channel.currentText())
            )
            if self._pending_output_matches(request, stage="set_output"):
                self._clear_pending_output(request_id=request.request_id)
            self._set_rigol_channel_output(channel, bool(result))
            self.status.emit(
                f"Rigol CH{channel} OUTPUT "
                f"{'ON' if bool(result) else 'OFF'}"
            )
        elif operation == "synchronize_phases":
            self.status.emit("Rigol: CH1/CH2 phases synchronized after capability confirmation")
        elif operation == "configure_counter":
            self.status.emit("Rigol frequency counter settings verified")
        elif operation == "read_counter" and isinstance(result, RigolCounterReading):
            self.counter_readout.setText(
                f"Frequency {format_quantity_auto(result.frequency_hz, DIMENSION_FREQUENCY)} · "
                f"Period {format_quantity_auto(result.period_s, DIMENSION_TIME)} · "
                f"Duty {result.duty_percent:.9g}% · +Width "
                f"{format_quantity_auto(result.positive_width_s, DIMENSION_TIME)} · -Width "
                f"{format_quantity_auto(result.negative_width_s, DIMENSION_TIME)}"
            )

    def _error(self, operation: str, error: str) -> None:
        request = self._completion_request(operation)
        if operation in {
            "configure",
            "set_output",
            "configure_modulation",
            "configure_output",
            "configure_sweep",
            "configure_burst",
            "trigger_sweep",
            "trigger_burst",
            "synchronize_phases",
            "configure_counter",
            "read_counter",
        }:
            if operation in {
                "configure_modulation",
                "configure_sweep",
                "configure_burst",
            }:
                self._complete_advanced_configuration(request, succeeded=False)
            if operation in {"configure", "configure_output", "set_output"}:
                channel = (
                    request.channel
                    if request is not None and request.channel in (1, 2)
                    else int(self.channel.currentText())
                )
                if self._pending_output_matches(request):
                    self._clear_pending_output(request_id=request.request_id)
                if self._device_state_value in {"verified", "output_off"}:
                    self._output_states[channel] = False
                    self._output_state_known[channel] = True
                else:
                    self._output_state_known[channel] = False
                self._refresh_rigol_output_controls()
            QMessageBox.warning(
                self,
                "Rigol",
                error.strip() or f"Rigol operation {operation} failed without details.",
            )
