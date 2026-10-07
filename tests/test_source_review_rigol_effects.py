"""Full manual carrier side effects remain visible beside the controls."""

from unittest.mock import Mock
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from app.devices.rigol_dg1000z.ui.page import RigolPage
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings


@pytest.fixture(scope="module")
def application():
    app = QApplication.instance() or QApplication([])
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        font = Path("C:/Windows/Fonts") / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont("Segoe UI", 10))
    return app


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_full_configuration_effects_visible_across_control_tabs(application, theme, tmp_path):
    apply_application_theme(application, theme)
    controller = Mock()
    page = RigolPage(controller, simulation_settings())
    try:
        page.resize(1280, 900)
        page.show()
        for index in range(page.control_tabs.count()):
            page.control_tabs.setCurrentIndex(index)
            application.processEvents()
            label = page.configuration_effects
            assert label.isVisibleTo(page)
            assert page.rect().contains(label.rect().translated(label.mapTo(page, QPoint())))
            assert label.height() >= label.heightForWidth(label.width())
            assert page.output_off.isVisibleTo(page)
        for token in ("OUTPUT OFF", "modulation", "hardware sweep", "burst", "harmonics",
                      "waveform summing", "VPP", "USER", "FREQ", "load", "phase", "OUTPUT ON"):
            assert token in page.configuration_effects.text()
        assert page.grab().save(str(tmp_path / f"rigol-effects-{theme}.png"))
        controller.call.assert_not_called()
    finally:
        page.close()
        page.deleteLater()
        application.processEvents()
