"""Modeless, read-only comparison of Anritsu hardware settings vs form settings."""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QSizePolicy,
    QTableWidgetItem,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    TableWidget,
)

from app.devices.anritsu_ms2830a.adapter import AnritsuFullConfigurationReadback
from app.domain.quantities import (
    DIMENSION_FREQUENCY,
    DIMENSION_TIME,
    format_quantity_auto,
)
from app.safety.anritsu import normalize_anritsu_detector
from app.ui.dialogs import StationDialog


class AnritsuReadbackDialog(StationDialog):
    """Modeless comparison dialog between Anritsu hardware and form values."""

    assign_requested = Signal(str, object)
    assign_all_requested = Signal(object)

    def __init__(
        self,
        readback: AnritsuFullConfigurationReadback,
        form_values: dict[str, Any],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._readback = readback
        self._form_values = form_values
        self.setObjectName("anritsuReadbackDialog")
        self.setWindowTitle("Anritsu MS2830A — settings read from device")
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMinimumSize(QSize(860, 560))
        self.setSizeGripEnabled(True)

        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(QSize(940, 640))
        else:
            available = screen.availableGeometry()
            self.resize(
                min(1000, max(860, available.width() - 80)),
                min(720, max(560, available.height() - 80)),
            )

        surface = self.use_modal_shell_content().surface
        layout = self.modal_content_layout(spacing=10)
        layout.setSpacing(8)

        title = StrongBodyLabel("Hardware configuration snapshot", surface)
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        note = BodyLabel(
            "Read-only SCPI queries were used. No setting or RF state was changed on the instrument. "
            "Compare hardware settings with the current form values and choose which parameters to sync.",
            surface,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        mode_text = (
            f"Instrument mode: {readback.instrument_mode or 'SPECT'} · "
            f"Sweep: {'Continuous' if readback.continuous_sweep else 'Single'}"
        )
        mode_label = BodyLabel(mode_text, surface)
        mode_label.setObjectName("muted")
        layout.addWidget(mode_label)

        self.table = TableWidget(surface)
        self.table.setObjectName("anritsuReadbackTable")
        self.table.setAccessibleName("Anritsu hardware settings comparison")
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(
            [
                "Parameter",
                "Hardware value",
                "Form comparison",
                "Action",
            ]
        )
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.table.setWordWrap(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)

        # Definitions: (param_key, display_name, hw_formatted, hw_raw, form_raw, is_assignable)
        self._param_definitions = [
            (
                "start_hz",
                "Start frequency",
                format_quantity_auto(readback.start_hz, DIMENSION_FREQUENCY),
                readback.start_hz,
                form_values.get("start_hz"),
                True,
            ),
            (
                "stop_hz",
                "Stop frequency",
                format_quantity_auto(readback.stop_hz, DIMENSION_FREQUENCY),
                readback.stop_hz,
                form_values.get("stop_hz"),
                True,
            ),
            (
                "center_hz",
                "Center frequency",
                format_quantity_auto(readback.center_hz, DIMENSION_FREQUENCY),
                readback.center_hz,
                form_values.get("center_hz"),
                True,
            ),
            (
                "span_hz",
                "Frequency span",
                format_quantity_auto(readback.span_hz, DIMENSION_FREQUENCY),
                readback.span_hz,
                form_values.get("span_hz"),
                True,
            ),
            (
                "reference_level_dbm",
                "Reference level",
                f"{readback.reference_level_dbm:.9g} dBm",
                readback.reference_level_dbm,
                form_values.get("reference_level_dbm"),
                True,
            ),
            (
                "points",
                "Points",
                str(readback.points),
                readback.points,
                form_values.get("points"),
                True,
            ),
            (
                "rbw_auto",
                "RBW mode",
                "Auto" if readback.rbw_auto else "Manual",
                readback.rbw_auto,
                form_values.get("rbw_auto"),
                True,
            ),
            (
                "rbw_hz",
                "Resolution bandwidth",
                format_quantity_auto(readback.rbw_hz, DIMENSION_FREQUENCY),
                readback.rbw_hz,
                form_values.get("rbw_hz"),
                True,
            ),
            (
                "vbw_mode",
                "VBW filter mode",
                "Video" if readback.vbw_mode == "VID" else "Power",
                readback.vbw_mode,
                form_values.get("vbw_mode"),
                True,
            ),
            (
                "vbw_auto",
                "VBW mode",
                "Auto" if readback.vbw_auto else "Manual",
                readback.vbw_auto,
                form_values.get("vbw_auto"),
                True,
            ),
            (
                "vbw_hz",
                "Video bandwidth",
                (
                    format_quantity_auto(readback.vbw_hz, DIMENSION_FREQUENCY)
                    if readback.vbw_hz is not None
                    else "OFF"
                ),
                readback.vbw_hz,
                form_values.get("vbw_hz"),
                True,
            ),
            (
                "sweep_time_s",
                "Sweep time",
                f"{format_quantity_auto(readback.sweep_time_s, DIMENSION_TIME)} ({'Auto' if readback.sweep_time_auto else 'Manual'})",
                readback.sweep_time_s,
                None,
                False,
            ),
            (
                "attenuation_db",
                "RF attenuation",
                f"{readback.attenuation_db:.9g} dB ({'Auto' if readback.attenuation_auto else 'Manual'})",
                readback.attenuation_db,
                None,
                False,
            ),
            (
                "detector",
                "Detector",
                normalize_anritsu_detector(readback.detector),
                normalize_anritsu_detector(readback.detector),
                form_values.get("detector"),
                True,
            ),
            (
                "average_count",
                "Average count",
                str(readback.average_count),
                readback.average_count,
                form_values.get("average_count"),
                True,
            ),
        ]

        self.table.setRowCount(len(self._param_definitions))
        self._status_items: dict[str, QTableWidgetItem] = {}
        self._action_buttons: dict[str, PushButton] = {}

        for row, (key, display_name, hw_text, hw_raw, form_raw, is_assignable) in enumerate(
            self._param_definitions
        ):
            # Parameter
            param_item = QTableWidgetItem(display_name)
            self.table.setItem(row, 0, param_item)

            # Hardware value
            hw_item = QTableWidgetItem(hw_text)
            self.table.setItem(row, 1, hw_item)

            # Match check
            matches = self._check_match(key, hw_raw, form_raw)
            status_text = self._status_text(key, matches, form_raw)
            status_item = QTableWidgetItem(status_text)
            color = QColor("#168a45" if matches else "#c43b3b")
            hw_item.setForeground(color)
            status_item.setForeground(color)
            self.table.setItem(row, 2, status_item)
            self._status_items[key] = status_item

            # Action
            if not is_assignable:
                action_item = QTableWidgetItem("—")
                action_item.setForeground(self.palette().color(QPalette.ColorRole.Mid))
                action_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, 3, action_item)
            else:
                btn = PushButton("Use hardware value", self.table)
                btn.setProperty("compact", True)
                btn.setToolTip(f"Copy the hardware value for {display_name} to the form.")
                btn.clicked.connect(
                    lambda _checked=False, k=key, v=hw_raw, r=row: self._on_assign(k, v, r)
                )
                self.table.setCellWidget(row, 3, btn)
                self._action_buttons[key] = btn

        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table, 1)

        # Footer actions
        footer_layout = QHBoxLayout()
        footer_layout.setSpacing(8)

        self.use_all_button = PrimaryPushButton("Use all compatible values", surface)
        self.use_all_button.setToolTip(
            "Copy all compatible hardware values (frequencies, points, ref level, RBW, VBW) into the form."
        )
        self.use_all_button.clicked.connect(self._on_use_all)
        footer_layout.addWidget(self.use_all_button)

        footer_layout.addStretch(1)

        self.close_button = PushButton("Close", surface)
        self.close_button.clicked.connect(self.close)
        footer_layout.addWidget(self.close_button)

        layout.addLayout(footer_layout)

    @staticmethod
    def _check_match(key: str, hw_val: Any, form_val: Any) -> bool:
        if form_val is None:
            return True
        if key == "detector":
            return normalize_anritsu_detector(str(hw_val)) == normalize_anritsu_detector(str(form_val))
        if isinstance(hw_val, float) and isinstance(form_val, (float, int)):
            return math.isclose(hw_val, float(form_val), rel_tol=1e-5, abs_tol=1e-3)
        if isinstance(hw_val, int) and isinstance(form_val, int):
            return hw_val == form_val
        if isinstance(hw_val, bool) and isinstance(form_val, bool):
            return hw_val == form_val
        if isinstance(hw_val, str) and isinstance(form_val, str):
            return hw_val.strip().upper() == form_val.strip().upper()
        return str(hw_val) == str(form_val)

    def _status_text(self, key: str, matches: bool, form_val: Any) -> str:
        if form_val is None:
            return "Info (read-only)"
        if matches:
            return "MATCH"
        # Format form value nicely
        if key in {"start_hz", "stop_hz", "center_hz", "span_hz", "rbw_hz", "vbw_hz"}:
            if form_val is not None:
                return f"Form: {format_quantity_auto(float(form_val), DIMENSION_FREQUENCY)}"
        elif key == "reference_level_dbm":
            return f"Form: {float(form_val):.9g} dBm"
        elif key in {"rbw_auto", "vbw_auto"}:
            return f"Form: {'Auto' if form_val else 'Manual'}"
        elif key == "vbw_mode":
            return f"Form: {'Video' if form_val == 'VID' else 'Power'}"
        return f"Form: {form_val}"

    def _on_assign(self, key: str, value: Any, row: int) -> None:
        self.assign_requested.emit(key, value)
        status_item = self._status_items.get(key)
        if status_item:
            status_item.setText("MATCH")
            status_item.setForeground(QColor("#168a45"))
        btn = self._action_buttons.get(key)
        if btn:
            btn.setEnabled(False)
            btn.setText("Applied")

    def _on_use_all(self) -> None:
        self.assign_all_requested.emit(self._readback)
        for key, status_item in self._status_items.items():
            if key in self._action_buttons:
                status_item.setText("MATCH")
                status_item.setForeground(QColor("#168a45"))
                btn = self._action_buttons.get(key)
                if btn:
                    btn.setEnabled(False)
                    btn.setText("Applied")
