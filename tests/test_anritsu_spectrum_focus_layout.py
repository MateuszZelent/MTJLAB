"""Rendered spectrum workspace and progressive disclosure in the real Fluent shell."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from app.ui.shell import MainWindow
from tests.helpers import SETTINGS_TEMPLATE
from tests.shell_test_isolation import (
    isolated_shell_persistence as isolated_shell_persistence,  # noqa: PLC0414
)
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_main_window import synthetic_anritsu_peaks


@pytest.mark.parametrize("theme,size", [
    ("light", (1600, 1000)), ("dark", (1100, 800)), ("dark", (820, 700)),
])
def test_spectrum_is_primary_and_controls_remain_reachable(shell_qt_application, theme, size):
    window = MainWindow(SETTINGS_TEMPLATE, simulation=True)
    window.resize(*size)
    window._set_theme_mode(theme, persist=False)
    window.show()
    window._navigate_to("anritsu")
    page = window.anritsu_page
    page._show_trace(synthetic_anritsu_peaks(), update_controls=False)
    QTest.qWait(80)
    shell_qt_application.processEvents()
    assert not page.control_scroll.isVisible()
    assert not page.analysis_details.isVisible()
    assert not page.mode_tabs.navigation.isVisible()
    assert page.spectrum_plot.width() > page.width() * .9
    assert window.width() == size[0]
    host = window.navigation_routes["anritsu"]
    assert page.height() == host.scroll_area.viewport().height()
    assert page.spectrum_plot.height() > page.height() * (.5 if size[0] >= 1500 else .35)
    assert window.rect().contains(page.spectrum_plot.mapTo(window, page.spectrum_plot.rect().bottomRight()))
    assert page.signal_analysis_card.height() <= (80 if size[0] > 1000 else 120)
    for widget in (page.live, page.single, page.acquire_single_reference, page.auto_background_button,
                   page.abort_button, page.configure_analysis,
                   page.toggle_acquisition_controls, *page.cleanup_filters.values(),
                   page.toggle_analysis_details, *page.spectrum_plot.toolbar_buttons,
                   page.spectrum_plot.readout):
        assert widget.isVisibleTo(window)
        origin = widget.mapTo(page, QPoint(0, 0))
        assert page.rect().contains(origin)
        assert page.rect().contains(origin + QPoint(widget.width() - 1, widget.height() - 1))
    rectangles = [widget.geometry() for widget in page.cleanup_filters.values()]
    for index, rect in enumerate(rectangles):
        assert all(not rect.intersects(other) for other in rectangles[index + 1:])
    artifact_dir = Path("artifacts/spectrum-layout")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(artifact_dir / f"shell-{theme}-{size[0]}.png"))

    plot_height = page.spectrum_plot.height()
    QTest.mouseClick(page.toggle_analysis_details, Qt.MouseButton.LeftButton)
    QTest.qWait(50)
    shell_qt_application.processEvents()
    assert page.analysis_source.isVisibleTo(window)
    assert page.overlay_analysis_source.isVisibleTo(window)
    assert page.analysis_status.isVisibleTo(window)
    assert page.spectrum_plot.height() == plot_height
    page.analysis_details_flyout.close()
    QTest.qWait(50)
    shell_qt_application.processEvents()
    assert not page.analysis_details.isVisible()
    assert page.spectrum_plot.height() == plot_height

    with patch.object(page._controller, "call") as dispatch:
        QTest.mouseClick(page.toggle_acquisition_controls, Qt.MouseButton.LeftButton)
        QTest.qWait(50)
        shell_qt_application.processEvents()
        assert page.configuration_panel.isVisibleTo(window)
        assert page.manual_save_card.isVisibleTo(window)
        assert page.live.isVisibleTo(window) and page.abort_button.isVisibleTo(window)
        assert not dispatch.called
        assert window.grab().save(str(artifact_dir / f"settings-{theme}-{size[0]}.png"))
        QTest.mouseClick(page.toggle_acquisition_controls, Qt.MouseButton.LeftButton)
        QTest.qWait(50)
        shell_qt_application.processEvents()
        assert page.spectrum_plot.width() > page.width() * .9
        # Existing emergency-off dispatch remains on the always-visible command strip.
        page.abort_button.setEnabled(True)
        QTest.mouseClick(page.abort_button, Qt.MouseButton.LeftButton)
        dispatch.assert_called_once_with("emergency_off")

    if size[0] == 1600:
        for width, height in ((820, 560), (1360, 880), (1600, 1000)):
            window.resize(width, height)
            QTest.qWait(80)
            shell_qt_application.processEvents()
            assert page.height() == host.scroll_area.viewport().height()
            assert window.rect().contains(page.spectrum_plot.mapTo(window, page.spectrum_plot.rect().bottomRight()))
            assert page.spectrum_plot.height() >= 100
            assert window.grab().save(str(artifact_dir / f"resized-{width}-{height}.png"))

    assert window.close()
