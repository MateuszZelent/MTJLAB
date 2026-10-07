"""Repeated presentation must not repolish unchanged station styles."""
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from qfluentwidgets import CardWidget, PushButton

from app.ui.design_system import tokens_for
from app.ui.design_system.fluent_theme import _apply_station_control_style


@pytest.fixture(scope="session")
def theme_application():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("kind", [CardWidget, PushButton])
def test_repeated_style_is_idle_but_theme_and_external_changes_apply(kind, monkeypatch, theme_application):
    app = theme_application
    root = QWidget()
    root.resize(1000, 720)
    layout = QVBoxLayout(root)
    widget = kind(root)
    layout.addWidget(widget)
    try:
        root.show()
        app.processEvents()
        light = tokens_for("light")
        _apply_station_control_style(widget, light)
        spy = Mock(wraps=widget.setStyleSheet)
        monkeypatch.setattr(widget, "setStyleSheet", spy)
        for _ in range(10):
            _apply_station_control_style(widget, light)
        spy.assert_not_called()
        # Fluent can replace QSS independently; station rules must be restored.
        widget.setStyleSheet("")
        spy.reset_mock()
        _apply_station_control_style(widget, light)
        assert spy.call_count == 1
        assert "station-" in widget.styleSheet()
        _apply_station_control_style(widget, tokens_for("dark"))
        app.processEvents()
        assert widget.isVisibleTo(root)
        assert widget.geometry().width() > 900
        assert not root.grab().isNull()
    finally:
        monkeypatch.undo()
        root.close()
        root.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
