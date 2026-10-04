"""Restore a workspace without clicks, hover, or navigation to repair its layout."""

import os
from pathlib import Path

import pytest
from PySide6.QtCore import QAbstractAnimation, QEvent, QObject, QPoint, QSettings
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from qfluentwidgets import FluentWindow

from app.ui.design_system import apply_application_theme
from app.ui.shell import MainWindow
from tests.shell_test_isolation import (
    isolated_shell_persistence as isolated_shell_persistence,  # noqa: PLC0414
)
from tests.shell_test_isolation import (
    shell_qt_application as shell_qt_application,  # noqa: PLC0414
)


class FirstPaint(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.bounds = None
        window.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Type.Paint and self.bounds is None:
            window = self.window
            self.bounds = (
                window.navigationInterface.geometry().right(),
                window.fluent_content.geometry().left(),
                window.safety_strip.estop.mapTo(window, QPoint()).x()
                + window.safety_strip.estop.width(),
                window.width(),
            )
        return False


@pytest.mark.parametrize("size, theme", [
    ((1360, 880), "light"), ((1360, 880), "dark"),
    ((900, 880), "light"), ((820, 560), "dark"),
])
def test_restored_spectrum_has_valid_first_paint_without_input(size, theme, shell_qt_application):
    QSettings("LabControl", "LabControl").setValue("main_window/current_route", "anritsu")
    window = MainWindow(".config/settings.yml", simulation=True)
    window.resize(*size)
    window._set_theme_mode(theme, persist=False)
    probe = FirstPaint(window)
    window.show()
    QTest.qWait(400)
    assert probe.bounds is not None
    nav_right, content_left, estop_right, window_right = probe.bounds
    assert nav_right < content_left
    assert estop_right <= window_right
    host = window.navigation_routes["anritsu"]
    assert host.isVisible()
    assert host.geometry() == window.stackedWidget.view.contentsRect()
    assert host.content.width() == host.scroll_area.viewport().width()
    assert host.content.height() > 100
    if size[0] < 1008:
        assert window.navigationInterface.panel.isCollapsed()
        assert not window.apparatus_navigation_item.isExpanded
        assert not window.navigationInterface.widget("anritsuPageHost").isVisibleTo(window)
    else:
        assert window.navigationInterface.width() == 248
    assert window.stackedWidget.isAnimationEnabled()
    screenshot_dir = os.environ.get("PYLAB_STARTUP_SCREENSHOTS")
    if screenshot_dir:
        target = Path(screenshot_dir)
        target.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(target / f"startup-{size[0]}-{theme}.png"))


def test_restoring_route_does_not_start_transition_before_show(shell_qt_application):
    QSettings("LabControl", "LabControl").setValue("main_window/current_route", "anritsu")
    window = MainWindow(".config/settings.yml", simulation=True)
    assert window._current_route() == "anritsu"
    assert all(
        info.ani.state() == QAbstractAnimation.State.Stopped
        for info in window.stackedWidget.view.aniInfos
    )


def test_first_show_cancels_an_obsolete_navigation_transition(shell_qt_application):
    window = MainWindow(".config/settings.yml", simulation=True)
    panel = window.navigationInterface.panel
    # Reproduce a resize/collapse queued while restoring the native window.
    window.navigationInterface.expand(useAni=False)
    panel.collapse()
    window.resize(1360, 880)
    window.show()
    assert panel.expandAni.state() == QAbstractAnimation.State.Stopped
    QTest.qWait(400)
    assert not panel.isCollapsed()
    assert window.navigationInterface.width() == panel.width() == 248
    assert window.apparatus_navigation_item.width() <= panel.width()
    assert window.fluent_content.geometry().left() == 248


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_hidden_window_background_matches_theme_at_first_show(theme, shell_qt_application):
    application = shell_qt_application
    apply_application_theme(application, "dark" if theme == "light" else "light")
    window = FluentWindow()
    window.setMicaEffectEnabled(False)
    try:
        apply_application_theme(application, theme)
        window.resize(1008, 600)
        window.show()
        # Assert before advancing animation time or sending mouse events.
        assert window.backgroundColor == window._normalBackgroundColor()
        assert window.backgroundColorAni.state() == QAbstractAnimation.State.Stopped
        expected = QColor("black" if theme == "light" else "white")
        for button in (window.titleBar.minBtn, window.titleBar.maxBtn, window.titleBar.closeBtn):
            assert button.normalColor == expected
    finally:
        window.close()
        window.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
