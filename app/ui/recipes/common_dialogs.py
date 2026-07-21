"""Shared dialogs and tree widgets for the recipe workspace."""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFormLayout, QHBoxLayout,
    QMessageBox, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CardWidget, CheckBox, ComboBox, LineEdit, PlainTextEdit,
    PrimaryPushButton, PushButton, SpinBox, StrongBodyLabel,
    TreeWidget,
)

from app.domain.errors import ConfigurationError
from app.domain.quantities import DIMENSION_CURRENT, DIMENSION_TIME, DIMENSION_VOLTAGE, parse_quantity
from app.recipes import RecipeNode, generate_sweep_points
from app.recipes.parameter_registry import SWEEPABLE_PARAMETERS as _SWEEPABLE_PARAMETERS
from app.recipes.parameter_registry import sweep_default as _sweep_default
from app.settings.models import StationSettings
from app.ui.common import line_edit as _line
from app.ui.recipes.sweep_editor import SweepGeneratorDialog
from app.ui.recipes.fluent_dialog import FluentRecipeDialog

__all__ = [
    "ActionNodeEditorDialog",
    "AnritsuAcquisitionEditorDialog",
    "CommentEditorDialog",
    "FixedValueDialog",
    "KeithleySweepBuilderDialog",
    "RecipeTreeWidget",
    "SweepLibraryButton",
]


class ActionNodeEditorDialog(FluentRecipeDialog):
    """Edit scalar fields of a non-device recipe action without raw YAML."""

    _TIME_FIELDS = {"duration", "deadline", "sync_delay", "settle_time", "sweep_time"}
    _FREQUENCY_FIELDS = {"frequency", "start_frequency", "stop_frequency", "rbw", "vbw"}

    def __init__(
        self,
        node: RecipeNode,
        parent: QWidget | None = None,
        *,
        in_finally: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        self._node = node
        self._in_finally = in_finally
        self._editors: dict[str, tuple[QWidget, str]] = {}
        self.setWindowTitle(f"Action settings — {node.type.replace('_', ' ').title()}")
        self.setMinimumSize(480, 280)
        self.resize(620, 420)
        layout = QVBoxLayout(self)
        heading = StrongBodyLabel(node.type.replace("_", " ").title(), self)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        note = BodyLabel(
            "These values are stored in the recipe only. Hardware limits and output "
            "interlocks are validated again during preflight and by the device adapter.",
            self,
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        for key, value in node.data.items():
            if key == "disabled":
                continue
            editor, value_kind = self._editor_for(key, value)
            if editor is None:
                rendered = BodyLabel(json.dumps(value, ensure_ascii=False, default=str), self)
                rendered.setWordWrap(True)
                form.addRow(key.replace("_", " ").title(), rendered)
                continue
            self._editors[key] = (editor, value_kind)
            form.addRow(self._field_label(key), editor)
        if not self._editors:
            empty = BodyLabel(
                "This block has no configurable scalar parameters. Its behavior is "
                "defined by its position and child actions.",
                self,
            )
            empty.setObjectName("muted")
            empty.setWordWrap(True)
            form.addRow(empty)
        layout.addLayout(form)
        layout.addStretch(1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = PushButton("Cancel", self)
        self.apply_button = PrimaryPushButton("Apply action settings", self)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.apply_button)
        layout.addLayout(footer)
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button.clicked.connect(self.accept)

    @staticmethod
    def _field_label(key: str) -> str:
        return {
            "enabled": "Output state",
            "checkpoint": "Store as checkpoint",
            "channel": "Channel",
        }.get(key, key.replace("_", " ").title())

    def _editor_for(self, key: str, value: object) -> tuple[QWidget | None, str]:
        if key == "channel":
            combo = ComboBox(self)
            values: tuple[object, ...] = (
                (1, 2)
                if isinstance(value, int)
                or "rigol" in self._node.type
                else ("A", "B")
            )
            for choice in values:
                combo.addItem(str(choice), userData=choice)
            index = combo.findData(value)
            combo.setCurrentIndex(index if index >= 0 else 0)
            return combo, "choice"
        if key == "enabled":
            combo = ComboBox(self)
            combo.addItem("OUTPUT OFF", userData=False)
            if not self._in_finally:
                combo.addItem("OUTPUT ON", userData=True)
            index = combo.findData(bool(value))
            combo.setCurrentIndex(index if index >= 0 else 0)
            if self._in_finally:
                combo.setEnabled(False)
                combo.setToolTip("Finally actions may only switch outputs OFF.")
            return combo, "choice"
        mode_choices = (
            ("NORM", "GAT")
            if self._node.type == "configure_rigol_output"
            else ("current", "voltage", "measure_only")
        )
        choices = {
            "mode": mode_choices,
            "sense_mode": ("2wire", "4wire"),
            "device": ("rigol", "keithley", "anritsu"),
            "operator": ("<", "<=", "==", "!=", ">=", ">"),
            "trace": ("TRAC1",),
            "polarity": ("NORM", "INV"),
            "gate_polarity": ("NORM", "INV"),
            "sync_polarity": ("NORM", "INV"),
        }.get(key)
        if choices is not None:
            combo = ComboBox(self)
            for choice in choices:
                combo.addItem(choice, userData=choice)
            index = combo.findData(str(value))
            combo.setCurrentIndex(index if index >= 0 else 0)
            return combo, "choice"
        if isinstance(value, bool):
            checkbox = CheckBox(self)
            checkbox.setChecked(value)
            return checkbox, "bool"
        if isinstance(value, int):
            spin = SpinBox(self)
            spin.setRange(0, 100_000)
            spin.setValue(value)
            return spin, "int"
        if isinstance(value, float):
            line = LineEdit(self)
            line.setText(f"{value:.12g}")
            return line, "float"
        if isinstance(value, str):
            line = LineEdit(self)
            line.setText(value)
            return line, "str"
        return None, "readonly"

    def node_fields(self) -> dict[str, object]:
        fields = dict(self._node.data)
        for key, (editor, value_kind) in self._editors.items():
            if value_kind == "choice":
                assert isinstance(editor, ComboBox)
                fields[key] = editor.currentData()
            elif value_kind == "bool":
                assert isinstance(editor, CheckBox)
                fields[key] = editor.isChecked()
            elif value_kind == "int":
                assert isinstance(editor, SpinBox)
                fields[key] = editor.value()
            else:
                assert isinstance(editor, LineEdit)
                text = editor.text().strip()
                fields[key] = float(text.replace(",", ".")) if value_kind == "float" else text
        return fields

    @staticmethod
    def _literal(value: object) -> bool:
        return not (isinstance(value, str) and value.startswith("${") and value.endswith("}"))

    def _validate_fields(self, fields: dict[str, object]) -> None:
        for key in self._TIME_FIELDS:
            if key in fields and self._literal(fields[key]):
                parse_quantity(fields[key], DIMENSION_TIME)
        for key in self._FREQUENCY_FIELDS:
            if key in fields and self._literal(fields[key]):
                parse_quantity(fields[key], "frequency")
        mode = str(fields.get("mode", ""))
        if "level" in fields and mode in {"current", "voltage"} and self._literal(fields["level"]):
            parse_quantity(
                fields["level"],
                DIMENSION_CURRENT if mode == "current" else DIMENSION_VOLTAGE,
            )
        if "compliance" in fields and mode in {"current", "voltage"} and self._literal(fields["compliance"]):
            parse_quantity(
                fields["compliance"],
                DIMENSION_VOLTAGE if mode == "current" else DIMENSION_CURRENT,
            )
        for key in ("high_level", "low_level"):
            if key in fields and self._literal(fields[key]):
                parse_quantity(fields[key], DIMENSION_VOLTAGE)

    def accept(self) -> None:
        try:
            fields = self.node_fields()
            self._validate_fields(fields)
            if self._in_finally and fields.get("enabled") is True:
                raise ConfigurationError("Finally actions cannot enable outputs.")
        except Exception as exc:
            QMessageBox.warning(self, "Action settings", str(exc))
            return
        super().accept()


class KeithleySweepBuilderDialog(SweepGeneratorDialog):
    """A device-first sweep window, not a generic point-generator popup."""

    def __init__(
        self, settings: StationSettings, parent: QWidget | None = None,
        *, initial_segments: list[dict[str, object]] | None = None,
    ) -> None:
        self._settings = settings
        definition = next(item for item in _SWEEPABLE_PARAMETERS if item["target"] == "keithley.B.current")
        super().__init__(dict(definition), parent, initial_segments=initial_segments)
        self.setWindowTitle("Keithley 2600 — sweep builder")
        self.resize(980, 650)
        parameters = CardWidget(self)
        parameters.setObjectName("recipeEditorParameters")
        parameter_layout = QVBoxLayout(parameters)
        parameter_layout.setContentsMargins(10, 8, 10, 8)
        title = BodyLabel("Keithley source settings")
        title.setObjectName("sectionTitle")
        parameter_layout.addWidget(title)
        form = QFormLayout()
        self.channel = ComboBox(self)
        self.channel.addItems(("A", "B"))
        self.channel.setCurrentText("B")
        self.mode = ComboBox(self)
        self.mode.addItems(("current", "voltage"))
        self.compliance = _line("67 mV")
        self.nplc = _line("1")
        self.settle_time = _line("100 ms")
        self.sense_mode = ComboBox(self)
        self.sense_mode.addItems(("2wire", "4wire"))
        form.addRow("Channel", self.channel)
        form.addRow("Source mode", self.mode)
        form.addRow("Compliance", self.compliance)
        form.addRow("NPLC", self.nplc)
        form.addRow("Settling time", self.settle_time)
        form.addRow("Sense", self.sense_mode)
        parameter_layout.addLayout(form)
        guidance = BodyLabel(
            "The source is configured at every generated point. This window only designs "
            "the recipe; it never enables OUTPUT."
        )
        guidance.setObjectName("muted")
        guidance.setWordWrap(True)
        parameter_layout.addWidget(guidance)
        self.limit_status = BodyLabel()
        self.limit_status.setObjectName("recipeLimitStatus")
        self.limit_status.setWordWrap(True)
        parameter_layout.addWidget(self.limit_status)
        self.layout().insertWidget(1, parameters)
        self.channel.currentTextChanged.connect(self._parameter_changed)
        self.mode.currentTextChanged.connect(self._parameter_changed)
        self._parameter_changed()

    def _parameter_changed(self) -> None:
        target = f"keithley.{self.channel.currentText()}.{self.mode.currentText()}"
        definition = next(item for item in _SWEEPABLE_PARAMETERS if item["target"] == target)
        previous_dimension = self.definition["dimension"]
        self.definition = dict(definition)
        self.setWindowTitle(f"Keithley 2600 — {definition['label']} sweep")
        self.plot.setLabel("left", definition["label"])
        if previous_dimension != definition["dimension"]:
            self.segments.blockSignals(True)
            self.segments.setRowCount(0)
            self.segments.blockSignals(False)
            self.add_interval()
        else:
            self._refresh_preview()

    def _refresh_preview(self) -> None:
        super()._refresh_preview()
        if not hasattr(self, "limit_status"):
            return
        try:
            points = generate_sweep_points(self.segment_data(), self.definition["dimension"])
            channel = self._settings.keithley.safety.channels[self.channel.currentText()]
            limits = channel.lab_limits
            bounds = (
                limits.source_current
                if self.mode.currentText() == "current"
                else limits.source_voltage
            )
            low = parse_quantity(bounds.min, self.definition["dimension"]).si_value
            high = parse_quantity(bounds.max, self.definition["dimension"]).si_value
            outside = [point for point in points if point.si_value < low or point.si_value > high]
        except Exception:
            self.limit_status.setText("")
            return
        if outside:
            self.limit_status.setText(
                f"BLOCKER — {len(outside):,} point(s) exceed approved range "
                f"{low:.6g} … {high:.6g} SI. Apply is blocked by compiler preflight."
            )
            self.limit_status.setProperty("severity", "blocker")
        else:
            self.limit_status.setText(
                f"✓ All {len(points):,} planned points are inside the approved station range."
            )
            self.limit_status.setProperty("severity", "ok")
        self.limit_status.style().unpolish(self.limit_status)
        self.limit_status.style().polish(self.limit_status)

    def keithley_options(self) -> dict[str, object]:
        return {
            "compliance": self.compliance.text().strip(),
            "nplc": float(self.nplc.text()),
            "settle_time": self.settle_time.text().strip(),
            "sense_mode": self.sense_mode.currentText(),
        }

    def accept(self) -> None:
        try:
            parse_quantity(self.compliance.text(), DIMENSION_VOLTAGE if self.mode.currentText() == "current" else DIMENSION_CURRENT)
            if float(self.nplc.text()) <= 0:
                raise ConfigurationError("NPLC must be positive.")
            parse_quantity(self.settle_time.text(), DIMENSION_TIME)
        except Exception as exc:
            QMessageBox.warning(self, "Keithley sweep", str(exc))
            return
        super().accept()


class FixedValueDialog(FluentRecipeDialog):
    """Create an auditable single setpoint without pretending it is a sweep."""

    def __init__(self, definition: dict[str, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        self.definition = definition
        self.setWindowTitle(f"Fixed value — {definition['label']}")
        layout = QVBoxLayout(self)
        layout.addWidget(
            BodyLabel(
                "This node configures one value at its position in the tree. It does not "
                "create a measurement axis or enable an output."
            )
        )
        form = QFormLayout()
        self.value = LineEdit(self)
        self.value.setText(_sweep_default(definition["dimension"])[0])
        form.addRow(definition["label"], self.value)
        layout.addLayout(form)
        self.preview = BodyLabel()
        layout.addWidget(self.preview)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = PushButton("Cancel", self)
        self.create_button = PrimaryPushButton("Create fixed setting", self)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.create_button)
        layout.addLayout(footer)
        self.value.textChanged.connect(self._validate)
        self.create_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self._validate()

    def _validate(self) -> None:
        try:
            quantity = parse_quantity(self.value.text(), self.definition["dimension"])
        except Exception as exc:
            self.preview.setText(f"Invalid value: {exc}")
        else:
            self.preview.setText(f"One fixed setpoint: {quantity.si_value:.12g} SI")

    def accept(self) -> None:
        try:
            parse_quantity(self.value.text(), self.definition["dimension"])
        except Exception as exc:
            QMessageBox.warning(self, "Fixed value", str(exc))
            return
        super().accept()


class AnritsuAcquisitionEditorDialog(FluentRecipeDialog):
    """Edit trace and reference-processing policy without touching VISA."""

    operations = (
        ("None — raw spectrum", "none"),
        ("Difference in dB", "difference_db"),
        ("Linear ratio", "ratio_linear"),
        ("Add power", "add_power"),
        ("Subtract power", "subtract_power"),
        ("Multiply linear", "multiply_linear"),
    )

    def __init__(
        self,
        node: RecipeNode,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        self._reference_only = node.type == "acquire_reference"
        self.setWindowTitle(
            "Anritsu reference acquisition"
            if self._reference_only
            else "Anritsu spectrum acquisition"
        )
        self.setMinimumSize(480, 300)
        layout = QVBoxLayout(self)
        heading = BodyLabel(
            "Reference acquisition"
            if self._reference_only
            else "Spectrum and reference processing"
        )
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        note = BodyLabel(
            "This editor changes only the declarative plan. The selected trace "
            "and storage policy are validated again during preflight."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        form = QFormLayout()
        self.trace = ComboBox(self)
        self.trace.addItems(("TRAC1",))
        self.trace.setCurrentText(str(node.data.get("trace", "TRAC1")))
        form.addRow("Trace", self.trace)
        self.average_count = SpinBox(self)
        self.average_count.setRange(1, 9999)
        self.average_count.setValue(int(node.data.get("average_count", 1)))
        self.average_count.setToolTip(
            "Complete spectra are averaged in linear mW, never directly in dBm."
        )
        form.addRow("Average complete spectra", self.average_count)
        self.reference_operation = ComboBox(self)
        for label, value in self.operations:
            self.reference_operation.addItem(label, userData=value)
        operation = str(node.data.get("reference_operation", "none"))
        index = self.reference_operation.findData(operation)
        self.reference_operation.setCurrentIndex(index if index >= 0 else 0)
        self.store_raw = CheckBox("Store raw spectrum", self)
        self.store_raw.setChecked(bool(node.data.get("store_raw", True)))
        self.store_processed = CheckBox("Store processed spectrum", self)
        self.store_processed.setChecked(
            bool(node.data.get("store_processed", operation != "none"))
        )
        if not self._reference_only:
            form.addRow("Reference operation", self.reference_operation)
            form.addRow("", self.store_raw)
            form.addRow("", self.store_processed)
        layout.addLayout(form)
        layout.addStretch(1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = PushButton("Cancel", self)
        self.apply_button = PrimaryPushButton("Apply acquisition settings", self)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.apply_button)
        self.apply_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        layout.addLayout(footer)
        self.reference_operation.currentIndexChanged.connect(
            self._operation_changed
        )
        self._operation_changed()

    def _operation_changed(self) -> None:
        processed = self.reference_operation.currentData() != "none"
        self.store_processed.setEnabled(processed)
        if not processed:
            self.store_processed.setChecked(False)

    def node_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "trace": self.trace.currentText(),
            "average_count": self.average_count.value(),
        }
        if not self._reference_only:
            fields.update(
                {
                    "reference_operation": str(
                        self.reference_operation.currentData()
                    ),
                    "store_raw": self.store_raw.isChecked(),
                    "store_processed": self.store_processed.isChecked(),
                }
            )
        return fields

    def accept(self) -> None:
        if (
            not self._reference_only
            and not self.store_raw.isChecked()
            and not self.store_processed.isChecked()
        ):
            QMessageBox.warning(
                self,
                "Anritsu acquisition",
                "Store at least the raw or processed spectrum.",
            )
            return
        super().accept()


class CommentEditorDialog(FluentRecipeDialog):
    """Focused editor for human-readable notes embedded in a sweep tree."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("stationSurface", "page")
        self.setObjectName("commentEditorDialog")
        self.setWindowTitle("Edit comment")
        self.resize(680, 460)
        self.setMinimumSize(500, 360)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(12)

        hero = CardWidget(self)
        hero.setObjectName("commentEditorHero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(14, 12, 14, 12)
        icon = BodyLabel("“")
        icon.setObjectName("commentEditorIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(42, 42)
        hero_layout.addWidget(icon)
        heading = QVBoxLayout()
        title = StrongBodyLabel("Comment in measurement sequence", hero)
        title.setObjectName("commentEditorTitle")
        hint = BodyLabel(
            "Describe the purpose of this step, sample preparation or an operator note. "
            "The comment does not send commands to any instrument."
        )
        hint.setObjectName("commentEditorHint")
        hint.setWordWrap(True)
        heading.addWidget(title)
        heading.addWidget(hint)
        hero_layout.addLayout(heading, 1)
        layout.addWidget(hero)

        self.editor = PlainTextEdit(self)
        self.editor.setObjectName("commentEditorText")
        self.editor.setPlaceholderText(
            "For example: Wait until the sample temperature is stable before acquisition…"
        )
        self.editor.setPlainText(text)
        layout.addWidget(self.editor, 1)
        footer = QHBoxLayout()
        self.counter = BodyLabel()
        self.counter.setObjectName("commentEditorCounter")
        footer.addWidget(self.counter)
        footer.addStretch(1)
        self.cancel_button = PushButton("Cancel", self)
        self.save_button = PrimaryPushButton("Save comment", self)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)
        self.editor.textChanged.connect(self._update_counter)
        self.save_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self._update_counter()
        self.editor.setFocus()

    def comment_text(self) -> str:
        return self.editor.toPlainText().strip()

    def _update_counter(self) -> None:
        count = len(self.editor.toPlainText())
        self.counter.setText(f"{count:,} characters")
        self.save_button.setEnabled(bool(self.comment_text()))


class SweepLibraryButton(PushButton):
    """Clickable library card that also exposes a stable drag payload."""

    mime_type = "application/x-lab-control-sweep-block"

    def __init__(self, drag_kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.drag_kind = drag_kind
        self.setProperty("dragKind", drag_kind)
        self._drag_start = None

    def drag_mime_data(self) -> QMimeData:
        mime = QMimeData()
        mime.setData(self.mime_type, self.drag_kind.encode("utf-8"))
        return mime

    def mousePressEvent(self, event: Any) -> None:
        self._drag_start = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if (
            self._drag_start is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and (event.position().toPoint() - self._drag_start).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            drag = QDrag(self)
            drag.setMimeData(self.drag_mime_data())
            drag.setPixmap(self.icon().pixmap(24, 24))
            drag.exec(Qt.DropAction.CopyAction)
            self._drag_start = None
            return
        super().mouseMoveEvent(event)


class RecipeTreeWidget(TreeWidget):
    """Tree that requests a validated YAML move instead of mutating Qt items."""

    move_requested = Signal(str, str, str, int)
    library_drop_requested = Signal(str, str, str, int)
    library_mime_type = SweepLibraryButton.mime_type

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dragged_node_id: str | None = None
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def startDrag(self, supported_actions: Any) -> None:
        """Pin the logical source before hover changes tree selection."""

        item = self.currentItem()
        node = (
            item.data(0, Qt.ItemDataRole.UserRole)
            if item is not None
            else None
        )
        if not isinstance(node, RecipeNode):
            return
        self._dragged_node_id = node.id
        try:
            super().startDrag(supported_actions)
        finally:
            self._dragged_node_id = None

    def dragEnterEvent(self, event: Any) -> None:
        if event.mimeData().hasFormat(self.library_mime_type):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: Any) -> None:
        if event.mimeData().hasFormat(self.library_mime_type):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: Any) -> None:
        target = self.itemAt(event.position().toPoint())
        if event.mimeData().hasFormat(self.library_mime_type):
            if target is None:
                event.ignore()
                return
            destination = self._drop_destination(target)
            if destination is None:
                event.ignore()
                return
            parent_id, branch, index = destination
            kind = bytes(event.mimeData().data(self.library_mime_type)).decode("utf-8")
            self.library_drop_requested.emit(kind, parent_id, branch, index)
            event.acceptProposedAction()
            return
        if self._dragged_node_id is None or target is None:
            event.ignore()
            return
        destination = self._drop_destination(target)
        if destination is None:
            event.ignore()
            return
        parent_id, branch, index = destination
        self.move_requested.emit(self._dragged_node_id, parent_id, branch, index)
        event.accept()

    def _drop_destination(self, target: QTreeWidgetItem) -> tuple[str, str, int] | None:
        indicator = self.dropIndicatorPosition()
        target_node = target.data(0, Qt.ItemDataRole.UserRole)
        if (
            indicator is QAbstractItemView.DropIndicatorPosition.OnItem
            and isinstance(target_node, RecipeNode)
            and target_node.type in {"sequence", "sweep", "repeat", "if"}
        ):
            return target_node.id, "children", self._logical_child_count(target)
        if (
            indicator is QAbstractItemView.DropIndicatorPosition.OnItem
            and target.text(0).startswith("Else")
            and target.parent() is not None
        ):
            parent_node = target.parent().data(0, Qt.ItemDataRole.UserRole)
            if isinstance(parent_node, RecipeNode) and parent_node.type == "if":
                return parent_node.id, "else", self._logical_child_count(target)
        if (
            indicator is QAbstractItemView.DropIndicatorPosition.OnItem
            and target.text(0).startswith("Finally")
        ):
            return "__finally__", "children", self._logical_child_count(target)

        # Parameter/ROI rows are projections of a node, not recipe nodes.
        # Accepting a drop relative to them produces an index that has no YAML
        # counterpart and was the main cause of apparently disappearing rows.
        if not isinstance(target_node, RecipeNode):
            return None

        parent = target.parent()
        if parent is None:
            return None
        index = self._logical_index(
            parent,
            target,
            below=indicator
            is QAbstractItemView.DropIndicatorPosition.BelowItem,
        )
        if parent.text(0).startswith("Finally"):
            return "__finally__", "children", index
        if parent.text(0).startswith("Else") and parent.parent() is not None:
            owner = parent.parent().data(0, Qt.ItemDataRole.UserRole)
            return (owner.id, "else", index) if isinstance(owner, RecipeNode) else None
        owner = parent.data(0, Qt.ItemDataRole.UserRole)
        return (owner.id, "children", index) if isinstance(owner, RecipeNode) else None

    @staticmethod
    def _logical_child_count(parent: QTreeWidgetItem) -> int:
        return sum(
            isinstance(
                parent.child(index).data(0, Qt.ItemDataRole.UserRole), RecipeNode
            )
            for index in range(parent.childCount())
        )

    @staticmethod
    def _logical_index(
        parent: QTreeWidgetItem,
        target: QTreeWidgetItem,
        *,
        below: bool,
    ) -> int:
        index = 0
        for child_index in range(parent.childCount()):
            child = parent.child(child_index)
            if child is target:
                return index + (1 if below else 0)
            if isinstance(child.data(0, Qt.ItemDataRole.UserRole), RecipeNode):
                index += 1
        return index
