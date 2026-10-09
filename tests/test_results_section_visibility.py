"""Results fallback navigation must work with the installed Fluent ComboBox."""
from pathlib import Path
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from app.ui.results.page import ResultsPage, _FluentResultSections


@pytest.fixture
def application():
    app = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Segoe UI", 10))
    return app


@pytest.mark.parametrize("width", [1280, 320])
def test_hiding_selected_section_uses_available_section_in_both_layouts(application, width):
    sections = _FluentResultSections()
    try:
        for label in ("Overview", "Sweep tree", "Spectrum", "Heatmaps"):
            sections.addTab(QWidget(), label)
        sections.resize(width, 600)
        sections.show()
        application.processEvents()
        assert sections.compact_navigation.isVisible() == (width == 320)
        sections.setTabVisible(0, False)
        sections.setCurrentIndex(3)
        sections.setTabVisible(3, False)
        assert sections.stack.currentIndex() == 1
        assert sections.compact_navigation.currentIndex() == 1
        assert sections.compact_navigation.items[3].isEnabled is False
        sections.setCurrentIndex(3)
        sections.setCurrentIndex(-1)
        assert sections.stack.currentIndex() == 1
        sections.setTabVisible(3, True)
        sections.setCurrentIndex(3)
        assert sections.stack.currentIndex() == 3
        assert sections.stack.currentWidget().isVisible()
        assert sections.stack.height() > 400
    finally:
        sections.close()
        sections.deleteLater()
        application.processEvents()


def test_file_selection_clears_active_heatmap_without_crashing(application, tmp_path):
    page = ResultsPage(str(tmp_path))
    try:
        page.resize(1280, 800)
        page.show()
        application.processEvents()
        page._set_heatmap_visible(True)
        page.result_tabs.setCurrentIndex(page._heatmap_index)
        page._on_file_selected(None)
        QTest.qWait(300)
        assert page.result_stack.currentIndex() == 0
        assert page.result_stack.currentWidget().isVisible()
        assert page.result_stack.height() > 300
        destination = Path("docs/audits/2026-10-07-results-navigation")
        destination.mkdir(parents=True, exist_ok=True)
        assert page.grab().save(str(destination / "after-heatmap-hidden.png"))
    finally:
        page.close()
        page.deleteLater()
        application.processEvents()
