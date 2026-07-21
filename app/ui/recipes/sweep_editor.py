"""Generic dynamic-sweep and ROI editors for recipe construction."""

# ruff: noqa: F401
from __future__ import annotations

import math
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLineEdit, QSplitter, QStyledItemDelegate,
    QTableWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, ComboBox, LineEdit, PrimaryPushButton, PushButton, TableWidget,
)

from app.recipes.parameter_registry import sweep_default as _sweep_default
from app.domain.errors import ConfigurationError
from app.recipes import estimate_sweep_point_count, generate_sweep_points, generate_sweep_stage_points
from app.ui.common import line_edit as _line
from app.ui.dialogs import StationMessageBox as QMessageBox
from app.ui.design_system import effective_theme, plot_theme, tokens_for
from app.ui.recipes.fluent_dialog import FluentRecipeDialog
from app.safety.quick_controls import QuickControlSafetyBound, quick_control_safety_bounds
from app.settings.models import StationSettings


class SeamlessRoiCellDelegate(QStyledItemDelegate):
    """Make ROI values edit like a spreadsheet cell, not a nested text box."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("seamlessRoiCellDelegate")

    def createEditor(self, parent: QWidget, _option: Any, _index: Any) -> LineEdit:
        editor = LineEdit(parent)
        editor.setFrame(False)
        editor.setContentsMargins(0, 0, 0, 0)
        editor.setStyleSheet(
            "LineEdit { border: none; border-radius: 0; "
            "background: transparent; padding: 0 7px; }"
        )
        return editor

    def updateEditorGeometry(self, editor: QWidget, option: Any, _index: Any) -> None:
        editor.setGeometry(option.rect)


class SweepGeneratorDialog(FluentRecipeDialog):
    """Dynamic interval editor with an exact point scatter preview."""

    def __init__(
        self,
        definition: dict[str, str],
        parent: QWidget | None = None,
        *,
        initial_segments: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        self.definition = definition
        self.setWindowTitle(f"Point generator — {definition['label']}")
        self.setMinimumSize(640, 560)
        self.resize(1180, 700)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        heading = BodyLabel(
            "Build any number of inclusive intervals. Each interval uses either a point count "
            "or a physical step; the scatter plot always shows the exact generated points."
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self._safety_bound = self._resolve_safety_bound()
        self.safety_limits = CaptionLabel(self)
        self.safety_limits.setObjectName("sweepSafetyLimits")
        self.safety_limits.setWordWrap(True)
        if self._safety_bound is None:
            self.safety_limits.setText(
                "Safety range: final limits are validated during recipe preflight."
            )
        else:
            self.safety_limits.setText(
                "Allowed sweep range  ·  "
                f"MIN {self._safety_bound.minimum_text}  ·  "
                f"MAX {self._safety_bound.maximum_text}"
            )
        layout.addWidget(self.safety_limits)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.segment_panel = QWidget()
        self.segment_panel.setProperty("stationSurface", "page")
        self.segment_panel.setMinimumWidth(0)
        left_layout = QVBoxLayout(self.segment_panel)
        left_layout.setContentsMargins(0, 0, 6, 0)
        self.segments = TableWidget(self.segment_panel)
        self.segments.setProperty("stationSurface", "surface")
        self.segments.viewport().setProperty("stationSurface", "surface")
        self.segments.setRowCount(0)
        self.segments.setColumnCount(5)
        self.segments.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.segments.setHorizontalHeaderLabels(
            ("Start / value", "Stop", "Method", "Points / step", "Spacing")
        )
        self.segments.setEditTriggers(
            QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        self._roi_cell_delegate = SeamlessRoiCellDelegate(self.segments)
        for column in (0, 1, 3):
            self.segments.setItemDelegateForColumn(
                column, self._roi_cell_delegate
            )
        header = self.segments.horizontalHeader()
        header.setMinimumSectionSize(72)
        header.setStretchLastSection(False)
        for column in range(5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        for column, width in ((0, 94), (1, 94), (2, 118), (3, 122), (4, 122)):
            self.segments.setColumnWidth(column, width)
        self.segments.verticalHeader().setMinimumSectionSize(34)
        self.segments.setMinimumHeight(190)
        left_layout.addWidget(self.segments, 1)
        actions = QHBoxLayout()
        self.add_segment = PushButton("+ Add interval", self.segment_panel)
        self.remove_segment = PushButton("Remove interval", self.segment_panel)
        actions.addWidget(self.add_segment)
        actions.addWidget(self.remove_segment)
        left_layout.addLayout(actions)
        self.splitter.addWidget(self.segment_panel)
        self.plot_panel = QWidget()
        self.plot_panel.setProperty("stationSurface", "page")
        self.plot_panel.setMinimumWidth(0)
        right_layout = QVBoxLayout(self.plot_panel)
        right_layout.setContentsMargins(6, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setMinimumHeight(280)
        self.plot_theme = self._resolved_plot_theme()
        self._apply_table_theme()
        self._apply_plot_theme()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.legend = self.plot.addLegend(offset=(10, 10))
        self._set_plot_labels()
        right_layout.addWidget(self.plot, 1)
        self.preview = BodyLabel("Add an interval to generate points.", self.plot_panel)
        self.preview.setWordWrap(True)
        right_layout.addWidget(self.preview)
        self.splitter.addWidget(self.plot_panel)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([450, 700])
        layout.addWidget(self.splitter, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = PushButton("Cancel", self)
        self.create_button = PrimaryPushButton("Create sweep node", self)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.create_button)
        layout.addLayout(footer)
        self.add_segment.clicked.connect(self.add_interval)
        self.remove_segment.clicked.connect(self.remove_interval)
        self.segments.cellChanged.connect(self._refresh_preview)
        self.create_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        if initial_segments:
            for segment in initial_segments:
                self.add_interval(segment)
        else:
            self.add_interval()
        self._update_responsive_layout()
        self._connect_theme_source()

    def _resolve_safety_bound(self) -> QuickControlSafetyBound | None:
        owner: QWidget | None = self.parentWidget()
        while owner is not None:
            settings = getattr(owner, "_settings", None)
            if isinstance(settings, StationSettings):
                return quick_control_safety_bounds(settings).get(
                    str(self.definition.get("target", ""))
                )
            owner = owner.parentWidget()
        return None

    def _refresh_safety_bound(self) -> None:
        """Refresh the visible limit contract after changing the sweep target."""

        self._safety_bound = self._resolve_safety_bound()
        if self._safety_bound is None:
            self.safety_limits.setText(
                "Safety range: final limits are validated during recipe preflight."
            )
            return
        self.safety_limits.setText(
            "Allowed sweep range  ·  "
            f"MIN {self._safety_bound.minimum_text}  ·  "
            f"MAX {self._safety_bound.maximum_text}"
        )

    def _validate_safety_bounds(self, points: tuple[Any, ...]) -> None:
        if self._safety_bound is None:
            return
        for point in points:
            if not (
                self._safety_bound.minimum_si
                <= point.si_value
                <= self._safety_bound.maximum_si
            ):
                raise ConfigurationError(
                    f"Sweep value {point.si_value:.12g} SI is outside the allowed "
                    f"range [{self._safety_bound.minimum_text}, "
                    f"{self._safety_bound.maximum_text}]."
                )

    def _resolved_plot_theme(self) -> str:
        application = QApplication.instance()
        if application is not None:
            for property_name in ("activeTheme", "stationAppliedTheme"):
                active = application.property(property_name)
                if str(active).lower() in {"light", "dark"}:
                    return str(active).lower()
        owner: QWidget | None = self.parentWidget()
        while owner is not None:
            settings = getattr(owner, "_settings", None)
            ui = getattr(settings, "ui", None)
            if isinstance(ui, dict):
                return effective_theme(str(ui.get("theme", "system")))
            owner = owner.parentWidget()
        return effective_theme("system")

    def _connect_theme_source(self) -> None:
        owner: QWidget | None = self.parentWidget()
        while owner is not None:
            signal = getattr(owner, "theme_changed", None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(self._set_plot_theme)
                return
            owner = owner.parentWidget()

    def _set_plot_theme(self, theme: str) -> None:
        resolved = effective_theme(theme)
        if resolved == self.plot_theme:
            return
        self.plot_theme = resolved
        self._apply_table_theme()
        self._apply_plot_theme()
        self._set_plot_labels()
        self._style_plot_legend()

    def _apply_plot_theme(self) -> None:
        palette = plot_theme(tokens_for(self.plot_theme))
        self.plot.setBackground(palette.background)
        plot_item = self.plot.getPlotItem()
        for name in ("left", "bottom"):
            axis = plot_item.getAxis(name)
            axis.setPen(pg.mkPen(palette.axes, width=1))
            axis.setTextPen(pg.mkPen(palette.axes))
        plot_item.getViewBox().setBorder(pg.mkPen(palette.grid, width=1))

    def _apply_table_theme(self) -> None:
        """Map QFluent's transparent table viewport onto station tokens."""

        tokens = tokens_for(self.plot_theme)
        for widget in (self.segments, self.segments.viewport()):
            palette = widget.palette()
            for role, color in (
                (QPalette.ColorRole.Window, tokens.surface),
                (QPalette.ColorRole.Base, tokens.surface),
                (QPalette.ColorRole.AlternateBase, tokens.surface_raised),
                (QPalette.ColorRole.Text, tokens.text_primary),
                (QPalette.ColorRole.WindowText, tokens.text_primary),
                (QPalette.ColorRole.Highlight, tokens.accent),
            ):
                palette.setColor(role, QColor(color))
            palette.setColor(
                QPalette.ColorRole.HighlightedText,
                QColor(tokens.on_emergency),
            )
            widget.setPalette(palette)
            widget.setAutoFillBackground(True)
            widget.update()

    def _set_plot_labels(self) -> None:
        foreground = tokens_for(self.plot_theme).plot_axes
        self.plot.setLabel("bottom", "Generated point index", color=foreground)
        self.plot.setLabel("left", self.definition["label"], color=foreground)

    def _style_plot_legend(self) -> None:
        tokens = tokens_for(self.plot_theme)
        foreground = tokens.plot_axes
        self.legend.setBrush(pg.mkBrush(tokens.surface))
        self.legend.setPen(pg.mkPen(tokens.border))
        for _sample, label in self.legend.items:
            try:
                label.setText(label.text, color=foreground)
            except (AttributeError, TypeError):
                continue

    def _update_responsive_layout(self) -> None:
        narrow = self.width() < 1040
        orientation = (
            Qt.Orientation.Vertical if narrow else Qt.Orientation.Horizontal
        )
        if self.splitter.orientation() != orientation:
            self.splitter.setOrientation(orientation)
        if narrow:
            self.segment_panel.setMinimumWidth(0)
            self.segments.setMinimumWidth(0)
            self.plot_panel.setMinimumWidth(0)
            self.splitter.setSizes([260, 350])
        else:
            self.segment_panel.setMinimumWidth(600)
            self.segments.setMinimumWidth(580)
            self.plot_panel.setMinimumWidth(420)
            available = max(960, self.splitter.width())
            table_width = max(600, min(640, round(available * 0.52)))
            self.splitter.setSizes([table_width, max(420, available - table_width)])
        self._resize_segment_columns()
        QTimer.singleShot(0, self._resize_segment_columns)

    def _resize_segment_columns(self) -> None:
        """Fit all five ROI fields without hiding Method or Spacing."""

        if not hasattr(self, "segments"):
            return
        viewport_width = self.segments.viewport().width()
        if viewport_width <= 0:
            return
        method_width = 118
        value_width = 122
        spacing_width = 122
        flexible = max(176, viewport_width - method_width - value_width - spacing_width - 12)
        start_width = flexible // 2
        stop_width = flexible - start_width
        for column, width in (
            (0, start_width),
            (1, stop_width),
            (2, method_width),
            (3, value_width),
            (4, spacing_width),
        ):
            self.segments.setColumnWidth(column, width)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "splitter"):
            self._update_responsive_layout()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if not hasattr(self, "plot_theme"):
            return
        if event.type() in {
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.StyleChange,
            QEvent.Type.ThemeChange,
        }:
            # QFluent updates the application palette/style before child
            # widgets receive this event. Reapply the explicit plot/table
            # tokens afterwards so an already-open ROI editor changes as one
            # coherent surface rather than retaining light fragments.
            QTimer.singleShot(
                0,
                lambda: self._set_plot_theme(self._resolved_plot_theme()),
            )

    def add_interval(self, initial: dict[str, object] | None = None) -> None:
        row = self.segments.rowCount()
        self.segments.blockSignals(True)
        self.segments.insertRow(row)
        start, stop = _sweep_default(self.definition["dimension"])
        values = initial or {}
        method_value = (
            "Single value"
            if "value" in values
            else "Step" if "step" in values else "Points"
        )
        point_value = str(values.get("step", values.get("points", "100")))
        for column, value in (
            (0, str(values.get("value", values.get("start", start)))),
            (1, str(values.get("stop", stop))),
            (3, point_value),
        ):
            self.segments.setItem(row, column, QTableWidgetItem(value))
        method = ComboBox(self.segments)
        method.setObjectName("roiCellCombo")
        method.addItems(("Points", "Step", "Single value"))
        method.setMinimumWidth(112)
        spacing = ComboBox(self.segments)
        spacing.setObjectName("roiCellCombo")
        spacing.addItems(("Linear", "Logarithmic"))
        spacing.setMinimumWidth(112)
        method.setCurrentText(method_value)
        spacing.setCurrentText("Logarithmic" if values.get("spacing") == "log" else "Linear")
        method.currentIndexChanged.connect(
            lambda _index, widget=method: self._update_row_method(widget)
        )
        spacing.currentIndexChanged.connect(self._refresh_preview)
        self.segments.setCellWidget(row, 2, method)
        self.segments.setCellWidget(row, 4, spacing)
        self.segments.blockSignals(False)
        self._update_row_method(method)
        self._refresh_preview()

    def _update_row_method(self, method: ComboBox) -> None:
        row = next(
            (
                index
                for index in range(self.segments.rowCount())
                if self.segments.cellWidget(index, 2) is method
            ),
            -1,
        )
        if row < 0:
            return
        single = method.currentText() == "Single value"
        start_item = self.segments.item(row, 0)
        if start_item is not None:
            start_item.setToolTip(
                "Exact value for one measurement"
                if single
                else "Inclusive start value of this interval"
            )
        for column, fallback in ((1, _sweep_default(self.definition["dimension"])[1]), (3, "100")):
            item = self.segments.item(row, column)
            if item is None:
                continue
            if single:
                if item.text() != "—":
                    item.setData(Qt.ItemDataRole.UserRole, item.text())
                item.setText("—")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setData(Qt.ItemDataRole.ForegroundRole, None)
                item.setToolTip("Not used by a Single value stage")
            else:
                stored = item.data(Qt.ItemDataRole.UserRole)
                if item.text() == "—":
                    item.setText(str(stored or fallback))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                item.setData(Qt.ItemDataRole.ForegroundRole, None)
                item.setToolTip(
                    "Inclusive stop value"
                    if column == 1
                    else "Number of points or physical step"
                )
        spacing = self.segments.cellWidget(row, 4)
        if isinstance(spacing, ComboBox):
            spacing.setEnabled(not single)
        self._refresh_preview()

    def remove_interval(self) -> None:
        if self.segments.rowCount() > 1:
            self.segments.removeRow(self.segments.currentRow() if self.segments.currentRow() >= 0 else self.segments.rowCount() - 1)
        self._refresh_preview()

    def select_interval(self, row: int | None) -> None:
        if row is None or not 0 <= row < self.segments.rowCount():
            return
        self.segments.setCurrentCell(row, 0)
        item = self.segments.item(row, 0)
        if item is not None:
            self.segments.scrollToItem(
                item, QAbstractItemView.ScrollHint.PositionAtCenter
            )
        self.segments.setFocus()

    def segment_data(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for row in range(self.segments.rowCount()):
            start = self.segments.item(row, 0)
            stop = self.segments.item(row, 1)
            value = self.segments.item(row, 3)
            method = self.segments.cellWidget(row, 2)
            spacing = self.segments.cellWidget(row, 4)
            if not all((start, stop, value)) or not isinstance(method, ComboBox) or not isinstance(spacing, ComboBox):
                raise ConfigurationError("Every interval needs start, stop and point data.")
            if method.currentText() == "Single value":
                result.append({"value": start.text().strip()})
                continue
            raw: dict[str, object] = {
                "start": start.text().strip(),
                "stop": stop.text().strip(),
                "spacing": "log" if spacing.currentText() == "Logarithmic" else "linear",
            }
            if method.currentText() == "Points":
                raw["points"] = int(value.text())
            else:
                raw["step"] = value.text().strip()
            result.append(raw)
        return result

    def _refresh_preview(self) -> None:
        try:
            segments = self.segment_data()
            point_count = estimate_sweep_point_count(
                segments, self.definition["dimension"]
            )
            if point_count > 100_000:
                self.plot.clear()
                self.preview.setText(
                    f"BLOCKER — {point_count:,} points exceed the 100,000 point "
                    "plan limit. Reduce the point count or increase the step."
                )
                self.create_button.setEnabled(False)
                return
            stages = generate_sweep_stage_points(
                segments, self.definition["dimension"]
            )
            points = tuple(point for stage in stages for point in stage)
            self._validate_safety_bounds(points)
        except Exception as exc:
            self.plot.clear()
            self.preview.setText(f"Invalid point generator: {exc}")
            self.create_button.setEnabled(False)
            return
        self.plot.clear()
        # PlotItem.clear() removes data but preserves the legend, so the labels
        # always describe precisely the intervals visible in this refresh.
        palette = ("#4fa3ff", "#ef9b4b", "#73c991", "#d484d8", "#e6ce55", "#69cbd0")
        point_index = 0
        previous_point: float | None = None
        for stage_index, stage in enumerate(stages):
            if not stage:
                continue
            color = palette[stage_index % len(palette)]
            stride = max(1, math.ceil(len(stage) / 2_000))
            visible_indices = list(range(0, len(stage), stride))
            if visible_indices[-1] != len(stage) - 1:
                visible_indices.append(len(stage) - 1)
            x_values = [point_index + index for index in visible_indices]
            y_values = [stage[index].si_value for index in visible_indices]
            # Shared boundaries are deduplicated in the execution axis. For
            # presentation, reconnect the next stage to the preceding point so
            # 0 → 1 followed by 1 → 0 is shown as one continuous trajectory.
            if previous_point is not None and point_index > 0:
                x_values.insert(0, point_index - 1)
                y_values.insert(0, previous_point)
            self.plot.plot(
                x_values,
                y_values,
                pen=pg.mkPen(color, width=1),
                symbol="o" if len(stage) <= 2_000 else None,
                symbolSize=6,
                symbolBrush=color,
                name=f"Stage {stage_index + 1} ({len(stage):,} points)",
            )
            point_index += len(stage)
            previous_point = stage[-1].si_value
        self._style_plot_legend()
        self.preview.setText(
            f"Generated {len(points):,} unique points • first {points[0].si_value:.12g} SI • "
            f"last {points[-1].si_value:.12g} SI"
        )
        self.create_button.setEnabled(True)

    def accept(self) -> None:
        try:
            segments = self.segment_data()
            point_count = estimate_sweep_point_count(
                segments, self.definition["dimension"]
            )
            self._validate_safety_bounds(
                generate_sweep_points(segments, self.definition["dimension"])
            )
        except Exception as exc:
            QMessageBox.warning(self, "Point generator", str(exc))
            return
        if point_count > 100_000:
            QMessageBox.warning(self, "Point generator", "The generator exceeds the 100,000 point safety preview limit.")
            return
        super().accept()
