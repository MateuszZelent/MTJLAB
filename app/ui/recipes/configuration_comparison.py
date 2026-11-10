"""Offline, unit-aware review of the fields a recipe will program."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from PySide6.QtGui import QColor
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidgetItem, QWidget, QVBoxLayout, QSizePolicy
from qfluentwidgets import TableWidget, isDarkTheme, StrongBodyLabel, PushButton, CaptionLabel

from app.domain.quantities import parse_quantity
from app.domain.errors import ConfigurationError
from app.ui.design_system.tokens import tokens_for


def equivalent_setting(left: object, right: object) -> bool:
    """Compare physical values in SI, without confusing dimensions or prefixes."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    try:
        a, b = parse_quantity(str(left)), parse_quantity(str(right))
        return a.dimension == b.dimension and math.isclose(
            a.si_value, b.si_value, rel_tol=1e-9, abs_tol=0.0
        )
    except (ValueError, ConfigurationError):
        try:
            return math.isclose(float(str(left)), float(str(right)), rel_tol=1e-9)
        except ValueError:
            return str(left) == str(right)


@dataclass(frozen=True)
class ConfigurationComparisonRow:
    key: str
    label: str
    current: object | None
    planned: object
    action: str
    source: str = ""
    note: str = ""


class ConfigurationComparison(TableWidget):
    """All programmed fields, including automatic and coupled settings."""

    summary_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("configurationComparison")
        self.setColumnCount(4)
        self.setHorizontalHeaderLabels(["Parameter", "Current device-page setting", "Sweep value", "Effect"])
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(28)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setMinimumHeight(180)
        self.setMaximumHeight(250)
        self.rows: tuple[ConfigurationComparisonRow, ...] = ()
        self.counts: dict[str, int] = {}
        self.summary_text = "No configuration changes"
        self._recipe_context = None
        self._device_values = None
        self._incoming_by_key = {}
        self._recipe_differences = set()
        self._authored_rows: list[ConfigurationComparisonRow] = []

    def set_recipe_context(self, resolver, device_values=None) -> None:
        """Compare device-tab settings with targets; retain recipe order as context."""
        self._recipe_context = resolver
        self._device_values = device_values
        self.setColumnCount(5)
        self.setHorizontalHeaderLabels([
            "Parameter", "Device tab setting", "Sweep value", "Effect", "Recipe context",
        ])
        self.set_rows(self._authored_rows)

    def _resolve_recipe_rows(self, rows):
        if self._recipe_context is None:
            return rows
        context = self._recipe_context()
        device = self._device_values() if self._device_values is not None else None
        self._incoming_by_key = {}
        self._recipe_differences = set()
        aliases = {
            "spectrum.start_frequency": "start_frequency",
            "spectrum.stop_frequency": "stop_frequency",
            "spectrum.reference_level": "reference_level",
            "spectrum.points": "points",
            "sg.frequency": "frequency", "sg.power": "power",
            "enabled": "output_enabled", "level": "source_level",
            "settle_time": "settling_time",
            "mode": "source_mode" if "source_mode" in context or device is not None and "source_mode" in device else "output_mode",
        }
        resolved = []
        for row in rows:
            key = aliases.get(row.key, row.key.removeprefix("advanced.").removeprefix("spectrum."))
            incoming = context.get(key)
            self._incoming_by_key[row.key] = incoming
            current = row.current if device is None else device.get(row.key, device.get(key))
            source = incoming.source if incoming is not None else "This block"
            if incoming is not None and incoming.varying:
                source += " (depends on execution)"
            planned = row.planned
            if key == "preamplifier_enabled" and isinstance(planned, str) and planned.casefold() in {"true", "false"}:
                planned = planned.casefold() == "true"
            if row.action == "Preserve":
                if incoming is not None:
                    planned = incoming.value if not incoming.varying else "Depends on earlier execution"
                else:
                    planned = current if current is not None else "Keep existing value"
                    source = "Device tab / preserved" if current is not None else "Not programmed"
            if (incoming is not None and not incoming.varying
                    and ((current is not None and not equivalent_setting(current, incoming.value))
                         or (row.action == "Set" and not equivalent_setting(incoming.value, planned)))):
                self._recipe_differences.add(row.key)
            resolved.append(replace(row, current=current, planned=planned, source=source))
        source_range = next((row for row in resolved if row.key == "source_range"), None)
        source_mode = next((row for row in resolved if row.key == "source_mode"), None)
        for index, row in enumerate(resolved):
            if row.key.startswith("effective_measure_") and source_range is not None:
                current_mode = device.get("source_mode") if device is not None else None
                current = device.get("source_range") if device is not None and current_mode == (source_mode.planned if source_mode else None) and device.get("source_autorange") is False else None
                planned = source_range.planned if source_range.planned != "AUTO" else "Depends on source autorange during execution"
                resolved[index] = replace(row, current=current, planned=planned,
                                          source=source_range.source)
        return resolved

    def set_rows(self, rows: list[ConfigurationComparisonRow]) -> None:
        self._authored_rows = list(rows)
        rows = self._resolve_recipe_rows(rows)
        self.rows = tuple(rows)
        tokens = tokens_for("dark" if isDarkTheme() else "light")
        self.setRowCount(len(rows))
        counts = dict(changed=0, same=0, preserved=0, unknown=0, sweep=0, blocked=0)
        for index, row in enumerate(rows):
            incoming = self._incoming_by_key.get(row.key)
            prerequisite_failed = (self._recipe_context is not None and row.action == "Require"
                                   and incoming is not None and not incoming.varying
                                   and not equivalent_setting(incoming.value, row.planned))
            prohibited_sense = row.key == "sense_mode" and row.action != "Preserve" and row.planned != "2wire"
            if prohibited_sense:
                status, color = "PROHIBITED: only 2wire", tokens.danger
            elif row.action == "Preserve":
                incoming = self._incoming_by_key.get(row.key)
                if self._recipe_context is not None and incoming is not None and incoming.varying:
                    status, color = "Depends on earlier execution", tokens.caution
                elif self._recipe_context is not None and row.current is not None:
                    same = equivalent_setting(row.current, row.planned)
                    status = "Same as device tab; preserved" if same else "Changes from device tab in earlier block"
                    color = tokens.success if same else tokens.caution
                else:
                    status, color = "Not set by this block" if self._recipe_context is not None else "Not programmed", tokens.text_muted
            elif row.action == "Sweep":
                status, color = "Changes over ROI", tokens.caution
            elif row.action == "Derived":
                if self._recipe_context is not None and row.current is not None:
                    same = equivalent_setting(row.current, row.planned)
                    status = "Same as device tab" if same else "Changes from device tab"
                    color = tokens.success if same else tokens.caution
                else:
                    status, color = "Automatic source-range coupling", tokens.caution
            elif prerequisite_failed:
                status, color = "Earlier recipe requirement not met", tokens.danger
            elif row.current is None:
                if self._recipe_context is not None:
                    status = "Set from recipe" if row.action == "Set" else "Check prerequisite"
                    color = tokens.caution if row.action == "Set" else tokens.text_muted
                else:
                    status, color = "Current unknown", tokens.text_muted
            elif row.action == "Require" and not equivalent_setting(row.current, row.planned):
                if self._recipe_context is not None:
                    status, color = "Differs from device tab; recipe prerequisite", tokens.caution
                else:
                    status, color = "Requirement not met", tokens.danger
            elif equivalent_setting(row.current, row.planned):
                status, color = "Same as device tab" if self._recipe_context is not None else "Same", tokens.success
                if row.key in self._recipe_differences:
                    status += "; changes earlier recipe value"
            else:
                status, color = "Changes", tokens.caution
            if prohibited_sense:
                counts["blocked"] += 1
            elif row.action == "Preserve":
                counts["preserved"] += 1
                incoming = self._incoming_by_key.get(row.key)
                if (self._recipe_context is not None and row.current is not None
                        and not (incoming is not None and incoming.varying)):
                    counts["same" if equivalent_setting(row.current, row.planned) else "changed"] += 1
            elif row.action == "Require":
                if prerequisite_failed:
                    counts["blocked"] += 1
                elif row.current is None:
                    counts["unknown"] += 1
                elif self._recipe_context is not None:
                    counts["same" if equivalent_setting(row.current, row.planned) else "changed"] += 1
                elif not equivalent_setting(row.current, row.planned):
                    counts["blocked"] += 1
            elif row.action == "Sweep":
                counts["sweep"] += 1
            elif row.action == "Derived":
                if self._recipe_context is None:
                    counts["unknown"] += 1
                elif row.current is not None:
                    counts["same" if equivalent_setting(row.current, row.planned) else "changed"] += 1
            elif row.action == "Set" and (row.current is not None or self._recipe_context is None):
                counts["unknown" if row.current is None else
                       "same" if equivalent_setting(row.current, row.planned) else "changed"] += 1
            missing = "Unknown" if self._recipe_context is None else (
                "Device tab unavailable"
            )
            values = (row.label, missing if row.current is None else str(row.current),
                      str(row.planned), f"{row.action} · {status}")
            if row.key in {"output_enabled", "enabled", "output"}:
                values = (values[0],
                          ("ON" if row.current else "OFF") if isinstance(row.current, bool) else values[1],
                          ("ON" if row.planned else "OFF") if isinstance(row.planned, bool) else values[2],
                          values[3])
            if self._recipe_context is not None:
                incoming = self._incoming_by_key.get(row.key)
                detail = row.source
                if incoming is not None:
                    detail = f"{incoming.source}: {'varies' if incoming.varying else incoming.value}"
                values += (detail,)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value + ("\n" + row.note if row.note else ""))
                item.setForeground(QColor(tokens.caution if column == 4 and row.key in self._recipe_differences else color))
                self.setItem(index, column, item)
        self.counts = counts
        noun = "parameter" if counts["changed"] == 1 else "parameters"
        parts = [f"{counts['changed']} {noun} changed", f"{counts['same']} same"]
        if self._recipe_context is not None:
            specified = sum(row.action == "Set" for row in rows)
            parts = [f"{counts['same']} same as device tab", f"{counts['changed']} different from device tab",
                     f"{specified} parameters to set"]
            if self._recipe_differences:
                parts.append(f"{len(self._recipe_differences)} recipe-order changes")
            derived = sum(row.action == "Derived" for row in rows)
            if derived:
                parts.append(f"{derived} automatic effects")
        for key, label in (("sweep", "varied by ROI"), ("preserved", "preserved"),
                           ("unknown", "unknown"), ("blocked", "blocked / mismatch")):
            if counts[key]:
                if key == "unknown" and self._recipe_context is not None:
                    label = "prerequisites to verify"
                parts.append(f"{counts[key]} {label}")
        self.summary_text = " · ".join(parts)
        self.summary_changed.emit(self.summary_text)


class ConfigurationReview(QWidget):
    """Compact persistent summary with an optional complete mutation table."""

    def __init__(self, parent=None, *, baseline_label="device-page settings at opening",
                 current_label="Current device-page setting"):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.summary = StrongBodyLabel(self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        note = CaptionLabel(f"Compared with {baseline_label}. Unknown values are not counted as unchanged.", self)
        self.baseline_note = note
        note.setWordWrap(True)
        layout.addWidget(note)
        self.toggle = PushButton("Show parameter comparison", self)
        self.toggle.setCheckable(True)
        layout.addWidget(self.toggle)
        self.table = ConfigurationComparison(self)
        self.table.horizontalHeaderItem(1).setText(current_label)
        self.table.setMinimumHeight(120)
        self.table.setMaximumHeight(160)
        self.table.hide()
        layout.addWidget(self.table)
        self.table.summary_changed.connect(self.summary.setText)
        self.table.summary_changed.connect(self._fit_table)
        self.toggle.toggled.connect(self._toggle)

    def _toggle(self, checked):
        self.table.setVisible(checked)
        self.toggle.setText("Hide parameter comparison" if checked else "Show parameter comparison")

    def _fit_table(self, _summary):
        self.table.setFixedHeight(min(160, 38 + 28 * self.table.rowCount()))

    def bind(self, host, rows):
        """Observe form edits only; the callback must never write to hardware."""
        def refresh(*args):
            self.table.set_rows(rows())
        for widget in host.findChildren(QWidget):
            if self.isAncestorOf(widget):
                continue
            for name in ("textChanged", "currentIndexChanged", "valueChanged", "toggled"):
                signal = getattr(widget, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(refresh)
                    break
        refresh()


def current_parameter_setting(parent, target):
    """Find the owning recipe page's read-only draft provider, never hardware."""
    while parent is not None:
        provider = getattr(parent, "parameter_snapshot_provider", None)
        if callable(provider):
            try:
                return provider().get(target)
            except Exception:
                return None
        parent = parent.parentWidget()
    return None

