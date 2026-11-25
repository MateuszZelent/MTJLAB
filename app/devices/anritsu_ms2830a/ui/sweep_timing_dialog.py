"""Fluent controls for the spectrum analyzer's sweep method and synchronization."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFormLayout, QFrame, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CheckBox,
    ComboBox,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
)

from app.devices.anritsu_ms2830a.sweep_timing import (
    FFT_WIDTHS_HZ,
    SWEEP_RULES,
    TIMING_PARAMETERS,
    TIMING_SOURCES,
    TRIGGER_EDGES,
    SpectrumTimingSettings,
    SpectrumTimingSnapshot,
)
from app.domain.errors import SafetyViolation
from app.domain.quantities import (
    DIMENSION_DBM,
    DIMENSION_FREQUENCY,
    DIMENSION_TIME,
    format_quantity_auto,
    parse_quantity,
)
from app.ui.dialogs import StationDialog
from app.ui.widgets import FluentTabView


class SweepTimingDialog(StationDialog):
    read_requested = Signal()
    apply_requested = Signal(object)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, resizable=True)
        self.setObjectName("anritsuSweepTimingDialog")
        self.setWindowTitle("Anritsu Sweep / FFT and timing")
        self.resize(650, 570)
        self.setMinimumSize(490, 440)
        self._snapshot: SpectrumTimingSnapshot | None = None
        self._can_edit = False
        self._editors: dict[str, ComboBox | CheckBox | LineEdit] = {}
        self._forms: dict[str, QFormLayout] = {}
        self._dimensions = {}
        self._forced_sweep = False
        self._sweep_rule_draft = "DRAN"
        self._forced_trigger = False
        self._trigger_source_draft = "IMM"

        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=10)
        self.method = BodyLabel("Read the analyzer to confirm its current settings.", surface)
        self.method.setWordWrap(True)
        layout.addWidget(self.method)
        self.tabs = FluentTabView(surface)
        layout.addWidget(self.tabs, 1)
        sweep = self._section("Sweep / FFT")
        trigger = self._section("Trigger")
        gate = self._section("Gate")

        self._choice(sweep, "Sweep rule", "sweep_rule", SWEEP_RULES)
        self._choice(sweep, "Requested FFT width", "fft_width_hz", {
            value: format_quantity_auto(value, DIMENSION_FREQUENCY) for value in FFT_WIDTHS_HZ
        })
        self._choice(sweep, "RBW filter", "rbw_filter", {"NORM": "Normal"})
        self.rbw_note = self._note(sweep, "CISPR requires a verified 016/116 hardware option.")
        self._note(sweep, "The tested MS2830A firmware 7.03.00 accepted up to 10,001 points. "
                          "It rejected 20,001 and 30,001 points in Spectrum.")
        self._note(sweep, "Gate and manual sweep time use Sweep only.")

        self._choice(trigger, "Source", "trigger_source", {
            value: TIMING_SOURCES[value] for value in ("IMM", "EXT", "WIF", "VID", "FRAM")
        })
        self._choice(trigger, "External edge", "external_edge", TRIGGER_EDGES)
        self._choice(trigger, "Wide IF edge", "wide_if_edge", TRIGGER_EDGES)
        self._quantity(trigger, "Wide IF threshold", "wide_if_level_dbm", DIMENSION_DBM)
        self._choice(trigger, "Video edge", "video_edge", TRIGGER_EDGES)
        self._quantity(trigger, "Video threshold", "video_level_dbm", DIMENSION_DBM)
        self._choice(trigger, "Frame sync input", "frame_sync_source", {
            value: TIMING_SOURCES[value] for value in ("IMM", "EXT", "WIF")
        })
        self._quantity(trigger, "Frame period", "frame_period_s", DIMENSION_TIME)
        self._quantity(trigger, "Frame sync offset", "frame_offset_s", DIMENSION_TIME)
        self._note(trigger, "Frame timing also applies when Gate uses Frame period.")

        gate_enabled = CheckBox("Enable gated sweep", surface)
        gate_enabled.setObjectName("gate_enabled")
        self._editors["gate_enabled"] = gate_enabled
        gate.addRow(gate_enabled)
        self._choice(gate, "Gate source", "gate_source", {
            "IMM": "Choose a source", **{value: TIMING_SOURCES[value] for value in ("EXT", "WIF", "FRAM")}
        })
        self._choice(gate, "Gate edge", "gate_edge", TRIGGER_EDGES)
        self._quantity(gate, "Wide IF threshold", "gate_level_dbm", DIMENSION_DBM)
        self._quantity(gate, "Gate delay", "gate_delay_s", DIMENSION_TIME)
        self._quantity(gate, "Gate length", "gate_length_s", DIMENSION_TIME)
        self._note(gate, "Gate uses its own synchronization source and sets Trigger to Free run.")
        self._note(gate, "For a periodic gate, set Frame period and sync input in Trigger.")

        self.status = BodyLabel("", surface)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.read_button = PushButton("Read from instrument", surface)
        self.apply_button = PrimaryPushButton("Apply and verify", surface)
        close = PushButton("Close", surface)
        actions.addWidget(self.read_button)
        actions.addWidget(self.apply_button)
        actions.addStretch(1)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.read_button.clicked.connect(self.read_requested)
        self.apply_button.clicked.connect(self._apply)
        close.clicked.connect(self.hide)
        for field in ("sweep_rule", "trigger_source", "gate_source", "frame_sync_source"):
            self._editors[field].currentIndexChanged.connect(self._sync_editors)
        gate_enabled.toggled.connect(self._sync_editors)
        self.set_available(read=False, apply=False)

    def _section(self, name: str) -> QFormLayout:
        section = ScrollArea(self.tabs)
        section.setWidgetResizable(True)
        section.setFrameShape(QFrame.Shape.NoFrame)
        section.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(section)
        layout = QVBoxLayout(content)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setVerticalSpacing(10)
        layout.addLayout(form)
        layout.addStretch(1)
        section.setWidget(content)
        self.tabs.addTab(section, name)
        return form

    def _choice(self, form: QFormLayout, label: str, field: str, values: Mapping) -> None:
        editor = ComboBox(self)
        editor.setAccessibleName(label)
        editor.setObjectName(field)
        for value, text in values.items():
            editor.addItem(text, userData=value)
        self._editors[field] = editor
        self._forms[field] = form
        form.addRow(BodyLabel(label), editor)

    def _quantity(self, form: QFormLayout, label: str, field: str, dimension: str) -> None:
        editor = LineEdit(self)
        editor.setAccessibleName(label)
        editor.setObjectName(field)
        self._editors[field] = editor
        self._forms[field] = form
        self._dimensions[field] = dimension
        minimum, maximum = TIMING_PARAMETERS[field].limits
        editor.setToolTip(f"{format_quantity_auto(minimum, dimension)} to {format_quantity_auto(maximum, dimension)}")
        form.addRow(BodyLabel(label), editor)

    def _note(self, form: QFormLayout, text: str) -> BodyLabel:
        note = BodyLabel(text, self)
        note.setWordWrap(True)
        form.addRow(note)
        return note

    def set_available(self, *, read: bool, apply: bool) -> None:
        self.read_button.setEnabled(read)
        self._can_edit = apply and self._snapshot is not None
        self.apply_button.setEnabled(self._can_edit)
        self._sync_editors()

    def reset_readback(self) -> None:
        self._snapshot = None
        self.method.setText("Read the analyzer to confirm its current settings.")
        self.apply_button.setEnabled(False)
        self._can_edit = False
        self._sync_editors()

    def load_snapshot(self, snapshot: SpectrumTimingSnapshot) -> None:
        self._snapshot = snapshot
        filter_editor = self._editors["rbw_filter"]
        if snapshot.rbw_filter_switchable and filter_editor.findData("CISP") < 0:
            filter_editor.addItem("CISPR", userData="CISP")
        if not snapshot.rbw_filter_switchable and filter_editor.findData("CISP") >= 0:
            filter_editor.removeItem(filter_editor.findData("CISP"))
        self.rbw_note.setVisible(not snapshot.rbw_filter_switchable)
        for field, editor in self._editors.items():
            value = getattr(snapshot.settings, field)
            signals = editor.blockSignals(True)
            if isinstance(editor, ComboBox):
                if value == "SG" and editor.findData(value) < 0:
                    editor.addItem(TIMING_SOURCES[value], userData=value)
                editor.setCurrentIndex(editor.findData(value))
            elif isinstance(editor, LineEdit):
                editor.setText(format_quantity_auto(value, self._dimensions[field]))
            else:
                editor.setChecked(value)
            editor.blockSignals(signals)
        self._forced_sweep = False
        self._sweep_rule_draft = snapshot.settings.sweep_rule
        self._forced_trigger = False
        self._trigger_source_draft = snapshot.settings.trigger_source
        method = "FFT" if snapshot.effective_method == "FFT" else "Sweep"
        if snapshot.actual_fft_width_hz is not None:
            method += " · " + format_quantity_auto(snapshot.actual_fft_width_hz, DIMENSION_FREQUENCY)
        self.method.setText(f"Instrument method  ·  {method}")
        self._sync_editors()

    def _sync_editors(self, *_args) -> None:
        rule = self._editors["sweep_rule"]
        trigger_editor = self._editors["trigger_source"]
        gate = self._editors["gate_enabled"].isChecked()
        if gate != self._forced_trigger:
            self._forced_trigger = gate
            signals = trigger_editor.blockSignals(True)
            if gate:
                self._trigger_source_draft = trigger_editor.currentData()
                trigger_editor.setCurrentIndex(trigger_editor.findData("IMM"))
            else:
                trigger_editor.setCurrentIndex(trigger_editor.findData(self._trigger_source_draft))
            trigger_editor.blockSignals(signals)
        trigger = trigger_editor.currentData()
        gate_source = self._editors["gate_source"].currentData()
        frame = trigger == "FRAM" or gate and gate_source == "FRAM"
        forced = gate or bool(self._snapshot and not self._snapshot.sweep_time_auto)
        if forced != self._forced_sweep:
            self._forced_sweep = forced
            signals = rule.blockSignals(True)
            if forced:
                self._sweep_rule_draft = rule.currentData()
                rule.setCurrentIndex(rule.findData("OSW"))
            else:
                rule.setCurrentIndex(rule.findData(self._sweep_rule_draft))
            rule.blockSignals(signals)
        availability = {
            "sweep_rule": not forced,
            "fft_width_hz": not forced and rule.currentData() == "PFFT",
            "rbw_filter": bool(self._snapshot and self._snapshot.rbw_filter_switchable),
            "trigger_source": not gate,
            "external_edge": trigger == "EXT",
            "wide_if_edge": trigger == "WIF",
            "wide_if_level_dbm": trigger == "WIF",
            "video_edge": trigger == "VID",
            "video_level_dbm": trigger == "VID",
            "frame_period_s": frame,
            "frame_sync_source": frame,
            "frame_offset_s": frame,
            "gate_source": gate,
            "gate_edge": gate and gate_source in {"EXT", "WIF", "SG"},
            "gate_level_dbm": gate and gate_source == "WIF",
            "gate_delay_s": gate,
            "gate_length_s": gate,
        }
        for field, editor in self._editors.items():
            editor.setEnabled(self._can_edit and availability.get(field, True))
        for field in (
            "external_edge", "wide_if_edge", "wide_if_level_dbm", "video_edge", "video_level_dbm",
            "frame_period_s", "frame_sync_source", "frame_offset_s", "gate_edge", "gate_level_dbm",
        ):
            self._forms[field].setRowVisible(self._editors[field], availability[field])

    def configuration(self) -> SpectrumTimingSettings:
        if self._snapshot is None:
            raise ValueError("Read the analyzer settings before applying.")
        values = {}
        for field, editor in self._editors.items():
            if not editor.isEnabled() and field not in {"sweep_rule", "trigger_source"}:
                continue
            if isinstance(editor, ComboBox):
                value = editor.currentData()
            elif isinstance(editor, CheckBox):
                value = editor.isChecked()
            else:
                value = parse_quantity(editor.text(), self._dimensions[field]).si_value
            TIMING_PARAMETERS[field].validate(field, value)
            values[field] = value
        requested = replace(self._snapshot.settings, **values)
        requested.validate_sources()
        return requested

    def _apply(self) -> None:
        try:
            config = self.configuration()
        except (ValueError, SafetyViolation) as exc:
            self.status.setText(str(exc))
            return
        self.apply_requested.emit(config)
