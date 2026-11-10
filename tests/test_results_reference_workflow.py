"""The user's Spectrum -> Heatmap clicks must retain one reference calculation."""

import hashlib
import os
from datetime import UTC, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.domain.recipe_spectrum import RecipeSpectrumSweep
from app.domain.spectrum_correction import SpectrumFrameRole, SweepEvidence
from app.spectrum.processing import average_dbm_traces
from app.storage import Hdf5RunWriter
from app.ui.results.page import ResultsPage
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.fixture
def reference_page(tmp_path, shell_qt_application):
    path = tmp_path / "reference-workflow.h5"
    frequencies = tuple(np.linspace(1e6, 2e6, 101))
    now = datetime.now(UTC)
    writer = Hdf5RunWriter(path, recipe_source="name: reference workflow\n",
        settings_source="schema_version: 1\n", plan_hash="test", device_idn={}, expected_points=3)
    for purpose, level in (("background", -70.), ("reference", -62.)):
        rows = tuple((level + offset,) * 101 for offset in (-1., 1.))
        indices = []
        for ordinal, values in enumerate(rows):
            indices.append(writer.store_recipe_spectrum_sweep(RecipeSpectrumSweep(
                frequencies_hz=frequencies, powers_dbm=values, recipe_node_id=purpose,
                execution_id="reference-test", role=SpectrumFrameRole.REFERENCE,
                average_index=ordinal, average_count=2, acquired_at_s=now.timestamp()+ordinal,
                configuration_generation=1, evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)))
        writer.store_reference(SpectrumTrace(frequencies, average_dbm_traces(rows), now, "TRAC1"),
            kind="averaged", average_count=2, source_sweep_indices=tuple(indices),
            acquisition_metadata={"purpose": purpose, "configuration_fingerprint": "same",
                                  "recipe_node_id": purpose, "configuration_generation": 1})
    for index in range(3):
        values = -60 + index + np.sin(np.linspace(0, 10, 101))
        values[50] += 5
        writer.append(MeasurementPoint(index=index, setpoints={"keithley.A.current": .001*(index+1)},
            measurements={}, metadata={"spectrum_processing_v1": {"configuration_fingerprint": "same"}}),
            SpectrumTrace(frequencies, tuple(values), now, "TRAC1"))
    writer.close("completed")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    app = shell_qt_application
    page = ResultsPage(str(tmp_path))
    page.resize(1360, 900)
    page.show()
    wait_until(app, lambda: page.file_browser._refresh_task is None)
    page.runs.setCurrentItem(page.runs.topLevelItem(0))
    wait_until(app, lambda: page.spectrum_tab.points.model().rowCount() == 3)
    page.result_tabs.setCurrentIndex(page._spectrum_index)
    page.spectrum_tab.show_stored_spectrum(0)
    wait_until(app, lambda: not page.spectrum_tab._read_tasks and not page.heatmap_tab._read_tasks)
    try:
        yield app, page
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    finally:
        assert page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_spectrum_reference_checkbox_selects_same_heatmap_math(reference_page):
    app, page = reference_page
    view = page.spectrum_tab.view_controls
    view.boxes["show_reference"].click()
    view.baselines["reference"].setCurrentIndex(view.baselines["reference"].findData(1))
    assert page.heatmap_tab.processing_controls.state.operation == "difference_db"
    assert page.heatmap_tab.processing_controls.state.reference_index == 1
    wait_until(app, lambda: not page.spectrum_tab._read_tasks and not page.heatmap_tab._read_tasks)
    expected = page.spectrum_tab.spectrum_plot._traces["Raw − reference"][1].copy()
    assert page.spectrum_tab.spectrum_plot._y_unit == "dB"
    page.result_tabs.setCurrentIndex(page._heatmap_index)
    wait_until(app, lambda: page.heatmap_tab.heatmap._data is not None and not page.heatmap_tab._read_tasks)
    np.testing.assert_array_equal(page.heatmap_tab.heatmap._data[0], expected)
    assert page.heatmap_tab._source_payload.z_unit == "dB"


@pytest.mark.parametrize("denoise", [False, True])
def test_selecting_heatmap_signed_w_updates_spectrum_reference_choice(reference_page, denoise):
    app, page = reference_page
    view = page.spectrum_tab.view_controls
    view.boxes["show_reference"].click()
    view.baselines["reference"].setCurrentIndex(view.baselines["reference"].findData(1))
    page.result_tabs.setCurrentIndex(page._heatmap_index)
    controls = page.heatmap_tab.processing_controls
    controls.operation.setCurrentIndex(controls.operation.findData("subtract_reference_signed"))
    controls.reference.setCurrentIndex(controls.reference.findData(1))
    controls.filters["denoise"].setChecked(denoise)
    assert view.state.reference_operation == "subtract_reference_signed"
    assert view.state.reference_index == 1
    wait_until(app, lambda: page.heatmap_tab.heatmap._data is not None and not page.heatmap_tab._read_tasks
               and not page.spectrum_tab._read_tasks)
    expected = page.heatmap_tab.heatmap._data[0].copy()
    page.result_tabs.setCurrentIndex(page._spectrum_index)
    wait_until(app, lambda: not page.spectrum_tab._read_tasks)
    assert page.spectrum_tab.spectrum_plot._y_unit == "W"
    np.testing.assert_array_equal(page.spectrum_tab.spectrum_plot._traces["Raw − reference"][1], expected)


def test_turning_reference_off_restores_raw_heatmap_processing(reference_page):
    _app, page = reference_page
    view = page.spectrum_tab.view_controls
    view.boxes["show_reference"].click()
    assert page.heatmap_tab.processing_controls.state.operation == "difference_db"
    view.boxes["show_reference"].click()
    assert page.heatmap_tab.processing_controls.state.operation == "none"


def test_heatmap_reference_choice_is_retained_when_enabling_spectrum_compare(reference_page):
    _app, page = reference_page
    controls = page.heatmap_tab.processing_controls
    controls.operation.setCurrentIndex(controls.operation.findData("subtract_reference_signed"))
    controls.reference.setCurrentIndex(controls.reference.findData(1))
    view = page.spectrum_tab.view_controls
    assert not view.state.show_reference
    assert view.state.reference_operation == "subtract_reference_signed"
    assert view.state.reference_index == 1
    view.boxes["show_reference"].click()
    assert page.heatmap_tab.processing_controls.state.operation == "subtract_reference_signed"
    assert page.heatmap_tab.processing_controls.state.reference_index == 1


def test_reference_repeat_is_shared_in_both_directions(reference_page):
    app, page = reference_page
    view = page.spectrum_tab.view_controls
    view.boxes["show_reference"].click()
    view.baselines["reference"].setCurrentIndex(view.baselines["reference"].findData(1))
    view.samples["reference"].setCurrentIndex(view.samples["reference"].findData(1))
    assert page.heatmap_tab.processing_controls.state.reference_sweep == 1
    page.result_tabs.setCurrentIndex(page._heatmap_index)
    wait_until(app, lambda: page.heatmap_tab.heatmap._data is not None and not page.heatmap_tab._read_tasks
               and not page.spectrum_tab._read_tasks)
    first = page.heatmap_tab.heatmap._data[0].copy()
    np.testing.assert_array_equal(first, page.spectrum_tab.spectrum_plot._traces["Raw − reference"][1])
    controls = page.heatmap_tab.processing_controls
    controls.reference_sample.setCurrentIndex(controls.reference_sample.findData(0))
    assert view.state.reference_sweep == 0
    wait_until(app, lambda: not page.heatmap_tab._read_tasks and not page.spectrum_tab._read_tasks)
    changed = page.heatmap_tab.heatmap._data[0]
    assert not np.array_equal(first, changed)
    np.testing.assert_array_equal(changed, page.spectrum_tab.spectrum_plot._traces["Raw − reference"][1])


@pytest.mark.parametrize("operation", ["none", "subtract_power_signed"])
def test_changing_shared_correction_keeps_unrelated_compare_choices(reference_page, operation):
    _app, page = reference_page
    view = page.spectrum_tab.view_controls
    view.reference_operation.setCurrentIndex(view.reference_operation.findData("subtract_reference_signed"))
    view.boxes["show_raw"].click()
    view.boxes["show_background"].click()
    view.boxes["show_reference"].click()
    view.baselines["background"].setCurrentIndex(view.baselines["background"].findData(0))
    page.heatmap_tab.processing_controls.operation.setCurrentIndex(
        page.heatmap_tab.processing_controls.operation.findData(operation))
    assert not view.state.show_reference
    assert view.state.show_raw and view.state.show_background
    assert view.state.background_index == 0
