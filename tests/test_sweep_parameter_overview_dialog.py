"""Offline, real-widget regression coverage for the aggregate settings review."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from app.ui.design_system import apply_application_theme
from app.ui.design_system.tokens import tokens_for


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    # The Windows offscreen platform does not discover system fonts itself.
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        font = Path("C:/Windows/Fonts") / name
        if font.exists():
            assert QFontDatabase.addApplicationFont(str(font)) >= 0
    application.setFont(QFont("Segoe UI", 10))
    apply_application_theme(application, "light")
    yield application
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def entries():
    from app.recipes.parameter_overview import SweepParameterEntry as Entry

    return (
        Entry("keithley", "B", "source_level", "Source current", "0 mA",
              "0 mA → 10 mA · 11 points", "sweep", "bias-roi", "Bias ROI",
              detail="Linear sweep; includes both endpoints."),
        Entry("keithley", "B", "source_range", "Source range", "100 mA",
              "10 mA", "set", "initial-b", "Initial B settings"),
        Entry("keithley", "B", "source_autorange", "Source autorange", False,
              False, "preserve", "initial-b", "Initial B settings"),
        Entry("keithley", "B", "settling_time", "Settling time", "100 ms",
              "0.1 s", "set", "initial-b", "Initial B settings"),
        Entry("keithley", "A", "output_enabled", "Output", False,
              False, "preserve", "initial-a", "Initial A settings"),
        Entry("rigol", "1", "frequency", "Carrier frequency", "1 MHz",
              "2 MHz", "set", "carrier", "RF carrier"),
        Entry("anritsu", "SA", "resolution_bandwidth", "Resolution bandwidth",
              None, "10 kHz", "set", "spectrum", "Spectrum acquisition",
              detail="Device tab snapshot is unavailable; check the analyzer tab."),
        Entry("moke_box", 0, "voltage", "Programming voltage", "0 V",
              "20 mV", "set", "disabled-field", "Optional field", enabled=False,
              detail="Disabled block is included for review."),
    )


@pytest.fixture
def dialog(app, entries):
    from app.ui.recipes.parameter_overview_dialog import SweepParameterOverviewDialog

    parent = QWidget()
    parent.resize(1360, 900)
    overview = SweepParameterOverviewDialog("Current and RF characterization", entries, parent)
    overview.resize(1280, 800)
    overview.show()
    app.processEvents()
    yield overview
    overview.close()
    overview.deleteLater()
    parent.deleteLater()


def leaves(dialog, *, visible=False):
    result = []
    tree = dialog.tree
    for index in range(tree.topLevelItemCount()):
        group = tree.topLevelItem(index)
        for child_index in range(group.childCount()):
            item = group.child(child_index)
            if not visible or not (group.isHidden() or item.isHidden()):
                result.append(item)
    return result


def test_all_settings_and_channels_are_expanded_initially(dialog, entries):
    assert len(leaves(dialog)) == len(leaves(dialog, visible=True)) == 8
    assert not dialog.only_differences.isChecked()
    assert dialog.device_filter.currentIndex() == 0
    assert all(dialog.tree.topLevelItem(i).isExpanded()
               for i in range(dialog.tree.topLevelItemCount()))
    assert {item.text(0) for item in leaves(dialog)} == {
        "Source current", "Source range", "Source autorange", "Settling time",
        "Output", "Carrier frequency", "Resolution bandwidth", "Programming voltage",
    }
    assert "8 / 8" in dialog.count_label.text()
    assert "1 swept" in dialog.summary_label.text().lower()
    assert "1 disabled" in dialog.summary_label.text().lower()
    assert "disabled" in leaves(dialog)[-1].text(4).lower()
    assert "Device tab value" == dialog.tree.headerItem().text(1)
    assert "Sweep value" == dialog.tree.headerItem().text(2)
    assert "100 ms" == leaves(dialog)[3].text(1)
    assert "0.1 s" == leaves(dialog)[3].text(2)
    assert leaves(dialog)[-1].parent().text(0) == "MOKE Box · VOUT 0"
    assert "3 same" in dialog.summary_label.text()
    assert "3 different" in dialog.summary_label.text()
    assert "2 unavailable" in dialog.summary_label.text()


def test_filters_are_composable_searches_values_and_can_be_cleared(dialog, app):
    dialog.device_filter.setCurrentIndex(dialog.device_filter.findData("keithley"))
    dialog.only_differences.setChecked(True)
    app.processEvents()
    assert {item.text(0) for item in leaves(dialog, visible=True)} == {
        "Source current", "Source range",
    }
    dialog.search.setText("10 mA")
    app.processEvents()
    assert len(leaves(dialog, visible=True)) == 2
    dialog.search.setText("no matching parameter")
    app.processEvents()
    assert not leaves(dialog, visible=True)
    assert dialog.empty_label.isVisibleTo(dialog)
    assert "0 / 8" in dialog.count_label.text()
    assert "filter" in dialog.empty_label.text().lower()
    dialog.search.clear()
    dialog.device_filter.setCurrentIndex(0)
    dialog.only_differences.setChecked(False)
    app.processEvents()
    assert len(leaves(dialog, visible=True)) == 8
    assert not dialog.empty_label.isVisibleTo(dialog)
    # Source names, device names, and channels are searchable as well as values.
    dialog.search.setText("initial a")
    assert [item.text(0) for item in leaves(dialog, visible=True)] == ["Output"]
    dialog.search.setText("Keithley B")
    assert len(leaves(dialog, visible=True)) == 4
    dialog.search.setText("moke 0")
    assert [item.text(0) for item in leaves(dialog, visible=True)] == ["Programming voltage"]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_comparison_uses_semantic_tokens_and_keeps_range_visible(dialog, app, theme):
    apply_application_theme(app, theme)
    app.processEvents()
    tokens = tokens_for(theme)
    rows = {item.text(0): item for item in leaves(dialog)}
    assert rows["Source current"].text(3) == "DIFFERENT"
    assert rows["Source range"].text(3) == "DIFFERENT"
    assert rows["Source autorange"].text(3) == "SAME"
    assert rows["Settling time"].text(3) == "SAME"
    assert rows["Resolution bandwidth"].text(3) == "UNAVAILABLE"
    assert rows["Resolution bandwidth"].text(1) == "Unavailable"
    assert rows["Source autorange"].text(1) == "OFF"
    assert rows["Output"].text(2) == "OFF"
    assert "0 mA → 10 mA" in rows["Source current"].text(2)
    assert rows["Source current"].foreground(3).color().name() == tokens.caution.lower()
    assert rows["Source range"].foreground(2).color().name() == tokens.caution.lower()
    assert rows["Settling time"].foreground(3).color().name() == tokens.success.lower()
    assert rows["Source autorange"].foreground(2).color().name() == tokens.success.lower()
    for item in leaves(dialog):
        for column in range(dialog.tree.columnCount()):
            assert item.text(column) in item.toolTip(column)
    assert "Linear sweep" in rows["Source current"].toolTip(2)


def test_keyboard_search_and_close(dialog, app):
    dialog.tree.setFocus()
    QTest.keyClick(dialog, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert dialog.search.hasFocus()
    QTest.keyClicks(dialog.search, "Carrier frequency")
    assert [item.text(0) for item in leaves(dialog, visible=True)] == ["Carrier frequency"]
    dialog.close_button.click()
    app.processEvents()
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_open_source_emits_before_exiting_so_the_page_can_navigate(dialog):
    requested = []
    dialog.source_requested.connect(requested.append)
    dialog.tree.setCurrentItem(leaves(dialog)[0])
    dialog.tree.setFocus()
    QTest.keyClick(dialog.tree, Qt.Key.Key_Return)
    assert requested == ["bias-roi"]
    assert not dialog.isVisible()
    assert dialog.result() == QDialog.DialogCode.Accepted


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width,height", [(1280, 800), (900, 700)])
def test_rendered_geometry_and_screenshot(dialog, app, theme, width, height):
    apply_application_theme(app, theme)
    dialog.resize(width, height)
    app.processEvents()
    assert dialog.size().toTuple() == (width, height)
    assert dialog.isModal()
    assert dialog.parentWidget() is not None
    surface = dialog.modal_shell.surface
    for widget in (dialog.tree, dialog.search, dialog.device_filter,
                   dialog.only_differences, dialog.summary_label, dialog.count_label,
                   dialog.close_button):
        assert surface.isAncestorOf(widget)
        assert widget.isVisibleTo(dialog)
        rect = widget.rect().translated(widget.mapTo(dialog, QPoint()))
        assert dialog.rect().contains(rect)
        assert rect.width() > 0 and rect.height() > 0
    assert dialog.tree.viewport().height() >= 250
    if width == 900:
        scrollbar = dialog.tree.horizontalScrollBar()
        assert scrollbar.maximum() > 0
        scrollbar.setValue(scrollbar.maximum())
        app.processEvents()
        source_x = dialog.tree.columnViewportPosition(5)
        assert 0 <= source_x < dialog.tree.viewport().width()
        scrollbar.setValue(0)
    first = leaves(dialog)[0]
    assert not dialog.tree.visualItemRect(first).isEmpty()
    dialog.tree.scrollToItem(leaves(dialog)[-1])
    app.processEvents()
    assert dialog.tree.viewport().rect().intersects(
        dialog.tree.visualItemRect(leaves(dialog)[-1]))
    dialog.tree.scrollToTop()
    app.processEvents()
    preview = Path("artifacts/sweep-overview")
    preview.mkdir(parents=True, exist_ok=True)
    assert dialog.grab().save(str(preview / f"settings-{theme}-{width}.png"))


def test_empty_overview_explains_missing_settings_and_can_close(app):
    from app.ui.recipes.parameter_overview_dialog import SweepParameterOverviewDialog

    dialog = SweepParameterOverviewDialog("Empty sweep", ())
    try:
        dialog.show()
        app.processEvents()
        assert dialog.empty_label.isVisibleTo(dialog)
        assert "No device settings" in dialog.empty_label.text()
        assert "0 / 0" in dialog.count_label.text()
        assert dialog.close_button.isEnabled()
        assert not dialog.device_filter.isEnabled()
        assert not dialog.only_differences.isEnabled()
        assert not dialog.search.isEnabled()
        preview = Path("artifacts/sweep-overview")
        preview.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(preview / "settings-empty.png"))
    finally:
        dialog.close()
        dialog.deleteLater()


def test_preserved_setting_without_source_does_not_navigate_or_close(app):
    from app.recipes.parameter_overview import SweepParameterEntry
    from app.ui.recipes.parameter_overview_dialog import SweepParameterOverviewDialog

    entry = SweepParameterEntry("keithley", "A", "nplc", "Measurement NPLC",
                                "1", "1", "preserve", "", "")
    dialog = SweepParameterOverviewDialog("Preserved settings", (entry,))
    requested = []
    dialog.source_requested.connect(requested.append)
    try:
        dialog.show()
        app.processEvents()
        dialog.tree.setCurrentItem(leaves(dialog)[0])
        QTest.keyClick(dialog.tree, Qt.Key.Key_Return)
        assert requested == []
        assert dialog.isVisible()
        assert leaves(dialog)[0].text(5) == "Device tab / preserved"
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert not dialog.isVisible()
    finally:
        dialog.close()
        dialog.deleteLater()
