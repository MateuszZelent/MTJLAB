"""Results follows the active DUT and preserves explicit catalogue navigation."""

import pytest
from types import SimpleNamespace
from PySide6.QtTest import QTest

from app.inventory import ActiveSampleTarget, Sample
from app.storage import Hdf5RunWriter
from app.domain.models import MeasurementPoint
from app.ui.shell import MainWindow
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence  # noqa: PLC0414
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


def test_catalogue_refresh_indexes_once_and_retains_manual_scope(tmp_path, shell_qt_application, monkeypatch):
    from app.ui.results.file_browser import FileBrowserPanel
    from app.ui.shell.main_window import MainWindow as Shell

    root = tmp_path / "catalogue"
    browser = FileBrowserPanel(str(root), catalogue_tree=True)
    target = ActiveSampleTarget(sample_id="wafer", row="20", col="4")
    directory = root / "wafer" / "devices" / "R20C4"
    calls = []
    refresh = browser.refresh
    def count_refresh():
        calls.append(browser.output_dir)
        refresh()
    monkeypatch.setattr(browser, "refresh", count_refresh)
    shell = SimpleNamespace(results_page=SimpleNamespace(file_browser=browser),
        inventory_store=SimpleNamespace(get_active_target=lambda: target),
        _set_results_sample_context=lambda _target: browser.set_sample_context(directory, root))
    try:
        Shell._refresh_results_catalogue(shell)
        assert calls == [directory]  # a changed context starts just one index job
        calls.clear()
        browser.set_output_directory(root)
        calls.clear()
        Shell._refresh_results_catalogue(shell)
        assert calls == [root] and browser.output_dir == root
        wait_until(shell_qt_application, lambda: browser._refresh_task is None)
    finally:
        assert browser.close()
        browser.deleteLater()


@pytest.mark.parametrize("width,theme", [(1440, "light"), (1024, "dark")])
def test_results_active_dut_scope_and_parent_navigation_are_rendered(tmp_path, shell_qt_application, width, theme):
    app = shell_qt_application
    window = MainWindow(".config/settings.yml", simulation=True)
    browser = window.results_page.file_browser
    store = window.inventory_store
    try:
        store.save_sample(Sample(sample_id="wafer", name="wafer", rows=("20",), cols=("4", "5")))
        targets = [ActiveSampleTarget(sample_id="wafer", row="20", col=col) for col in ("4", "5")]
        paths = []
        for target in targets:
            directory = store.measurement_directory_for("wafer", "sweeps", row=target.row, col=target.col)
            path = directory / "data-test.h5"
            writer = Hdf5RunWriter(path, recipe_source="name: target-test\n", settings_source="schema_version: 1\n",
                                   plan_hash="target", device_idn={})
            writer.append(MeasurementPoint(index=0, setpoints={}, measurements={"keithley.A.resistance_ohm": 100.}))
            writer.close("completed")
            paths.append(path)
        store.set_active_target(targets[0])
        window._on_active_sample_target_changed(targets[0])
        active = store.device_directory_for("wafer", "20", "4")
        assert browser.output_dir == active
        assert "R20C4" in browser.location.text()
        assert browser.location.toolTip() == str(active)
        wait_until(app, lambda: browser._refresh_task is None)
        assert [summary.path for summary in browser._all_summaries] == [paths[0]]
        browser.parent_directory_button.click()
        assert browser.output_dir == active.parent
        window._refresh_results_catalogue()
        assert browser.output_dir == active.parent  # refresh does not steal operator navigation
        wait_until(app, lambda: browser._refresh_task is None)
        assert {summary.path for summary in browser._all_summaries} == set(paths)
        browser.catalogue_directory_button.click()
        assert browser.output_dir == store.catalogue_root
        browser.active_directory_button.click()
        assert browser.output_dir == active
        external = tmp_path / "external"
        browser.set_output_directory(external)
        window._refresh_results_catalogue()
        assert browser.output_dir == external
        store.set_active_target(targets[1])
        window._on_active_sample_target_changed(targets[1])
        assert browser.output_dir == store.device_directory_for("wafer", "20", "5")
        store.clear_active_target()
        window._on_active_sample_target_changed(store.get_active_target())
        assert browser.output_dir == store.catalogue_root
        assert not browser.active_directory_button.isEnabled()
        store.set_active_target(targets[0])
        window._on_active_sample_target_changed(targets[0])
        wait_until(app, lambda: browser._refresh_task is None)
        window._set_theme_mode(theme, persist=False)
        window.resize(width, 900)
        window.show()
        window._navigate_to("results")
        QTest.qWait(150)
        host = window.navigation_routes["results"].scroll_area
        assert host.horizontalScrollBar().maximum() == 0
        for button in (browser.parent_directory_button, browser.active_directory_button, browser.catalogue_directory_button):
            assert button.isVisible() and button.height() >= 28
            assert browser.actions_host.rect().contains(button.mapTo(browser.actions_host, button.rect().bottomRight()))
        assert window.grab().save(str(tmp_path / f"results-active-device-{theme}-{width}.png"))
        # A newly constructed shell must recover the persisted active DUT,
        # rather than reopening the entire catalogue at startup.
        window.recipe_page._close_discard_confirmed = True
        assert window.close()
        restored = MainWindow(".config/settings.yml", simulation=True)
        assert restored.results_page.file_browser.output_dir == active
        restored.recipe_page._close_discard_confirmed = True
        assert restored.close()
    finally:
        window.recipe_page._close_discard_confirmed = True
        window.close()
