"""Emergency caption fits its rendered Fluent control at every supported width."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtTest import QTest

from app.ui.design_system import apply_application_theme
from app.ui.shell import MainWindow, StationSafetySnapshot, StationSafetyStrip
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [240, 500, 700, 1000, 1500])
def test_emergency_caption_and_actions_fit_without_test_only_shortening(shell_qt_application, theme, width):
    application = shell_qt_application
    apply_application_theme(application, theme)
    strip = StationSafetyStrip()
    emissions, saves = [], []
    strip.estop_requested.connect(lambda: emissions.append(True))
    strip.save_settings_requested.connect(lambda: saves.append(True))
    strip.update_snapshot(StationSafetySnapshot(False, 2, True,
        "LAB/operator-with-a-deliberately-long-identity", ("engineer", "operator"), 1))
    try:
        strip.resize(width, 140 if width < 520 else 110 if width < 900 else 48)
        strip.show()
        application.processEvents()
        assert strip.width() == width
        for active, unknown in ((0, 1), (2, 0), (2, 1)):
            strip.update_snapshot(StationSafetySnapshot(False, active, True,
                "LAB/operator-with-a-deliberately-long-identity", ("engineer", "operator"), unknown))
            application.processEvents()
            for label in (strip.readiness, strip.outputs, strip.mode):
                assert label.width() >= label.fontMetrics().horizontalAdvance(label.text()) + 4
                assert strip.rect().contains(label.geometry())
        assert strip.estop.isVisible() and strip.estop.isEnabled()
        assert "disable all outputs and abort acquisition" in strip.estop.accessibleName()
        assert strip.estop.text() == ("E-STOP | ALL OFF" if width < 520 else "E-STOP  |  ALL OUTPUTS OFF")
        bold = QFont(strip.estop.font())
        bold.setWeight(QFont.Weight.Bold)
        assert strip.estop.width() >= QFontMetrics(bold).horizontalAdvance(strip.estop.text()) + 32
        assert not strip.estop.geometry().intersects(strip.save_settings.geometry())
        for button in (strip.estop, strip.save_settings):
            assert button.width() > 0 and button.height() >= 28
            assert strip.rect().contains(button.geometry())
        QTest.mouseClick(strip.estop, Qt.MouseButton.LeftButton)
        QTest.mouseClick(strip.save_settings, Qt.MouseButton.LeftButton)
        assert emissions == [True] and saves == [True]
        directory = Path("artifacts/safety-strip-layout")
        directory.mkdir(parents=True, exist_ok=True)
        assert strip.grab().save(str(directory / f"{theme}-{width}.png"))
    finally:
        strip.close()
        strip.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()


def test_full_shell_emergency_caption_fits_normal_and_minimum_windows(shell_qt_application):
    application = shell_qt_application
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.show()
        directory = Path("artifacts/safety-strip-layout")
        directory.mkdir(parents=True, exist_ok=True)
        for theme in ("light", "dark"):
            window.theme_actions[theme].trigger()
            for width, height in ((1500, 950), (820, 650)):
                window.resize(width, height)
                application.processEvents()
                QTest.qWait(350)  # Let the native Fluent navigation transition settle.
                strip = window.safety_strip
                assert window.width() == width
                assert strip.estop.isVisibleTo(window) and strip.estop.isEnabled()
                bold = QFont(strip.estop.font())
                bold.setWeight(QFont.Weight.Bold)
                assert strip.estop.width() >= QFontMetrics(bold).horizontalAdvance(strip.estop.text()) + 32
                assert strip.rect().contains(strip.estop.geometry())
                assert not strip.estop.geometry().intersects(strip.save_settings.geometry())
                assert strip.mapTo(window, QPoint(0, 0)).x() >= window.navigationInterface.width()
                for label in (strip.readiness, strip.outputs, strip.mode):
                    assert label.width() >= label.fontMetrics().horizontalAdvance(label.text())
                    assert label.toolTip() == label.text()
                assert window.grab().save(str(directory / f"shell-{theme}-{width}.png"))
    finally:
        assert window.close()
