"""Visible feedback follows the current result job, including chained jobs."""

from dataclasses import replace
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from app.ui.design_system import apply_application_theme
from app.ui.results.processing import ResultProcessing, ResultSpectrumProcessor
from app.ui.results.processing_controls import ResultProcessingControls
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_results_reference_workflow import reference_page as reference_page  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


def processing_overlay(tab):
    overlay = tab.findChild(QWidget, "resultProcessingOverlay")
    assert overlay is not None, "Recalculation must have a visible progress surface"
    return overlay


def test_emi_selection_does_not_promise_filtering_in_an_unsupported_domain(shell_qt_application):
    controls = ResultProcessingControls()
    try:
        controls.set_state(ResultProcessing("subtract_reference_signed"))
        assert not controls.filters["emi_reject"].isEnabled()
        assert controls.filters["narrow_reject"].isEnabled()
        assert controls.filters["denoise"].isEnabled()
        controls.set_state(ResultProcessing("difference_db"))
        assert controls.filters["emi_reject"].isEnabled()
    finally:
        controls.deleteLater()
        shell_qt_application.processEvents()


@pytest.mark.parametrize("kind", ["median", "gaussian"])
def test_input_preview_recovers_a_map_without_a_frequency_axis(kind):
    from app.ui.results.heatmap_tab import _HeatmapPayload, _process_map_payload
    from app.ui.results.map_processing import MapProcessing

    values = np.array([[1., 2., np.nan], [-1., -2., -3.]]) * 1e-9
    original = values.copy()
    source = _HeatmapPayload(values, np.arange(3.), np.arange(2.),
        "Current A", "A", "Current B", "A", "Power", "W", (-3e-9, 2e-9),
        1, np.zeros((2, 3), dtype=int))
    state = MapProcessing(component="median_power", mask_lines=True,
                          image_filter=kind, view="input")
    result = _process_map_payload(source, state, None)
    np.testing.assert_array_equal(result.matrix, original)
    assert result.z_unit == "W" and result.levels == source.levels
    np.testing.assert_array_equal(source.matrix, original)


@pytest.mark.parametrize("destination", ["spectrum", "heatmap"])
@pytest.mark.parametrize("mode", ["narrow_reject", "emi_reject", "denoise"])
def test_filter_click_displays_progress_until_real_processing_finishes(
    reference_page, monkeypatch, destination, mode
):
    app, page = reference_page
    tab = page.spectrum_tab if destination == "spectrum" else page.heatmap_tab
    if destination == "heatmap":
        page.result_tabs.setCurrentIndex(page._heatmap_index)
        tab.load_button.click()
        wait_until(app, lambda: tab.heatmap._data is not None and not tab._read_tasks)
    entered, release = Event(), Event()
    original = ResultSpectrumProcessor.process

    def slow_process(*args, **kwargs):
        entered.set()
        assert release.wait(8), "Test must release the result worker"
        return original(*args, **kwargs)

    monkeypatch.setattr(ResultSpectrumProcessor, "process", slow_process)
    try:
        tab.processing_controls.filters[mode].click()
        wait_until(app, entered.is_set)
        overlay = processing_overlay(tab)
        assert overlay.isVisible()
        assert not overlay.isWindow()  # The app navigation and safety controls stay usable.
        assert overlay.parentWidget() is tab
        assert overlay.rect().contains(overlay.widget.geometry())
        assert overlay.widget.width() >= 280 and overlay.widget.height() >= 100
        # The GUI continues processing events while the calculation waits.
        page.resize(1100, 820)
        app.processEvents()
        assert overlay.geometry() == tab.rect()
        assert overlay.rect().contains(overlay.widget.geometry())
    finally:
        release.set()
        wait_until(app, lambda: not page.spectrum_tab._read_tasks and not page.heatmap_tab._read_tasks)
    assert not overlay.isVisible()


def test_failed_calculation_closes_progress_and_displays_error(reference_page, monkeypatch):
    app, page = reference_page
    tab = page.spectrum_tab

    def fail(*_args, **_kwargs):
        raise ValueError("Controlled filter failure")

    monkeypatch.setattr(ResultSpectrumProcessor, "process", fail)
    tab.processing_controls.filters["denoise"].click()
    overlay = processing_overlay(tab)
    assert overlay.isVisible()
    wait_until(app, lambda: not tab._read_tasks)
    assert not overlay.isVisible()
    assert "Controlled filter failure" in tab.spectrum_info.text()


def test_superseded_worker_does_not_dismiss_new_calculation(reference_page, monkeypatch):
    app, page = reference_page
    tab = page.spectrum_tab
    entered = [Event(), Event()]
    release = [Event(), Event()]
    original = ResultSpectrumProcessor.process
    calls = []

    def slow_process(*args, **kwargs):
        index = len(calls)
        calls.append(index)
        entered[index].set()
        assert release[index].wait(8)
        return original(*args, **kwargs)

    monkeypatch.setattr(ResultSpectrumProcessor, "process", slow_process)
    try:
        tab.processing_controls.filters["denoise"].click()
        wait_until(app, entered[0].is_set)
        tab.processing_controls.filters["narrow_reject"].click()
        release[0].set()
        wait_until(app, entered[1].is_set)
        overlay = processing_overlay(tab)
        assert overlay.isVisible(), "An old completion must not hide the new job"
    finally:
        for event in release:
            event.set()
        wait_until(app, lambda: not tab._read_tasks)
    assert not overlay.isVisible()


def test_heatmap_progress_covers_chained_map_analysis(reference_page, monkeypatch):
    from app.ui.results import heatmap_tab as module
    from app.ui.results.map_processing import MapProcessing

    app, page = reference_page
    tab = page.heatmap_tab
    page.result_tabs.setCurrentIndex(page._heatmap_index)
    tab.load_button.click()
    wait_until(app, lambda: tab.heatmap._data is not None and not tab._read_tasks)
    tab.map_controls.set_state(MapProcessing(component="median_db"), emit=True)
    wait_until(app, lambda: not tab._read_tasks)
    entered, release = Event(), Event()
    original = module._process_map_payload

    def slow_map(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_process_map_payload", slow_map)
    try:
        tab.processing_controls.filters["denoise"].click()
        wait_until(app, entered.is_set)
        overlay = processing_overlay(tab)
        assert overlay.isVisible()
    finally:
        release.set()
        wait_until(app, lambda: not tab._read_tasks)
    assert not overlay.isVisible()
    assert tab._source_payload.z_unit == "dBm"
    assert tab.heatmap._data is not None


@pytest.mark.parametrize("kind", ["median", "gaussian"])
def test_heatmap_image_filter_has_separate_input_removed_and_reset_views(reference_page, kind):
    from app.ui.results.map_processing import MapProcessing

    app, page = reference_page
    tab = page.heatmap_tab
    page.result_tabs.setCurrentIndex(page._heatmap_index)
    tab.processing_controls.set_state(ResultProcessing("difference_db", 1), emit=True)
    wait_until(app, lambda: tab.heatmap._data is not None and not tab._read_tasks)
    source = tab._source_payload
    original = source.matrix.copy()
    state = MapProcessing(image_filter=kind)
    tab.map_controls.set_state(state, emit=True)
    wait_until(app, lambda: not tab._read_tasks)
    assert tab._source_payload is source
    assert not np.array_equal(tab.heatmap._data, original)
    filtered = tab.heatmap._data.copy()
    assert tab.heatmap.export_metadata["map_processing"]["image_filter"] == kind
    tab.map_controls.set_state(replace(state, view="removed"), emit=True)
    wait_until(app, lambda: not tab._read_tasks)
    np.testing.assert_allclose(tab.heatmap._data + filtered, original, atol=1e-12)
    tab.map_controls.set_state(replace(state, view="input"), emit=True)
    wait_until(app, lambda: not tab._read_tasks)
    np.testing.assert_array_equal(tab.heatmap._data, original)
    assert tab.heatmap.export_metadata["value_unit"] == "dB"
    tab.map_controls.reset.click()
    wait_until(app, lambda: not tab._read_tasks)
    np.testing.assert_array_equal(tab.heatmap._data, original)
    np.testing.assert_array_equal(source.matrix, original)


def test_shutdown_dismisses_progress(reference_page, monkeypatch):
    app, page = reference_page
    tab = page.spectrum_tab
    entered, release = Event(), Event()
    original = ResultSpectrumProcessor.process

    def slow_process(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return original(*args, **kwargs)

    monkeypatch.setattr(ResultSpectrumProcessor, "process", slow_process)
    try:
        tab.processing_controls.filters["denoise"].click()
        wait_until(app, entered.is_set)
        overlay = processing_overlay(tab)
        assert overlay.isVisible()
        tab._invalidate_pending_reads()
        assert not overlay.isVisible()
    finally:
        release.set()
        assert tab._read_pool.waitForDone(5000)
        app.processEvents()


@pytest.mark.parametrize("theme,size", [("light", (1360, 900)), ("dark", (900, 760))])
def test_processing_feedback_renders_inside_desktop_and_narrow_page(
    reference_page, monkeypatch, tmp_path, theme, size
):
    app, page = reference_page
    tab = page.spectrum_tab
    apply_application_theme(app, theme)
    page.resize(*size)
    entered, release = Event(), Event()
    original = ResultSpectrumProcessor.process

    def slow_process(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return original(*args, **kwargs)

    monkeypatch.setattr(ResultSpectrumProcessor, "process", slow_process)
    try:
        tab.processing_controls.filters["denoise"].click()
        wait_until(app, entered.is_set)
        overlay = processing_overlay(tab)
        app.processEvents()
        assert overlay.progress.isVisible() and overlay.title.isVisible()
        assert overlay.rect().contains(overlay.widget.geometry())
        assert overlay.widget.rect().contains(overlay.description.geometry())
        QTest.keyClick(overlay, Qt.Key.Key_Escape)
        assert overlay.isVisible()
        assert page.grab().save(str(tmp_path / f"processing-{theme}-{size[0]}.png"))
    finally:
        release.set()
        wait_until(app, lambda: not tab._read_tasks)
        apply_application_theme(app, "dark")
