"""Owned native animations retain scrolling and page transitions through teardown."""

from PySide6.QtCore import QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import PopUpAniStackedWidget, ScrollArea, qrouter
from shiboken6 import isValid
from shiboken6 import delete

from app.ui.widgets.fluent_ownership import own_fluent_helpers
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


def test_stack_cleanup_when_router_was_destroyed_first(shell_qt_application, monkeypatch):
    import sys
    from app.ui.widgets import fluent_ownership
    router = type(qrouter)()
    monkeypatch.setattr(fluent_ownership, "qrouter", router)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: errors.append(args))
    stack = PopUpAniStackedWidget()
    page = QWidget(stack)
    page.setObjectName("test-page")
    stack.addWidget(page)
    own_fluent_helpers(stack)
    router.push(stack, "test-page")
    delete(router)
    assert not isValid(router)
    delete(stack)
    assert not errors
    assert not router.history
    assert not router.stackHistories


def test_scroll_and_page_animations_work_and_die_with_their_target(shell_qt_application):
    application = shell_qt_application
    owner = QWidget()
    layout = QVBoxLayout(owner)
    popup = PopUpAniStackedWidget(owner)
    for _ in range(2):
        popup.addWidget(QWidget(popup))
    scroll = ScrollArea(owner)
    content = QWidget()
    content.setMinimumHeight(2000)
    scroll.setWidget(content)
    layout.addWidget(popup)
    layout.addWidget(scroll)
    animations = []
    try:
        own_fluent_helpers(owner)
        animations.extend(info.ani for info in popup.aniInfos)
        animations.extend([scroll.scrollDelagate.vScrollBar.ani,
                           scroll.scrollDelagate.hScrollBar.ani])
        owner.resize(900, 650)
        owner.show()
        application.processEvents()
        assert popup.isVisibleTo(owner) and scroll.isVisibleTo(owner)
        assert popup.width() > 300 and scroll.height() > 100
        assert all(animation.parent() is animation.targetObject() for animation in animations)
        popup.setCurrentIndex(1, needPopOut=True, duration=20)
        bar = scroll.scrollDelagate.vScrollBar
        bar.duration = 20
        assert bar.maximum() >= 80
        bar.setValue(80)
        QTest.qWait(100)
        assert popup.currentIndex() == 1
        assert bar.value() == 80
    finally:
        owner.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
    assert all(not isValid(animation) for animation in animations)


def test_router_forgets_destroyed_stack_without_removing_foreign_same_named_routes(shell_qt_application):
    application = shell_qt_application
    owned, foreign = PopUpAniStackedWidget(), PopUpAniStackedWidget()
    try:
        for stack in (owned, foreign):
            page = QWidget(stack)
            page.setObjectName("overview")
            stack.addWidget(page)
            page = QWidget(stack)
            page.setObjectName("anritsu")
            stack.addWidget(page)
            own_fluent_helpers(stack)
            qrouter.setDefaultRouteKey(stack, "overview")
            qrouter.push(stack, "anritsu")
        foreign_routes = [item for item in qrouter.history if item.stacked is foreign]
        owned.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
        assert not isValid(owned) and isValid(foreign)
        assert owned not in qrouter.stackHistories
        assert all(item.stacked is not owned for item in qrouter.history)
        assert [item for item in qrouter.history if item.stacked is foreign] == foreign_routes
        assert qrouter.stackHistories[foreign].top() == "anritsu"
        qrouter.pop()
        assert foreign.currentWidget().objectName() == "overview"
    finally:
        if isValid(owned):
            owned.deleteLater()
        foreign.deleteLater()
        application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
