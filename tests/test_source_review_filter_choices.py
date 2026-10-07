"""Large filter popups use visible rows, with all values still reachable."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from app.ui.design_system.fluent_theme import apply_application_theme
from app.ui.results.spectrum_tab import SpectrumResultsTab
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_large_filter_popup_formats_visible_rows_and_keyboard_reaches_last(shell_qt_application, tmp_path, theme):
    app = shell_qt_application
    apply_application_theme(app, theme)
    page = SpectrumResultsTab()
    combo = page.parameter_set_combo
    values = tuple(range(10000))
    formatted = []
    def format_value(value):
        formatted.append(value)
        return f"Source current = {value} microampere"
    # Isolate choice rendering from applying a synthetic non-signature value.
    combo.currentIndexChanged.disconnect()
    try:
        combo.set_options(("All parameter sets", "all"), values, format_value)
        assert combo.options.records is values
        assert combo.count() == 10001
        assert not formatted
        page.resize(1440, 900)
        page.show()
        app.processEvents()
        combo.showPopup()
        app.processEvents()
        assert combo.view().isVisible()
        assert len(set(formatted)) < 100
        assert combo.view().window().grab().save(str(tmp_path / f"filter-popup-{theme}.png"))
        QTest.keyClick(combo.view(), Qt.Key.Key_End)
        QTest.keyClick(combo.view(), Qt.Key.Key_Return)
        app.processEvents()
        assert combo.currentData() == 9999
        assert combo.currentText() == "Source current = 9999 microampere"
        assert combo.geometry().height() >= 32
        assert page.grab().save(str(tmp_path / f"filter-choices-{theme}.png"))
    finally:
        combo.hidePopup()
        page.close()
        page.deleteLater()
        app.processEvents()
