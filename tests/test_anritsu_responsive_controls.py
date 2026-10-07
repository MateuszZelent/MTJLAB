"""Visible controls must fit their Fluent cards, including the empty state."""
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox, QComboBox

from app.ui.shell import MainWindow
from tests.helpers import SETTINGS_TEMPLATE
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence  # noqa: PLC0414
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_main_window import synthetic_anritsu_peaks


@pytest.mark.parametrize("font_pixels", [13, 18])
def test_anritsu_controls_never_clip_when_viewport_changes(shell_qt_application, font_pixels):
    window = MainWindow(SETTINGS_TEMPLATE, simulation=True)
    page = window.anritsu_page
    font = QFont(page.font())
    font.setPixelSize(font_pixels)
    page.setFont(font)
    try:
        window.show()
        window._navigate_to("anritsu")
        for size in ((1900, 1030), (1366, 768), (1280, 720), (1024, 768), (820, 560), (1500, 900)):
            window.resize(*size)
            QTest.qWait(100)
            shell_qt_application.processEvents()
            if page.compact_plot_settings.isVisibleTo(window):
                page._open_compact_plot_settings()
                QTest.qWait(60)
            for card in (page.correction_controls, page.signal_analysis_card, page.presentation_controls):
                assert card.isVisible()
                controls = set()
                for kind in (QAbstractButton, QAbstractSpinBox, QComboBox):
                    controls.update(card.findChildren(kind))
                for control in controls:
                    if not control.isVisibleTo(card):
                        continue
                    bounds = control.rect().translated(control.mapTo(card, QPoint()))
                    assert card.rect().contains(bounds), (size, font_pixels, card.objectName(), control.objectName(), card.size(), bounds)
            if page._presentation_popup.isVisible():
                page._presentation_popup.hide()
            if size in ((1900, 1030), (1280, 720)):
                folder = Path("artifacts/anritsu-responsive")
                folder.mkdir(parents=True, exist_ok=True)
                assert window.grab().save(str(folder / f"empty-{size[0]}-{font_pixels}.png"))
            assert page.analysis_tabs.height() >= 150
            assert page.height() == window.navigation_routes["anritsu"].scroll_area.viewport().height()
            assert window.navigation_routes["anritsu"].scroll_area.horizontalScrollBar().maximum() == 0
        page._show_trace(synthetic_anritsu_peaks(), update_controls=False)
        QTest.qWait(80)
        folder = Path("artifacts/anritsu-responsive")
        folder.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(folder / f"controls-{font_pixels}.png"))
    finally:
        window.close()
        shell_qt_application.processEvents()
