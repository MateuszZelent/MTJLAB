"""Plot context menus work as popups and die with their own plot only."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu
from shiboken6 import isValid

from app.ui.widgets.plot_ownership import create_plot_widget, own_plot_item_menus, own_signal_proxy, own_viewbox_menu


def test_plot_menus_preserve_popup_actions_and_delete_with_the_plot():
    application = QApplication.instance() or QApplication([])
    foreign = QMenu("Other window")
    plot = create_plot_widget()
    primary = plot.getViewBox()
    secondary = pg.ViewBox()
    own_viewbox_menu(secondary, plot)
    menus = (plot.getPlotItem().ctrlMenu, primary.menu, secondary.menu)
    plot.resize(900, 600)
    plot.show()
    curve = plot.plot([1e6, 2e6, 3e6], [1e-12, -1e-12, 2e-12])
    application.processEvents()
    try:
        for menu in menus:
            assert menu.parentWidget() is plot
            assert menu.windowFlags() & Qt.WindowType.Popup
            assert menu.actions()
            menu.popup(plot.mapToGlobal(QPoint(100, 100)))
            application.processEvents()
            assert menu.isVisible()
            menu.hide()
        np.testing.assert_array_equal(curve.yData, [1e-12, -1e-12, 2e-12])
        plot.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
        assert not isValid(plot)
        assert all(not isValid(menu) for menu in menus)
        assert isValid(foreign)
    finally:
        if isValid(plot):
            plot.deleteLater()
        secondary.deleteLater()
        foreign.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()


def test_colorbar_plot_item_menus_delete_with_the_host_plot():
    application = QApplication.instance() or QApplication([])
    plot = create_plot_widget()
    image = pg.ImageItem(np.arange(9, dtype=float).reshape(3, 3))
    plot.addItem(image)
    bar = pg.ColorBarItem(values=(0, 8), colorMap=pg.colormap.get("viridis"))
    bar.setImageItem(image, insert_in=plot.getPlotItem())
    own_plot_item_menus(bar, plot)
    menus = (bar.ctrlMenu, bar.getViewBox().menu)
    try:
        plot.resize(900, 600)
        plot.show()
        application.processEvents()
        assert plot.isVisible() and plot.width() == 900
        assert all(menu is None or menu.parentWidget() is plot for menu in menus)
        assert bar.isVisible()
    finally:
        plot.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
    assert all(menu is None or not isValid(menu) for menu in menus)


def test_plot_signal_proxy_delivers_mouse_events_and_deletes_all_timer_levels():
    application = QApplication.instance() or QApplication([])
    plot = create_plot_widget()
    received = []
    proxy = pg.SignalProxy(plot.scene().sigMouseMoved, rateLimit=30, slot=received.append)
    own_signal_proxy(proxy, plot)
    helpers = (proxy, proxy.timer, proxy.timer.timer, plot.getPlotItem().stateGroup,
               *plot.getViewBox().menu.widgetGroups)
    try:
        plot.resize(900, 600)
        plot.show()
        application.processEvents()
        plot.scene().sigMouseMoved.emit(QPointF(1, 2))
        QTest.qWait(50)
        assert received and received[-1][0] == QPointF(1, 2)
        assert plot.isVisible() and plot.width() == 900
    finally:
        plot.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
    assert all(not isValid(helper) for helper in helpers)
