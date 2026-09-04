"""Fluent code viewer with syntax highlighting, line numbers, and copy action."""

from __future__ import annotations

import re

from PySide6.QtCore import QRect, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    FluentIcon,
    InfoBadge,
    PlainTextEdit,
    TransparentToolButton,
    isDarkTheme,
    qconfig,
)


class CodeSyntaxHighlighter(QSyntaxHighlighter):
    """High-performance theme-aware regex syntax highlighter for YAML and JSON."""

    def __init__(self, document: QTextDocument, language: str = "yaml") -> None:
        super().__init__(document)
        self._language = language.lower()
        self._rules: list[tuple[re.Pattern[str], QTextCharFormat]] = []
        self._formats: dict[str, QTextCharFormat] = {}
        self._build_formats()
        self._build_rules()

    def set_language(self, language: str) -> None:
        self._language = language.lower()
        self._build_rules()
        self.rehighlight()

    def update_theme(self) -> None:
        self._build_formats()
        self._build_rules()
        self.rehighlight()

    def _build_formats(self) -> None:
        dark = isDarkTheme()

        # Token color palettes designed for high readability in dark/light modes
        colors = {
            "key": QColor("#4EC9B0" if dark else "#0451A5"),
            "string": QColor("#CE9178" if dark else "#A31515"),
            "number": QColor("#B5CEA8" if dark else "#098658"),
            "boolean": QColor("#569CD6" if dark else "#0000FF"),
            "comment": QColor("#6A9955" if dark else "#008000"),
            "operator": QColor("#D4D4D4" if dark else "#24292E"),
            "tag": QColor("#C586C0" if dark else "#AF00DB"),
            "anchor": QColor("#9CDCFE" if dark else "#795E26"),
        }

        self._formats.clear()
        for name, color in colors.items():
            fmt = QTextCharFormat()
            fmt.setForeground(color)
            if name == "comment":
                fmt.setFontItalic(True)
            elif name in {"key", "boolean", "tag"}:
                fmt.setFontWeight(QFont.Weight.DemiBold)
            self._formats[name] = fmt

    def _build_rules(self) -> None:
        self._rules.clear()
        if self._language == "yaml":
            self._build_yaml_rules()
        elif self._language == "json":
            self._build_json_rules()

    def _build_yaml_rules(self) -> None:
        # Comments first
        self._rules.append(
            (re.compile(r"#[^\n]*"), self._formats["comment"])
        )
        # Directives / Tags: !tag
        self._rules.append(
            (re.compile(r"![a-zA-Z0-9_\.\-]+"), self._formats["tag"])
        )
        # Anchors and references: &name, *name
        self._rules.append(
            (re.compile(r"[&*][a-zA-Z0-9_\.\-]+"), self._formats["anchor"])
        )
        # Dictionary keys: words before colon
        self._rules.append(
            (re.compile(r"(?:^|\s)([\w\.\-]+)(?=\s*:)"), self._formats["key"])
        )
        # Booleans and nulls
        self._rules.append(
            (
                re.compile(
                    r"\b(true|false|True|False|TRUE|FALSE|null|Null|NULL|none|None|yes|no|on|off)\b"
                ),
                self._formats["boolean"],
            )
        )
        # Numbers with optional scientific notation and physical SI units
        self._rules.append(
            (
                re.compile(
                    r"\b[-+]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?\s*(?:mA|uA|nA|A|mV|uV|nV|V|Hz|kHz|MHz|GHz|THz|dBm|dB|s|ms|us|ns|Ohm|kOhm|MOhm|deg|K|C)?\b"
                ),
                self._formats["number"],
            )
        )
        # Quoted strings (double and single)
        self._rules.append(
            (re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"'), self._formats["string"])
        )
        self._rules.append(
            (re.compile(r"'[^'\\]*(?:\\.[^'\\]*)*'"), self._formats["string"])
        )
        # List markers: -
        self._rules.append(
            (re.compile(r"^\s*(-)\s+"), self._formats["operator"])
        )

    def _build_json_rules(self) -> None:
        # JSON keys: "key":
        self._rules.append(
            (re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"(?=\s*:)'), self._formats["key"])
        )
        # JSON string values
        self._rules.append(
            (re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"'), self._formats["string"])
        )
        # Booleans and null
        self._rules.append(
            (re.compile(r"\b(true|false|null)\b"), self._formats["boolean"])
        )
        # Numbers
        self._rules.append(
            (
                re.compile(r"\b[-+]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?\b"),
                self._formats["number"],
            )
        )

    def highlightBlock(self, text: str) -> None:
        for pattern, fmt in self._rules:
            for match in pattern.finditer(text):
                start = match.start()
                length = match.end() - start
                self.setFormat(start, length, fmt)


class LineNumberArea(QWidget):
    """Gutter widget rendering line numbers next to the editor."""

    def __init__(self, editor: CodeEditorArea) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event: QPaintEvent) -> None:
        self._editor.line_number_area_paint_event(event)


class CodeEditorArea(PlainTextEdit):
    """Monospace text editor area with line numbers and syntax highlighting."""

    def __init__(self, language: str = "yaml", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)

        # Typography
        code_font = QFont(["Cascadia Code", "Consolas", "Fira Code", "Courier New", "monospace"])
        code_font.setPointSize(10)
        code_font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(code_font)

        # Tab width: 2 spaces for YAML
        space_width = self.fontMetrics().horizontalAdvance(" ")
        self.setTabStopDistance(space_width * 2)

        # Line number area
        self._line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self._update_line_number_area_width(0)

        # Syntax highlighter
        self.highlighter = CodeSyntaxHighlighter(self.document(), language=language)

        # Theme sync
        qconfig.themeChanged.connect(self._on_theme_changed)

    def set_language(self, language: str) -> None:
        self.highlighter.set_language(language)

    def _on_theme_changed(self) -> None:
        self.highlighter.update_theme()
        self._line_number_area.update()

    def line_number_area_width(self) -> int:
        digits = max(1, len(str(max(1, self.blockCount()))))
        space = 16 + self.fontMetrics().horizontalAdvance("9") * digits
        return space

    def _update_line_number_area_width(self, _: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self._line_number_area.scroll(0, dy)
        else:
            self._line_number_area.update(
                0, rect.y(), self._line_number_area.width(), rect.height()
            )
        if rect.contains(self.viewport().rect()):
            self._update_line_number_area_width(0)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height())
        )

    def line_number_area_paint_event(self, event: QPaintEvent) -> None:
        painter = QPainter(self._line_number_area)
        dark = isDarkTheme()
        bg_color = QColor(32, 32, 32) if dark else QColor(245, 245, 245)
        text_color = QColor(120, 120, 120) if dark else QColor(160, 160, 160)
        border_color = QColor(48, 48, 48) if dark else QColor(228, 228, 228)

        painter.fillRect(event.rect(), bg_color)
        painter.setPen(border_color)
        painter.drawLine(
            self._line_number_area.width() - 1,
            event.rect().top(),
            self._line_number_area.width() - 1,
            event.rect().bottom(),
        )

        painter.setPen(text_color)
        painter.setFont(self.font())

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                painter.drawText(
                    0,
                    int(top),
                    self._line_number_area.width() - 8,
                    self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    number,
                )
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            block_number += 1


class FluentCodeViewer(QWidget):
    """Complete product-quality code viewer widget with header bar and copy button."""

    def __init__(
        self,
        language: str = "yaml",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._language = language.upper()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Top Header Bar ---
        self.header = QWidget(self)
        self.header.setObjectName("codeViewerHeader")
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(10, 5, 10, 5)
        header_layout.setSpacing(8)

        # Language Badge
        self.badge = InfoBadge(self._language, self.header)
        header_layout.addWidget(self.badge)

        # Stats label (lines / size)
        self.stats_label = CaptionLabel("0 lines", self.header)
        self.stats_label.setObjectName("muted")
        header_layout.addWidget(self.stats_label)

        header_layout.addStretch(1)

        # Copy Button
        self.copy_button = TransparentToolButton(FluentIcon.COPY, self.header)
        self.copy_button.setToolTip("Copy code to clipboard")
        self.copy_button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.copy_button.clicked.connect(self.copy_to_clipboard)
        header_layout.addWidget(self.copy_button)

        layout.addWidget(self.header)

        # --- Code Editor Area ---
        self.editor = CodeEditorArea(language=language, parent=self)
        self.editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.editor.textChanged.connect(self._update_stats)
        layout.addWidget(self.editor, 1)

        self._apply_header_style()
        qconfig.themeChanged.connect(self._apply_header_style)

    def _apply_header_style(self) -> None:
        dark = isDarkTheme()
        bg = "rgba(40, 40, 40, 0.95)" if dark else "rgba(242, 242, 242, 0.95)"
        border = "rgba(255, 255, 255, 0.08)" if dark else "rgba(0, 0, 0, 0.08)"
        self.header.setStyleSheet(
            f"#codeViewerHeader {{ background-color: {bg}; border-bottom: 1px solid {border}; border-top-left-radius: 6px; border-top-right-radius: 6px; }}"
        )

    def set_language(self, language: str) -> None:
        self._language = language.upper()
        self.badge.setText(self._language)
        self.editor.set_language(language)

    def setPlainText(self, text: str) -> None:
        self.editor.setPlainText(text)
        self._update_stats()

    def toPlainText(self) -> str:
        return self.editor.toPlainText()

    def clear(self) -> None:
        self.editor.clear()
        self._update_stats()

    def setReadOnly(self, read_only: bool) -> None:
        self.editor.setReadOnly(read_only)

    def _update_stats(self) -> None:
        text = self.editor.toPlainText()
        lines = len(text.splitlines()) if text else 0
        chars = len(text.encode("utf-8"))
        if chars >= 1024 * 1024:
            size_str = f"{chars / (1024 * 1024):.1f} MB"
        elif chars >= 1024:
            size_str = f"{chars / 1024:.1f} KB"
        else:
            size_str = f"{chars} B"
        self.stats_label.setText(f"{lines} lines | {size_str}")

    def copy_to_clipboard(self) -> None:
        text = self.editor.toPlainText()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self.copy_button.setIcon(FluentIcon.ACCEPT)
        self.copy_button.setToolTip("Copied!")
        QTimer.singleShot(1500, self._reset_copy_button)

    def _reset_copy_button(self) -> None:
        self.copy_button.setIcon(FluentIcon.COPY)
        self.copy_button.setToolTip("Copy code to clipboard")

    def __getattr__(self, name: str):
        editor = self.__dict__.get("editor")
        if editor is not None and hasattr(editor, name):
            return getattr(editor, name)
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
