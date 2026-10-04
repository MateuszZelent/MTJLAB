"""Keep Fluent helper objects and routing inside their owning widget lifetime."""

from PySide6.QtWidgets import QWidget
from qfluentwidgets import PopUpAniStackedWidget, SmoothScrollBar, qrouter
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
