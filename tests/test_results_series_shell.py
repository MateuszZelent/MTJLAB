"""Rendered sweep-series workflow in the actual Fluent station shell."""

from pathlib import Path

import pytest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest

from app.ui.results.processing import ResultProcessing
from app.ui.shell import MainWindow
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence  # noqa: PLC0414
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_results_series_navigation import nested_archive as nested_archive  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.mark.parametrize("width,theme", [(1440, "light"), (1024, "dark")])
def test_fluent_shell_series_controls_and_plot_fit_viewport(nested_archive, shell_qt_application, tmp_path, width, theme):
    app = shell_qt_application
    font = Path("C:/Windows/Fonts/arial.ttf")
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Arial", 10))
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window._set_theme_mode(theme, persist=False)
        window.resize(width, 900)
        window.show()
        window._navigate_to("results")
        page = window.results_page
        page.set_output_directory(nested_archive.parent)
        assert page.file_browser.select_path(nested_archive)
        wait_until(app, lambda: page._selected_path == nested_archive and page._result_task is None
                   and page.spectrum_tab._filter_task is None and not page.spectrum_tab._read_tasks)
        page.result_tabs.setCurrentIndex(page._spectrum_index)
        tab = page.spectrum_tab
        tab.series_controls.axis.setCurrentIndex(tab.series_controls.axis.findData("keithley.A.current"))
        combo = tab.series_controls.fixed["keithley.B.current"]
        combo.setCurrentIndex(combo.findData(-.005))
        tab.processing_controls.set_state(ResultProcessing("subtract_reference_signed"), emit=True)
        wait_until(app, lambda: tab._filter_task is None and not tab._read_tasks)
        assert tab.points_model.rowCount() == 41
        QTest.qWait(300)
        host = window.navigation_routes["results"].scroll_area
        assert page.isVisibleTo(window)
        assert host.horizontalScrollBar().maximum() == 0
        assert tab.spectrum_view.width() <= host.viewport().width()
        host.ensureWidgetVisible(tab.series_controls)
        QTest.qWait(50)
        assert combo.isVisibleTo(window) and combo.height() >= 30
        assert window.grab().save(str(tmp_path / f"results-shell-series-{theme}-{width}.png"))
        host.verticalScrollBar().setValue(host.verticalScrollBar().maximum())
        QTest.qWait(50)
        offset = host.verticalScrollBar().value()
        tab.next_button.click()
        wait_until(app, lambda: not tab._read_tasks)
        assert host.verticalScrollBar().value() == offset
        assert "Post-processed spectrum" in tab.spectrum_plot._traces
        assert window.grab().save(str(tmp_path / f"results-shell-plot-{theme}-{width}.png"))
    finally:
        window.recipe_page._close_discard_confirmed = True
        assert page.shutdown()
        window.close()
