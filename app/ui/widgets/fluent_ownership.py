"""Keep Fluent helper objects and routing inside their owning widget lifetime."""

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import QCoreApplication
from qfluentwidgets import PopUpAniStackedWidget, SmoothScrollBar, qrouter
from shiboken6 import isValid
import weakref


def bind_fluent_router_lifetime(stack: PopUpAniStackedWidget) -> None:
    """Remove only this stack's global routing records at native destruction."""
    if stack.property("stationRouterLifetimeBound"):
        return
    reference = weakref.ref(stack)

    def forget_stack(_destroyed=None):
        owned = reference()
        if owned is None:
            return
        qrouter.history[:] = [item for item in qrouter.history if item.stacked is not owned]
        qrouter.stackHistories.pop(owned, None)
        # QApplication shutdown can destroy the global router before the
        # remaining stacks. Its Python history still needs clearing, but its
        # C++ signal source (and possibly its receivers) no longer exists.
        if isValid(qrouter) and not QCoreApplication.closingDown():
            qrouter.emptyChanged.emit(not bool(qrouter.history))

    stack.destroyed.connect(forget_stack)
    stack.setProperty("stationRouterLifetimeBound", True)


def own_fluent_helpers(root: QWidget) -> None:
    stacks = root.findChildren(PopUpAniStackedWidget)
    if isinstance(root, PopUpAniStackedWidget):
        stacks.append(root)
    for stack in stacks:
        bind_fluent_router_lifetime(stack)
        for info in stack.aniInfos:
            animation = info.ani
            if animation.parent() is None and animation.targetObject() is info.widget:
                animation.setParent(info.widget)
    scrollbars = root.findChildren(SmoothScrollBar)
    if isinstance(root, SmoothScrollBar):
        scrollbars.append(root)
    for scrollbar in scrollbars:
        animation = scrollbar.ani
        if animation.parent() is None and animation.targetObject() is scrollbar:
            animation.setParent(scrollbar)
