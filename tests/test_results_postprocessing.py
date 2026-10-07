"""Full-grid, read-only DSP parity and rendered Results interactions."""

import hashlib
import os
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QLayout
from qfluentwidgets import Theme

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.spectrum.analysis import clean_spectrum_pipeline
from app.spectrum.processing import apply_reference_operation
from app.storage import Hdf5RunReader, Hdf5RunWriter, ThatecRunReader
from app.storage.hdf5_reader import iter_recipe_spectrum_sweeps
from app.ui.design_system import apply_application_theme
from app.ui.results.data_classifier import find_heatmap_rows
from app.ui.results.heatmap_coordinates import HeatmapRequest, build_heatmap_coordinates
from app.ui.results.heatmap_tab import _read_heatmap_payload
from app.ui.results.page import ResultsPage
from app.ui.results.processing import (
    ResultProcessing,
    ResultSpectrumProcessor,
    read_processed_private,
)
from app.ui.results.processing_controls import ResultProcessingControls
from tests.test_recipe_raw_sweeps import SOURCE, execute


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "postprocessing.h5"
    frequencies = tuple(np.linspace(1e6, 2e6, 5001))

    def trace(values):
        return SpectrumTrace(frequencies, tuple(values), datetime.now(UTC), "TRAC1")

    writer = Hdf5RunWriter(
        path,
        recipe_source="name: postprocessing\n",
        settings_source="schema_version: 1\n",
        plan_hash="test",
        device_idn={},
        expected_points=2,
    )
    writer.store_reference(
        trace(np.full(5001, -60.0)),
        acquisition_metadata={"purpose": "reference", "configuration_fingerprint": "settings"},
    )
    writer.store_reference(
        trace(np.full(5001, -70.0)),
        acquisition_metadata={"purpose": "background", "configuration_fingerprint": "settings"},
    )
    for index in range(2):
        values = -65 + 12 * np.sin(np.linspace(0, 12, 5001)) + index
        values[2200] += 25
        writer.append(
            MeasurementPoint(
                index=index,
                setpoints={"keithley.B.current": index * 0.001},
                measurements={},
                metadata={
                    "spectrum_processing_v1": {"configuration_fingerprint": "settings"},
                },
            ),
            trace(values),
        )
    writer.close("completed")
    return path


@pytest.mark.parametrize(
    "operation,index,unit",
    [
        ("none", None, "dBm"),
        ("difference_db", 0, "dB"),
        ("subtract_power_signed", 1, "W"),
        ("subtract_reference_signed", 0, "W"),
        ("ratio_linear", 0, "ratio"),
        ("add_power", 0, "dBm"),
        ("multiply_linear", 0, "mW²"),
    ],
)
def test_processing_matches_live_math_and_filters_without_mutating_archive(
    archive, operation, index, unit
):
    before = hashlib.sha256(archive.read_bytes()).hexdigest()
    points = Hdf5RunReader.points(archive)
    raw = Hdf5RunReader.spectrum(archive, 0)
    state = ResultProcessing(operation, index, ("narrow_reject", "denoise"))
    values = raw.powers_dbm
    if operation != "none":
        values, _ = apply_reference_operation(
            values, Hdf5RunReader.reference(archive, index).powers_dbm, state.math_operation
        )
    expected = clean_spectrum_pipeline(
        values,
        unit=unit,
        modes=state.modes,
        parameters=state.parameters,
        frequencies_hz=raw.frequencies_hz,
    )
    derived = read_processed_private(archive, points[0], state)
    assert len(derived.values) == 5001
    assert derived.unit == unit
    np.testing.assert_allclose(derived.values, expected.values)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("transpose", [False, True])
def test_heatmap_processes_full_spectra_before_frequency_crop(archive, transpose):
    run = ThatecRunReader.describe(archive)
    points = Hdf5RunReader.points(archive)
    row = find_heatmap_rows(run)[0]
    coords = build_heatmap_coordinates(archive, run, row, points)
    sweep = coords.dimensions[1].id
    request = HeatmapRequest(
        sweep if transpose else "frequency",
        "frequency" if transpose else sweep,
        {"frequency": (1.3e6, 1.6e6)},
    )
    state = ResultProcessing("subtract_power_signed", 1, ("denoise",))
    payload = _read_heatmap_payload(archive, row, coords, request, processing=state, points=points)
    derived = read_processed_private(archive, points[0], state)
    mask = (np.asarray(derived.frequencies_hz) >= 1.3e6) & (
        np.asarray(derived.frequencies_hz) <= 1.6e6
    )
    np.testing.assert_allclose(
        payload.matrix[:, 0] if transpose else payload.matrix[0], np.asarray(derived.values)[mask]
    )
    assert payload.z_unit == "W"
    assert np.any(payload.matrix < 0)


def test_positive_only_subtraction_preserves_undefined_bins_and_notes(archive):
    point = Hdf5RunReader.points(archive)[0]
    derived = read_processed_private(
        archive, point, ResultProcessing("subtract_power", 0, ("denoise",))
    )
    assert np.any(np.isnan(derived.values))
    assert np.any(np.isfinite(derived.values))
    assert any("undefined" in note for note in derived.notes)


def test_invalid_baseline_and_missing_temporal_sources_are_explicit(archive):
    point = Hdf5RunReader.points(archive)[0]
    with pytest.raises(ValueError, match="unavailable"):
        read_processed_private(archive, point, ResultProcessing("difference_db", 100))
    state = ResultProcessing(
        parameters=replace(ResultProcessing().parameters, temporal_average_frames=4)
    )
    with pytest.raises(ValueError, match="individual sweeps"):
        read_processed_private(archive, point, state)
    mismatched = replace(
        point, metadata={"spectrum_processing_v1": {"configuration_fingerprint": "different"}}
    )
    with pytest.raises(ValueError, match="settings differ"):
        read_processed_private(archive, mismatched, ResultProcessing("difference_db", 0))


def test_raw_without_link_automatically_resolves_unique_baseline_by_purpose(archive):
    point = Hdf5RunReader.points(archive)[0]
    # This is the shape of the recorded one-RAW/background/reference sweep:
    # the baseline records exist, but RAW carries no processing reference link.
    raw = Hdf5RunReader.spectrum(archive, 0)
    assert raw.reference_index is None
    reference = read_processed_private(archive, point, ResultProcessing("difference_db"))
    background = read_processed_private(archive, point, ResultProcessing("subtract_power_signed"))
    expected_reference = read_processed_private(
        archive, point, ResultProcessing("difference_db", 0)
    )
    expected_background = read_processed_private(
        archive, point, ResultProcessing("subtract_power_signed", 1)
    )
    np.testing.assert_allclose(reference.values, expected_reference.values)
    np.testing.assert_allclose(background.values, expected_background.values)
    assert "reference 0" in reference.method
    assert "background 1" in background.method


@pytest.mark.parametrize("linked_index", [None, 0, 1])
def test_signed_reference_subtraction_selects_reference_even_when_linked_to_background(
    archive, monkeypatch, linked_index
):
    before = hashlib.sha256(archive.read_bytes()).hexdigest()
    point = Hdf5RunReader.points(archive)[0]
    read_spectrum = Hdf5RunReader.spectrum

    def linked_spectrum(*args, **kwargs):
        trace = read_spectrum(*args, **kwargs)
        return replace(trace, reference_index=linked_index)

    monkeypatch.setattr(Hdf5RunReader, "spectrum", linked_spectrum)
    reference = read_processed_private(archive, point, ResultProcessing("subtract_reference_signed"))
    background = read_processed_private(archive, point, ResultProcessing("subtract_power_signed"))
    raw = read_spectrum(archive, 0)
    expected, unit = apply_reference_operation(
        raw.powers_dbm, Hdf5RunReader.reference(archive, 0).powers_dbm, "subtract_power_signed"
    )
    np.testing.assert_allclose(reference.values, expected)
    assert reference.unit == unit == "W"
    assert np.any(np.asarray(reference.values) < 0)
    assert "reference 0" in reference.method
    assert "background 1" in background.method
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == before


def test_reference_subtraction_never_substitutes_background(archive, monkeypatch):
    point = Hdf5RunReader.points(archive)[0]
    with pytest.raises(ValueError, match="requires a recorded reference"):
        read_processed_private(archive, point, ResultProcessing("subtract_reference_signed", 1))
    catalogue = Hdf5RunReader.references(archive, metadata_only=True)
    monkeypatch.setattr(Hdf5RunReader, "references", lambda *args, **kwargs: catalogue[1:])
    with pytest.raises(ValueError, match="No unique recorded reference"):
        read_processed_private(archive, point, ResultProcessing("subtract_reference_signed"))


def test_switching_baseline_purpose_clears_previous_explicit_selection(archive):
    app = QApplication.instance() or QApplication([])
    controls = ResultProcessingControls()
    controls.set_references(Hdf5RunReader.references(archive, metadata_only=True))
    try:
        controls.set_state(ResultProcessing("subtract_power_signed", 1))
        controls.operation.setCurrentIndex(controls.operation.findData("subtract_reference_signed"))
        assert controls.state.reference_index is None
        assert controls.reference.currentText() == "Automatic reference"
        controls.reference.setCurrentIndex(controls.reference.findData(0))
        controls.operation.setCurrentIndex(controls.operation.findData("subtract_power_signed"))
        assert controls.state.reference_index is None
        assert controls.reference.currentText() == "Automatic background"
    finally:
        controls.deleteLater()
        app.processEvents()


def test_emi_uses_only_recorded_same_point_sweeps(tmp_path):
    path = tmp_path / "sources.h5"
    result, _ = execute(path, source=SOURCE.replace("average_count: 2", "average_count: 5"))
    assert result.error is None
    point = Hdf5RunReader.points(path)[0]
    state = ResultProcessing("difference_db", 0, ("emi_reject",))
    derived = read_processed_private(path, point, state)
    assert not any("needs history" in note for note in derived.notes)
    raw = Hdf5RunReader.spectrum(path, 0)
    no_sources = replace(point, metadata={})
    missing = ResultSpectrumProcessor(path, state, (no_sources,)).process(
        0, raw.frequencies_hz, raw.powers_dbm
    )
    assert any("0/5" in note for note in missing.notes)

    averaged_state = replace(
        state, modes=(), parameters=replace(state.parameters, temporal_average_frames=4)
    )
    averaged = read_processed_private(path, point, averaged_state)
    indices = set(point.metadata["raw_recipe_sweep_indices"][-4:])
    sources = tuple(iter_recipe_spectrum_sweeps(path, selected_indices=indices))
    assert {ordinal for ordinal, _, _ in sources} == indices
    watts = np.mean([10 ** ((record.powers_dbm - 30) / 10) for _, _, record in sources], axis=0)
    mean_dbm = 10 * np.log10(watts) + 30
    expected, _ = apply_reference_operation(
        mean_dbm, Hdf5RunReader.reference(path, 0).powers_dbm, "difference_db"
    )
    np.testing.assert_allclose(averaged.values, expected)
    assert "4/4" in averaged.method


def _wait(app, predicate):
    deadline = time.monotonic() + 15
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert predicate()


@pytest.mark.parametrize(
    "theme,width", [(Theme.LIGHT, 1440), (Theme.DARK, 1440), (Theme.LIGHT, 760)]
)
@pytest.mark.parametrize("operation,baseline", [
    ("subtract_power_signed", 1), ("subtract_reference_signed", 0),
])
def test_rendered_controls_sync_between_1d_and_heatmap_and_reset(
    archive, tmp_path, theme, width, operation, baseline
):
    app = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Segoe UI", 10))
    apply_application_theme(app, "dark" if theme == Theme.DARK else "light")
    page = ResultsPage(str(archive.parent))
    page.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
    page.resize(width, 1000)
    page.show()
    page.runs.setCurrentItem(page.runs.topLevelItem(0))
    try:
        _wait(app, lambda: page.spectrum_tab.points.model().rowCount() == 2)
        page.result_tabs.setCurrentIndex(page._spectrum_index)
        page.spectrum_tab.show_stored_spectrum(0)
        app.processEvents()
        controls = page.spectrum_tab.processing_controls
        assert controls.reference.count() == 3
        controls.reference.setCurrentIndex(controls.reference.findData(baseline))
        controls.operation.setCurrentIndex(controls.operation.findData(operation))
        controls.filters["denoise"].setChecked(True)
        _wait(
            app,
            lambda: (
                "Post-processed spectrum" in page.spectrum_tab.spectrum_plot._traces
                and not page.spectrum_tab._read_tasks
                and not page.heatmap_tab._read_tasks
            ),
        )
        assert page.heatmap_tab.processing_controls.state == controls.state
        assert controls.state.operation == operation
        assert "reference" in controls.operation.currentText() if baseline == 0 else "background" in controls.operation.currentText()
        spectrum_values = page.spectrum_tab.spectrum_plot._traces["Post-processed spectrum"][1]
        np.testing.assert_allclose(page.heatmap_tab.heatmap._data[0], spectrum_values)
        assert "(W)" in page.heatmap_tab.heatmap.color_bar.getAxis("right").label.toPlainText()
        assert not page.heatmap_tab.heatmap.color_bar.getAxis("left").label.toPlainText()
        assert page.width() == width
        assert controls.isVisible() and controls.width() > 200
        for child in (
            controls.operation,
            controls.reference,
            controls.settings,
            *controls.filters.values(),
        ):
            assert child.isVisible() and child.height() > 0
            assert controls.rect().contains(child.mapTo(controls, child.rect().bottomRight()))
        assert page.spectrum_tab.spectrum_plot.height() >= 150
        page.spectrum_tab.spectrum_plot.apply_theme("dark" if theme == Theme.DARK else "light")
        page.heatmap_tab.apply_theme("dark" if theme == Theme.DARK else "light")
        page.grab().save(str(tmp_path / f"results-1d-{theme.value}-{width}.png"))
        page.result_tabs.setCurrentIndex(page._heatmap_index)
        app.processEvents()
        assert page.heatmap_tab.processing_controls.isVisible()
        assert page.heatmap_tab.heatmap.isVisible() and page.heatmap_tab.heatmap.height() > 150
        page.grab().save(str(tmp_path / f"results-heatmap-{theme.value}-{width}.png"))
        page.heatmap_tab.processing_controls.reset.click()
        _wait(app, lambda: not page.spectrum_tab._read_tasks and not page.heatmap_tab._read_tasks)
        assert not controls.state.active
        assert "Stored spectrum" in page.spectrum_tab.spectrum_plot._traces
    finally:
        page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()
        apply_application_theme(app, "light")
