"""Main PySide6 application window and manual-control pages."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PySide6.QtCharts import QChart, QChartView, QLineSeries, QValueAxis
from PySide6.QtCore import QPointF, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.devices.anritsu import AnritsuAdapter, SpectrumConfig, SpectrumTrace
from app.devices.keithley import KeithleyAdapter, KeithleySourceRequest
from app.devices.rigol import (
    RigolAdapter,
    RigolBurstConfig,
    RigolChannelConfig,
    RigolFrequencySweepConfig,
    RigolModulationConfig,
    RigolOutputConfig,
)
from app.devices.simulators import SimulatedVisaFactory, simulated_station_settings
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_RESISTANCE,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    parse_quantity,
)
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from app.storage import Hdf5RunReader, RunDetail, StoredPoint
from app.ui.settings_page import SettingsPage
from app.ui.run_worker import RunController
from app.ui.workers import DeviceController


def _line(value: str, width: int = 14) -> QLineEdit:
    edit = QLineEdit(value)
    edit.setMinimumWidth(width * 8)
    return edit


class DeviceCard(QFrame):
    connect_requested = Signal()
    disconnect_requested = Signal()

    def __init__(self, title: str, resource: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deviceCard")
        layout = QVBoxLayout(self)
        name = QLabel(title)
        name.setObjectName("cardTitle")
        self.state = QLabel("DISCONNECTED")
        self.state.setObjectName("stateDisconnected")
        self.identity = QLabel(resource or "No VISA resource configured")
        self.identity.setWordWrap(True)
        self.identity.setObjectName("muted")
        controls = QHBoxLayout()
        connect = QPushButton("Connect")
        disconnect = QPushButton("Disconnect")
        controls.addWidget(connect)
        controls.addWidget(disconnect)
        layout.addWidget(name)
        layout.addWidget(self.state)
        layout.addWidget(self.identity)
        layout.addStretch(1)
        layout.addLayout(controls)
        connect.clicked.connect(self.connect_requested)
        disconnect.clicked.connect(self.disconnect_requested)

    def update_state(self, state: str) -> None:
        self.state.setText(state.upper())
        self.state.setObjectName("state" + "".join(part.title() for part in state.split("_")))
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)

    def update_identity(self, value: object) -> None:
        idn = getattr(value, "idn", None)
        if idn:
            self.identity.setText(str(idn))


class DashboardPage(QWidget):
    emergency_requested = Signal()

    def __init__(self, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel("Measurement station")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Connect the instruments, verify the profile, then prepare a measurement recipe.")
        subtitle.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        grid = QGridLayout()
        self.cards = {
            "rigol": DeviceCard(settings.rigol.display_name, settings.rigol.connection.resource),
            "keithley": DeviceCard(settings.keithley.display_name, settings.keithley.connection.resource),
            "anritsu": DeviceCard(settings.anritsu.display_name, settings.anritsu.connection.resource),
        }
        for column, card in enumerate(self.cards.values()):
            grid.addWidget(card, 0, column)
        layout.addLayout(grid)
        self.checklist = QLabel()
        self.checklist.setObjectName("checklist")
        self.checklist.setWordWrap(True)
        layout.addWidget(self.checklist)
        emergency = QPushButton("E-STOP — disable all outputs")
        emergency.setObjectName("emergencyButton")
        emergency.setMinimumHeight(44)
        layout.addWidget(emergency)
        layout.addStretch(1)
        emergency.clicked.connect(self.emergency_requested)
        self.update_settings(settings)

    def update_settings(self, settings: StationSettings) -> None:
        profile = "✓ approved" if not settings.outputs_locked else "✕ unverified — outputs locked"
        rigol_serial = "✓" if settings.rigol.identity.require_serial_match else "✕"
        anritsu = "✓" if settings.anritsu.safety.acquisition_allowed else "✕ RF input limit required"
        self.checklist.setText(
            "Readiness:\n"
            f"• Profile: {profile}\n"
            f"• Rigol serial-number binding: {rigol_serial}\n"
            f"• Anritsu acquisition: {anritsu}\n"
            "• Declare the DUT and verify every limit before OUTPUT ON."
        )


class RigolPage(QWidget):
    status = Signal(str)

    def __init__(self, controller: DeviceController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        header = QFrame()
        header.setObjectName("rigolHero")
        header_layout = QHBoxLayout(header)
        heading = QVBoxLayout()
        title = QLabel("Rigol DG1032Z")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Function generator · channel control and safe output activation")
        subtitle.setObjectName("muted")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header_layout.addLayout(heading, 1)

        self.device_led = QLabel("●")
        self.device_led.setObjectName("rigolLed")
        self.device_state = QLabel("DISCONNECTED")
        self.device_state.setObjectName("rigolState")
        state_box = QVBoxLayout()
        state_line = QHBoxLayout()
        state_line.addWidget(self.device_led)
        state_line.addWidget(self.device_state)
        state_box.addLayout(state_line)
        self.capability_badge = QLabel("Capabilities: awaiting identification")
        self.capability_badge.setObjectName("rigolBadge")
        state_box.addWidget(self.capability_badge)
        header_layout.addLayout(state_box)
        layout.addWidget(header)

        self.channel = QComboBox()
        self.channel.addItems(["1", "2"])
        self.waveform = QComboBox()
        self.waveform.addItems(["SIN", "SQU", "RAMP", "PULS", "NOIS", "DC"])
        self.waveform.setCurrentText("SIN")
        self.time_mode = QComboBox()
        self.time_mode.addItems(["Frequency", "Period"])
        self.frequency = _line("1 kHz")
        self.period = _line("1 ms")
        self.level_mode = QComboBox()
        self.level_mode.addItems(["HighL / LowL", "Amplitude / Offset"])
        self.level_mode.setCurrentText("Amplitude / Offset")
        self.high_level = _line("1 mV")
        self.low_level = _line("-1 mV")
        self.vpp = _line("2 mV")
        self.offset = _line("0 V")
        self.load = _line("HIGHZ")
        self.output_polarity = QComboBox()
        self.output_polarity.addItems(["NORM", "INV"])
        self.output_mode = QComboBox()
        self.output_mode.addItems(["NORM", "GAT"])
        self.gate_polarity = QComboBox()
        self.gate_polarity.addItems(["NORM", "INV"])
        self.sync_enabled = QCheckBox("SYNC enabled")
        self.sync_polarity = QComboBox()
        self.sync_polarity.addItems(["NORM", "INV"])
        self.sync_delay = _line("0 s")
        self.dut_impedance = _line("50 ohm")
        self.phase = _line("0")
        self.duty = _line("50")
        self.ramp_symmetry = _line("50")
        self.pulse_width = _line("100 us")
        self.pulse_leading = _line("10 ns")
        self.pulse_trailing = _line("10 ns")
        self._level_syncing = False
        self._time_syncing = False

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.control_tabs = QTabWidget()
        self.control_tabs.setObjectName("rigolControlTabs")

        configure = QPushButton("Validate and apply waveform")
        configure.setObjectName("primaryButton")
        self.basic_scroll = self._form_page(
            "Basic parameters",
            "For a standard sine wave, change only Frequency and Amplitude. Other fields already contain safe defaults.",
            (
                ("Channel", self.channel),
                ("Waveform", self.waveform),
                ("Time representation", self.time_mode),
                ("Frequency", self.frequency),
                ("Period", self.period),
                ("Level representation", self.level_mode),
                ("HighL", self.high_level),
                ("LowL", self.low_level),
                ("Amplitude (Vpp)", self.vpp),
                ("Offset / DC level", self.offset),
                ("Minimum DUT impedance", self.dut_impedance),
                ("Phase [deg]", self.phase),
            ),
            (configure,),
        )
        self.basic_form = self.basic_scroll.widget().findChild(QFormLayout)
        shape_apply = QPushButton("Apply shape parameters")
        shape_apply.setObjectName("primaryButton")
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

        configure_output = QPushButton("Apply output path")
        configure_output.setObjectName("primaryButton")
        self.sync_phases_button = QPushButton("Synchronize CH1/CH2 phases")
        self.sync_phases_button.setEnabled(False)
        arm = QPushButton("ARM (30 s)")
        arm.setObjectName("warningButton")
        output_on = QPushButton("OUTPUT ON")
        output_on.setObjectName("outputOnButton")
        output_off = QPushButton("OUTPUT OFF")
        output_off.setObjectName("outputOffButton")
        self.output_scroll = self._form_page(
            "Output path and SYNC",
            "Output-path changes are allowed only while OUTPUT is OFF. OUTPUT ON requires ARM and confirmation.",
            (
                ("Generator load setting", self.load),
                ("Output polarity", self.output_polarity),
                ("Output mode", self.output_mode),
                ("Gate polarity", self.gate_polarity),
                ("", self.sync_enabled),
                ("SYNC polarity", self.sync_polarity),
                ("SYNC delay", self.sync_delay),
            ),
            (configure_output, self.sync_phases_button, arm, output_on, output_off),
        )

        self.advanced = QTabWidget()
        self.advanced.setObjectName("rigolAdvancedTabs")
        self.advanced.addTab(self._modulation_tab(), "Modulation")
        self.advanced.addTab(self._sweep_tab(), "Sweep")
        self.advanced.addTab(self._burst_tab(), "Burst")
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
        preview_title = QLabel("Waveform preview")
        preview_title.setObjectName("sectionTitle")
        insight_layout.addWidget(preview_title)
        self.preview_series = QLineSeries()
        self.preview_chart = QChart()
        self.preview_chart.addSeries(self.preview_series)
        self.preview_chart.legend().hide()
        self.preview_chart.setBackgroundVisible(False)
        self.preview_chart.setPlotAreaBackgroundVisible(True)
        self.preview_chart.setPlotAreaBackgroundBrush(Qt.GlobalColor.transparent)
        self.preview_x_axis = QValueAxis()
        self.preview_x_axis.setRange(0, 1)
        self.preview_x_axis.setLabelFormat("%.1f")
        self.preview_x_axis.setTitleText("period")
        self.preview_y_axis = QValueAxis()
        self.preview_y_axis.setLabelFormat("%.3g")
        self.preview_y_axis.setTitleText("V")
        self.preview_chart.addAxis(self.preview_x_axis, Qt.AlignmentFlag.AlignBottom)
        self.preview_chart.addAxis(self.preview_y_axis, Qt.AlignmentFlag.AlignLeft)
        self.preview_series.attachAxis(self.preview_x_axis)
        self.preview_series.attachAxis(self.preview_y_axis)
        self.preview_chart_view = QChartView(self.preview_chart)
        self.preview_chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.preview_chart_view.setMinimumHeight(260)
        insight_layout.addWidget(self.preview_chart_view, 1)

        safety = QFrame()
        safety.setObjectName("rigolSafetyCard")
        safety_layout = QVBoxLayout(safety)
        safety_title = QLabel("Load safety")
        safety_title.setObjectName("sectionTitle")
        safety_layout.addWidget(safety_title)
        self.estimate = QLabel("Estimated current: —")
        self.estimate.setObjectName("muted")
        self.estimate.setWordWrap(True)
        safety_layout.addWidget(self.estimate)
        warning = QLabel("⚠ This estimate is not a measurement. Verify DUT impedance and profile limits before ARM.")
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
        arm.clicked.connect(self.arm_output)
        output_on.clicked.connect(lambda: self.request_output(True))
        output_off.clicked.connect(lambda: self._controller.call("set_output", (int(self.channel.currentText()), False)))
        controller.result.connect(self._result)
        controller.error.connect(self._error)
        controller.state_changed.connect(self._device_state_changed)
        self.waveform.currentTextChanged.connect(self._update_dynamic_controls)
        self.level_mode.currentTextChanged.connect(self._update_dynamic_controls)
        self.time_mode.currentTextChanged.connect(self._update_dynamic_controls)
        self.waveform.currentTextChanged.connect(self._update_preview)
        self.channel.currentTextChanged.connect(self._update_preview)
        for field in (self.frequency, self.period, self.high_level, self.low_level, self.vpp, self.offset, self.duty, self.ramp_symmetry, self.pulse_width):
            field.textChanged.connect(self._update_preview)
        self._sync_vpp_offset_from_levels()
        self._sync_period_from_frequency()
        self._update_dynamic_controls()
        self._update_preview()
        self._install_rigol_help(
            configure=configure,
            shape_apply=shape_apply,
            configure_output=configure_output,
            arm=arm,
            output_on=output_on,
            output_off=output_off,
        )

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
        arm: QPushButton,
        output_on: QPushButton,
        output_off: QPushButton,
    ) -> None:
        help_items = {
            self.channel: ("Channel", "Selects physical output CH1 or CH2. Each channel has independent settings and safety limits."),
            self.waveform: ("Waveform", "SIN is sine, SQU square, RAMP triangular/ramp, PULS pulse, NOIS noise and DC a constant voltage."),
            self.time_mode: ("Time representation", "Choose whether the same repetition rate is entered as frequency or period. The application converts one into the other."),
            self.frequency: ("Frequency", "Number of waveform cycles per second. For a standard sine wave this and Amplitude are normally the only values you change."),
            self.period: ("Period", "Duration of one complete waveform cycle. Period equals 1/frequency."),
            self.level_mode: ("Level representation", "HighL/LowL defines the upper and lower levels directly. Amplitude/Offset defines Vpp and the vertical center; both describe the same signal."),
            self.high_level: ("HighL", "Highest programmed waveform voltage. This is a generator setting/read-back, not a measured DUT voltage."),
            self.low_level: ("LowL", "Lowest programmed waveform voltage. Vpp = HighL − LowL."),
            self.vpp: ("Amplitude (Vpp)", "Peak-to-peak voltage: the difference between maximum and minimum level. A 2 mVpp sine at 0 V offset spans −1 mV to +1 mV."),
            self.offset: ("Offset / DC level", "Moves the waveform vertically around zero. In DC mode this is the constant programmed output voltage."),
            self.dut_impedance: ("Minimum DUT impedance", "Safety declaration used to estimate worst-case current. The Rigol does not measure DUT impedance; enter the lowest credible load impedance."),
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
            self.burst_mode: ("Burst mode", "TRIG outputs the configured number of cycles after a trigger. GAT outputs while the external gate has the active level."),
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
            arm: ("ARM", "Creates a 30-second permission window for OUTPUT ON after safety validation. ARM does not energize the connector."),
            output_on: ("OUTPUT ON", "Energizes the physical BNC output. It requires a valid ARM and an additional confirmation dialog."),
            output_off: ("OUTPUT OFF", "Immediately requests the selected physical output to be disabled. No ARM is required."),
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
    ) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(18, 18, 18, 24)
        content_layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        content_layout.addWidget(heading)
        help_text = QLabel(description)
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

    @staticmethod
    def _format_voltage(value_v: float) -> str:
        if 0 < abs(value_v) < 1:
            return f"{value_v * 1e3:.12g} mV"
        return f"{value_v:.12g} V"

    def set_capabilities(self, capabilities: object) -> None:
        supports = getattr(capabilities, "supports", lambda _feature: False)
        features = (
            ("modulation", "MOD"),
            ("frequency_sweep", "SWEEP"),
            ("burst", "BURST"),
            ("phase_sync", "PHASE SYNC"),
        )
        supported = [label for feature, label in features if supports(feature)]
        self.advanced.setTabEnabled(0, bool(supports("modulation")))
        self.advanced.setTabEnabled(1, bool(supports("frequency_sweep")))
        self.advanced.setTabEnabled(2, bool(supports("burst")))
        self.sync_phases_button.setEnabled(bool(supports("phase_sync")))
        self.capability_badge.setText("Capabilities: " + (" · ".join(supported) if supported else "no extensions"))

    def _device_state_changed(self, state: str) -> None:
        normalized = str(state).strip().lower()
        colors = {
            "verified": "#38d996",
            "output_off": "#38d996",
            "output_on": "#ffcc66",
            "connecting": "#66b3ff",
            "fault": "#ff657a",
            "unknown": "#ff657a",
            "disconnected": "#91a0b2",
        }
        self.device_state.setText(normalized.replace("_", " ").upper())
        self.device_led.setStyleSheet(f"color: {colors.get(normalized, '#91a0b2')};")

    def _update_dynamic_controls(self, *_args: object) -> None:
        waveform = self.waveform.currentText()
        is_dc = waveform == "DC"
        has_time = waveform not in {"DC", "NOIS"}
        high_low_mode = self.level_mode.currentText() == "HighL / LowL"

        visibility = {
            self.time_mode: has_time,
            self.frequency: has_time and self.time_mode.currentText() == "Frequency",
            self.period: has_time and self.time_mode.currentText() == "Period",
            self.level_mode: not is_dc,
            self.high_level: not is_dc and high_low_mode,
            self.low_level: not is_dc and high_low_mode,
            self.vpp: not is_dc and not high_low_mode,
            self.offset: is_dc or not high_low_mode,
            self.phase: waveform not in {"DC", "NOIS"},
        }
        for widget, visible in visibility.items():
            self.basic_form.setRowVisible(widget, visible)

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
        self.control_tabs.setTabVisible(3, not is_dc)
        if is_dc and self.control_tabs.currentIndex() in {1, 3}:
            self.control_tabs.setCurrentIndex(0)
        self._update_preview()

    def _effective_levels(self) -> tuple[float, float]:
        if self.waveform.currentText() == "DC":
            value = parse_quantity(self.offset.text(), DIMENSION_VOLTAGE).si_value
            return value, value
        if self.level_mode.currentText() == "Amplitude / Offset":
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
        duty = self._bounded_number(self.duty.text(), 50.0, 0.01, 99.99) / 100
        symmetry = self._bounded_number(self.ramp_symmetry.text(), 50.0, 0.01, 99.99) / 100
        try:
            frequency = parse_quantity(self.frequency.text(), DIMENSION_FREQUENCY).si_value
            width = parse_quantity(self.pulse_width.text(), DIMENSION_TIME).si_value
            pulse_duty = min(max(frequency * width, 0.001), 0.999)
        except Exception:
            pulse_duty = 0.2
        points: list[QPointF] = []
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
            points.append(QPointF(x, value))
        self.preview_series.replace(points)
        span = max(high - low, abs(high) * 0.1, 1e-6)
        self.preview_y_axis.setRange(low - span * 0.12, high + span * 0.12)
        if waveform == "DC":
            title = f"CH{self.channel.currentText()} · DC · Offset {high:.6g} V"
        else:
            title = f"CH{self.channel.currentText()} · {waveform} · HighL {high:.6g} V · LowL {low:.6g} V"
        self.preview_chart.setTitle(title)

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
        self.mod_enabled = QCheckBox("Modulation enabled")
        self.mod_type = QComboBox()
        self.mod_type.addItems(["AM", "FM", "PM", "ASK", "FSK", "PSK", "PWM"])
        self.mod_source = QComboBox()
        self.mod_source.addItems(["INT", "EXT"])
        self.mod_rate = _line("1 kHz")
        self.mod_parameter = _line("50")
        self.mod_shape = QComboBox()
        self.mod_shape.addItems(["SIN", "SQU", "RAMP", "NOIS", "ARB"])
        self.mod_polarity = QComboBox()
        self.mod_polarity.addItems(["POS", "NEG"])
        apply = QPushButton("Apply modulation while OUTPUT is OFF")
        for label, widget in (
            ("Stan", self.mod_enabled),
            ("Typ", self.mod_type),
            ("Source", self.mod_source),
            ("Rate / freq.", self.mod_rate),
            ("Parametr typu", self.mod_parameter),
            ("Internal shape", self.mod_shape),
            ("Polarity", self.mod_polarity),
            ("", apply),
        ):
            form.addRow(label, widget)
        apply.clicked.connect(self.configure_modulation)
        self._set_help(apply, "Apply modulation", "Validates and applies modulation settings while the physical output remains OFF.")
        return self._scroll_widget(tab)

    def _sweep_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.sweep_enabled = QCheckBox("Sweep enabled")
        self.sweep_start = _line("100 Hz")
        self.sweep_stop = _line("1 kHz")
        self.sweep_duration = _line("1 s")
        self.sweep_start_hold = _line("0 s")
        self.sweep_stop_hold = _line("0 s")
        self.sweep_return_time = _line("0 s")
        self.sweep_spacing = QComboBox()
        self.sweep_spacing.addItems(["LIN", "LOG", "STEP"])
        self.sweep_steps = QSpinBox()
        self.sweep_steps.setRange(2, 1_000_000)
        self.sweep_steps.setValue(10)
        self.sweep_trigger = QComboBox()
        self.sweep_trigger.addItems(["INT", "EXT", "MAN"])
        self.sweep_trigger_slope = QComboBox()
        self.sweep_trigger_slope.addItems(["POS", "NEG"])
        self.sweep_trigger_out = QCheckBox("Trigger output")
        apply = QPushButton("Apply sweep while OUTPUT is OFF")
        trigger = QPushButton("Trigger sweep")
        for label, widget in (
            ("Stan", self.sweep_enabled),
            ("Start", self.sweep_start),
            ("Stop", self.sweep_stop),
            ("Czas", self.sweep_duration),
            ("Hold start", self.sweep_start_hold),
            ("Hold stop", self.sweep_stop_hold),
            ("Return time", self.sweep_return_time),
            ("Spacing", self.sweep_spacing),
            ("Steps", self.sweep_steps),
            ("Trigger source", self.sweep_trigger),
            ("Trigger slope", self.sweep_trigger_slope),
            ("", self.sweep_trigger_out),
            ("", apply),
            ("", trigger),
        ):
            form.addRow(label, widget)
        apply.clicked.connect(self.configure_sweep)
        trigger.clicked.connect(lambda: self._controller.call("trigger_sweep", int(self.channel.currentText())))
        self._set_help(apply, "Apply sweep", "Validates and programs the sweep while the physical output remains OFF.")
        self._set_help(trigger, "Manual sweep trigger", "Starts one sweep when Trigger source is MAN. It does not bypass output safety interlocks.")
        return self._scroll_widget(tab)

    def _burst_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)
        self.burst_enabled = QCheckBox("Burst enabled")
        self.burst_mode = QComboBox()
        self.burst_mode.addItems(["TRIG", "GAT"])
        self.burst_cycles = QSpinBox()
        self.burst_cycles.setRange(1, 1_000_000)
        self.burst_cycles.setValue(1)
        self.burst_phase = _line("0")
        self.burst_period = _line("1 ms")
        self.burst_delay = _line("0 s")
        self.burst_trigger = QComboBox()
        self.burst_trigger.addItems(["INT", "EXT", "MAN"])
        self.burst_trigger_slope = QComboBox()
        self.burst_trigger_slope.addItems(["POS", "NEG"])
        self.burst_trigger_out = QCheckBox("Trigger output")
        self.burst_gate_polarity = QComboBox()
        self.burst_gate_polarity.addItems(["POS", "NEG"])
        self.burst_idle = QComboBox()
        self.burst_idle.addItems(["FPT", "TOP", "CENT", "BOT"])
        apply = QPushButton("Apply burst while OUTPUT is OFF")
        trigger = QPushButton("Trigger burst")
        for label, widget in (
            ("Stan", self.burst_enabled),
            ("Mode", self.burst_mode),
            ("Cycles", self.burst_cycles),
            ("Phase [deg]", self.burst_phase),
            ("Period", self.burst_period),
            ("Delay", self.burst_delay),
            ("Trigger source", self.burst_trigger),
            ("Trigger slope", self.burst_trigger_slope),
            ("", self.burst_trigger_out),
            ("Gate polarity", self.burst_gate_polarity),
            ("Idle", self.burst_idle),
            ("", apply),
            ("", trigger),
        ):
            form.addRow(label, widget)
        apply.clicked.connect(self.configure_burst)
        trigger.clicked.connect(lambda: self._controller.call("trigger_burst", int(self.channel.currentText())))
        self._set_help(apply, "Apply burst", "Validates and programs burst settings while the physical output remains OFF.")
        self._set_help(trigger, "Manual burst trigger", "Emits one configured burst when Trigger source is MAN.")
        return self._scroll_widget(tab)

    @staticmethod
    def _scroll_widget(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        return scroll

    def configure(self) -> None:
        try:
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
                dut_min_impedance_ohm=parse_quantity(self.dut_impedance.text(), DIMENSION_RESISTANCE).si_value,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Invalid data", str(exc))
            return
        self._controller.call("configure", config)

    def configure_output(self) -> None:
        try:
            config = RigolOutputConfig(
                channel=int(self.channel.currentText()),
                output_load=self.load.text().strip(),
                polarity=self.output_polarity.currentText(),  # type: ignore[arg-type]
                mode=self.output_mode.currentText(),  # type: ignore[arg-type]
                gate_polarity=self.gate_polarity.currentText(),  # type: ignore[arg-type]
                sync_enabled=self.sync_enabled.isChecked(),
                sync_polarity=self.sync_polarity.currentText(),  # type: ignore[arg-type]
                sync_delay_s=parse_quantity(self.sync_delay.text(), DIMENSION_TIME).si_value,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Output Rigol", str(exc))
            return
        self._controller.call("configure_output", config)

    def configure_modulation(self) -> None:
        try:
            config = RigolModulationConfig(
                channel=int(self.channel.currentText()),
                enabled=self.mod_enabled.isChecked(),
                modulation_type=self.mod_type.currentText(),  # type: ignore[arg-type]
                source=self.mod_source.currentText(),  # type: ignore[arg-type]
                rate_hz=parse_quantity(self.mod_rate.text(), DIMENSION_FREQUENCY).si_value,
                parameter=float(self.mod_parameter.text().replace(",", ".")),
                internal_shape=self.mod_shape.currentText(),  # type: ignore[arg-type]
                polarity=self.mod_polarity.currentText(),  # type: ignore[arg-type]
            )
        except Exception as exc:
            QMessageBox.warning(self, "Rigol modulation", str(exc))
            return
        self._controller.call("configure_modulation", config)

    def configure_sweep(self) -> None:
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
                trigger_output=self.sweep_trigger_out.isChecked(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Sweep Rigol", str(exc))
            return
        self._controller.call("configure_sweep", config)

    def configure_burst(self) -> None:
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
                trigger_output=self.burst_trigger_out.isChecked(),
                gate_polarity=self.burst_gate_polarity.currentText(),  # type: ignore[arg-type]
                idle=self.burst_idle.currentText(),  # type: ignore[arg-type]
            )
        except Exception as exc:
            QMessageBox.warning(self, "Burst Rigol", str(exc))
            return
        self._controller.call("configure_burst", config)

    def arm_output(self) -> None:
        channel = self.channel.currentText()
        answer = QMessageBox.question(
            self,
            "ARM Rigol",
            f"Arm Rigol CH{channel} for 30 seconds? This does not enable the output yet.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self._controller.call("arm", int(channel))

    def request_output(self, enabled: bool) -> None:
        channel = self.channel.currentText()
        answer = QMessageBox.warning(
            self,
            "OUTPUT ON Rigol",
            f"Enable the physical Rigol CH{channel} output? A valid ARM is required.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self._controller.call("set_output", (int(channel), enabled))

    def _result(self, operation: str, result: object) -> None:
        if operation == "configure" and hasattr(result, "peak_absolute_current_a"):
            estimate = result
            self.estimate.setText(
                "Estimated load current (not measured): "
                f"{estimate.peak_absolute_current_a * 1e3:.6g} mA; "
                f"Vth High/Low: {estimate.open_circuit_high_v:.6g} / {estimate.open_circuit_low_v:.6g} V"
            )
            self.status.emit("Rigol configured while OUTPUT is OFF")
        elif operation in {"configure_modulation", "configure_sweep", "configure_burst"}:
            self.status.emit(f"Rigol: {operation} configured while OUTPUT is OFF")
        elif operation == "configure_output":
            self.status.emit("Rigol: output path confirmed while OUTPUT is OFF")
        elif operation == "arm":
            self.status.emit("Rigol armed for 30 seconds; OUTPUT ON requires separate confirmation")
        elif operation == "synchronize_phases":
            self.status.emit("Rigol: CH1/CH2 phases synchronized after capability confirmation")

    def _error(self, operation: str, error: str) -> None:
        if operation in {
            "configure",
            "set_output",
            "arm",
            "configure_modulation",
            "configure_output",
            "configure_sweep",
            "configure_burst",
            "trigger_sweep",
            "trigger_burst",
            "synchronize_phases",
        }:
            QMessageBox.warning(self, "Rigol", error)


class KeithleyPage(QWidget):
    status = Signal(str)

    def __init__(self, controller: DeviceController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        layout = QVBoxLayout(self)
        title = QLabel("Keithley 2600 — SMU")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.channel = QComboBox()
        self.channel.addItems(["A", "B"])
        self.channel.setCurrentText("B")
        self.mode = QComboBox()
        self.mode.addItems(["current", "voltage", "measure_only"])
        self.level = _line("1 mA")
        self.compliance = _line("67 mV")
        self.nplc = _line("1")
        self.settle = _line("100 ms")
        self.sense_mode = QComboBox()
        self.sense_mode.addItems(["2wire", "4wire"])
        self.source_autorange = QCheckBox("Source autorange")
        self.source_autorange.setChecked(True)
        self.source_range = _line("AUTO")
        self.measure_voltage_autorange = QCheckBox("Measure V autorange")
        self.measure_voltage_autorange.setChecked(True)
        self.measure_voltage_range = _line("AUTO")
        self.measure_current_autorange = QCheckBox("Measure I autorange")
        self.measure_current_autorange.setChecked(True)
        self.measure_current_range = _line("AUTO")
        for label, widget in (
            ("Channel", self.channel),
            ("Source mode", self.mode),
            ("Level", self.level),
            ("Compliance", self.compliance),
            ("NPLC", self.nplc),
            ("Settling time", self.settle),
            ("Sense mode", self.sense_mode),
            ("", self.source_autorange),
            ("Source range (AUTO or value with unit)", self.source_range),
            ("", self.measure_voltage_autorange),
            ("Measure V range (AUTO or value with unit)", self.measure_voltage_range),
            ("", self.measure_current_autorange),
            ("Measure I range (AUTO or value with unit)", self.measure_current_range),
        ):
            form.addRow(label, widget)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        configure = QPushButton("Configure source while OUTPUT is OFF")
        measure = QPushButton("Measure I / V")
        arm = QPushButton("ARM (30 s)")
        on = QPushButton("OUTPUT ON")
        off = QPushButton("Ramp to zero + OFF")
        for button in (configure, measure, arm, on, off):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.readout = QLabel("I: —   V: —   P: —")
        self.readout.setObjectName("readout")
        layout.addWidget(self.readout)
        layout.addStretch(1)
        configure.clicked.connect(self.configure)
        measure.clicked.connect(lambda: self._controller.call("measure", self.channel.currentText()))
        arm.clicked.connect(self.arm_output)
        on.clicked.connect(self.request_output)
        off.clicked.connect(lambda: self._controller.call("ramp_to_zero", self.channel.currentText()))
        controller.result.connect(self._result)
        controller.error.connect(self._error)

    def configure(self) -> None:
        try:
            mode = self.mode.currentText()
            level_dimension = DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE
            compliance_dimension = DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT
            request = KeithleySourceRequest(
                channel=self.channel.currentText(),  # type: ignore[arg-type]
                mode=mode,  # type: ignore[arg-type]
                level_si=0.0 if mode == "measure_only" else parse_quantity(self.level.text(), level_dimension).si_value,
                compliance_si=0.0 if mode == "measure_only" else parse_quantity(self.compliance.text(), compliance_dimension).si_value,
                nplc=float(self.nplc.text().replace(",", ".")),
                settle_time_s=parse_quantity(self.settle.text(), "time").si_value,
                sense_mode=self.sense_mode.currentText(),  # type: ignore[arg-type]
                source_autorange=self.source_autorange.isChecked(),
                source_range_si=self._manual_range(
                    self.source_range.text(), level_dimension, self.source_autorange.isChecked()
                ),
                measure_voltage_autorange=self.measure_voltage_autorange.isChecked(),
                measure_voltage_range_si=self._manual_range(
                    self.measure_voltage_range.text(), DIMENSION_VOLTAGE, self.measure_voltage_autorange.isChecked()
                ),
                measure_current_autorange=self.measure_current_autorange.isChecked(),
                measure_current_range_si=self._manual_range(
                    self.measure_current_range.text(), DIMENSION_CURRENT, self.measure_current_autorange.isChecked()
                ),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Invalid data", str(exc))
            return
        self._controller.call("configure", request)

    @staticmethod
    def _manual_range(text: str, dimension: str, autorange: bool) -> float | None:
        value = text.strip()
        if value.upper() == "AUTO":
            return None
        if autorange:
            raise ValueError("Disable autorange before entering a manual range.")
        return parse_quantity(value, dimension).si_value

    def arm_output(self) -> None:
        channel = self.channel.currentText()
        answer = QMessageBox.question(
            self,
            "ARM Keithley",
            f"Arm Keithley CH{channel} for 30 seconds? This does not enable the output yet.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self._controller.call("arm", channel)

    def request_output(self) -> None:
        channel = self.channel.currentText()
        answer = QMessageBox.warning(
            self,
            "OUTPUT ON Keithley",
            f"Enable the physical Keithley CH{channel} output? A valid ARM is required.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self._controller.call("set_output", (channel, True))

    def _result(self, operation: str, result: object) -> None:
        if operation == "measure" and hasattr(result, "current_a"):
            measurement = result
            self.readout.setText(
                f"I: {measurement.current_a * 1e3:.8g} mA   "
                f"V: {measurement.voltage_v * 1e3:.8g} mV   P: {measurement.power_w * 1e6:.8g} µW"
                + ("   COMPLIANCE" if measurement.compliance_detected else "")
            )
            self.status.emit("Keithley measurement completed")
        elif operation == "configure":
            self.status.emit("Keithley configured while OUTPUT is OFF")
        elif operation == "arm":
            self.status.emit("Keithley armed for 30 seconds; OUTPUT ON requires separate confirmation")

    def _error(self, operation: str, error: str) -> None:
        if operation in {"configure", "measure", "set_output", "ramp_to_zero", "arm"}:
            QMessageBox.warning(self, "Keithley", error)


class AnritsuPage(QWidget):
    status = Signal(str)

    def __init__(
        self,
        controller: DeviceController,
        *,
        single_sweep_available: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._single_sweep_configured = single_sweep_available
        self._fetch_pending = False
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.fetch_live)
        layout = QVBoxLayout(self)
        title = QLabel("Anritsu MS2830A — Spectrum / Live")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.start = _line("1 MHz")
        self.stop = _line("10 MHz")
        self.reference = _line("0 dBm")
        self.points = QSpinBox()
        self.points.setRange(101, 10001)
        self.points.setValue(1001)
        self.refresh = QSpinBox()
        self.refresh.setRange(100, 5000)
        self.refresh.setValue(500)
        self.refresh.setSuffix(" ms")
        for label, widget in (
            ("Start", self.start),
            ("Stop", self.stop),
            ("Reference level", self.reference),
            ("Points", self.points),
            ("Live refresh interval", self.refresh),
        ):
            form.addRow(label, widget)
        layout.addLayout(form)
        controls = QHBoxLayout()
        configure = QPushButton("Apply configuration")
        self.single = QPushButton("Single + Fetch")
        self.single.setEnabled(single_sweep_available)
        if not single_sweep_available:
            self.single.setToolTip("Requires standard_scpi_opc qualification in Anritsu settings.")
        self.live = QPushButton("Start Live")
        abort = QPushButton("Abort")
        controls.addWidget(configure)
        controls.addWidget(self.single)
        controls.addWidget(self.live)
        controls.addWidget(abort)
        layout.addLayout(controls)
        self.series = QLineSeries()
        self.chart = QChart()
        self.chart.addSeries(self.series)
        self.chart.legend().hide()
        self.chart.setTitle("Current spectrum")
        self.axis_x = QValueAxis()
        self.axis_x.setTitleText("Frequency [MHz]")
        self.axis_y = QValueAxis()
        self.axis_y.setTitleText("Power [dBm]")
        self.chart.addAxis(self.axis_x, Qt.AlignmentFlag.AlignBottom)
        self.chart.addAxis(self.axis_y, Qt.AlignmentFlag.AlignLeft)
        self.series.attachAxis(self.axis_x)
        self.series.attachAxis(self.axis_y)
        view = QChartView(self.chart)
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setMinimumHeight(300)
        layout.addWidget(view, 1)
        self.info = QLabel("Live stopped. Each frame is a complete trace, not a push stream.")
        self.info.setObjectName("muted")
        layout.addWidget(self.info)
        configure.clicked.connect(self.configure)
        self.single.clicked.connect(lambda: self._controller.call("single_sweep", "TRAC1"))
        self.live.clicked.connect(self.toggle_live)
        abort.clicked.connect(lambda: self._controller.call("emergency_off"))
        controller.result.connect(self._result)
        controller.error.connect(self._error)

    def set_capabilities(self, capabilities: object) -> None:
        supports = getattr(capabilities, "supports", lambda _feature: False)
        self.single.setEnabled(self._single_sweep_configured and bool(supports("synchronized_single_sweep")))

    def configure(self) -> None:
        try:
            config = SpectrumConfig(
                start_hz=parse_quantity(self.start.text(), DIMENSION_FREQUENCY).si_value,
                stop_hz=parse_quantity(self.stop.text(), DIMENSION_FREQUENCY).si_value,
                reference_level_dbm=parse_quantity(self.reference.text(), DIMENSION_DBM).si_value,
                points=self.points.value(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Invalid data", str(exc))
            return
        self._controller.call("configure", config)

    def toggle_live(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self._controller.call("stop_live")
            self.live.setText("Start Live")
            self.info.setText("Live stopped.")
            return
        self._timer.setInterval(self.refresh.value())
        self._controller.call("start_live")

    def fetch_live(self) -> None:
        if not self._fetch_pending:
            self._fetch_pending = True
            self._controller.call("fetch_trace", "TRAC1")

    def _result(self, operation: str, result: object) -> None:
        if operation == "configure":
            self.status.emit("Anritsu configured")
        elif operation == "start_live":
            self._timer.start()
            self.live.setText("Stop Live")
            self.status.emit("Anritsu Live started")
        elif operation in {"fetch_trace", "single_sweep"} and isinstance(result, SpectrumTrace):
            self._fetch_pending = False
            self._show_trace(result)

    def _show_trace(self, trace: SpectrumTrace) -> None:
        limit = 1_000
        stride = max(1, len(trace.powers_dbm) // limit)
        points = [QPointF(trace.frequencies_hz[index] / 1e6, trace.powers_dbm[index]) for index in range(0, len(trace.powers_dbm), stride)]
        self.series.replace(points)
        self.axis_x.setRange(points[0].x(), points[-1].x())
        self.axis_y.setRange(min(point.y() for point in points) - 2, max(point.y() for point in points) + 2)
        self.info.setText(
            f"{len(trace.powers_dbm)} points • {trace.acquired_at_utc.isoformat()} • "
            f"max {max(trace.powers_dbm):.4g} dBm"
        )

    def _error(self, operation: str, error: str) -> None:
        if operation == "fetch_trace":
            self._fetch_pending = False
        if operation in {"configure", "start_live", "fetch_trace", "single_sweep", "emergency_off"}:
            self._timer.stop()
            self.live.setText("Start Live")
            QMessageBox.warning(self, "Anritsu", error)


class RecipePage(QWidget):
    status = Signal(str)
    run_requested = Signal(object)

    def __init__(self, settings: StationSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._plan = None
        layout = QVBoxLayout(self)
        title = QLabel("Measurement recipes")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        path_line = QHBoxLayout()
        self.path = _line("recipes/example_nested_sweep.yml", 42)
        load_button = QPushButton("Load into editor")
        save_button = QPushButton("Save YAML")
        compile_button = QPushButton("Compile")
        self.run_button = QPushButton("Run plan")
        self.run_button.setEnabled(False)
        path_line.addWidget(self.path, 1)
        path_line.addWidget(load_button)
        path_line.addWidget(save_button)
        path_line.addWidget(compile_button)
        path_line.addWidget(self.run_button)
        layout.addLayout(path_line)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("Declarative YAML recipe — no Python code and no raw SCPI.")
        self.editor.setMinimumWidth(520)
        splitter.addWidget(self.editor)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Node", "Type / details"])
        splitter.addWidget(self.tree)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.summary = QLabel("The recipe has not been compiled.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        load_button.clicked.connect(self.load_editor)
        save_button.clicked.connect(self.save_recipe)
        compile_button.clicked.connect(self.compile_recipe)
        self.run_button.clicked.connect(self.request_run)
        self.editor.textChanged.connect(self._source_changed)
        self.load_editor(show_error=False)

    def set_settings(self, settings: StationSettings) -> None:
        self._settings = settings
        self._plan = None
        self.run_button.setEnabled(False)

    def _source_changed(self) -> None:
        self._plan = None
        self.run_button.setEnabled(False)
        self.summary.setText("YAML changed; compile it again before running.")

    def load_editor(self, *, show_error: bool = True) -> None:
        try:
            source = Path(self.path.text()).read_text(encoding="utf-8")
        except Exception as exc:
            if show_error:
                QMessageBox.warning(self, "Recipe", f"Cannot load YAML: {exc}")
            else:
                self.summary.setText(f"Example not loaded: {exc}")
            return
        self.editor.setPlainText(source)
        self.status.emit("Recipe loaded into the editor")

    def save_recipe(self) -> None:
        source = self.editor.toPlainText()
        try:
            parse_recipe_text(source, origin=self.path.text())
            path = Path(self.path.text())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        except Exception as exc:
            QMessageBox.warning(self, "Recipe", f"YAML was not saved: {exc}")
            return
        self.status.emit(f"Recipe saved: {self.path.text()}")

    def compile_recipe(self) -> None:
        try:
            recipe = parse_recipe_text(self.editor.toPlainText(), origin=self.path.text())
            plan = RecipeCompiler(self._settings).compile(recipe)
        except Exception as exc:
            QMessageBox.warning(self, "Recipe", str(exc))
            return
        self.tree.clear()
        self._plan = plan
        self.run_button.setEnabled(True)
        for action in plan.actions:
            item = QTreeWidgetItem([action.node_id, action.kind])
            item.setToolTip(1, str(action.setpoints_si))
            self.tree.addTopLevelItem(item)
        self.summary.setText(
            f"Plan: {len(plan.actions)} actions • {plan.total_points} spectra • hash {plan.sha256}\n"
            "Compilation sends no instrument commands. Execution requires an approved profile and Run Engine."
        )
        self.status.emit("Recipe compiled")

    def request_run(self) -> None:
        if self._plan is not None:
            self.run_requested.emit(self._plan)


class RunMonitorPage(QWidget):
    stop_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel("Recipe execution")
        title.setObjectName("pageTitle")
        self.state = QLabel("IDLE")
        self.state.setObjectName("readout")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        controls = QHBoxLayout()
        pause = QPushButton("Pause after point")
        resume = QPushButton("Resume")
        stop = QPushButton("Stop safely")
        controls.addWidget(pause)
        controls.addWidget(resume)
        controls.addWidget(stop)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        layout.addWidget(title)
        layout.addWidget(self.state)
        layout.addWidget(self.progress)
        layout.addLayout(controls)
        layout.addWidget(self.events, 1)
        pause.clicked.connect(self.pause_requested)
        resume.clicked.connect(self.resume_requested)
        stop.clicked.connect(self.stop_requested)

    def run_started(self, actions: int) -> None:
        self.state.setText("RUNNING")
        self.progress.setRange(0, max(actions, 1))
        self.progress.setValue(0)
        self.events.clear()

    def append_event(self, name: str, data: dict[str, object]) -> None:
        if name == "action_finished":
            self.progress.setValue(min(self.progress.value() + 1, self.progress.maximum()))
        if name == "pause_pending":
            self.state.setText("PAUSED")
        elif name == "run_fault":
            self.state.setText("FAULT")
        self.events.appendPlainText(f"{name}: {data}")

    def complete(self, result: object) -> None:
        run_result = result["result"]
        self.state.setText(f"{run_result.state.value.upper()} • {run_result.stored_points} points")
        self.events.appendPlainText(f"File: {result['path']}")

    def failed(self, error: str) -> None:
        self.state.setText("FAULT")
        self.events.appendPlainText(error)


class ResultsPage(QWidget):
    """Browse immutable run files without opening an instrument session."""

    def __init__(self, output_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._output_dir = Path(output_dir)
        self._selected_path: Path | None = None
        layout = QVBoxLayout(self)
        title = QLabel("Results")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.location = QLabel()
        self.location.setObjectName("muted")
        layout.addWidget(self.location)
        refresh = QPushButton("Refresh file list")
        layout.addWidget(refresh)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.runs = QTreeWidget()
        self.runs.setHeaderLabels(["File", "State", "Spectra", "Points"])
        self.runs.setMinimumWidth(380)
        self.runs.setColumnWidth(0, 220)
        splitter.addWidget(self.runs)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.details_tabs = QTabWidget()
        self.metadata = QPlainTextEdit()
        self.recipe_snapshot = QPlainTextEdit()
        self.settings_snapshot = QPlainTextEdit()
        for widget in (self.metadata, self.recipe_snapshot, self.settings_snapshot):
            widget.setReadOnly(True)
        self.details_tabs.addTab(self.metadata, "Metadata")
        self.details_tabs.addTab(self.recipe_snapshot, "Recipe")
        self.details_tabs.addTab(self.settings_snapshot, "Settings")
        self.details_tabs.setMaximumHeight(250)
        right_layout.addWidget(self.details_tabs)

        self.points = QTreeWidget()
        self.points.setHeaderLabels(["Point", "State", "UTC time", "Data"])
        self.points.setMinimumHeight(150)
        self.points.setColumnWidth(0, 70)
        self.points.setColumnWidth(1, 80)
        self.points.setColumnWidth(2, 210)
        right_layout.addWidget(self.points)

        self.spectrum_series = QLineSeries()
        self.spectrum_chart = QChart()
        self.spectrum_chart.addSeries(self.spectrum_series)
        self.spectrum_chart.legend().hide()
        self.spectrum_chart.setTitle("Select a point containing a stored spectrum")
        self.spectrum_x = QValueAxis()
        self.spectrum_x.setTitleText("Frequency [MHz]")
        self.spectrum_y = QValueAxis()
        self.spectrum_y.setTitleText("Power [dBm]")
        self.spectrum_chart.addAxis(self.spectrum_x, Qt.AlignmentFlag.AlignBottom)
        self.spectrum_chart.addAxis(self.spectrum_y, Qt.AlignmentFlag.AlignLeft)
        self.spectrum_series.attachAxis(self.spectrum_x)
        self.spectrum_series.attachAxis(self.spectrum_y)
        self.spectrum_x.setRange(0.0, 1.0)
        self.spectrum_y.setRange(-100.0, 0.0)
        spectrum_view = QChartView(self.spectrum_chart)
        spectrum_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        spectrum_view.setMinimumHeight(280)
        right_layout.addWidget(spectrum_view, 1)
        self.spectrum_info = QLabel("Spectra are read from HDF5 without contacting instruments.")
        self.spectrum_info.setObjectName("muted")
        right_layout.addWidget(self.spectrum_info)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        refresh.clicked.connect(self.refresh)
        self.runs.currentItemChanged.connect(self._run_selected)
        self.points.currentItemChanged.connect(self._point_selected)
        self.refresh()

    def refresh(self) -> None:
        self.location.setText(f"Directory: {self._output_dir.resolve()}")
        self.runs.clear()
        self.points.clear()
        self._clear_details()
        for summary in Hdf5RunReader.list_runs(self._output_dir):
            item = QTreeWidgetItem(
                [
                    summary.path.name,
                    summary.status,
                    str(summary.spectrum_count),
                    str(summary.point_count),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, str(summary.path))
            item.setToolTip(0, str(summary.path.resolve()))
            self.runs.addTopLevelItem(item)
        if self.runs.topLevelItemCount() == 0:
            self.metadata.setPlainText("No HDF5 files in the results directory.")
        elif self._selected_path is not None:
            for index in range(self.runs.topLevelItemCount()):
                candidate = self.runs.topLevelItem(index)
                if Path(str(candidate.data(0, Qt.ItemDataRole.UserRole))) == self._selected_path:
                    self.runs.setCurrentItem(candidate)
                    break

    def _run_selected(self, item: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        self.points.clear()
        self._clear_spectrum()
        if item is None:
            return
        path = Path(str(item.data(0, Qt.ItemDataRole.UserRole)))
        self._selected_path = path
        try:
            detail = Hdf5RunReader.detail(path)
            points = Hdf5RunReader.points(path)
        except Exception as exc:
            self.metadata.setPlainText(f"Cannot read result:\n{exc}")
            self.recipe_snapshot.clear()
            self.settings_snapshot.clear()
            return
        self._show_detail(detail)
        for point in points:
            fields = {**point.setpoints, **point.measurements}
            suffix = " • spectrum" if point.has_spectrum else ""
            point_item = QTreeWidgetItem(
                [
                    str(point.index),
                    point.status,
                    point.timestamp_utc or "—",
                    f"{len(fields)} values{suffix}",
                ]
            )
            point_item.setData(0, Qt.ItemDataRole.UserRole, point)
            point_item.setToolTip(3, self._point_tooltip(point))
            self.points.addTopLevelItem(point_item)

    def _show_detail(self, detail: RunDetail) -> None:
        summary = detail.summary
        lines = [
            f"File: {summary.path}",
            f"State: {summary.status}",
            f"Created (UTC): {summary.created_at_utc or 'missing'}",
            f"Application version: {summary.application_version or 'missing'}",
            f"Plan hash: {summary.plan_sha256 or 'missing'}",
            f"Checkpoints: {summary.point_count}; stored spectra: {summary.spectrum_count}",
            "",
            "Instrument identities:",
        ]
        lines.extend(f"  {name}: {idn}" for name, idn in sorted(detail.device_idn.items()))
        lines.extend(("", "Capabilities (snapshot):", self._format_json(detail.capabilities)))
        if detail.events:
            lines.extend(("", f"Recent events ({len(detail.events)}):"))
            lines.extend(
                f"  {event.timestamp_utc} [{event.severity}] {event.name}"
                for event in detail.events[-20:]
            )
        self.metadata.setPlainText("\n".join(lines))
        self.recipe_snapshot.setPlainText(detail.recipe_yaml)
        self.settings_snapshot.setPlainText(detail.settings_yaml)

    @staticmethod
    def _format_json(value: object) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)

    def _point_selected(self, item: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        self._clear_spectrum()
        if item is None or self._selected_path is None:
            return
        point = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(point, StoredPoint):
            return
        if not point.has_spectrum:
            self.spectrum_info.setText("This checkpoint contains no spectrum.")
            return
        try:
            trace = Hdf5RunReader.spectrum(self._selected_path, point.index, max_points=2_000)
        except Exception as exc:
            self.spectrum_info.setText(f"Cannot read spectrum: {exc}")
            return
        if trace is None:
            self.spectrum_info.setText("No spectrum for the selected checkpoint.")
            return
        chart_points = [
            QPointF(frequency / 1e6, power)
            for frequency, power in zip(trace.frequencies_hz, trace.powers_dbm, strict=True)
        ]
        self.spectrum_series.replace(chart_points)
        self.spectrum_x.setRange(chart_points[0].x(), chart_points[-1].x())
        self.spectrum_y.setRange(
            min(chart_item.y() for chart_item in chart_points) - 2,
            max(chart_item.y() for chart_item in chart_points) + 2,
        )
        self.spectrum_chart.setTitle(f"Spectrum at point {point.index} ({trace.trace_name})")
        self.spectrum_info.setText(
            f"{trace.source_point_count} points in file; displayed {len(chart_points)} • "
            f"{trace.acquired_at_utc or 'missing time'} • max {max(trace.powers_dbm):.4g} dBm"
        )

    @staticmethod
    def _point_tooltip(point: StoredPoint) -> str:
        payload = {"setpoints": point.setpoints, "measurements": point.measurements, "metadata": point.metadata}
        return ResultsPage._format_json(payload)

    def _clear_details(self) -> None:
        self.metadata.clear()
        self.recipe_snapshot.clear()
        self.settings_snapshot.clear()
        self._clear_spectrum()

    def _clear_spectrum(self) -> None:
        self.spectrum_series.clear()
        self.spectrum_chart.setTitle("Select a point containing a stored spectrum")
        self.spectrum_info.setText("Spectra are read from HDF5 without contacting instruments.")


class MainWindow(QMainWindow):
    """Local Qt client with manual control, live spectrum and safe settings."""

    theme_changed = Signal(str)

    def __init__(self, settings_path: str | Path = ".config/settings.yml", *, simulation: bool = False) -> None:
        super().__init__()
        self._repository = SettingsRepository(settings_path)
        self._simulation = simulation
        persisted = self._repository.load().settings
        self._settings = simulated_station_settings(persisted) if simulation else persisted
        suffix = " — SIMULATION" if simulation else ""
        self.setWindowTitle("Lab Control — Rigol · Keithley · Anritsu" + suffix)
        self.resize(1360, 880)
        self._controllers = {name: DeviceController(self._make_adapter(name), self) for name in ("rigol", "keithley", "anritsu")}
        self._device_states = {"rigol": "disconnected", "keithley": "disconnected", "anritsu": "disconnected"}
        self._run_controller = RunController(self)
        self._build()
        self._connect_controllers()

    def _make_adapter(self, name: str):
        if name == "rigol":
            return RigolAdapter(self._settings, session_factory=SimulatedVisaFactory("rigol") if self._simulation else None)
        if name == "keithley":
            return KeithleyAdapter(self._settings, session_factory=SimulatedVisaFactory("keithley") if self._simulation else None)
        if name == "anritsu":
            return AnritsuAdapter(self._settings, session_factory=SimulatedVisaFactory("anritsu") if self._simulation else None)
        raise ValueError(f"Unknown adapter {name!r}.")

    def _build(self) -> None:
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.dashboard = DashboardPage(self._settings)
        self.rigol_page = RigolPage(self._controllers["rigol"])
        self.keithley_page = KeithleyPage(self._controllers["keithley"])
        self.anritsu_page = AnritsuPage(
            self._controllers["anritsu"],
            single_sweep_available=self._settings.anritsu.acquisition.single_sweep_mode == "standard_scpi_opc",
        )
        self.recipe_page = RecipePage(self._settings)
        self.run_monitor = RunMonitorPage()
        self.results_page = ResultsPage(str(self._settings.storage.get("output_directory", "./measurements")))
        self.settings_page = SettingsPage(self._repository, read_only=self._simulation)
        for widget, name in (
            (self.dashboard, "Dashboard"),
            (self.rigol_page, "Rigol"),
            (self.keithley_page, "Keithley"),
            (self.anritsu_page, "Anritsu"),
            (self.recipe_page, "Recipes"),
            (self.run_monitor, "Execution"),
            (self.results_page, "Results"),
            (self.settings_page, "Settings"),
        ):
            self.tabs.addTab(widget, name)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setFixedHeight(130)
        self.setStatusBar(self.statusBar())
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._log_dock())
        self.dashboard.emergency_requested.connect(self._emergency_off_all)
        self.settings_page.settings_saved.connect(self._settings_saved)
        self.recipe_page.run_requested.connect(self._start_run)
        self.run_monitor.stop_requested.connect(self._run_controller.request_stop)
        self.run_monitor.pause_requested.connect(self._run_controller.request_pause)
        self.run_monitor.resume_requested.connect(self._run_controller.request_resume)
        self._run_controller.event.connect(self._run_event)
        self._run_controller.finished.connect(self._run_finished)
        self._run_controller.failed.connect(self._run_failed)
        self._run_controller.emergency_completed.connect(self._run_emergency_completed)
        for page in (self.rigol_page, self.keithley_page, self.anritsu_page, self.recipe_page, self.settings_page):
            page.status.connect(self._log)
        menu = self.menuBar().addMenu("Application")
        self.theme_action = QAction("Light theme", self)
        self.theme_action.setCheckable(True)
        self.theme_action.setChecked(str(self._settings.ui.get("theme", "dark")).lower() == "light")
        self.theme_action.setToolTip("Switch between the light and dark application themes.")
        self.theme_action.toggled.connect(self._toggle_theme)
        menu.addAction(self.theme_action)
        menu.addSeparator()
        quit_action = QAction("Safe shutdown", self)
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

    def _toggle_theme(self, light: bool) -> None:
        theme = "light" if light else "dark"
        chart_theme = QChart.ChartTheme.ChartThemeLight if light else QChart.ChartTheme.ChartThemeDark
        for chart_view in self.findChildren(QChartView):
            chart_view.chart().setTheme(chart_theme)
            chart_view.chart().legend().hide()
            chart_view.chart().setBackgroundVisible(False)
        self.theme_changed.emit(theme)
        self._log(f"Theme changed to {theme}")

    def _log_dock(self):
        from PySide6.QtWidgets import QDockWidget

        dock = QDockWidget("Event log", self)
        dock.setWidget(self.log)
        return dock

    def _connect_controllers(self) -> None:
        for name, controller in self._controllers.items():
            card = self.dashboard.cards[name]
            card.connect_requested.connect(lambda current=controller: current.call("connect"))
            card.disconnect_requested.connect(lambda current=controller: current.call("disconnect"))
            controller.state_changed.connect(card.update_state)
            controller.state_changed.connect(lambda state, device=name: self._set_device_state(device, state))
            controller.result.connect(lambda operation, result, current=card: self._device_result(current, operation, result))
            controller.error.connect(lambda operation, error, device=name: self._device_error(device, operation, error))
            if name == "rigol":
                controller.capabilities_changed.connect(self.rigol_page.set_capabilities)
            elif name == "anritsu":
                controller.capabilities_changed.connect(self.anritsu_page.set_capabilities)

    def _device_result(self, card: DeviceCard, operation: str, result: object) -> None:
        if operation == "connect":
            card.update_identity(result)
            self._log(f"Connected: {getattr(result, 'idn', result)}")
        elif operation == "disconnect":
            self._log("Instrument disconnected")

    def _device_error(self, device: str, operation: str, error: str) -> None:
        self._log(f"{device}/{operation}: {error}")

    def _set_device_state(self, device: str, state: str) -> None:
        self._device_states[device] = state

    def _start_run(self, plan: object) -> None:
        if self._settings.outputs_locked:
            QMessageBox.warning(
                self,
                "Unverified profile",
                "Approve the profile in Settings before running a recipe.",
            )
            return
        connected = [name for name, state in self._device_states.items() if state != "disconnected"]
        if connected:
            QMessageBox.warning(
                self,
                "Disconnect manual control",
                "Run Engine opens its own VISA sessions. Disconnect first: " + ", ".join(connected) + ".",
            )
            return
        try:
            self._run_controller.start(
                self._settings,
                self._repository.path,
                plan,  # type: ignore[arg-type]
                simulation=self._simulation,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Run not started", str(exc))
            return
        self.run_monitor.run_started(plan.actions)  # type: ignore[union-attr]
        for index in (1, 2, 3, 4, 7):
            self.tabs.setTabEnabled(index, False)
        self.tabs.setCurrentWidget(self.run_monitor)
        self._log("Run Engine started")

    def _run_event(self, name: str, data: object) -> None:
        payload = data if isinstance(data, dict) else {"data": data}
        self.run_monitor.append_event(name, payload)

    def _run_finished(self, result: object) -> None:
        self._set_run_ui_locked(False)
        self.run_monitor.complete(result)
        self.results_page.refresh()
        self._log("Run Engine completed the measurement")

    def _run_failed(self, error: str) -> None:
        self._set_run_ui_locked(False)
        self.run_monitor.failed(error)
        self._log(f"Run Engine: {error}")
        QMessageBox.critical(self, "Run Engine", error)

    def _run_emergency_completed(self, errors: object) -> None:
        if errors:
            self._log("E-STOP Run Engine: " + "; ".join(str(item) for item in errors))
        else:
            self._log("E-STOP Run Engine: OFF/ABORT sent through emergency sessions")

    def _emergency_off_all(self) -> None:
        answer = QMessageBox.warning(
            self,
            "E-STOP",
            "Disable every instrument output and abort acquisition?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is QMessageBox.StandardButton.Yes:
            if self._run_controller.running:
                self._run_controller.request_emergency_stop(self._settings, simulation=self._simulation)
            for controller in self._controllers.values():
                controller.call("emergency_off")
            self._log("E-STOP sent to all instruments")

    def _settings_saved(self, settings: StationSettings) -> None:
        self._settings = simulated_station_settings(settings) if self._simulation else settings
        self.dashboard.update_settings(self._settings)
        self.recipe_page.set_settings(self._settings)
        for name, controller in self._controllers.items():
            controller.reconfigure(self._make_adapter(name))
            self._device_states[name] = "disconnected"
            self.dashboard.cards[name].update_state("disconnected")
        self._log("Profile changed. VISA sessions were safely switched OFF and disconnected; new limits apply on the next connection.")

    def _set_run_ui_locked(self, locked: bool) -> None:
        for index in (1, 2, 3, 4, 7):
            self.tabs.setTabEnabled(index, not locked)

    def _log(self, message: str) -> None:
        self.log.appendPlainText(message)
        self.statusBar().showMessage(message, 8_000)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.anritsu_page._timer.stop()
        self._run_controller.close()
        for controller in self._controllers.values():
            controller.close()
        event.accept()
