"""Shown Fluent sweep acquisition editors and processed execution preview."""

import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
import yaml
from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog

from app.devices.anritsu_ms2830a.ui.analysis_settings_dialog import SpectrumAnalysisSettingsDialog
from app.devices.anritsu_ms2830a.ui.recipe_dialog import AnritsuNodeEditorDialog
from app.domain.errors import ConfigurationError
from app.recipes import RecipeNode, parse_recipe_text
from app.recipes.semantic_tree import normalize_recipe_tree
from app.ui.design_system import apply_application_theme
from app.ui.execution.page import RunMonitorPage
from app.ui.measurement_tree.model import MeasurementTreeModel
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


@pytest.mark.parametrize("kind", ["acquire_spectrum", "acquire_reference"])
def test_live_acquisition_mode_is_visible_and_round_trips_in_the_sweep_editor(application, kind):
    fields = {"average_count": 32, "acquisition_mode": "continuous_live"}
    dialog = AnritsuAcquisitionEditorDialog(RecipeNode("live", kind, fields))
    managed = AnritsuNodeEditorDialog(simulation_settings())
    try:
        dialog.resize(600, 560)
        dialog.show()
        application.processEvents()
        contained(dialog, dialog.acquisition_mode)
        contained(dialog, dialog.apply_button)
        assert dialog.node_fields()["acquisition_mode"] == "continuous_live"
        assert dialog.node_fields()["average_count"] == 32
        managed.node_role.setCurrentIndex(managed.node_role.findData(kind))
        managed.load_acquisition_options(dialog.node_fields())
        assert managed.acquisition_options()["acquisition_mode"] == "continuous_live"
        dialog.acquisition_mode.setCurrentIndex(dialog.acquisition_mode.findData("single_sweep"))
        assert "acquisition_mode" not in dialog.node_fields()
    finally:
        dialog.close()
        managed.close()
        dialog.deleteLater()
        managed.deleteLater()
        application.processEvents()


@pytest.mark.parametrize(("kind", "theme"), [
    ("acquire_spectrum", "light"),
    ("acquire_reference", "dark"),
])
def test_rendered_sweep_editor_selects_and_preserves_hardware_average(
    application, tmp_path, kind, theme,
):
    apply_application_theme(application, theme)
    fields = {"average_count": 32, "acquisition_mode": "continuous_live"}
    if kind == "acquire_reference":
        fields["purpose"] = "background"
    dialog = AnritsuAcquisitionEditorDialog(RecipeNode("capture", kind, fields))
    managed = AnritsuNodeEditorDialog(simulation_settings())
    try:
        dialog.show()
        dialog.acquisition_mode.setCurrentIndex(dialog.acquisition_mode.findData("instrument_average"))
        application.processEvents()
        assert "Lin Average" in dialog.acquisition_mode.currentText()
        assert not dialog.average_count.isVisible()
        assert not dialog.spectrum_storage.isVisible()
        assert not dialog.instrument_average_duration.isVisible()
        dialog.instrument_average_count.setValue(64)
        for width, height in ((760, 610), (600, 560)):
            dialog.resize(width, height)
            application.processEvents()
            for widget in (dialog.acquisition_mode, dialog.instrument_average_target,
                           dialog.instrument_average_count, dialog.apply_button):
                contained(dialog, widget)
            assert dialog.grab().save(str(tmp_path / f"lin-average-{theme}-{width}.png"))

        count_fields = dialog.node_fields()
        assert count_fields["instrument_average_count"] == 64
        assert count_fields["average_count"] == 1 and count_fields["store_source_spectra"] is False
        assert "instrument_average_duration" not in count_fields
        if kind == "acquire_reference":
            assert count_fields["purpose"] == "background"
        recipe = parse_recipe_text(yaml.safe_dump({
            "schema_version": 1, "name": "count-average",
            "root": {"id": "capture", "type": kind, **count_fields},
        }))
        tree = normalize_recipe_tree(recipe, {})
        model = MeasurementTreeModel(tree)
        assert "64 sweeps" in model.value_for("capture")

        managed.node_role.setCurrentIndex(managed.node_role.findData(kind))
        managed.load_acquisition_options(count_fields)
        assert managed.acquisition_options()["instrument_average_count"] == 64
        assert not managed.average_count.isVisibleTo(managed)
        parent = RecipeNode("analyzer", "sequence", {"device_module": "anritsu"})
        updated = RecipePage._configured_anritsu_node(
            parent, parameter_actions=[], acquire_single=False, trace="TRAC1",
            post_configuration_operation=kind, acquisition_options=managed.acquisition_options(),
        )
        assert updated["children"][-1]["instrument_average_count"] == 64

        dialog.instrument_average_target.setCurrentIndex(
            dialog.instrument_average_target.findData("duration")
        )
        dialog.instrument_average_duration.setText("2500 ms")
        application.processEvents()
        contained(dialog, dialog.instrument_average_duration)
        assert not dialog.instrument_average_count.isVisible()
        duration_fields = dialog.node_fields()
        assert duration_fields["instrument_average_duration"] == "2500 ms"
        assert "instrument_average_count" not in duration_fields

        reopened = AnritsuAcquisitionEditorDialog(RecipeNode("capture", kind, duration_fields))
        try:
            assert reopened.instrument_average_target.currentData() == "duration"
            assert reopened.node_fields() == duration_fields
        finally:
            reopened.deleteLater()

        dialog.acquisition_mode.setCurrentIndex(dialog.acquisition_mode.findData("continuous_live"))
        application.processEvents()
        assert "PyLab Live" in dialog.acquisition_mode.currentText()
        contained(dialog, dialog.average_count)
        assert dialog.node_fields()["average_count"] == 32
        assert not {"instrument_average_count", "instrument_average_duration"} & dialog.node_fields().keys()
    finally:
        dialog.close()
        managed.close()
        dialog.deleteLater()
        managed.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("kind", ["acquire_spectrum", "acquire_reference"])
def test_storage_choice_round_trips_independently_of_average_count(application, kind):
    node = RecipeNode("capture", kind, {"average_count": 32, "store_source_spectra": False})
    dialog = AnritsuAcquisitionEditorDialog(node)
    managed = AnritsuNodeEditorDialog(simulation_settings())
    try:
        dialog.show()
        for width, height in ((660, 610), (600, 560)):
            dialog.resize(width, height)
            application.processEvents()
            contained(dialog, dialog.spectrum_storage)
            contained(dialog, dialog.apply_button)
        assert dialog.spectrum_storage.currentData() is False
        dialog.average_count.setValue(16)
        assert dialog.node_fields()["average_count"] == 16
        assert dialog.node_fields()["store_source_spectra"] is False
        managed.node_role.setCurrentIndex(managed.node_role.findData(kind))
        managed.load_acquisition_options(dialog.node_fields())
        assert managed.acquisition_options()["store_source_spectra"] is False
        dialog.spectrum_storage.setCurrentIndex(dialog.spectrum_storage.findData(True))
        assert "store_source_spectra" not in dialog.node_fields()
    finally:
        dialog.close()
        managed.close()
        dialog.deleteLater()
        managed.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("fields", [
    {"instrument_average_count": 2.5},
    {"instrument_average_count": 10000},
    {"instrument_average_count": 5, "instrument_average_duration": "1 s"},
])
def test_editor_rejects_invalid_saved_hardware_targets_instead_of_replacing_them(application, fields):
    with pytest.raises(ConfigurationError, match="sweep count|averaging target"):
        AnritsuAcquisitionEditorDialog(RecipeNode("acquisition", "acquire_spectrum", fields))


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("reference", [False, True])
def test_shown_editor_geometry_and_roundtrip(application, tmp_path, theme, reference):
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
            assert dialog.grab().save(
                str(tmp_path / f"{'reference' if reference else 'spectrum'}-{theme}-{width}.png")
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


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_timed_background_editor_preserves_duration_and_purpose(application, tmp_path, theme):
    apply_application_theme(application, theme)
    fields = {"average_count": 30, "minimum_duration": "30 s", "purpose": "background"}
    dialog = AnritsuAcquisitionEditorDialog(RecipeNode("bg", "acquire_reference", fields))
    try:
        dialog.resize(760, 800)
        dialog.show()
        application.processEvents()
        for widget in (dialog.minimum_duration, dialog.reference_purpose, dialog.average_count, dialog.apply_button):
            contained(dialog, widget)
        values = dialog.node_fields()
        assert values["minimum_duration"] == "30 s" and values["purpose"] == "background"
        assert values["average_count"] == 30
        assert dialog.grab().save(str(tmp_path / f"background-editor-{theme}.png"))
        managed = AnritsuNodeEditorDialog(simulation_settings())
        try:
            managed.node_role.setCurrentIndex(managed.node_role.findData("acquire_reference"))
            managed.load_acquisition_options(values)
            assert managed.acquisition_options()["minimum_duration"] == "30 s"
            assert managed.acquisition_options()["purpose"] == "background"
        finally:
            managed.close()
            managed.deleteLater()
    finally:
        dialog.close()
        dialog.deleteLater()
        application.processEvents()


def test_import_replaces_hidden_acquisition_settings_and_retains_the_local_draft(application):
    node = RecipeNode("reference", "acquire_reference", {
        "acquisition_mode": "continuous_live",
        "average_count": 24,
        "minimum_duration": "500 ms",
        "inter_sweep_delay": "250 ms",
        "store_source_spectra": False,
        "purpose": "background",
    })
    dialog = AnritsuAcquisitionEditorDialog(node)
    try:
        dialog.resize(760, 640)
        dialog.show()
        source = dialog.processing_options.source
        source.setCurrentIndex(source.findData("background"))
        dialog.processing_options.path.setText("background.h5")
        application.processEvents()
        contained(dialog, dialog.processing_options.path)
        contained(dialog, dialog.apply_button)
        assert not dialog.minimum_duration.isVisible()
        assert not dialog.inter_sweep_delay.isVisible()
        assert dialog.node_fields() == {
            "purpose": "background", "source_file": "background.h5", "file_kind": "background",
        }
        source.setCurrentIndex(source.findData("acquire"))
        application.processEvents()
        contained(dialog, dialog.minimum_duration)
        fields = dialog.node_fields()
        assert fields == {**node.data, "trace": "TRAC1"}
    finally:
        dialog.close()
        dialog.deleteLater()
        application.processEvents()


def test_managed_editor_opens_shared_options_and_rebuild_preserves_them(application, tmp_path):
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
        assert dialog.grab().save(str(tmp_path / "managed-analyzer.png"))
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


def test_managed_reopen_keeps_legacy_acquisition_id_order_and_storage_choice():
    recipe = parse_recipe_text("""
schema_version: 1
name: managed-roundtrip
root:
  id: analyzer
  type: sequence
  device_module: anritsu
  post_configuration_operation: acquire_spectrum
  children:
    - {id: capture, type: acquire_spectrum, instrument_average_count: 64, reference_operation: difference_db, store_processed: false}
    - {id: settle, type: wait, duration: '250 ms'}
""")
    updated = RecipePage._configured_anritsu_node(
        recipe.root, parameter_actions=[], acquire_single=False, trace="TRAC1",
        post_configuration_operation="acquire_spectrum", acquisition_reference_operation="difference_db",
    )
    assert updated["managed_acquisition_id"] == "capture"
    assert [child["id"] for child in updated["children"]] == ["capture", "settle"]
    capture = updated["children"][0]
    assert capture["instrument_average_count"] == 64
    assert capture["store_processed"] is False


def test_sweep_tree_names_pylab_triggering_and_saved_sources():
    recipe = parse_recipe_text("""
schema_version: 1
name: acquisition-labels
root:
  id: root
  type: sequence
  children:
    - {id: load, type: acquire_reference, source_file: background.h5, file_kind: background}
    - {id: pylab, type: acquire_spectrum, average_count: 3}
""")
    tree = normalize_recipe_tree(recipe, {})
    assert tree.by_id["load"].label == "Load saved background"
    assert tree.by_id["pylab"].label == "Acquire spectrum · PyLab triggered sweeps"


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


def test_execution_preview_prefers_raw_minus_reference_and_falls_back_to_current_raw(application):
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
        corrected = {**source, "raw_minus_reference_db": (-2.0, 0.0, 3.0)}
        page.update_spectrum_preview(corrected)
        assert page.spectrum_preview._y_unit == "dB"
        curve = page.spectrum_preview._curves["Raw − Reference"]
        np.testing.assert_array_equal(curve.getData()[1], corrected["raw_minus_reference_db"])
        page.update_spectrum_preview(corrected)
        assert page.spectrum_preview._curves["Raw − Reference"] is curve
        page.update_spectrum_preview(source)
        assert page.spectrum_preview._y_unit == "dBm"
        np.testing.assert_array_equal(page.spectrum_preview._traces["Stored spectrum"][1], source["power_dbm"])
        page.update_spectrum_preview({**source, "preview_kind": "reference"})
        assert page.spectrum_preview._y_unit == "dBm"
        assert list(page.spectrum_preview._traces) == ["Stored reference"]
    finally:
        page.close()
        page.deleteLater()
        application.processEvents()


def test_execution_preview_identifies_the_anritsu_mean_and_count(application):
    page = RunMonitorPage()
    try:
        page.resize(1280, 800)
        page.show()
        application.processEvents()
        preview = {
            "frequency_hz": (1e6, 2e6, 3e6),
            "power_dbm": (-60.0, -50.0, -40.0),
            "raw_minus_reference_db": (-2.0, 0.0, 3.0),
            "instrument_average": {"sweep_count": 50},
        }
        page.update_spectrum_preview(preview)
        contained(page, page.spectrum_preview)
        assert "Anritsu average − Reference" in page.spectrum_preview._traces
        assert "50 sweeps" in page.spectrum_preview.plot.plotItem.titleLabel.text
        page.update_spectrum_preview({**preview, "preview_kind": "reference"})
        assert "Anritsu reference average" in page.spectrum_preview._traces
        assert "50 sweeps" in page.spectrum_preview.plot.plotItem.titleLabel.text
    finally:
        page.close()
        page.deleteLater()
        application.processEvents()


def test_execution_spectrum_fits_new_frames_and_resumes_after_manual_zoom(application):
    page = RunMonitorPage()
    try:
        page.resize(1280, 800)
        page.show()
        application.processEvents()
        plot = page.spectrum_preview
        view = plot.plot.getViewBox()

        def show(frequencies, powers, **preview):
            page.update_spectrum_preview({"frequency_hz": frequencies, "power_dbm": powers, **preview})
            application.processEvents()

        show((100e6, 101e6, 102e6), (-85.0, -60.0, -80.0))
        view.setRange(xRange=(100.25e6, 101.75e6), yRange=(-75, -65), padding=0)
        view.sigRangeChangedManually.emit([True, True])
        zoom = view.viewRange()
        show((200e6, 201e6, 202e6), (-120.0, -90.0, -115.0))
        assert view.viewRange() == zoom

        plot.toolbar_buttons[0].click()
        show((4e9, 5e9, 6e9), (-145.0, -110.0, -130.0))
        x_range, y_range = view.viewRange()
        assert x_range[0] < 4e9 and x_range[1] > 6e9 and x_range[1] - x_range[0] < 3e9
        assert y_range[0] < -145.0 and y_range[1] > -110.0 and y_range[1] - y_range[0] < 50

        show((4e9, 5e9, 6e9), (-145.0, -110.0, -130.0), raw_minus_reference_db=(0.0, 0.0, 0.0))
        assert plot._y_unit == "dB"
        assert view.viewRange()[1][0] < 0 < view.viewRange()[1][1]
        assert view.viewRange()[1][1] - view.viewRange()[1][0] < 5

        view.setRange(xRange=(2e6, 3e6), yRange=(-10, 10), padding=0)
        view.sigRangeChangedManually.emit([True, True])
        page.run_started(1)
        show((4e9, 5e9, 6e9), (-70.0, -65.0, -70.0))
        x_range, y_range = view.viewRange()
        assert x_range[0] < 4e9 and x_range[1] > 6e9
        assert y_range[0] < -70.0 and y_range[1] > -65.0
    finally:
        page.close()
        page.deleteLater()
        application.processEvents()


def test_execution_spectrogram_loads_every_stored_spectrum_by_number(application, tmp_path):
    from datetime import UTC, datetime, timedelta

    from PySide6.QtCore import QEventLoop
    from PySide6.QtTest import QTest

    from app.devices.anritsu_ms2830a import SpectrumTrace
    from app.domain.models import MeasurementPoint
    from app.storage.hdf5_writer import Hdf5RunWriter

    path = tmp_path / "spectrogram.h5"
    writer = Hdf5RunWriter(path, recipe_source="", settings_source="", plan_hash="preview",
                           expected_points=4, device_idn={})
    page = RunMonitorPage()
    try:
        frequencies = (1e6, 2e6, 3e6)
        now = datetime.now(UTC)
        for index in range(4):
            point = MeasurementPoint(index, {}, {}, timestamp_utc=now + timedelta(seconds=index ** 2 * 15))
            trace = SpectrumTrace(frequencies, (-80.0 + index, -70.0 + index, -60.0 + index),
                                  point.timestamp_utc, "TRAC1")
            writer.append(point, None if index == 1 else trace)
        page.run_started(4)
        page.resize(1280, 800)
        page.show()
        application.processEvents()
        assert page.preview_tabs.currentIndex() == 0
        page.spectrogram_preview.set_archive(path, 3)
        page.preview_tabs.setCurrentIndex(1)
        for _ in range(100):
            application.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
            if page.spectrogram_preview.image.image is not None:
                break
            QTest.qWait(10)
        contained(page, page.spectrogram_preview)
        np.testing.assert_array_equal(page.spectrogram_preview.image.image, [
            [-80, -70, -60], [-78, -68, -58],
        ])
        view = page.spectrogram_preview.plot.getViewBox()
        view.setRange(xRange=(1.1e6, 1.9e6), yRange=(0.8, 1.2), padding=0)
        zoom = view.viewRange()
        page.spectrogram_preview.set_stored_points(4)
        for _ in range(100):
            application.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
            if len(page.spectrogram_preview.image.image) == 3:
                break
            QTest.qWait(10)
        np.testing.assert_array_equal(page.spectrogram_preview.image.image, [
            [-80, -70, -60], [-78, -68, -58], [-77, -67, -57],
        ])
        assert page.spectrogram_preview.plot.getAxis("left").labelText == "Spectrum number"
        assert page.spectrogram_preview.image.mapRectToParent(page.spectrogram_preview.image.boundingRect()).height() == 3
        assert view.viewRange() == zoom
        page.spectrogram_preview.fit_axes.click()
        x_range, y_range = view.viewRange()
        assert x_range[0] < 1e6 and x_range[1] > 3e6
        assert y_range[0] < 1 and y_range[1] > 3
        page.preview_tabs.setCurrentIndex(0)
        contained(page, page.spectrum_preview)
    finally:
        writer.close("completed")
        page.close()
        page.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("scan_holds_outputs", [False, True])
def test_sweep_finished_dialog_stays_inside_execute_and_offers_result_folder(
    application, monkeypatch, tmp_path, scan_holds_outputs,
):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget

    from app.domain.models import ApplicationState
    from app.engine.runner import RunResult
    from app.ui.shell.page_host import FluentPageHost

    theme = apply_application_theme(application, "dark" if scan_holds_outputs else "light")
    shell = QWidget()
    layout = QVBoxLayout(shell)
    emergency_stop = QPushButton("E-STOP", shell)
    layout.addWidget(emergency_stop)
    page = RunMonitorPage()
    host = FluentPageHost(page, shell)
    layout.addWidget(host)
    viewport = host.scroll_area.viewport()
    opened = []
    monkeypatch.setattr("app.ui.execution.page.QDesktopServices.openUrl", opened.append)
    try:
        shell.resize(1360, 880)
        shell.show()
        application.processEvents()
        page.run_started(6)
        state = ApplicationState.HOLDING if scan_holds_outputs else ApplicationState.SAFE
        result = {"result": RunResult(state, 6, 4), "path": str(tmp_path / "data.h5")}
        page.complete(result, dialog_parent=viewport)
        dialog = page._completion_dialog
        assert dialog is not None
        QTest.qWait(240)
        for width, height in ((1360, 880), (820, 720)):
            shell.resize(width, height)
            application.processEvents()
            contained(viewport, dialog.widget)
            assert shell.childAt(emergency_stop.mapTo(shell, emergency_stop.rect().center())) is emergency_stop
            surface = dialog.widget.mapTo(shell, QPoint(12, 12))
            assert shell.grab().toImage().pixelColor(surface).name() == theme.tokens.dialog_surface
        assert dialog.title.text() == "Sweep finished"
        assert "4 measurement points saved" in dialog.summary.text()
        assert ("remain active" in dialog.summary.text()) == scan_holds_outputs
        dialog.cancelButton.click()
        QTest.qWait(160)
        assert not dialog.isVisible()
        assert [url.toLocalFile() for url in opened] == [tmp_path.as_posix()]
        page.run_started(6)
        assert page._completion_dialog is None
    finally:
        shell.close()
        shell.deleteLater()
        application.processEvents()


def test_sweep_finished_dialog_does_not_show_for_an_aborted_sweep(application, tmp_path):
    from app.domain.models import ApplicationState
    from app.engine.runner import RunResult

    page = RunMonitorPage()
    try:
        page.run_started(6)
        result = RunResult(ApplicationState.SAFE, 3, 2, "The operator stopped the measurement.")
        page.complete({"result": result, "path": str(tmp_path / "aborted.h5")})
        assert page._completion_dialog is None
        assert "stopped safely" in page.completion_title.text()
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


def test_validate_groups_repeated_spectrum_messages_and_keeps_layout_compact(
    application, tmp_path, monkeypatch,
):
    import time

    from app.ui.shell.page_host import FluentPageHost

    source = """
schema_version: 1
name: preview repeated acquisition
root:
  id: measurement
  type: sequence
  children:
  - {id: reference, type: acquire_reference, average_count: 64}
  - id: repeat
    type: repeat
    count: 4949
    children:
    - {id: spectrum-live, type: acquire_spectrum, average_count: 64}
  - {id: other-spectrum, type: acquire_spectrum, average_count: 32}
"""
    monkeypatch.chdir(tmp_path)
    page = RecipePage(simulation_settings())
    host = FluentPageHost(page)
    summaries = []
    page.plan_preflight_changed.connect(lambda result: summaries.append(result) if result is not None else None)
    try:
        page._restore_tree_history_source(source, "Repeated spectrum preview")
        page._close_discard_confirmed = True
        host.resize(1360, 880)
        host.show()
        application.processEvents()
        page.compile_recipe_action.trigger()
        deadline = time.monotonic() + 20
        while not summaries and time.monotonic() < deadline:
            application.processEvents()
            time.sleep(0.01)
        assert summaries
        plan, estimate = summaries[0]
        assert plan.total_spectra == 4950
        repeated = [warning for warning in estimate.warnings if warning.startswith("spectrum-live:")]
        assert len(repeated) == 1
        assert "4,949 plan actions" in repeated[0]
        summary = page.summary.text()
        assert summary.count("spectrum-live:") == 1
        assert "other-spectrum: averages 32 complete spectra" in summary
        assert "Large run" in summary
        for width, height in ((1360, 880), (900, 730)):
            host.resize(width, height)
            application.processEvents()
            host.scroll_area.ensureWidgetVisible(page.summary)
            application.processEvents()
            assert page.summary.isVisibleTo(host)
            assert page.summary.height() >= page.summary.heightForWidth(page.summary.width())
            assert page.summary.height() <= 275
            assert page.run_button.isEnabled()
    finally:
        page.cancel_preflight()
        host.close()
        host.deleteLater()
        application.processEvents()


def test_sweeps_page_shows_processing_recipe_at_desktop_and_narrow_sizes(
    application, tmp_path, monkeypatch
):
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
            assert page.grab().save(str(tmp_path / f"sweeps-{theme}-{width}.png"))
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
