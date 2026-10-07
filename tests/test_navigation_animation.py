"""Exercise intersecting Fluent tree transitions after real layout/paint."""

import pytest
from PySide6.QtCore import QAbstractAnimation
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget
from qfluentwidgets import FluentIcon, NavigationInterface

from app.ui.shell.navigation import StationNavigationTreeWidget
from app.ui.shell import MainWindow
from tests.shell_test_isolation import isolated_shell_persistence, shell_qt_application  # noqa: F401


@pytest.fixture
def tree():
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.resize(1200, 900)
    layout = QHBoxLayout(window)
    navigation = NavigationInterface(window)
    layout.addWidget(navigation)
    layout.addWidget(QWidget(), 1)
    items = {}
    for key, parent in (
        ("Devices", None), ("Rigol", "Devices"), ("Keithley", "Devices"),
        ("Characterization", "Keithley"), ("Anritsu", "Devices"),
        ("MOKE", "Devices"), ("Lake Shore", "Devices"), ("Overview", None),
    ):
        item = StationNavigationTreeWidget(FluentIcon.SETTING, key, True, navigation.panel)
        navigation.addWidget(key, item, parentRouteKey=parent)
        items[key] = item
    window.show()
    navigation.expand(useAni=False)
    items["Devices"].setExpanded(True)
    app.processEvents()
    yield app, window, navigation, items
    window.close()
    window.deleteLater()
    app.processEvents()


def assert_rows_separate(app, navigation, items):
    app.processEvents()
    rectangles = []
    for key, item in items.items():
        row = item.itemWidget
        visible = row.visibleRegion().boundingRect()
        if row.isVisible() and not visible.isEmpty():
            y = row.mapTo(navigation, visible.topLeft()).y()
            rectangles.append((y, y + visible.height(), key))
    rectangles.sort()
    for first, second in zip(rectangles, rectangles[1:]):
        assert first[1] <= second[0], (first, second)


@pytest.mark.parametrize("frames", [(20, 50, 80, 119, 120), (120,)])
def test_leaf_animation_cannot_restore_position_before_sibling_expansion(tree, frames):
    app, window, navigation, items = tree
    # Clicking an ordinary device route starts a Fluent expansion even for a leaf.
    items["MOKE"].setExpanded(True, ani=True)
    items["MOKE"].expandAni.pause()
    items["Keithley"].setExpanded(True, ani=True)
    items["Keithley"].expandAni.pause()
    for frame in frames:
        items["Keithley"].expandAni.setCurrentTime(frame)
        app.processEvents()
        items["MOKE"].expandAni.setCurrentTime(frame)
        assert_rows_separate(app, navigation, items)


@pytest.mark.parametrize("expanded", [True, False])
def test_instant_transition_cancels_pending_frames_even_for_same_state(tree, expanded):
    app, window, navigation, items = tree
    branch = items["Keithley"]
    branch.setExpanded(True, ani=True)
    branch.expandAni.pause()
    branch.expandAni.setCurrentTime(40)
    branch.setExpanded(expanded, ani=False)
    assert branch.expandAni.state() == QAbstractAnimation.Stopped
    assert branch.height() == branch.sizeHint().height()
    assert_rows_separate(app, navigation, items)


def test_reversed_expansion_and_compact_flyout_keep_layout_ownership(tree):
    app, window, navigation, items = tree
    branch = items["Keithley"]
    for expanded in (True, False, True, False, True):
        branch.setExpanded(expanded, ani=True)
        branch.expandAni.pause()
        branch.expandAni.setCurrentTime(50)
        app.processEvents()
    branch.setExpanded(True, ani=False)
    assert_rows_separate(app, navigation, items)
    clone = items["Devices"].clone()
    assert isinstance(clone, StationNavigationTreeWidget)
    assert all(isinstance(child, StationNavigationTreeWidget) for child in clone.childItems())
    clone.deleteLater()


def test_parent_and_child_expanding_together_finish_at_content_height(tree):
    app, window, navigation, items = tree
    root = items["Devices"]
    branch = items["Keithley"]
    root.setExpanded(False)
    root.setExpanded(True, ani=True)
    root.expandAni.pause()
    root.expandAni.setCurrentTime(60)
    branch.setExpanded(True, ani=True)
    branch.expandAni.pause()
    branch.expandAni.setCurrentTime(120)
    root.expandAni.setCurrentTime(120)
    assert_rows_separate(app, navigation, items)
    assert root.height() == root.sizeHint().height()


def test_application_navigation_during_interrupted_expansion(tmp_path):
    app = QApplication.instance()
    window = MainWindow(".config/settings.yml", simulation=True)
    window.resize(1360, 880)
    window.show()
    app.processEvents()
    navigation = window.navigationInterface
    items = {key: navigation.widget(key) for key in (
        "apparatusMenu", "rigolPageHost", "keithleyPageHost",
        "keithley_characterizationPageHost", "anritsuPageHost", "moke_boxPageHost",
        "lakeshore_gaussmeterPageHost", "overviewPageHost",
    )}
    branch = items["keithleyPageHost"]
    leaf = items["moke_boxPageHost"]
    assert all(isinstance(item, StationNavigationTreeWidget) for item in items.values())
    for expanded in (True, False, True):
        leaf._onClicked(True, False)
        branch.setExpanded(expanded, ani=True)
        for animation in (leaf.expandAni, branch.expandAni):
            if animation.state() == QAbstractAnimation.Running:
                animation.pause()
        for frame in (30, 70, 120):
            branch.expandAni.setCurrentTime(frame)
            app.processEvents()
            leaf.expandAni.setCurrentTime(frame)
            assert_rows_separate(app, navigation, items)
    assert window.stackedWidget.currentWidget() is window.navigation_routes["moke_box"]
    QTest.qWait(250)
    assert window.grab().save(str(tmp_path / "navigation-expanded.png"))
    window.resize(820, 560)
    app.processEvents()
    navigation.panel.expandAni.setCurrentTime(navigation.panel.expandAni.duration())
    app.processEvents()
    assert navigation.panel.isCollapsed()
    assert window.grab().save(str(tmp_path / "navigation-compact.png"))
    window.close()
