"""Recorded means/repeats remain explicit, physically correct and read-only."""

from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

import h5py
import numpy as np
import pytest
from PySide6.QtCore import QThread
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.domain.recipe_spectrum import RecipeSpectrumSweep
from app.domain.spectrum_correction import SpectrumFrameRole, SweepEvidence
from app.storage import Hdf5RunReader, Hdf5RunWriter, ThatecRunReader, ThatecCompatibilityValidator
from app.ui.results.processing import ResultProcessing, read_processed_private
from app.ui.results.read_session import ResultReadSession
from app.ui.results.spectrum_views import SpectrumView, read_private_view
from app.ui.results.baseline_controls import BaselineSampleCombo
from app.ui.results.heatmap_tab import _read_heatmap_payload
from app.ui.results.heatmap_coordinates import HeatmapRequest, build_heatmap_coordinates
from app.ui.shell import MainWindow
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence  # noqa: PLC0414
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_results_series_navigation import load_tab, close_tab
from tests.test_spectrum_correction_controller import wait_until


@pytest.fixture(params=[False, True], ids=["counted", "timed-background"])
def repeat_archive(tmp_path, request):
    path = tmp_path / "repeats.h5"
    frequencies = np.linspace(1e6, 2e6, 101)
    timestamp = datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp()
    writer = Hdf5RunWriter(path, recipe_source="name: baseline repeats\n", settings_source="schema_version: 1\n",
        plan_hash="repeat-review", device_idn={}, expected_points=5)
    for collection, purpose in enumerate(("background", "reference")):
        duration = 30. if request.param and collection == 0 else 0.
        sources, powers = [], []
        node_id = f"{purpose}-collection"
        for ordinal in range(4):
            values = -85 + collection * 20 + ordinal * 5 + np.linspace(0., 1., frequencies.size)
            if purpose == "reference":
                values += 15. * np.exp(-((frequencies - (1.2e6 + ordinal * 1.5e5)) / 4e4) ** 2)
            powers.append(values)
            record = RecipeSpectrumSweep(frequencies, values, node_id, "execution-1", SpectrumFrameRole.REFERENCE,
                ordinal, None if duration else 4, timestamp + collection * 40 + ordinal * 10 + 1,
                1, SweepEvidence.QUALIFIED_SINGLE_SWEEP, minimum_duration_s=duration)
            sources.append(writer.store_recipe_spectrum_sweep(record))
        watts = np.mean(np.power(10., (np.array(powers) - 30.) / 10.), axis=0)
        mean_dbm = 10. * np.log10(watts) + 30.
        metadata = {"purpose": purpose, "configuration_fingerprint": "settings",
                    "recipe_node_id": node_id, "configuration_generation": 1}
        if duration:
            metadata.update(minimum_duration_s=duration, collection_elapsed_s=31., requested_minimum_sweeps=4)
        writer.store_reference(SpectrumTrace(tuple(frequencies), tuple(mean_dbm), datetime.fromtimestamp(timestamp + 35, UTC), "TRAC1"),
            kind="averaged", average_count=4, source_sweep_indices=tuple(sources), acquisition_metadata=metadata)
    for index in range(5):
        values = -60. + 10. * np.exp(-((frequencies - (1.45e6 + index * 2e4)) / 5e4) ** 2)
        writer.append(MeasurementPoint(index=index, setpoints={"keithley.A.current": .002 + index * .0001}, measurements={},
            metadata={"spectrum_processing_v1": {"configuration_fingerprint": "settings"}}),
            SpectrumTrace(tuple(frequencies), tuple(values), datetime.now(UTC), "TRAC1"))
    writer.close("completed")
    return path


def test_mean_and_individual_reader_preserve_source_identity_and_pythat(repeat_archive):
    before = hashlib.sha256(repeat_archive.read_bytes()).hexdigest()
    catalogue = Hdf5RunReader.references(repeat_archive, metadata_only=True)
    assert [item.source_sweep_indices for item in catalogue] == [tuple(range(4)), tuple(range(4, 8))]
    for collection in (0, 1):
        mean = Hdf5RunReader.reference(repeat_archive, collection)
        repeats = [Hdf5RunReader.reference_sweep(repeat_archive, collection, sweep) for sweep in range(4)]
        watts = np.mean([np.power(10., (np.array(item.powers_dbm) - 30.) / 10.) for item in repeats], axis=0)
        np.testing.assert_allclose(mean.powers_dbm, 10 * np.log10(watts) + 30.)
        assert not np.allclose(mean.powers_dbm, np.mean([item.powers_dbm for item in repeats], axis=0))
        for ordinal, item in enumerate(repeats):
            assert item.average_count == 1 and item.collection_average_count == 4
            assert item.selected_sweep == ordinal and item.source_sweep_indices == (collection * 4 + ordinal,)
            assert item.purpose == mean.purpose and item.configuration_fingerprint == "settings"
        with pytest.raises(ExecutionError, match="outside"):
            Hdf5RunReader.reference_sweep(repeat_archive, collection, 4)
    assert ThatecCompatibilityValidator().validate(repeat_archive, require_pythat=True).valid
    assert hashlib.sha256(repeat_archive.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("operation,collection", [("subtract_power_signed", 0), ("subtract_reference_signed", 1)])
def test_subtraction_uses_only_the_selected_mean_or_repeat_in_spectra_and_maps(repeat_archive, operation, collection):
    before = hashlib.sha256(repeat_archive.read_bytes()).hexdigest()
    points = Hdf5RunReader.points(repeat_archive, include_details=False)
    raw = Hdf5RunReader.spectrum(repeat_archive, 0)
    for sweep in (None, 0, 2, 3):
        state = ResultProcessing(operation, collection, reference_sweep=sweep)
        baseline = (Hdf5RunReader.reference(repeat_archive, collection) if sweep is None else
                    Hdf5RunReader.reference_sweep(repeat_archive, collection, sweep))
        derived = read_processed_private(repeat_archive, Hdf5RunReader.point(repeat_archive, 0), state)
        expected = (np.power(10., (np.array(raw.powers_dbm) - 30.) / 10.)
                    - np.power(10., (np.array(baseline.powers_dbm) - 30.) / 10.))
        np.testing.assert_allclose(derived.values, expected, atol=1e-24)
        assert derived.baselines[0].selected_sweep == sweep
        assert derived.baselines[0].source_sweep_indices == baseline.source_sweep_indices
        run = ThatecRunReader.describe(repeat_archive)
        row = next(row for row in run.rows.values() if len(row.shape) == 2)
        coordinates = build_heatmap_coordinates(repeat_archive, run, row, points)
        payload = _read_heatmap_payload(repeat_archive, row, coordinates,
            HeatmapRequest("frequency", "keithley.A.current", {}), processing=state, points=points)
        np.testing.assert_allclose(payload.matrix[0], expected, atol=1e-24)
        assert payload.baselines[0].selected_sweep == sweep
    assert hashlib.sha256(repeat_archive.read_bytes()).hexdigest() == before


def test_direct_baseline_overlays_and_subtraction_share_explicit_sample(repeat_archive):
    session = ResultReadSession(repeat_archive)
    point = session.point(0)
    view = SpectrumView(show_analysis=False, show_background_spectrum=True, show_reference_spectrum=True,
        background_index=0, reference_index=1, background_sweep=1, reference_sweep=3)
    payload = read_private_view(session, point, ResultProcessing(), view)
    assert [trace.label for trace in payload.traces] == ["Background spectrum", "Reference spectrum"]
    for collection, sweep, trace in zip((0, 1), (1, 3), payload.traces, strict=True):
        expected = session.reference_sweep(repeat_archive, collection, sweep)
        np.testing.assert_array_equal(trace.values, expected.powers_dbm)
    payload = read_private_view(session, point, ResultProcessing(),
        replace(view, show_reference=True, show_background_spectrum=False))
    assert {trace.unit for trace in payload.traces} == {"W"}
    reference = session.reference_sweep(repeat_archive, 1, 3)
    raw = session.spectrum(repeat_archive, 0)
    baseline_w = np.power(10., (np.array(reference.powers_dbm) - 30.) / 10.)
    np.testing.assert_allclose(payload.traces[0].values,
        np.power(10., (np.array(raw.powers_dbm) - 30.) / 10.) - baseline_w)
    np.testing.assert_allclose(payload.traces[1].values, baseline_w)


def test_peak_trajectory_uses_the_chosen_reference_repeat_not_the_linked_mean(repeat_archive):
    from app.spectrum.analysis import SpectrumAnalysisParameters
    from app.ui.results.peak_tools import track_result_series
    points = Hdf5RunReader.points(repeat_archive, include_details=False)
    view = SpectrumView(show_analysis=False, show_reference_spectrum=True, reference_index=1, reference_sweep=0)
    records, unit = track_result_series(repeat_archive, points, axis="keithley.A.current",
        source=("Reference spectrum", "raw", None, 0), processing=ResultProcessing(), view=view, points=points,
        parameters=SpectrumAnalysisParameters(peak_fit_models=False), target_hz=1.2e6, gate_hz=1e5)
    assert unit == "dBm" and all(point.state == "detected" for point in records)
    assert all(abs(point.frequency_hz - 1.2e6) < 2e4 for point in records)
    assert all(abs(point.amplitude - (-49.8)) < .05 for point in records)


def test_invalid_or_missing_repeat_never_falls_back_to_the_mean(repeat_archive):
    point = Hdf5RunReader.point(repeat_archive, 0)
    for sweep in (-1, True, 1.5):
        with pytest.raises(ValueError):
            ResultProcessing("subtract_reference_signed", 1, reference_sweep=sweep)
    with pytest.raises(ExecutionError, match="outside"):
        read_processed_private(repeat_archive, point, ResultProcessing("subtract_reference_signed", 1, reference_sweep=99))
    with h5py.File(repeat_archive, "r+") as file:
        del file["recipe_raw_sweeps_v1/6"]
    with pytest.raises(ExecutionError, match="missing"):
        read_processed_private(repeat_archive, point, ResultProcessing("subtract_reference_signed", 1, reference_sweep=2))
    assert Hdf5RunReader.reference(repeat_archive, 1).average_count == 4


def test_single_repeat_reads_no_other_source_arrays(repeat_archive, monkeypatch):
    seen = []
    original = h5py.Dataset.__getitem__
    def read(dataset, item):
        if dataset.name.startswith("/recipe_raw_sweeps_v1/") and dataset.name.endswith("/power_dbm"):
            seen.append(dataset.name)
        return original(dataset, item)
    monkeypatch.setattr(h5py.Dataset, "__getitem__", read)
    Hdf5RunReader.references(repeat_archive, metadata_only=True)
    assert not seen
    Hdf5RunReader.reference_sweep(repeat_archive, 1, 2)
    assert set(seen) == {"/recipe_raw_sweeps_v1/4/power_dbm", "/recipe_raw_sweeps_v1/6/power_dbm"}


@pytest.mark.parametrize("corruption", ["power", "block", "units"])
def test_individual_reader_rejects_corrupt_payload_or_wrong_acquisition(repeat_archive, corruption):
    with h5py.File(repeat_archive, "r+") as file:
        if corruption == "power":
            file["recipe_raw_sweeps_v1/2/power_dbm"][0] += 1.
        elif corruption == "block":
            file["references/0/source_recipe_sweep_indices"][:] = [4, 5, 6, 7]
        else:
            file["recipe_raw_sweeps_v1/2/power_dbm"].attrs["unit"] = "W"
    with pytest.raises(ExecutionError, match={"power": "corrupted", "block": "acquisition block", "units": "units"}[corruption]):
        Hdf5RunReader.reference_sweep(repeat_archive, 0, 2)


def test_baseline_repeats_remain_browsable_without_measurement_checkpoints(tmp_path, shell_qt_application):
    path = tmp_path / "baseline-only.h5"
    writer = Hdf5RunWriter(path, recipe_source="name: baselines only\n", settings_source="schema_version: 1\n",
        plan_hash="baseline-only", device_idn={})
    frequencies = np.linspace(1e6, 2e6, 101)
    sources = []
    for ordinal in range(4):
        sources.append(writer.store_recipe_spectrum_sweep(RecipeSpectrumSweep(frequencies, np.full(101, -80. + ordinal),
            "reference", "baseline-only", SpectrumFrameRole.REFERENCE, ordinal, 4,
            datetime.now(UTC).timestamp(), 0, SweepEvidence.QUALIFIED_SINGLE_SWEEP)))
    mean = 10. * np.log10(np.mean(np.power(10., (np.arange(4) - 110.) / 10.))) + 30.
    writer.store_reference(SpectrumTrace(tuple(frequencies), (float(mean),) * 101, datetime.now(UTC), "TRAC1"),
        kind="averaged", average_count=4, source_sweep_indices=tuple(sources))
    writer.close("faulted")
    tab = load_tab(shell_qt_application, path)
    try:
        assert tab.points_model.rowCount() == 0
        tab.baseline_controls.select(0, 2)
        tab.baseline_controls.show_button.click()
        wait_until(shell_qt_application, lambda: not tab._read_tasks)
        assert tab._selected_reference.selected_sweep == 2
        np.testing.assert_array_equal(tab.spectrum_plot._traces["Reference spectrum"][1], np.full(101, -78.))
    finally:
        close_tab(shell_qt_application, tab)


def test_legacy_mean_has_no_fabricated_repeat_choices(tmp_path, shell_qt_application):
    path = tmp_path / "legacy.h5"
    writer = Hdf5RunWriter(path, recipe_source="name: old\n", settings_source="schema_version: 1\n", plan_hash="old", device_idn={})
    writer.store_reference(SpectrumTrace((1e6, 2e6), (-70., -70.), datetime.now(UTC), "TRAC1"), kind="averaged", average_count=4)
    writer.close("completed")
    summary = Hdf5RunReader.references(path, metadata_only=True)[0]
    control = BaselineSampleCombo()
    control.set_reference(summary)
    assert control.count() == 1 and control.currentData() is None
    assert "4 sweep" in control.currentText()
    with pytest.raises(ExecutionError, match="not stored"):
        Hdf5RunReader.reference_sweep(path, 0, 0)
    control.deleteLater()
    shell_qt_application.processEvents()


def test_viewing_samples_preserves_checkpoint_subtraction_and_exports_choices(
    repeat_archive, shell_qt_application, monkeypatch, tmp_path
):
    app = shell_qt_application
    before = hashlib.sha256(repeat_archive.read_bytes()).hexdigest()
    original = Hdf5RunReader.reference_sweep
    def read(*args, **kwargs):
        assert QThread.currentThread() != app.thread()
        return original(*args, **kwargs)
    monkeypatch.setattr(Hdf5RunReader, "reference_sweep", read)
    tab = load_tab(app, repeat_archive)
    try:
        controls = tab.processing_controls
        controls.operation.setCurrentIndex(controls.operation.findData("subtract_reference_signed"))
        controls.reference.setCurrentIndex(controls.reference.findData(1))
        controls.reference_sample.setCurrentIndex(controls.reference_sample.findData(2))
        wait_until(app, lambda: not tab._read_tasks)
        assert controls.state.reference_sweep == 2
        measurement = tab.spectrum_plot._traces["Post-processed spectrum"][1].copy()
        tab.baseline_controls.collection.setCurrentIndex(tab.baseline_controls.collection.findData(0))
        tab.baseline_controls.show_button.click()
        wait_until(app, lambda: not tab._read_tasks)
        assert tab._selected_reference.purpose == "background" and tab._selected_reference.selected_sweep is None
        assert not controls.operation.isEnabled() and controls.state.reference_sweep == 2
        tab.baseline_controls.sample.setCurrentIndex(tab.baseline_controls.sample.findData(1))
        wait_until(app, lambda: not tab._read_tasks)
        assert tab._selected_reference.selected_sweep == 1
        assert "repeat 2/4" in tab.spectrum_plot._plot_title
        destination = tmp_path / "background-repeat.csv"
        tab.spectrum_plot._export_csv(destination)
        metadata = json.loads(Path(str(destination) + ".analysis.json").read_text(encoding="utf-8"))
        assert metadata["standalone_baseline"]["selected_sweep"] == 1
        assert metadata["standalone_baseline"]["source_sweep_indices"] == [1]
        assert metadata["spectrum_processing_effective"]["operation"] == "none"
        tab.baseline_controls.measurement_button.click()
        wait_until(app, lambda: not tab._read_tasks)
        assert controls.operation.isEnabled() and not tab.baseline_controls.viewing
        np.testing.assert_array_equal(tab.spectrum_plot._traces["Post-processed spectrum"][1], measurement)
        view = tab.view_controls
        view.boxes["show_reference_spectrum"].setChecked(True)
        view.baselines["reference"].setCurrentIndex(view.baselines["reference"].findData(1))
        view.samples["reference"].setCurrentIndex(view.samples["reference"].findData(0))
        wait_until(app, lambda: not tab._read_tasks)
        assert view.state.reference_sweep == 0
        assert "Reference spectrum" in tab.spectrum_plot._traces
        controls.operation.setCurrentIndex(controls.operation.findData("subtract_power_signed"))
        wait_until(app, lambda: not tab._read_tasks)
        assert controls.state.reference_sweep is None  # changing purpose cannot retain another source repeat
        assert hashlib.sha256(repeat_archive.read_bytes()).hexdigest() == before
    finally:
        tab.peak_tools.close()
        assert tab.peak_tools.pool.waitForDone(5000)
        close_tab(app, tab)


@pytest.mark.parametrize("width,theme", [(1440, "light"), (1024, "dark")])
def test_baseline_repeat_controls_render_in_full_fluent_shell(
    repeat_archive, shell_qt_application, tmp_path, width, theme
):
    app = shell_qt_application
    font = Path("C:/Windows/Fonts/arial.ttf")
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Arial", 10))
    window = MainWindow(".config/settings.yml", simulation=True)
    page = window.results_page
    try:
        window._set_theme_mode(theme, persist=False)
        window.resize(width, 900)
        window.show()
        window._navigate_to("results")
        page.set_output_directory(repeat_archive.parent)
        page.file_browser.select_path(repeat_archive)
        wait_until(app, lambda: page._result_task is None and page.spectrum_tab._filter_task is None
                   and not page.spectrum_tab._read_tasks and page._selected_path == repeat_archive,
                   timeout=20)
        page.result_tabs.setCurrentIndex(page._spectrum_index)
        tab = page.spectrum_tab
        assert tab.spectrum_plot.tools.isHidden()
        assert tab.spectrum_plot.tools.parentWidget() is tab.spectrum_plot
        tab.baseline_controls.select(1, 2)
        tab.baseline_controls.show_button.click()
        wait_until(app, lambda: not tab._read_tasks)
        host = window.navigation_routes["results"].scroll_area
        host.ensureWidgetVisible(tab.baseline_controls)
        QTest.qWait(150)
        assert host.horizontalScrollBar().maximum() == 0
        for card, controls in ((tab.baseline_controls, (tab.baseline_controls.collection, tab.baseline_controls.sample,
                                                       tab.baseline_controls.show_button, tab.baseline_controls.measurement_button)),
                               (tab.view_controls, tuple(tab.view_controls.samples.values()))):
            for control in controls:
                assert control.height() >= 30
                assert card.rect().contains(control.mapTo(card, control.rect().bottomRight()))
        assert window.grab().save(str(tmp_path / f"results-baseline-repeats-{theme}-{width}.png"))
        tab.spectrum_plot.show_floating()
        QTest.qWait(100)
        floating = tab.spectrum_plot._floating_window
        assert floating.mirror.tools.isVisible()
        assert floating.grab().save(str(tmp_path / f"results-baseline-repeat-floating-{theme}.png"))
    finally:
        window.recipe_page._close_discard_confirmed = True
        assert page.shutdown()
        window.close()
