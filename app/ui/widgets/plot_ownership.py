"""Keep native plot menus inside their owning QWidget's lifetime."""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QGraphicsView, QWidget


class _PlotRefreshScheduler(QObject):
    """Coalesce scene changes instead of repainting during each scene callback."""

    def __init__(self, plot: pg.PlotWidget, interval_ms: int) -> None:
        super().__init__(plot)
        self.plot = plot
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.refresh)
        plot.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.NoViewportUpdate)
        plot.scene().changed.connect(self.request)

    def request(self, *_args) -> None:
        if not self.timer.isActive():
            self.timer.start()

    def refresh(self) -> None:
        self.plot.viewport().update()


def coalesce_plot_refresh(plot: pg.PlotWidget, interval_ms: int = 33) -> None:
    if not hasattr(plot, "_station_refresh_scheduler"):
        plot._station_refresh_scheduler = _PlotRefreshScheduler(plot, interval_ms)


def own_viewbox_menu(view: pg.ViewBox, owner: QWidget) -> None:
    menu = view.menu
    if menu is not None and menu.parentWidget() is None:
        menu.setParent(owner, menu.windowFlags())
    if menu is not None:
        for group in menu.widgetGroups:
            if group.parent() is None:
                group.setParent(menu)


def own_plot_item_menus(item: pg.PlotItem, owner: QWidget) -> None:
    menu = item.ctrlMenu
    if menu is not None and menu.parentWidget() is None:
        menu.setParent(owner, menu.windowFlags())
    if item.stateGroup.parent() is None:
        item.stateGroup.setParent(owner)
    own_viewbox_menu(item.getViewBox(), owner)


def own_signal_proxy(proxy: pg.SignalProxy, owner: QWidget) -> None:
    """Own the proxy and both levels of pyqtgraph's GUI timer helper."""
    if proxy.parent() is None:
        proxy.setParent(owner)
    timer = proxy.timer
    if timer.parent() is None:
        timer.setParent(proxy)
    native_timer = getattr(timer, "timer", None)
    if native_timer is not None and native_timer.parent() is None:
        native_timer.setParent(timer)


def create_plot_widget(parent: QWidget | None = None, *, factory=None) -> pg.PlotWidget:
    # PlotItem/ViewBox allocate parentless QWidget menus; their signals can
    # retain each other after the graphics item has been destroyed. Native
    # QWidget ownership releases these menus and their embedded controls.
    plot = factory() if factory is not None else pg.PlotWidget(parent)
    if parent is not None and factory is not None:
        plot.setParent(parent)
    own_plot_item_menus(plot.getPlotItem(), plot)
    return plot
