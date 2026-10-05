"""Rendered boundaries and keyboard dismissal of shared station dialogs."""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import SpectrumAnalysisSettingsDialog
from app.ui.design_system import apply_application_theme, tokens_for
from app.ui.dialogs import StationAlertDialog


@pytest.fixture(scope="module")
def application():
    application = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        application.setFont(QFont("Segoe UI", 10))
    return application


def luminance(color):
    values = [color.redF(), color.greenF(), color.blueF()]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
    return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("kind", ["alert", "settings"])
def test_modal_outline_is_visible_and_content_fits(application, theme, kind):
    apply_application_theme(application, theme)
    tokens = tokens_for(theme)
    parent = QWidget()
    parent.setProperty("stationSurface", "page")
    parent.resize(1360, 880)
    parent.show()
    if kind == "alert":
        dialog = StationAlertDialog(
            parent, "Save station settings", "Review the configuration before saving changes.",
            QMessageBox.StandardButton.Save, QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        dialog.resize(640, 260)
    else:
        dialog = SpectrumAnalysisSettingsDialog(parent)
        dialog.resize(820, 600)
    try:
        dialog.move(parent.mapToGlobal(QPoint(180, 120)))
        dialog.show()
        application.processEvents()
        capture = dialog.grab().toImage()
        ratio = capture.devicePixelRatio()
        boundary = capture.pixelColor(0, round(dialog.height() * ratio / 2))
        assert boundary == QColor(tokens.dialog_border)
        background = QColor(tokens.background)
        a, b = sorted((luminance(boundary), luminance(background)))
        assert (b + .05) / (a + .05) >= 3
        assert dialog.palette().window().color() == QColor(tokens.dialog_chrome)
        surface = dialog.modal_shell.surface
        assert surface.palette().window().color() == QColor(tokens.dialog_surface)
        assert surface.isVisibleTo(dialog)
        assert surface.width() > 250
        assert surface.height() > 150
        assert dialog.rect().contains(dialog.titleBar.geometry())
        assert dialog.modal_shell.y() > dialog.titleBar.geometry().bottom()
        if kind == "alert":
            assert dialog.focusWidget() is dialog.secondary_button
            for button in (dialog.primary_button, dialog.secondary_button):
                assert surface.rect().contains(button.geometry())

        directory = os.environ.get("PYLAB_MODAL_SCREENSHOTS")
        if directory:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            # Composite actual widget grabs to show the boundary against the
            # application surface without capturing unrelated desktop windows.
            preview = parent.grab()
            painter = QPainter(preview)
            painter.drawPixmap(QPoint(180, 120), dialog.grab())
            painter.end()
            assert preview.save(str(target / f"{kind}-{theme}.png"))

        # The existing cancellation path and safe default remain intact.
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        application.processEvents()
        assert not dialog.isVisible()
        if kind == "alert":
            assert dialog.selected_button == QMessageBox.StandardButton.NoButton
    finally:
        dialog.close()
        parent.close()
        parent.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_open_modal_rethemes_its_frame_and_content(application):
    dialog = StationAlertDialog(
        None, "Configuration", "Review station settings.",
        QMessageBox.StandardButton.Ok, None, QMessageBox.StandardButton.Ok,
    )
    dialog.resize(640, 320)
    try:
        for theme in ("light", "dark", "light"):
            apply_application_theme(application, theme)
            dialog.show()
            application.processEvents()
            tokens = tokens_for(theme)
            frame = dialog.grab().toImage()
            assert frame.pixelColor(0, frame.height() // 2) == QColor(tokens.dialog_border)
            assert dialog.modal_shell.surface.palette().window().color() == QColor(tokens.dialog_surface)
    finally:
        dialog.close()
        dialog.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
