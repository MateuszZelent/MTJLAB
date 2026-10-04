"""Rendered spectrum workspace and progressive disclosure in the real Fluent shell."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer
from PySide6.QtTest import QSignalSpy, QTest

from app.ui.shell import MainWindow
from tests.helpers import SETTINGS_TEMPLATE
from tests.shell_test_isolation import (
    isolated_shell_persistence as isolated_shell_persistence,  # noqa: PLC0414
)
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_main_window import synthetic_anritsu_peaks
from tests.test_spectrum_correction_controller import wait_until


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
    artifact_dir = Path("artifacts/spectrum-layout")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(artifact_dir / f"shell-{theme}-{size[0]}.png"))
    assert page.spectrum_plot.height() > page.height() * (.5 if size[0] >= 1500 else .32)
    assert window.rect().contains(page.spectrum_plot.mapTo(window, page.spectrum_plot.rect().bottomRight()))
    assert page.signal_analysis_card.height() <= (80 if size[0] > 1000 else 120)
    for widget in (page.live, page.single, page.correction_controls.configure_background, page.correction_controls.configure_reference,
                   page.abort_button, page.configure_analysis,
                   page.toggle_acquisition_controls, *page.cleanup_filters.values(),
                   page.toggle_analysis_details, page.auto_peak_detection, page.peak_settings, page.plot_scales, *page.spectrum_plot.toolbar_buttons,
                   page.spectrum_plot.readout):
        assert widget.isVisibleTo(window)
        origin = widget.mapTo(page, QPoint(0, 0))
        assert page.rect().contains(origin)
        assert page.rect().contains(origin + QPoint(widget.width() - 1, widget.height() - 1))
    rectangles = [widget.rect().translated(widget.mapTo(page, QPoint())) for widget in page.cleanup_filters.values()]
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
        assert page.processing_card.isVisibleTo(window)
        assert not page.recording_dialog.isVisible()
        assert not page.reference_dialog.isVisible()
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
            assert page.spectrum_plot.height() >= 170
            if height == 560:
                page.compact_plot_settings.click()
                QTest.qWait(50)
                for card in (page.correction_controls, page.signal_analysis_card, page.presentation_controls):
                    assert card.isVisibleTo(page._presentation_popup)
                    assert page._presentation_popup_view.rect().contains(
                        card.rect().translated(card.mapTo(page._presentation_popup_view, QPoint())))
                for control in (page.correction_controls.configure_reference, page.toggle_analysis_details):
                    assert page._presentation_popup_view.rect().contains(
                        control.rect().translated(control.mapTo(page._presentation_popup_view, QPoint())))
                    assert control.parentWidget().rect().contains(control.geometry())
                assert page._presentation_popup.grab().save(str(artifact_dir / "compact-settings.png"))
                page._presentation_popup.hide()
            assert window.grab().save(str(artifact_dir / f"resized-{width}-{height}.png"))

    assert window.close()



def test_hidden_anritsu_route_buffers_frames_without_background_gui_work(shell_qt_application):
    window = MainWindow(SETTINGS_TEMPLATE, simulation=True)
    window.resize(1360, 880)
    window.show()
    window._navigate_to("anritsu")
    page = window.anritsu_page
    page.auto_peak_detection.setChecked(False)
    page.cleanup_filters["denoise"].setChecked(True)
    page._show_trace(synthetic_anritsu_peaks(), update_controls=False)
    wait_until(shell_qt_application, lambda: page._cleanup_result is not None)
    window._navigate_to("overview")
    QTest.qWait(60)
    assert not page.isVisibleTo(window)
    before = page._received_trace_count
    latest = synthetic_anritsu_peaks(primary_hz=1.1e9)
    spectrum_jobs = QSignalSpy(page._analysis_controller._request)
    spectrogram_jobs = QSignalSpy(page._spectrogram_analysis_controller._request)
    with patch.object(page.spectrum_plot, "set_trace") as repaint:
        page._show_trace(latest, update_controls=False)
        assert page._received_trace_count == before + 1
        assert spectrum_jobs.count() == 0
        assert spectrogram_jobs.count() == 0
        repaint.assert_not_called()
    window._navigate_to("anritsu")
    # Run the same native Qt event loop as the application so both queued
    # worker signals and the Fluent navigation transition can finish.
    loop = QEventLoop()
    ready = QTimer()
    ready.setInterval(10)
    ready.timeout.connect(lambda: loop.quit() if page._analysis_raw_snapshot is latest
                          and "Analysis" in page.spectrum_plot._traces else None)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    ready.start()
    timeout.start(8_000)
    loop.exec()
    ready.stop()
    timeout.stop()
    assert page._analysis_raw_snapshot is latest, page.analysis_status.text()
    assert page.isVisibleTo(window)
    assert "Analysis" in page.spectrum_plot._traces
    assert window.close()
