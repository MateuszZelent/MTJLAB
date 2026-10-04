"""Rendered regression coverage for the Anritsu acquisition forms."""

import os
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QFormLayout

from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
from app.settings import SettingsRepository
from app.ui.design_system import apply_application_theme
from tests.helpers import SETTINGS_TEMPLATE


def assert_form_rows(form, host):
    rectangles = []
    for row in range(form.rowCount()):
        item = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
        if item is None:
            continue
        widget = item.widget()
        rect = item.geometry()
        assert rect.height() >= item.minimumSize().height()
        assert rect.height() > 0
        if widget:
            assert widget.height() >= widget.minimumHeight()
            assert host.rect().contains(widget.mapTo(host, QPoint(0, 0)))
        label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        if label:
            assert not label.geometry().intersects(rect)
            assert label.geometry().height() >= label.minimumSize().height()
        for previous in rectangles:
            assert not previous.intersects(rect)
        rectangles.append(rect)


@pytest.mark.parametrize("size", [(1500, 900), (800, 700)])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_anritsu_forms_render_without_overlap(size, theme):
    application = QApplication.instance() or QApplication([])
    apply_application_theme(application, theme)
    controller = MagicMock()
    controller.is_connected = False
    controller.visa_address = "SIM::ANRITSU"
    settings = SettingsRepository(SETTINGS_TEMPLATE).load().settings
    page = AnritsuPage(controller, settings, single_sweep_available=True)
    try:
        page.resize(*size)
        page.show()
        page.toggle_acquisition_controls.click()
        application.processEvents()
        panel = page.configuration_panel
        assert panel.isVisible()
        assert_form_rows(panel.layout().itemAt(0).layout(), panel)
        dialog = page._advanced_dialog
        dialog.resize(720, 780) if size[0] > 900 else dialog.resize(540, 460)
        dialog.show()
        application.processEvents()
        advanced = page.advanced_configuration_panel
        assert_form_rows(advanced.layout(), advanced)
        assert advanced.height() >= advanced.minimumSizeHint().height()
        assert page.advanced_read_button.isVisible()
        assert page.advanced_apply_button.isVisible()
        scroll = dialog.findChild(type(page.control_scroll), "anritsuAdvancedSettingsScroll")
        assert scroll is not None
        if size[0] < 900:
            assert scroll.verticalScrollBar().maximum() > 0
        artifact_dir = Path("artifacts/anritsu-layout")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        assert page.grab().save(str(artifact_dir / f"page-{theme}-{size[0]}.png"))
        assert dialog.grab().save(str(artifact_dir / f"advanced-{theme}-{size[0]}.png"))
    finally:
        page._advanced_dialog.close()
        page.close()
        page.deleteLater()
        application.processEvents()
