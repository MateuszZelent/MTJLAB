"""Fluent manual voltage control and coupled Lake Shore calibration pages."""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import asdict
from threading import Event

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QSizePolicy, QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    FlowLayout,
    LineEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    Slider,
    SpinBox,
    StrongBodyLabel,
    SwitchButton,
    isDarkTheme,
)

from app.devices.moke_box.calibration import CalibrationContext, CalibrationRequest, MokeCalibration
from app.devices.moke_box.calibration_runner import (
    CalibrationRunResult,
    MokeCalibrationRunner,
    WorkflowCancellation,
)
from app.devices.moke_box.ui.voltage_history import MokeVoltageHistory
from app.devices.moke_box.ui.configuration_panel import MokeVoltageConfigurationPanel
from app.domain.errors import ConfigurationError
from app.domain.quick_controls import quantity_step_si, render_quantity_si_like
from app.domain.quantities import (
    DIMENSION_MAGNETIC_FIELD,
    DIMENSION_TIME,
    DIMENSION_VOLTAGE,
    parse_quantity,
)
from app.safety.moke_box import MokeControlProfile, MokeVoltagePlan, MokeVoltageResult, control_profile_from_settings
from app.settings.models import StationSettings
from app.storage.moke_calibration_store import MokeCalibrationRepository
from app.ui.design_system import plot_theme, tokens_for
from app.ui.common import line_edit
from app.ui.workers import DeviceController
from app.ui.widgets.quick_quantity_slider import QuantitySliderMapping


class MokeFieldWorker(QObject):
    progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, kind, request, leases, directory, cancel):
        super().__init__()
        self.kind, self.request = kind, request
        self.leases, self.directory, self.cancel = leases, directory, cancel

    @Slot()
    def run(self):
        moke = self.leases["moke_box"]
        interrupts = tuple(lease.interruption_event for lease in self.leases.values())
        cancel = WorkflowCancellation((self.cancel, *interrupts))
        try:
            if self.kind == "calibration":
                result = MokeCalibrationRunner(
                    moke, self.leases["lakeshore_gaussmeter"], self.directory,
                    cancel=self.cancel, interruption_events=interrupts,
                    progress=self.progress.emit,
                ).run(self.request, armed=True)
            elif self.kind == "zero":
                profile = moke.get_control_profile()
                with moke.io_timeout(profile.ramp_timeout_s + 5):
                    result = moke.stop_vout()
            else:
                plan = self.request
                if cancel.is_set():
                    raise ConfigurationError("MOKE manual operation was cancelled before arming.")
                moke.configure_voltage_plan(plan)
                moke.arm_voltage_plan(plan)
                profile = moke.get_control_profile()
                with moke.io_timeout(profile.ramp_timeout_s + 5):
                    result = moke.ramp_vout(plan.channel, plan.targets_v[0], cancel=cancel)
            self.succeeded.emit(result)
        except Exception as exc:  # noqa: BLE001 - report failure after attempting qualified cleanup
            try:
                moke.emergency_off()
            except Exception:  # noqa: BLE001 - report the primary workflow failure
                self.failed.emit(f"{exc}; emergency shutdown also failed")
            else:
                self.failed.emit(str(exc))
        finally:
            for lease in reversed(tuple(self.leases.values())):
                try:
                    lease.release()
                except Exception as exc:  # noqa: BLE001 - report release failure and retain controller reservation
                    self.failed.emit(f"Instrument reservation could not be released: {exc}")
            self.finished.emit()


class MokeFieldWorkflow(QObject):
    """Own one worker at a time; keep every hardware operation off the GUI."""

    status = Signal(str)
    calibration_changed = Signal(object)
    profile_changed = Signal(object)
    busy_changed = Signal(bool)
    vout_overview_requested = Signal()
    voltage_confirmed = Signal(int, float)
    quick_draft_changed = Signal(str, str)
    quick_failed = Signal(str)
    quick_bounds_changed = Signal()

    def quick_control_bounds(self):
        """Expose the qualified channel and operator envelope without I/O."""
        if (self._profile is None or not self._connected
                or self._selected_channel() != self._profile.channel):
            return None
        minimum = max(self._profile.minimum_v, self._voltage(self.configuration_panel.minimum_text))
        maximum = min(self._profile.maximum_v, self._voltage(self.configuration_panel.maximum_text))
        if minimum >= maximum:
            return None
        return self._profile.channel, f"{minimum:.12g} V", f"{maximum:.12g} V"

    def quick_control_draft(self):
        return f"moke_box.vout{self._selected_channel()}.voltage", self.target.text()

    def refresh_quick_values(self):
        if self._connected and not self.busy and not self._external_controlled:
            self.vout_overview_requested.emit()

    def set_quick_control_draft(self, target: str, text: str):
        if target != f"moke_box.vout{self._selected_channel()}.voltage":
            return
        if self.busy or self._external_controlled:
            return
        parse_quantity(text, DIMENSION_VOLTAGE)
        previous = self.target.blockSignals(True)
        self.target.setText(text)
        self.target.blockSignals(previous)
        # A shared draft must not schedule a second live ramp.
        self._target_changed()

    def request_quick_voltage(self, target: str, text: str):
        """Use the same authorized, reserved worker as manual Apply voltage."""
        if self.busy or self._external_controlled:
            raise ConfigurationError("MOKE Box is busy or reserved by a recipe.")
        if (not self._connected or self._profile is None
                or target != f"moke_box.vout{self._profile.channel}.voltage"
                or self._selected_channel() != self._profile.channel):
            raise ConfigurationError("Select the connected, qualified MOKE output channel on its card.")
        voltage = parse_quantity(text, DIMENSION_VOLTAGE).si_value
        self._manual_voltage_plan((voltage,))  # Validate before changing the shared draft.
        self.set_quick_control_draft(target, text)
        self._start_manual()

    def request_quick_zero(self):
        if not self._connected or self._profile is None or self._external_controlled:
            raise ConfigurationError("MOKE zero ramp is unavailable; check connection and reservation.")
        self._zero()

    def __init__(self, controller: DeviceController, settings: StationSettings, parent: QWidget):
        super().__init__(parent)
        self._controller = controller
        self._reference: DeviceController | None = None
        self._settings = settings
        self._profile: MokeControlProfile | None = None
        self._reference_identity = None
        self._simulation = False
        self._thread: QThread | None = None
        self._worker = None
        self._running_kind = None
        self._cancel = Event()
        self._manual_plan = None
        self._manual_envelope = None
        self._slider_mapping = None
        self._last_voltages = {}
        self._calibration_request = None
        self._last_result: CalibrationRunResult | None = None
        self._review_model = None
        self._active_model = None
        self._authorize: Callable[[str, object], None] | None = None
        self._pause_live: Callable[[], None] | None = None
        self._connected = False
        self._reference_connected = False
        self._external_controlled = False
        self._input_controls = []
        self._live_pending = False
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(400)
        self._live_timer.timeout.connect(self._submit_live_target)
        self.control_page = self._build_control(parent)
        self.calibration_page = self._build_calibration(parent)
        self._controller.result.connect(self._device_result)
        self._controller.state_changed.connect(self._state_changed)
        self._controller.error.connect(self._device_error)
        self.set_settings(settings)
        self._refresh_controls()

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def bind_reference(self, reference: DeviceController, *, simulation: bool,
                       authorize: Callable[[str, object], None] | None,
                       pause_live: Callable[[], None]) -> None:
        self._reference = reference
        self._simulation = simulation
        self._authorize = authorize
        self._pause_live = pause_live
        reference.result.connect(self._reference_result)
        reference.state_changed.connect(self._reference_state)

    def _scroll_page(self, parent):
        area = ScrollArea(parent)
        area.setWidgetResizable(True)
        body = QWidget(area)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 6, 2, 6)
        layout.setSpacing(12)
        area.setWidget(body)
        return area, body, layout

    def _card(self, parent, heading):
        card = CardWidget(parent)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)
        title = StrongBodyLabel(heading, card)
        layout.addWidget(title)
        return card, layout

    def _entry(self, form, label, text):
        entry = LineEdit()
        entry.setText(text)
        entry.setAccessibleName(label)
        if form is not None:
            form.addRow(BodyLabel(label), entry)
        self._input_controls.append(entry)
        entry.textChanged.connect(self._revoke_calibration)
        return entry

    def _build_control(self, parent):
        page = QWidget(parent)
        page.setObjectName("mokeVoltageControlPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal, page)
        self.workspace_splitter.setObjectName("mokeWorkspace")
        self.workspace_splitter.setChildrenCollapsible(False)
        layout.addWidget(self.workspace_splitter, 1)
        page.installEventFilter(self)
        source_scroll, source_tab, content = self._scroll_page(page)
        source_pane = QWidget(page)
        source_layout = QVBoxLayout(source_pane)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(6)
        source_scroll.setObjectName("mokeControlPanel")
        source_scroll.setMinimumWidth(430)
        source_tab.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        content.setContentsMargins(8, 6, 8, 6)
        content.setSpacing(6)
        self.live_control_switch = SwitchButton(source_tab)
        self.live_control_switch.setObjectName("mokeLiveControlSwitch")
        self.live_control_switch.setOnText("Live control")
        self.live_control_switch.setOffText("Live control")
        self.live_control_switch.setChecked(False)
        self.live_control_switch.setToolTip(
            "Live ON: valid voltage changes are applied automatically after 400 ms. "
            "Live OFF: use Apply voltage. Enabling Live approves the selected range without sending a voltage.")
        self.live_control_switch.checkedChanged.connect(self._live_control_toggled)
        source_layout.addWidget(self.live_control_switch)
        source_layout.addWidget(source_scroll, 1)
        action_footer = CardWidget(source_pane)
        source_layout.addWidget(action_footer)
        buttons = FlowLayout(needAni=False, isTight=True)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setHorizontalSpacing(6)
        buttons.setVerticalSpacing(6)
        self.read_configuration_button = PushButton("Read all VOUT", source_tab)
        self.read_configuration_button.setToolTip("Open the VOUT 0–7 table and read all DAC registers. This does not change an output or measure Kepco current.")
        self.read_voltage_button = PushButton("Read selected VOUT", source_tab)
        self.read_voltage_button.setToolTip("Refresh the selected channel's confirmed DAC value and history. The protocol returns all eight registers. This does not measure current or magnetic field.")
        for button in (self.read_configuration_button, self.read_voltage_button):
            buttons.addWidget(button)
        content.addLayout(buttons)
        self.configuration_panel = MokeVoltageConfigurationPanel(source_tab)
        content.addWidget(self.configuration_panel)
        self.channel_selector = self.configuration_panel.channel
        self.channel_selector.setAccessibleName("MOKE output channel")
        self.channel_selector.currentIndexChanged.connect(self._channel_changed)
        self._input_controls.append(self.channel_selector)
        self.profile_label = self.configuration_panel.profile_summary
        self.field_readout = self.configuration_panel.calculated_field
        self.target = self.configuration_panel.level
        self._input_controls.append(self.target)
        self.target.textChanged.connect(self._target_changed)
        self.target.returnPressed.connect(self._submit_live_target)
        self.configuration_panel.range_changed.connect(self._operator_limits_changed)
        self.configuration_panel.validation_failed.connect(self._failed)
        self.voltage_slider = Slider(Qt.Orientation.Horizontal, source_tab)
        self.voltage_slider.setAccessibleName("Programming voltage slider")
        self.voltage_slider.setToolTip("Live ON: automatically apply voltage. Live OFF: select a voltage, then click Apply voltage.")
        self.voltage_slider.valueChanged.connect(self._slider_changed)
        self.voltage_slider.sliderReleased.connect(self._submit_live_target)
        self._input_controls.append(self.voltage_slider)
        self.configuration_panel.form.insertRow(2, "Voltage slider", self.voltage_slider)
        self.manual_settling = line_edit("2 s")
        self.manual_settling.setAccessibleName("Voltage settling time")
        self.manual_settling.setToolTip("Wait after the final DAC readback before allowing the next target. Must be at least the station minimum; elapsed time does not confirm field stability.")
        self.manual_settling.textChanged.connect(self._revoke_arm)
        self._input_controls.append(self.manual_settling)
        self.configuration_panel.form.insertRow(3, "Settling time", self.manual_settling)
        workflow = CardWidget(source_tab)
        workflow.setObjectName("mokeOutputWorkflow")
        workflow_layout = QVBoxLayout(workflow)
        workflow_layout.setContentsMargins(7, 5, 7, 5)
        self.manual_status = BodyLabel("Output control is disabled until qualified.", workflow)
        self.manual_status.setWordWrap(True)
        workflow_layout.addWidget(self.manual_status)
        note = CaptionLabel("Programming voltage controls Kepco current. DAC zero does not confirm power-off or zero field.", workflow)
        note.setWordWrap(True)
        workflow_layout.addWidget(note)
        actions = QHBoxLayout(action_footer)
        actions.setContentsMargins(8, 5, 8, 5)
        self.set_button = PrimaryPushButton("Apply voltage", action_footer)
        self.zero_button = PushButton("Ramp DAC to zero", action_footer)
        actions.addWidget(self.set_button)
        actions.addWidget(self.zero_button)
        self.voltage_readout = StrongBodyLabel("Confirmed DAC: — V", workflow)
        workflow_layout.addWidget(self.voltage_readout)
        content.addWidget(workflow)
        self.set_button.clicked.connect(self._start_manual)
        self.zero_button.clicked.connect(self._zero)
        self.read_configuration_button.clicked.connect(self._read_vout_overview)
        self.read_voltage_button.clicked.connect(lambda: self._controller.call("read_vouts"))
        content.addStretch(1)
        self.workspace_splitter.addWidget(source_pane)
        history_card, history_layout = self._card(page, "Voltage / field · last 3 minutes")
        history_layout.setContentsMargins(7, 6, 7, 6)
        history_layout.setSpacing(4)
        self.voltage_history = MokeVoltageHistory(history_card)
        history_layout.addWidget(self.voltage_history, 1)
        history_actions = QHBoxLayout()
        self.clear_history_button = PushButton("Clear history", history_card)
        self.clear_history_button.clicked.connect(self.voltage_history.clear)
        history_actions.addWidget(self.clear_history_button)
        history_layout.addLayout(history_actions)
        self.workspace_splitter.addWidget(history_card)
        self.workspace_splitter.setStretchFactor(0, 3)
        self.workspace_splitter.setStretchFactor(1, 7)
        self.workspace_splitter.setSizes([450, 910])
        self._sync_slider()
        return page

    def _read_vout_overview(self):
        self.vout_overview_requested.emit()
        self._controller.call("read_vouts")

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize and hasattr(self, "workspace_splitter"):
            self.workspace_splitter.setOrientation(Qt.Orientation.Vertical if watched.width() < 720 else Qt.Orientation.Horizontal)
        return super().eventFilter(watched, event)

    def _selected_channel(self):
        return self.channel_selector.currentData()

    def _channel_changed(self, *_args):
        self._revoke_arm()
        if hasattr(self, "voltage_history"):
            self.voltage_history.clear()
            self.voltage_readout.setText("Confirmed DAC: — V")
            voltage = self._last_voltages.get(self._selected_channel())
            if voltage is not None:
                self._record_voltage(voltage)
            self._preview_target()

    def _sync_slider(self, *_args):
        try:
            minimum = self._voltage(self.configuration_panel.minimum_text)
            maximum = self._voltage(self.configuration_panel.maximum_text)
            self._slider_mapping = QuantitySliderMapping(minimum, maximum, (maximum - minimum) / 10000)
            self.voltage_slider.setRange(0, self._slider_mapping.maximum_position)
            self._target_changed()
        except (ValueError, RuntimeError):
            self._slider_mapping = None
            self.voltage_slider.setEnabled(False)

    def _slider_changed(self, position):
        if self._slider_mapping is not None:
            voltage = self._slider_mapping.value_for_position(position)
            self.target.setText(render_quantity_si_like(self.target.text(), DIMENSION_VOLTAGE, voltage, preferred_unit="mV"))

    def _target_changed(self, *_args):
        try:
            parse_quantity(self.target.text(), DIMENSION_VOLTAGE)
        except ValueError:
            pass
        else:
            self.quick_draft_changed.emit(
                f"moke_box.vout{self._selected_channel()}.voltage", self.target.text())
        try:
            voltage = self._voltage(self.target)
            if self._slider_mapping is not None:
                step_v = quantity_step_si(self.target.text(), DIMENSION_VOLTAGE,
                                          integer_step=self.target.property("precisionIntegerStep"))
                self.voltage_slider.setSingleStep(max(1, round(step_v / self._slider_mapping.step_si)))
                self.voltage_slider.blockSignals(True)
                self.voltage_slider.setValue(self._slider_mapping.position_for_value(voltage))
                self.voltage_slider.blockSignals(False)
            self._manual_plan = self._manual_voltage_plan((voltage,))
        except (ValueError, RuntimeError):
            self._manual_plan = None
        self._preview_target()
        self._refresh_controls()
        if _args and self.live_control_switch.isChecked():
            self._live_timer.stop()
            self._live_pending = self._manual_plan is not None
            if self._live_pending:
                self._live_timer.start()

    def _disable_live(self):
        self._live_timer.stop()
        self._live_pending = False
        self.live_control_switch.blockSignals(True)
        self.live_control_switch.setChecked(False)
        self.live_control_switch.blockSignals(False)

    def _live_control_toggled(self, enabled):
        self._live_timer.stop()
        self._live_pending = False
        if enabled:
            if self.busy or self._external_controlled or not self._connected:
                self._disable_live()
                return
            self._prepare_manual()
            if self._manual_envelope is None:
                self._disable_live()
                return
            self.manual_status.setText("Live ON. Valid voltage changes are applied automatically after 400 ms.")
        else:
            self.manual_status.setText("Live OFF. Change voltage, then click Apply voltage.")
        self._refresh_controls()

    def _submit_live_target(self):
        self._live_timer.stop()
        if not self.live_control_switch.isChecked() or not self._live_pending:
            return
        if self.busy:
            return  # Keep only the latest draft; never run two output workers.
        self._live_pending = False
        if self._connected and not self._external_controlled and self._manual_envelope is not None:
            self._start_manual()

    def _preview_target(self):
        if not hasattr(self, "field_readout"):
            return
        try:
            voltage = self._voltage(self.target)
            if self._profile is not None and self._selected_channel() == self._profile.channel:
                voltage = self._manual_voltage_plan((voltage,)).applied_voltage(voltage)
            self.show_field_preview(voltage)
        except (ValueError, RuntimeError):
            self.field_readout.setText("Calculated field: enter a valid voltage")

    def _record_voltage(self, voltage):
        self._last_voltages[self._selected_channel()] = voltage
        self.voltage_confirmed.emit(self._selected_channel(), voltage)
        self.voltage_readout.setText(f"Confirmed DAC: {voltage:+.6f} V")
        model = self._active_model if self._profile and self._selected_channel() == self._profile.channel else None
        self.voltage_history.append(voltage, model)

    def show_execution_voltage(self, channel, voltage_v, calibration_document=None):
        """Render runner-confirmed voltage without querying a leased device."""
        if type(channel) is not int or channel not in range(8):
            return
        if self._selected_channel() != channel:
            self.channel_selector.setCurrentIndex(channel)
        self._last_voltages[channel] = voltage_v
        self.voltage_confirmed.emit(channel, voltage_v)
        self.voltage_readout.setText(f"Confirmed DAC: {voltage_v:+.6f} V")
        model = MokeCalibration.from_document(calibration_document) if isinstance(calibration_document, dict) else None
        self.voltage_history.append(voltage_v, model)
        if model is None:
            self.field_readout.setText("Calculated field: no calibration in this run")
            return
        try:
            up, down = model.ascending.estimate(voltage_v), model.descending.estimate(voltage_v)
            self.field_readout.setText(f"Calculated field: B↑ {up * 1000:+.6g} mT · B↓ {down * 1000:+.6g} mT. Conditioning history unverified.")
        except ConfigurationError as exc:
            self.field_readout.setText(f"Calculated field unavailable: {exc}")

    def _build_calibration(self, parent):
        page = QWidget(parent)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll, body, layout = self._scroll_page(page)
        outer.addWidget(scroll, 1)
        page.setObjectName("mokeFieldCalibrationPage")
        card, content = self._card(body, "Field calibration · Lake Shore reference")
        card.setObjectName("mokeFieldCalibrationCard")
        self.reference_label = BodyLabel("Connect Lake Shore 475 in DC mode.", card)
        self.reference_label.setWordWrap(True)
        content.addWidget(self.reference_label)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.cal_minimum = self._entry(form, "Calibration minimum", "-0.5 V")
        self.cal_maximum = self._entry(form, "Calibration maximum", "0.5 V")
        self.grid_points = SpinBox(card)
        self.grid_points.setRange(3, 1001)
        self.grid_points.setValue(21)
        form.addRow(BodyLabel("Points per branch"), self.grid_points)
        self.samples = SpinBox(card)
        self.samples.setRange(2, 1000)
        self.samples.setValue(3)
        form.addRow(BodyLabel("Samples per point"), self.samples)
        self.repetitions = SpinBox(card)
        self.repetitions.setRange(1, 100)
        self.repetitions.setValue(2)
        form.addRow(BodyLabel("Repeat cycles"), self.repetitions)
        for entry in (self.grid_points, self.samples, self.repetitions):
            self._input_controls.append(entry)
            entry.valueChanged.connect(self._revoke_calibration)
        self.settling = self._entry(form, "Settling time", "2 s")
        self.sample_interval = self._entry(form, "Sample interval", "100 ms")
        self.point_timeout = self._entry(form, "Point deadline", "30 s")
        self.stability = self._entry(form, "Maximum field stddev", "100 uT")
        self.probe = self._entry(form, "Probe identity", "")
        self.orientation = self._entry(form, "Probe orientation", "")
        self.geometry = self._entry(form, "Gap / geometry", "")
        self.uncertainty = self._entry(form, "Reference uncertainty", "")
        content.addLayout(form)
        self.acquire_hall = CheckBox("Also record Hall-1 voltage and raw samples", card)
        self.acquire_hall.setChecked(True)
        self.acquire_hall.toggled.connect(self._revoke_calibration)
        self._input_controls.append(self.acquire_hall)
        content.addWidget(self.acquire_hall)
        hint = CaptionLabel(
            "One full-range conditioning cycle runs before recorded ascending and descending branches. "
            "The complete trajectory is included in the arm permission.", card)
        hint.setWordWrap(True)
        content.addWidget(hint)
        footer = CardWidget(page)
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(14, 10, 14, 10)
        actions = QHBoxLayout()
        self.arm_calibration_button = PushButton("Arm calibration", footer)
        self.start_calibration_button = PrimaryPushButton("Start calibration", footer)
        self.stop_button = PushButton("Stop", footer)
        for button in (self.arm_calibration_button, self.start_calibration_button, self.stop_button):
            actions.addWidget(button)
        footer_layout.addLayout(actions)
        self.arm_calibration_button.clicked.connect(self._arm_calibration)
        self.start_calibration_button.clicked.connect(self._start_calibration)
        self.stop_button.clicked.connect(self.stop)
        self.progress_bar = ProgressBar(footer)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        footer_layout.addWidget(self.progress_bar)
        self.calibration_status = BodyLabel("No calibration run yet.", footer)
        self.calibration_status.setWordWrap(True)
        footer_layout.addWidget(self.calibration_status)
        outer.addWidget(footer)
        layout.addWidget(card)
        review, review_layout = self._card(body, "Calibration review and activation")
        saved_row = QHBoxLayout()
        self.saved_models = ComboBox(review)
        self.saved_models.setAccessibleName("Saved field calibration")
        self.load_model_button = PushButton("Load for review", review)
        self.load_model_button.clicked.connect(self._load_model)
        saved_row.addWidget(self.saved_models, 1)
        saved_row.addWidget(self.load_model_button)
        review_layout.addLayout(saved_row)
        self.review_summary = BodyLabel("Completed models are saved without activation.", review)
        self.review_summary.setWordWrap(True)
        review_layout.addWidget(self.review_summary)
        self.reviewed = CheckBox("I reviewed the branches, repeatability and reference conditions", review)
        self.reviewed.toggled.connect(self._refresh_controls)
        review_layout.addWidget(self.reviewed)
        self.activate_button = PrimaryPushButton("Activate reviewed calibration", review)
        self.activate_button.clicked.connect(self._activate)
        review_layout.addWidget(self.activate_button)
        self.plot = pg.PlotWidget(review)
        self.plot.setMinimumHeight(230)
        self.plot.setLabel("bottom", "Programming voltage", units="V")
        self.plot.setLabel("left", "Reference field", units="T")
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.addLegend()
        palette = plot_theme(tokens_for("dark" if isDarkTheme() else "light"))
        self.plot.setBackground(palette.background)
        for name in ("left", "bottom"):
            self.plot.getAxis(name).setPen(pg.mkPen(palette.axes))
            self.plot.getAxis(name).setTextPen(pg.mkPen(palette.axes))
        self.up_curve = self.plot.plot(name="Ascending", pen=pg.mkPen(palette.measurement, width=2))
        self.down_curve = self.plot.plot(name="Descending", pen=pg.mkPen(palette.axes, width=2))
        review_layout.addWidget(self.plot)
        layout.addWidget(review)
        return page

    def set_settings(self, settings: StationSettings):
        self._disable_live()
        if self.busy:
            self.stop()
        self._settings = settings
        self.manual_settling.setText(settings.moke_box.voltage_control.minimum_settling_time)
        self._manual_envelope = None
        self._manual_plan = self._calibration_request = None
        self._active_model = None
        self._review_model = None
        self.reviewed.setChecked(False)
        self._profile = None
        if self._connected:
            self._controller.call("get_control_profile")
        self._refresh_controls()

    def _revoke_arm(self, *_args):
        self._disable_live()
        self._manual_envelope = None
        self._manual_plan = self._calibration_request = None
        if hasattr(self, "manual_status"):
            self.manual_status.setText(self._control_disabled_reason())
            self._target_changed()

    def _control_disabled_reason(self):
        if not self._connected:
            return "Connect MOKE Box to read DAC values. Voltage control requires a qualified station profile."
        if self._profile is None:
            return "Connected for readout only. Live, Apply voltage and DAC zero are locked: no approved voltage-control profile. Qualify the Kepco connection and limits in the station profile before enabling control."
        if self._selected_channel() != self._profile.channel:
            return f"Voltage control is qualified only for VOUT {self._profile.channel}. Other channels remain read-only."
        return "Set voltage and click Apply voltage. The current min/max and settling time are validated automatically. Live applies subsequent voltage edits automatically."

    def _revoke_calibration(self, *_args):
        self._calibration_request = None
        self._refresh_controls()

    def _voltage(self, widget):
        return parse_quantity(widget if isinstance(widget, str) else widget.text(), DIMENSION_VOLTAGE).si_value

    def _manual_voltage_plan(self, targets):
        settling_s = parse_quantity(self.manual_settling.text(), DIMENSION_TIME).si_value
        if self._profile is not None and settling_s < self._profile.minimum_settling_s:
            raise ConfigurationError("Settling time must be at least the qualified station minimum.")
        return self._make_plan(self.configuration_panel.minimum_text, self.configuration_panel.maximum_text, targets, settling_s=settling_s)

    def _operator_limits_changed(self, *_args):
        self._revoke_arm()
        self._sync_slider()

    def _make_plan(self, minimum, maximum, targets, *, settling_s=0.0):
        if self._profile is None:
            raise ConfigurationError("Connect a qualified output profile first.")
        plan = MokeVoltagePlan(self._profile.fingerprint, self._profile.channel,
                               self._voltage(minimum), self._voltage(maximum), tuple(targets), settling_s)
        plan.validate(self._profile)
        return plan

    def _approve(self, kind, request):
        if self._authorize is None and not self._simulation:
            raise ConfigurationError("This output workflow is not bound to the station authorization service.")
        if self._authorize is not None:
            self._authorize(kind, asdict(request))

    def _prepare_manual(self):
        try:
            if not self._connected or self._profile is None or self._selected_channel() != self._profile.channel:
                raise ConfigurationError("This channel is read-only. Select the qualified Kepco output channel.")
            plan = self._manual_voltage_plan((self._voltage(self.target),))
            self._approve("arm_voltage_plan", plan)
            self._manual_envelope = (plan.profile_fingerprint, plan.channel, plan.minimum_v, plan.maximum_v, plan.settling_s)
            self._manual_plan = plan
            self.manual_status.setText("Control enabled. Click Apply voltage, or enable Live control for automatic changes.")
            self._refresh_controls()
            return plan
        except (ConfigurationError, ValueError, RuntimeError) as exc:
            self._failed(str(exc))

    def _start_manual(self):
        if self.busy or self._external_controlled:
            return
        self._live_timer.stop()
        self._live_pending = False
        plan = self._prepare_manual()
        if plan is not None:
            self._start_job("voltage", plan)

    def _make_calibration_request(self):
        if self._reference_identity is None or self._profile is None:
            raise ConfigurationError("Connect both MOKE-Box and Lake Shore first.")
        lo, hi = self._voltage(self.cal_minimum), self._voltage(self.cal_maximum)
        count = self.grid_points.value()
        grid = tuple(lo + (hi - lo) * index / (count - 1) for index in range(count))
        plan = self._make_plan(self.cal_minimum, self.cal_maximum, grid)
        context = CalibrationContext(
            self._profile.fingerprint, self._profile.binding_id, self._profile.channel,
            self._profile.simulation, self._reference_identity.idn, self.probe.text(),
            self.orientation.text(), self.geometry.text(),
            parse_quantity(self.uncertainty.text(), DIMENSION_MAGNETIC_FIELD).si_value,
        )
        return CalibrationRequest(
            plan, context, samples_per_point=self.samples.value(),
            settling_s=parse_quantity(self.settling.text(), DIMENSION_TIME).si_value,
            sample_interval_s=parse_quantity(self.sample_interval.text(), DIMENSION_TIME).si_value,
            point_timeout_s=parse_quantity(self.point_timeout.text(), DIMENSION_TIME).si_value,
            repetitions=self.repetitions.value(), acquire_hall=self.acquire_hall.isChecked(),
            maximum_stddev_t=parse_quantity(self.stability.text(), DIMENSION_MAGNETIC_FIELD).si_value,
        )

    def _arm_calibration(self):
        self._disable_live()
        try:
            request = self._make_calibration_request()
            request.trajectory.validate(self._profile)
            self._approve("arm_field_calibration", request)
            self._manual_envelope = None
            self._manual_plan = None
            self._calibration_request = request
            count = len(request.trajectory.targets_v)
            self.calibration_status.setText(f"Calibration armed · {count} voltage targets including conditioning.")
            self._refresh_controls()
        except (ConfigurationError, ValueError, RuntimeError) as exc:
            self._failed(str(exc))

    def _start_calibration(self):
        if self._calibration_request is not None:
            self._start_job("calibration", self._calibration_request)

    def _zero(self):
        self._disable_live()
        self._manual_envelope = None
        if self.busy:
            self.stop()
        elif self._profile is not None:
            self._start_job("zero", None)

    def _start_job(self, kind, request):
        if self.busy or self._external_controlled:
            return
        leases = {}
        try:
            if kind != "zero":
                self._approve("start_field_calibration" if kind == "calibration" else "ramp_vout", request)
            if self._pause_live is not None:
                self._pause_live()
            controllers = {"moke_box": self._controller}
            if kind == "calibration":
                if self._reference is None:
                    raise ConfigurationError("Lake Shore controller is unavailable.")
                controllers["lakeshore_gaussmeter"] = self._reference
            for key in sorted(controllers):
                leases[key] = controllers[key].acquire_run_lease()
        except (ConfigurationError, ValueError, RuntimeError) as exc:
            for lease in reversed(tuple(leases.values())):
                lease.release()
            self._failed(str(exc))
            return
        self._cancel = Event()
        self._manual_plan = self._calibration_request = None
        if kind == "calibration":
            self._manual_envelope = None
            self.reviewed.setChecked(False)
            self._last_result = None
            self._review_model = None
            self.progress_bar.setValue(0)
            self.progress_bar.setMaximum(2 * len(request.plan.targets_v) * request.repetitions)
        self._thread = QThread(self)
        self._running_kind = kind
        self._worker = MokeFieldWorker(kind, request, leases, self._settings.moke_box.calibration_directory, self._cancel)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._progress)
        self._worker.succeeded.connect(self._succeeded)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._job_finished)
        self.manual_status.setText("Returning DAC to zero…" if kind == "zero" else (
            f"Ramping voltage, then waiting {max(request.settling_s, self._profile.minimum_settling_s):g} s for settling…"
            if kind == "voltage" else "Applying armed voltage trajectory…"))
        self.calibration_status.setText("Calibration running…" if kind == "calibration" else self.calibration_status.text())
        self._refresh_controls()
        self.busy_changed.emit(True)
        self._thread.start()

    def stop(self):
        self._disable_live()
        self._manual_envelope = None
        self._cancel.set()
        if self.busy:
            self.calibration_status.setText("Stopping · waiting for DAC-zero confirmation and durable close…")
        elif self._profile is not None:
            self._zero()

    def prepare_application_shutdown(self) -> bool:
        self._disable_live()
        if self.busy:
            self.stop()
            return False
        return True

    def _job_finished(self):
        thread = self._thread
        kind = self._running_kind
        self._worker = None
        self._running_kind = None
        self._thread = None
        self.busy_changed.emit(False)
        if thread is not None:
            thread.deleteLater()
        if kind == "calibration":
            try:
                self._list_saved_models()
            except (ValueError, RuntimeError, OSError) as exc:
                self._failed(str(exc))
        self._controller.call("get_control_profile")
        self._target_changed()
        if self._live_pending and self.live_control_switch.isChecked():
            self._live_timer.start()

    def _progress(self, point):
        self._record_voltage(point.actual_v)
        self.progress_bar.setValue(point.index + 1)
        self.calibration_status.setText(
            f"{point.direction} · cycle {point.cycle + 1} · {point.actual_v:+.6g} V → "
            f"{statistics.fmean(point.reference_samples_t) * 1000:+.6g} mT")

    def _succeeded(self, result):
        if isinstance(result, CalibrationRunResult):
            self._last_result = result
            model = result.model
            self._review_model = model
            self.up_curve.setData(model.ascending.voltage_v, model.ascending.field_t)
            self.down_curve.setData(model.descending.voltage_v, model.descending.field_t)
            stddev = max((*model.ascending.stddev_t, *model.descending.stddev_t))
            self.review_summary.setText(
                f"Saved {len(result.points)} points · maximum repeat stddev {stddev * 1000:.6g} mT · "
                f"reference uncertainty {model.context.reference_uncertainty_t * 1000:.6g} mT.\n"
                f"Raw data: {result.path}\nModel: {model.calibration_id}")
            self.calibration_status.setText("Calibration saved. DAC zero confirmed; Kepco power/current state remains unknown.")
            self._record_voltage(0.0)
        elif isinstance(result, MokeVoltageResult):
            self._record_voltage(result.actual_v)
            self.manual_status.setText(
                "DAC zero confirmed. Kepco power/current state remains unknown." if result.safe_target_confirmed else
                f"Requested {result.requested_v:+.6g} V · applied {result.applied_v:+.6g} V · readback confirmed.")
            self._preview_target()
        self.status.emit("MOKE field workflow completed")

    def _activate(self):
        if self._review_model is None or self._profile is None:
            return
        try:
            self._active_model = MokeCalibrationRepository(self._settings.moke_box.calibration_directory).activate(
                self._review_model.calibration_id, profile_fingerprint=self._profile.fingerprint,
                simulation=self._profile.simulation, reviewed=self.reviewed.isChecked())
            self.calibration_changed.emit(self._active_model)
            self.calibration_status.setText("Reviewed calibration activated for this output profile.")
            self._disable_live()
            self._manual_envelope = None
            self._manual_plan = None
            self.voltage_history.clear()
            self._target_changed()
        except (ConfigurationError, ValueError, OSError) as exc:
            self._failed(str(exc))

    def _list_saved_models(self):
        selected = self.saved_models.currentData()
        self.saved_models.clear()
        if self._profile is None:
            return
        repository = MokeCalibrationRepository(self._settings.moke_box.calibration_directory)
        for identity in repository.list_ids():
            model = repository.load(identity)
            if model.context.profile_fingerprint == self._profile.fingerprint and model.context.simulation == self._profile.simulation:
                self.saved_models.addItem(f"{model.created_utc[:19]} · {identity[:10]}", userData=identity)
        index = self.saved_models.findData(selected)
        if index >= 0:
            self.saved_models.setCurrentIndex(index)

    def _load_model(self):
        identity = self.saved_models.currentData()
        if not identity or self.busy:
            return
        try:
            model = MokeCalibrationRepository(self._settings.moke_box.calibration_directory).load(identity)
            if self._profile is None or model.context.profile_fingerprint != self._profile.fingerprint:
                raise ConfigurationError("Calibration does not match the connected output profile.")
            self._review_model = model
            self.reviewed.setChecked(False)
            self.up_curve.setData(model.ascending.voltage_v, model.ascending.field_t)
            self.down_curve.setData(model.descending.voltage_v, model.descending.field_t)
            self.review_summary.setText(
                f"Saved calibration: {model.created_utc}\nProbe: {model.context.probe_id}\n"
                f"Raw run: {model.raw_run_id}\nModel: {model.calibration_id}")
            self._refresh_controls()
        except (ValueError, RuntimeError, OSError) as exc:
            self._failed(str(exc))

    def show_field_preview(self, voltage_v):
        if self._active_model is None or self._profile is None or self._selected_channel() != self._profile.channel:
            self.field_readout.setText("Predicted field: no active calibration")
            return
        try:
            up = self._active_model.ascending.estimate(voltage_v)
            down = self._active_model.descending.estimate(voltage_v)
            self.field_readout.setText(
                f"Calculated field: B↑ {up * 1000:+.6g} mT · B↓ {down * 1000:+.6g} mT. "
                "Prediction assumes recorded full-range conditioning; current field is not measured here.")
        except (ConfigurationError, ValueError) as exc:
            self.field_readout.setText(f"Predicted field unavailable: {exc}")

    def _failed(self, message):
        self.quick_failed.emit(message)
        self._disable_live()
        self._manual_envelope = None
        self._manual_plan = self._calibration_request = None
        self.manual_status.setText(message)
        self.calibration_status.setText(message)
        self.status.emit(f"MOKE field workflow: {message}")
        self._target_changed()

    def _device_result(self, operation, result):
        if operation == "connect":
            self._connected = True
            self._controller.call("get_control_profile")
        elif operation == "get_control_profile":
            previous_profile = self._profile
            previously_qualified = self._profile is not None
            self._profile = result if isinstance(result, MokeControlProfile) else None
            self.configuration_panel.profile = self._profile
            if self._profile is not None:
                try:
                    expected = control_profile_from_settings(self._settings, simulation=self._simulation)
                    if expected.fingerprint != self._profile.fingerprint:
                        raise ConfigurationError("Reconnect after changing the MOKE output profile.")
                except (ValueError, RuntimeError) as exc:
                    self._profile = None
                    self._failed(str(exc))
            if self._profile is not None:
                profile = self._profile
                if profile == previous_profile:
                    # The adapter re-checks the connected binding after a job.
                    # An unchanged profile needs no form/catalog reconstruction.
                    self._target_changed()
                    return
                if not previously_qualified:
                    self.channel_selector.setCurrentIndex(profile.channel)
                self.profile_label.setText(
                    f"{'SIMULATION · ' if profile.simulation else ''}VOUT {profile.channel} · "
                    f"station {profile.minimum_v:g}…{profile.maximum_v:g} V · {profile.binding_id}\n"
                    f"Ramp ≤ {profile.maximum_slew_v_s:g} V/s · step ≥ {profile.step_interval_s:g} s · settling ≥ {profile.minimum_settling_s:g} s"
                    + (" · simulated hardware waits are skipped" if profile.simulation else ""))
                if not previously_qualified:
                    self.manual_status.setText(self._control_disabled_reason())
                try:
                    self._active_model = MokeCalibrationRepository(self._settings.moke_box.calibration_directory).active(
                        profile_fingerprint=profile.fingerprint, simulation=profile.simulation)
                    self._list_saved_models()
                    self.show_field_preview(self._voltage(self.target))
                except (ConfigurationError, ValueError) as exc:
                    self._failed(str(exc))
            else:
                self._revoke_arm()
                self.profile_label.setText("Read-only · no approved voltage-control profile.")
            self.profile_changed.emit(self._profile)
        elif operation == "read_vouts" and isinstance(result, dict):
            self._last_voltages.update(result)
            voltage = result.get(self._selected_channel())
            if voltage is not None:
                self._record_voltage(voltage)
                self._preview_target()
        self._target_changed()

    def _device_error(self, operation, _message):
        if operation in {"connect", "read_vouts", "get_control_profile"}:
            self._connected = False
            self._profile = None
            self._revoke_arm()

    def _state_changed(self, state):
        if state in {"disconnected", "fault"}:
            self._connected = False
            self._profile = None
            self._revoke_arm()
        self._refresh_controls()

    def _reference_result(self, operation, result):
        if operation == "connect":
            self._reference_connected = True
            self._reference_identity = result
            self.reference_label.setText(result.idn)
        self._refresh_controls()

    def _reference_state(self, state):
        if state in {"disconnected", "fault", "unknown"}:
            self._reference_connected = False
            self._reference_identity = None
            self._calibration_request = None
        self._refresh_controls()

    def set_execution_controlled(self, controlled: bool):
        self._external_controlled = controlled
        self._revoke_arm()
        self._refresh_controls()

    def _refresh_controls(self, *_args):
        self.quick_bounds_changed.emit()
        if not hasattr(self, "activate_button"):
            return
        ready = self._connected and self._profile is not None and not self.busy and not self._external_controlled
        manual_ready = ready and self._selected_channel() == self._profile.channel
        self.live_control_switch.setEnabled(manual_ready or (self.busy and self.live_control_switch.isChecked()))
        self.set_button.setEnabled(manual_ready and self._manual_plan is not None)
        self.read_voltage_button.setEnabled(self._connected and not self.busy and not self._external_controlled)
        self.read_configuration_button.setEnabled(self.read_voltage_button.isEnabled())
        self.zero_button.setEnabled(self._connected and self._profile is not None and not self._external_controlled)
        reason = self._control_disabled_reason()
        for button in (self.set_button, self.zero_button, self.live_control_switch):
            if not button.isEnabled():
                button.setToolTip("Operation in progress or reserved by a recipe." if self.busy or self._external_controlled else reason)
            else:
                button.setToolTip("Apply edits automatically after debounce and the previous ramp/settling completes." if button is self.live_control_switch else
                                  "Ramp the qualified DAC to zero; this does not confirm Kepco power-off." if button is self.zero_button else
                                  "Send the draft target through the approved voltage ramp.")
        calibration_ready = ready and self._reference_connected
        self.arm_calibration_button.setEnabled(calibration_ready)
        self.start_calibration_button.setEnabled(calibration_ready and self._calibration_request is not None)
        self.stop_button.setEnabled(self.busy)
        self.activate_button.setEnabled(ready and self._review_model is not None and self.reviewed.isChecked())
        self.load_model_button.setEnabled(ready and self.saved_models.count() > 0)
        self.saved_models.setEnabled(ready)
        live_draft = self.busy and self._running_kind == "voltage" and self.live_control_switch.isChecked()
        editable = (not self.busy or live_draft) and not self._external_controlled
        # Set each input directly to its final state. Disabling a pressed
        # slider/active editor and then re-enabling it interrupts the gesture.
        for control in self._input_controls:
            if control is self.target or control is self.voltage_slider:
                continue
            control.setEnabled(not self.busy and not self._external_controlled)
        self.configuration_panel.level_field.setEnabled(editable)
        self.target.setEnabled(editable)
        self.configuration_panel.level_field.edit_button.setEnabled(not self.busy and editable)
        self.configuration_panel.level_field.range_pill.setEnabled(not self.busy and editable)
        self.voltage_slider.setEnabled(editable and self._slider_mapping is not None)
