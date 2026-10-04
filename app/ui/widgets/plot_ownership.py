"""Keep native plot menus inside their owning QWidget's lifetime."""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtWidgets import QWidget


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
