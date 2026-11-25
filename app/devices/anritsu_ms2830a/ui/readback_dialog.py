"""Modeless, read-only comparison of Anritsu hardware settings vs form settings."""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
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
    isDarkTheme,
)

from app.devices.anritsu_ms2830a.adapter import AnritsuFullConfigurationReadback
from app.domain.quantities import (
    DIMENSION_FREQUENCY,
    DIMENSION_TIME,
    format_quantity_auto,
)
from app.safety.anritsu import normalize_anritsu_detector
from app.ui.design_system import tokens_for
from app.ui.dialogs import StationDialog


def _relative_luminance(color: QColor) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.redF())
        + 0.7152 * channel(color.greenF())
        + 0.0722 * channel(color.blueF())
    )


def _contrast_ratio(foreground: QColor, background: QColor) -> float:
    low, high = sorted((_relative_luminance(foreground), _relative_luminance(background)))
    return (high + 0.05) / (low + 0.05)


def _ensure_wcag_aa(
    foreground: QColor,
    background: QColor,
    *,
    dark: bool,
    min_ratio: float = 4.5,
) -> QColor:
    candidate = QColor(foreground)
    for _ in range(12):
        if _contrast_ratio(candidate, background) >= min_ratio:
            return candidate
        candidate = candidate.lighter(110) if dark else candidate.darker(110)
    return candidate


def _format_vbw_mode_label(vbw_auto: Any, vbw_hz: float | None) -> str:
    if isinstance(vbw_auto, str):
        normalized = vbw_auto.strip().lower()
        if normalized == "auto":
            return "Auto"
        if normalized == "off":
            return "OFF"
        if normalized == "manual":
            return "Manual"
        return vbw_auto
    if vbw_auto:
        return "Auto"
    return "OFF" if vbw_hz is None else "Manual"


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
        super().__init__(parent, resizable=True)
        self._readback = readback
        self._form_values = form_values
        self.setObjectName("anritsuReadbackDialog")
        self.setWindowTitle("Anritsu MS2830A — settings read from device")
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMinimumSize(QSize(860, 560))

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
                _format_vbw_mode_label(readback.vbw_auto, readback.vbw_hz),
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
        self._readonly_action_items: list[QTableWidgetItem] = []
        match_color, mismatch_color, muted_color = self._semantic_colors()

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
            color = match_color if matches else mismatch_color
            hw_item.setForeground(color)
            status_item.setForeground(color)
            self.table.setItem(row, 2, status_item)
            self._status_items[key] = status_item

            # Action
            if not is_assignable:
                action_item = QTableWidgetItem("—")
                action_item.setForeground(muted_color)
                action_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, 3, action_item)
                self._readonly_action_items.append(action_item)
            else:
                btn = PushButton("Use hardware value", self.table)
                btn.setProperty("compact", True)
                btn.setToolTip(f"Copy the hardware value for {display_name} to the form.")
                btn.clicked.connect(
                    lambda _checked=False, k=key, v=hw_raw, r=row: self._on_assign(k, v, r)
                )
                self.table.setCellWidget(row, 3, btn)
                self._action_buttons[key] = btn

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setStretchLastSection(False)
        self.table.setColumnWidth(3, 172)
        layout.addWidget(self.table, 1)

        # Footer actions (standard right-aligned compact modal footer)
        footer_layout = QHBoxLayout()
        footer_layout.setSpacing(8)
        footer_layout.addStretch(1)

        self.close_button = PushButton("Close", surface)
        self.close_button.clicked.connect(self.close)
        footer_layout.addWidget(self.close_button)

        self.use_all_button = PrimaryPushButton("Use all compatible values", surface)
        self.use_all_button.setToolTip(
            "Copy all compatible hardware values (frequencies, points, ref level, RBW, VBW) into the form."
        )
        self.use_all_button.clicked.connect(self._on_use_all)
        footer_layout.addWidget(self.use_all_button)

        layout.addLayout(footer_layout)
        self.refresh_form_values(form_values)

    def _semantic_colors(self) -> tuple[QColor, QColor, QColor]:
        dark = isDarkTheme()
        tokens = tokens_for("dark" if dark else "light")
        surface = QColor(tokens.dialog_surface)
        return (
            _ensure_wcag_aa(QColor(tokens.success), surface, dark=dark),
            _ensure_wcag_aa(QColor(tokens.danger), surface, dark=dark),
            _ensure_wcag_aa(QColor(tokens.text_muted), surface, dark=dark),
        )

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.StyleChange,
        } and hasattr(self, "_status_items"):
            self.refresh_form_values(self._form_values)

    def refresh_form_values(self, form_values: dict[str, Any]) -> None:
        """Compare against the actual form after its owner handles a request."""
        self._form_values = dict(form_values)
        match_color, mismatch_color, muted_color = self._semantic_colors()
        for item in self._readonly_action_items:
            item.setForeground(muted_color)
        for row, (key, _, _, hardware, _, assignable) in enumerate(self._param_definitions):
            current = form_values.get(key)
            compared = key in form_values and not (
                current is None and (hardware is not None or (key == "vbw_hz" and form_values.get("vbw_auto")))
            )
            matches = compared and self._check_match(key, hardware, current)
            status = self._status_items[key]
            if not assignable:
                text = "Info (read-only)"
            elif not compared:
                text = "Not compared (automatic or unavailable)"
            else:
                text = "MATCH" if matches else self._status_text(key, False, current)
            color = (
                (match_color if matches else mismatch_color)
                if assignable and compared
                else muted_color
            )
            status.setText(text)
            status.setToolTip(text)
            status.setForeground(color)
            hw_item = self.table.item(row, 1)
            if hw_item is not None:
                hw_item.setForeground(color)
            button = self._action_buttons.get(key)
            if button is not None:
                button.setEnabled(not matches)
                button.setText("Matches form" if matches else "Use hardware value")

    @staticmethod
    def _check_match(key: str, hw_val: Any, form_val: Any) -> bool:
        if form_val is None:
            return hw_val is None
        if key == "detector":
            return normalize_anritsu_detector(str(hw_val)) == normalize_anritsu_detector(str(form_val))
        if isinstance(hw_val, bool) or isinstance(form_val, bool):
            return isinstance(hw_val, bool) and isinstance(form_val, bool) and hw_val == form_val
        if isinstance(hw_val, float) and isinstance(form_val, (float, int)):
            return (
                math.isfinite(hw_val)
                and math.isfinite(float(form_val))
                and math.isclose(hw_val, float(form_val), rel_tol=5e-9, abs_tol=1e-12)
            )
        if isinstance(hw_val, int) and isinstance(form_val, int):
            return hw_val == form_val
        if isinstance(hw_val, int) and isinstance(form_val, float):
            return math.isfinite(form_val) and math.isclose(
                float(hw_val), form_val, rel_tol=5e-9, abs_tol=1e-12
            )
        if isinstance(hw_val, str) and isinstance(form_val, str):
            return hw_val.strip().upper() == form_val.strip().upper()
        return False

    def _status_text(self, key: str, matches: bool, form_val: Any) -> str:
        if form_val is None:
            return "Info (read-only)"
        if matches:
            return "MATCH"
        if isinstance(form_val, str):
            if key == "vbw_mode":
                upper = form_val.strip().upper()
                if upper in {"VID", "POW"}:
                    return f"Form: {'Video' if upper == 'VID' else 'Power'}"
            elif key == "vbw_auto":
                return f"Form: {_format_vbw_mode_label(form_val, None)}"
            return f"Form: {form_val}"
        # Format numeric / boolean form value cleanly
        if key in {"start_hz", "stop_hz", "center_hz", "span_hz", "rbw_hz", "vbw_hz"}:
            return f"Form: {format_quantity_auto(float(form_val), DIMENSION_FREQUENCY)}"
        if key == "reference_level_dbm":
            return f"Form: {float(form_val):.9g} dBm"
        if key == "rbw_auto":
            return f"Form: {'Auto' if form_val else 'Manual'}"
        if key == "vbw_auto":
            return f"Form: {'Auto' if form_val else 'Manual'}"
        if key == "vbw_mode":
            return f"Form: {'Video' if form_val == 'VID' else 'Power'}"
        return f"Form: {form_val}"

    def _on_assign(self, key: str, value: Any, row: int) -> None:
        self.assign_requested.emit(key, value)

    def _on_use_all(self) -> None:
        self.assign_all_requested.emit(self._readback)
