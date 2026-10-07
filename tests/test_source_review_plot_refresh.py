"""Burst updates render the latest plot without per-event repaint requests."""
import pytest
from PySide6.QtCore import QEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from app.ui.widgets.plot_ownership import coalesce_plot_refresh, create_plot_widget


@pytest.fixture(scope="session")
def refresh_application():
    return QApplication.instance() or QApplication([])


def test_burst_keeps_latest_trace_and_schedules_one_refresh(refresh_application):
    plot = create_plot_widget()
    plot.resize(1000, 720)
    coalesce_plot_refresh(plot)
    scheduler = plot._station_refresh_scheduler
    coalesce_plot_refresh(plot)
    assert plot._station_refresh_scheduler is scheduler
    try:
        plot.show()
        plot.setXRange(0, 99)
        plot.setYRange(0, 100)
        curve = plot.plot(list(range(100)), [0] * 100)
        QTest.qWait(150)
        before = plot.grab().toImage()
        timer = scheduler.timer
        spy = QSignalSpy(timer.timeout)
        for value in range(100):
            curve.setData(list(range(100)), [value] * 100)
            scheduler.request()
        assert timer.isActive()
        assert spy.count() == 0
        QTest.qWait(80)
        assert 1 <= spy.count() <= 2
        assert list(curve.getData()[1]) == [99] * 100
        assert plot.grab().toImage() != before
        assert plot.isVisible() and plot.width() == 1000
    finally:
        plot.close()
        plot.deleteLater()
        refresh_application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        refresh_application.processEvents()
