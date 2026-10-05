"""Shown Fluent sweep acquisition editors and processed execution preview."""

import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import yaml
from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog

from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import SpectrumAnalysisSettingsDialog
from app.devices.anritsu_ms2830a.ui.recipe_dialog import AnritsuNodeEditorDialog
from app.recipes import RecipeNode, parse_recipe_text
from app.ui.design_system import apply_application_theme
from app.ui.execution.page import RunMonitorPage
from app.ui.recipes.common_dialogs import AnritsuAcquisitionEditorDialog
from app.ui.recipes.page import RecipePage
from tests.helpers import simulation_settings


@pytest.fixture(scope="module")
def application():
    application = QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    application.setFont(QFont("Segoe UI", 10))
    return application


def contained(parent, child):
    assert child.isVisible()
    top = child.mapTo(parent, QPoint(0, 0))
    assert parent.rect().contains(top)
    assert parent.rect().contains(top + QPoint(child.width() - 1, child.height() - 1))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("reference", [False, True])
def test_shown_editor_geometry_and_roundtrip(application, theme, reference):
    apply_application_theme(application, theme)
    fields = (
        {"source_file": "measurements/background.h5", "file_kind": "background"}
        if reference
        else {
            "reference_operation": "subtract_power_signed",
            "average_count": 5,
            "processing": {
                "filters": ["denoise"],
                "parameters": {"protected_bands": [["1 MHz", "2 MHz"]]},
            },
        }
    )
    node = RecipeNode("acquire", "acquire_reference" if reference else "acquire_spectrum", fields)
    dialog = AnritsuAcquisitionEditorDialog(node)
    try:
        for width, height in ((660, 640), (600, 560)):
            dialog.resize(width, height)
            dialog.show()
            application.processEvents()
            contained(dialog, dialog.apply_button)
            contained(dialog, dialog.processing_options.note)
            options = dialog.processing_options
            if reference:
                contained(dialog, options.path)
                assert not dialog.average_count.isEnabled()
            else:
                contained(dialog, options.filters_row)
                assert not options.filters["emi_reject"].isEnabled()
                assert dialog.store_processed.isChecked()
            folder = Path("artifacts/sweeps-spectrum")
            folder.mkdir(parents=True, exist_ok=True)
            assert dialog.grab().save(
                str(folder / f"{'reference' if reference else 'spectrum'}-{theme}-{width}.png")
            )
        saved = dialog.node_fields()
        second = AnritsuAcquisitionEditorDialog(RecipeNode(node.id, node.type, saved))
        try:
            assert second.node_fields() == saved
        finally:
            second.close()
            second.deleteLater()
        if reference:
            options.source.setCurrentIndex(options.source.findData("acquire"))
            assert "source_file" not in dialog.node_fields()
            assert dialog.average_count.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()
        application.processEvents()


def test_managed_editor_opens_shared_options_and_rebuild_preserves_them(application):
    dialog = AnritsuNodeEditorDialog(simulation_settings())
    try:
        dialog.node_role.setCurrentIndex(dialog.node_role.findData("acquire_spectrum"))
        with (
            patch.object(
                AnritsuAcquisitionEditorDialog, "exec", return_value=QDialog.DialogCode.Accepted
            ),
            patch.object(
                AnritsuAcquisitionEditorDialog,
                "node_fields",
                return_value={
                    "average_count": 7,
                    "reference_operation": "subtract_power_signed",
                    "processing": {"filters": ["denoise"]},
                },
            ),
        ):
            dialog._edit_acquisition_options()
        assert dialog.average_count.value() == 7
        assert dialog.reference_operation.currentData() == "subtract_power_signed"
        dialog.show()
        application.processEvents()
        contained(dialog, dialog.acquisition_options_button)
        assert dialog.grab().save("artifacts/sweeps-spectrum/managed-analyzer.png")
        node = RecipeNode("anritsu", "sequence", {"device_module": "anritsu"})
        kwargs = {
            "parameter_actions": [],
            "acquire_single": False,
            "trace": "TRAC1",
            "post_configuration_operation": "acquire_spectrum",
            "acquisition_average_count": 7,
            "acquisition_reference_operation": "subtract_power_signed",
        }
        updated = RecipePage._configured_anritsu_node(
            node, **kwargs, acquisition_options=dialog.acquisition_options()
        )
        reparsed = parse_recipe_text(
            yaml.safe_dump({"schema_version": 1, "name": "roundtrip", "root": updated})
        ).root
        rebuilt = RecipePage._configured_anritsu_node(reparsed, **kwargs)
        assert rebuilt["children"][-1]["processing"] == {"filters": ["denoise"]}
        assert rebuilt["children"][-1]["store_processed"]
    finally:
        dialog.close()
        dialog.deleteLater()


def test_recipe_parameters_do_not_mix_average_across_points(application):
    dialog = SpectrumAnalysisSettingsDialog(
        section="filters", allow_temporal_average=False, source_unit="W"
    )
    try:
        dialog.show()
        application.processEvents()
        assert not dialog.temporal_frames.isVisible()
        assert not dialog.emi_min_frames.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_execution_preview_renders_processed_unit_and_resets_for_reference(application):
    page = RunMonitorPage()
    try:
        page.resize(1280, 800)
        page.show()
        application.processEvents()
        source = {
            "frequency_hz": (1e6, 2e6, 3e6),
            "power_dbm": (-60.0, -50.0, -40.0),
            "processed_values": (-1e-10, 0.0, 1e-10),
            "processed_unit": "W",
        }
        page.update_spectrum_preview(source)
        assert page.spectrum_preview._y_unit == "W"
        assert "Processed spectrum" in page.spectrum_preview._traces
        curve = page.spectrum_preview._curves["Processed spectrum"]
        page.update_spectrum_preview(source)
        assert page.spectrum_preview._curves["Processed spectrum"] is curve
        page.update_spectrum_preview(
            {
                "frequency_hz": source["frequency_hz"],
                "power_dbm": source["power_dbm"],
                "preview_kind": "reference",
            }
        )
        assert page.spectrum_preview._y_unit == "dBm"
        assert "Processed spectrum" not in page.spectrum_preview._traces
    finally:
        page.close()
        page.deleteLater()
        application.processEvents()


def test_processing_runs_off_gui_thread_and_timer_remains_live(application, tmp_path, monkeypatch):
    import threading
    import time

    from PySide6.QtCore import QTimer

    from app.engine import RecipeCompiler
    from app.spectrum import analysis
    from app.ui.run_worker import RunController
    from tests.test_sweep_spectrum_processing import recipe_with

    settings = simulation_settings()
    source = recipe_with(
        reference_operation="subtract_power_signed", processing={"filters": ["denoise"]}
    )
    plan = RecipeCompiler(settings).compile(parse_recipe_text(source))
    main_thread = threading.get_ident()
    processing_threads = []
    original = analysis.clean_spectrum_pipeline

    def observed(*args, **kwargs):
        processing_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(analysis, "clean_spectrum_pipeline", observed)
    controller = RunController()
    completed, errors, ticks = [], [], []
    controller.finished.connect(completed.append)
    controller.failed.connect(errors.append)
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    try:
        controller.start(
            settings,
            tmp_path / "settings.yml",
            plan,
            simulation=True,
            output_dir_override=str(tmp_path),
            file_stem_override="spectrum-worker",
        )
        deadline = time.monotonic() + 30
        while controller.running and time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.002)
        application.processEvents()
        assert not errors and completed
        assert completed[0]["result"].error is None
        assert processing_threads and all(
            identity != main_thread for identity in processing_threads
        )
        assert len(ticks) >= 3
    finally:
        timer.stop()
        assert controller.close()
        controller.deleteLater()


def test_sweeps_page_shows_processing_recipe_at_desktop_and_narrow_sizes(
    application, tmp_path, monkeypatch
):
    artifact_dir = Path("artifacts/sweeps-spectrum").resolve()
    source = Path("recipes/example_spectrum_background_filters.yml").read_text(encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    page = RecipePage(simulation_settings())
    try:
        page._restore_tree_history_source(source, "Spectrum processing rendering")
        page._close_discard_confirmed = True
        assert "signed W" in page.tree_model.value_for("measurement")
        assert "Denoise" in page.tree_model.value_for("measurement")
        for width, height, theme in ((1360, 880, "light"), (1000, 760, "dark")):
            apply_application_theme(application, theme)
            page.resize(width, height)
            page.show()
            for _ in range(4):
                application.processEvents()
            assert page.workspace_splitter.width() > 400
            assert page.workspace_splitter.height() > 180
            assert page.grab().save(str(artifact_dir / f"sweeps-{theme}-{width}.png"))
    finally:
        page.close()
        page.deleteLater()
        application.processEvents()


def test_completed_result_waits_for_visible_results_and_readers_stop(application, tmp_path):
    import threading

    from app.ui.results.page import ResultsPage
    from app.ui.results.workers import ResultReadTask

    page = ResultsPage(str(tmp_path))
    path = tmp_path / 'completed.h5'
    started, release = threading.Event(), threading.Event()
    try:
        with patch.object(page, 'refresh') as refresh, patch.object(page, 'select_result_path') as select:
            page.offer_completed_result(path)
            refresh.assert_not_called()
            select.assert_not_called()
            page.resize(1100, 740)
            application.processEvents()
            select.assert_not_called()
            page.show()
            application.processEvents()
            select.assert_called_once_with(path)
            assert page._pending_completed_result is None
            page.hide()
            second_path = tmp_path / 'next.h5'
            page.offer_completed_result(second_path)
            page.show()
            application.processEvents()
            assert select.call_count == 2
            select.assert_called_with(second_path)
        def read():
            started.set()
            release.wait(5)
        task = ResultReadTask(99, read)
        page._result_task = task
        page._read_pool.start(task)
        assert started.wait(1)
        assert not page.shutdown(timeout_ms=1)
        release.set()
        assert page.shutdown(timeout_ms=1000)
    finally:
        release.set()
        assert page.shutdown()
        page.close()
        page.deleteLater()
        application.processEvents()
