"""Scientific differential-map contracts, real archive slices and GUI workers."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QThread

from app.storage import Hdf5RunReader, ThatecRunReader
from app.ui.results.analysis_export import write_analysis_manifest
from app.ui.results.heatmap_tab import HeatmapResultsTab
from app.ui.results.map_controls import MapSettingsDialog
from app.ui.results.map_processing import MapProcessing, analyse_map
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_results_series_navigation import nested_archive as nested_archive  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.fixture
def scientific_map():
    f = np.linspace(1e9, 3e9, 1001)
    coordinate = np.linspace(.002, .004, 41)
    stationary = 5e-10 * np.exp(-((f - 1.08e9) / 1.5e6) ** 2)
    broad = 2e-10 * np.exp(-((f - 2e9) / 100e6) ** 2)
    moving = 2e-10 * np.exp(-((f[None, :] - np.linspace(1.2e9, 2.8e9, 41)[:, None]) / 30e6) ** 2)
    power = 1e-11 + stationary + broad + moving + 5e-13 * np.sin(np.arange(41))[:, None]
    dbm = 10 * np.log10(power / 1e-3)
    return f, coordinate, power, dbm, moving


@pytest.mark.parametrize("transpose", [False, True])
@pytest.mark.parametrize("operation", ["median_power", "quantile_power", "reference_power", "median_db"])
def test_differential_maps_have_correct_physical_math_and_signs(scientific_map, transpose, operation):
    f, x, power, dbm, moving = scientific_map
    before = dbm.copy()
    state = MapProcessing(component=operation, reference_value=x[10] if operation == "reference_power" else None)
    matrix = dbm.T if transpose else dbm
    result = analyse_map(matrix, unit="dBm", frequencies_hz=f, coordinate_values=x,
                         frequency_axis=0 if transpose else 1, state=state)
    if operation == "median_db":
        expected = dbm - np.median(dbm, axis=0)
        assert result.unit == "dB" and result.component_unit == "dBm"
    else:
        component = (power[10] if operation == "reference_power" else
                     np.quantile(power, .2 if operation == "quantile_power" else .5, axis=0))
        expected = power - component
        assert result.unit == result.component_unit == "W"
    np.testing.assert_allclose(result.values, expected.T if transpose else expected, atol=2e-24)
    assert np.any(result.values < 0) and np.any(result.values > 0)
    np.testing.assert_array_equal(dbm, before)
    if operation == "median_power":
        # A resonance moving across the selected axis remains; static features
        # are removed as common signal, without claiming they were interference.
        assert np.max(np.abs(result.values[:, 40] if not transpose else result.values[40])) < 1e-12
        assert np.nanmax(result.values) >= .95 * np.max(moving)


def test_common_component_inspection_and_missing_coverage_do_not_invent_samples(scientific_map):
    f, x, _, dbm, _ = scientific_map
    dbm = dbm.copy()
    dbm[3] = np.nan
    dbm[:15, 650] = np.nan
    dbm[:, 750] = np.nan
    result = analyse_map(dbm, unit="dBm", frequencies_hz=f, coordinate_values=x, frequency_axis=1,
                         state=MapProcessing(component="median_power", view="component"))
    assert np.all(np.isnan(result.values[3]))
    assert np.all(np.isnan(result.values[:, 650]))
    assert np.all(np.isnan(result.values[:, 750]))
    np.testing.assert_allclose(result.values[1, :600], result.values[2, :600])


@pytest.mark.parametrize("nonuniform", [False, True])
def test_line_mask_checks_width_stability_and_protects_signal_bands(scientific_map, nonuniform):
    f, x, _, dbm, _ = scientific_map
    if nonuniform:
        f = f + 1e5 * np.sin(np.arange(f.size))
    state = MapProcessing(mask_lines=True)
    result = analyse_map(dbm, unit="dBm", frequencies_hz=f, coordinate_values=x, frequency_axis=1, state=state)
    assert result.masked_frequencies_hz
    assert all(1.075e9 < value < 1.085e9 for value in result.masked_frequencies_hz)
    assert np.any(np.isnan(result.values[:, 39:42]))
    assert np.all(np.isfinite(result.values[:, 100:900]))  # moving + broad stationary resonances
    protected = analyse_map(dbm, unit="dBm", frequencies_hz=f, coordinate_values=x, frequency_axis=1,
        state=replace(state, protected_bands_hz=((1.075e9, 1.085e9),)))
    assert not protected.masked_frequencies_hz
    np.testing.assert_array_equal(protected.values, dbm)
    variable = dbm.copy()
    variable[:, 39:42] += np.linspace(-4, 4, len(x))[:, None]
    result = analyse_map(variable, unit="dBm", frequencies_hz=f, coordinate_values=x, frequency_axis=1, state=state)
    assert not result.masked_frequencies_hz
    # MAD alone would miss a response confined to a minority of sweep points.
    minority = dbm.copy()
    minority[:6, 39:42] += 8.
    result = analyse_map(minority, unit="dBm", frequencies_hz=f, coordinate_values=x, frequency_axis=1, state=state)
    assert not result.masked_frequencies_hz


def test_signed_input_not_relabelled_as_log_power_and_colour_limits_do_not_clip(scientific_map):
    f, x, power, _, _ = scientific_map
    signed = power - np.median(power, axis=0)
    result = analyse_map(signed, unit="W", frequencies_hz=f, coordinate_values=x, frequency_axis=1,
                         state=MapProcessing(component="median_power", colour_range="robust"))
    assert result.unit == "W"
    assert np.any(result.values < 0)
    assert np.nanmax(result.values) > result.levels[1]
    for state in (MapProcessing(mask_lines=True), MapProcessing(component="median_db")):
        with pytest.raises(ValueError, match="dBm"):
            analyse_map(signed, unit="W", frequencies_hz=f, coordinate_values=x, frequency_axis=1, state=state)


@pytest.mark.parametrize("transpose", [False, True])
def test_raw_line_classifier_can_mask_signed_residual_without_changing_its_units(scientific_map, transpose):
    f, x, power, dbm, _ = scientific_map
    signed = power - np.median(power, axis=0)
    original = signed.copy()
    raw_before = dbm.copy()
    result = analyse_map(signed.T if transpose else signed, unit="W", frequencies_hz=f,
        coordinate_values=x, frequency_axis=0 if transpose else 1, state=MapProcessing(mask_lines=True),
        line_input_dbm=dbm.T if transpose else dbm)
    assert result.unit == "W" and result.masked_frequencies_hz
    output = result.values.T if transpose else result.values
    masked = np.isin(f, result.masked_frequencies_hz)
    assert np.all(np.isnan(output[:, masked]))
    np.testing.assert_array_equal(output[:, ~masked], signed[:, ~masked])
    assert np.any(output < 0) and np.any(output > 0)
    np.testing.assert_array_equal(signed, original)
    np.testing.assert_array_equal(dbm, raw_before)
    with pytest.raises(ValueError, match="grid"):
        analyse_map(signed, unit="W", frequencies_hz=f, coordinate_values=x,
            frequency_axis=1, state=MapProcessing(mask_lines=True), line_input_dbm=dbm[:, :-1])


@pytest.mark.parametrize("transpose", [False, True])
def test_archive_companion_is_exact_raw_plane_before_signed_reference_subtraction(nested_archive, transpose):
    from app.ui.results.heatmap_coordinates import HeatmapRequest, build_heatmap_coordinates
    from app.ui.results.heatmap_tab import _read_heatmap_payload, _process_map_payload
    from app.ui.results.processing import ResultProcessing
    run = ThatecRunReader.describe(nested_archive)
    row = next(row for row in run.rows.values() if len(row.shape) == 2 and
               dict(row.definition).get("lab control role") != "spectrum_processed")
    points = Hdf5RunReader.points(nested_archive, include_details=False)
    coordinates = build_heatmap_coordinates(nested_archive, run, row, points)
    request = HeatmapRequest("keithley.A.current" if transpose else "frequency",
        "frequency" if transpose else "keithley.A.current",
        {"keithley.B.current": -.005, "frequency": (1.25e6, 1.75e6)})
    raw = _read_heatmap_payload(nested_archive, row, coordinates, request)
    corrected = _read_heatmap_payload(nested_archive, row, coordinates, request,
        processing=ResultProcessing("subtract_reference_signed", 1), points=points)
    assert corrected.z_unit == "W" and corrected.line_input_is_raw
    np.testing.assert_array_equal(corrected.line_input_dbm, raw.matrix)
    np.testing.assert_array_equal(corrected.cell_checkpoints, raw.cell_checkpoints)
    expected = np.power(10., (raw.matrix - 30.) / 10.) - 1e-9
    np.testing.assert_allclose(corrected.matrix, expected, atol=2e-24)
    assert np.any(corrected.matrix < 0) and np.any(corrected.matrix > 0)
    derived = _process_map_payload(corrected, MapProcessing(mask_lines=True), 0 if transpose else 1)
    assert derived.z_unit == "W"
    np.testing.assert_array_equal(derived.matrix, corrected.matrix)  # moving resonance remains
    assert derived.baselines[0].purpose == "reference"
    with pytest.raises(ValueError, match="Raw spectrum row"):
        _process_map_payload(replace(corrected, line_input_is_raw=False),
                             MapProcessing(mask_lines=True), 0 if transpose else 1)


def test_map_negative_validation_and_cooperative_cancellation(scientific_map):
    f, x, _, dbm, _ = scientific_map
    for kwargs in ({"quantile": 1.}, {"line_max_width_hz": float("nan")},
                   {"line_neighbourhood_hz": 1.}, {"minimum_coverage": .1},
                   {"protected_bands_hz": ((1e9, 0.),)}, {"component": "unknown"}):
        with pytest.raises(ValueError):
            MapProcessing(**kwargs)
    with pytest.raises(ValueError, match="three"):
        analyse_map(dbm[:2], unit="dBm", frequencies_hz=f, coordinate_values=x[:2], frequency_axis=1,
                    state=MapProcessing(component="median_power"))
    count = 0
    def cancel():
        nonlocal count
        count += 1
        return count > 3
    with pytest.raises(InterruptedError):
        analyse_map(dbm, unit="dBm", frequencies_hz=f, coordinate_values=x, frequency_axis=1,
                    state=MapProcessing(component="median_power"), cancelled=cancel)


@pytest.mark.parametrize("transpose", [False, True])
def test_stationary_stripe_subtraction_preserves_sweep_variation_and_gaps(transpose):
    f = np.linspace(780e6, 850e6, 141)
    current = np.arange(20, dtype=float)
    baseline = (f - 810e6)[None, :] * 1e-8 + current[:, None] / 10
    source = baseline.copy()
    stripe = (f >= 801e6) & (f <= 808e6)
    source[:, stripe] -= 3
    source[:, f == 817e6] += 2
    source[:5, f == 810e6] += 3
    source[0, f == 805e6] = np.nan
    state = MapProcessing(component="stationary_stripes_db", stripe_bands_hz=((797e6, 824e6),))
    matrix = source.T if transpose else source
    kwargs = dict(unit="dB", frequencies_hz=f, coordinate_values=current, frequency_axis=int(not transpose))
    corrected = analyse_map(matrix, state=state, **kwargs)
    actual = corrected.values.T if transpose else corrected.values
    expected = baseline.copy()
    expected[:5, f == 810e6] += 3
    expected[0, f == 805e6] = np.nan
    np.testing.assert_allclose(actual, expected, atol=1e-12)
    np.testing.assert_array_equal(actual[:, (f < 797e6) | (f > 824e6)], source[:, (f < 797e6) | (f > 824e6)])
    np.testing.assert_array_equal(analyse_map(matrix, state=replace(state, view="input"), **kwargs).values, matrix)
    component = analyse_map(matrix, state=replace(state, view="component"), **kwargs)
    np.testing.assert_allclose(component.component[stripe], -3, atol=1e-12)
    with pytest.raises(ValueError, match="relative dB"):
        analyse_map(matrix, state=state, **{**kwargs, "unit": "dBm"})


@pytest.mark.parametrize("transpose", [False, True])
def test_stripe_band_correction_tracks_sweep_and_preserves_local_features(transpose):
    f = np.arange(775, 851, dtype=float) * 1e6
    sweep = np.arange(31, dtype=float)
    baseline = (f - 810e6)[None, :] * 1e-8 + sweep[:, None] * .01
    band = (f >= 800e6) & (f <= 822e6)
    protected = f == 815e6
    corrected = band & ~protected
    # Both a wide residual and its narrow core change with the sweep.
    stripe = np.where(sweep < 15, -4., 1.4)[:, None] * (1 + (f == 807e6))
    source = baseline + stripe * band
    source[6, f == 805e6] += 2
    source[25, f == 809e6] = np.nan
    expected = source.copy()
    expected[:, corrected] -= stripe[:, corrected]
    state = MapProcessing(component="repair_stripe_bands_db",
                          stripe_bands_hz=((800e6, 822e6),),
                          protected_bands_hz=((814.5e6, 815.5e6),))
    orient = lambda matrix: matrix.T if transpose else matrix
    kwargs = dict(unit="dB", frequencies_hz=f, coordinate_values=sweep, frequency_axis=int(not transpose))
    result = analyse_map(orient(source), state=state, **kwargs)
    np.testing.assert_allclose(result.values, orient(expected), atol=1e-12)
    np.testing.assert_array_equal(orient(result.values)[:, ~corrected], source[:, ~corrected])
    component = analyse_map(orient(source), state=replace(state, view="component"), **kwargs)
    np.testing.assert_allclose(component.values, orient(source - expected), atol=1e-12)
    assert result.corrected_frequencies_hz == tuple(f[corrected])


def test_map_ui_changes_reuse_one_exact_slice_off_gui_and_export_provenance(
    nested_archive, shell_qt_application, monkeypatch, tmp_path
):
    app = shell_qt_application
    before = hashlib.sha256(nested_archive.read_bytes()).hexdigest()
    calls = []
    original = ThatecRunReader.spectrum_slice
    def read(*args, **kwargs):
        assert QThread.currentThread() != app.thread()
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(ThatecRunReader, "spectrum_slice", read)
    import app.ui.results.heatmap_tab as module
    original_processing = module._process_map_payload
    def process(*args, **kwargs):
        assert QThread.currentThread() != app.thread()
        return original_processing(*args, **kwargs)
    monkeypatch.setattr(module, "_process_map_payload", process)
    tab = HeatmapResultsTab()
    tab.resize(1150, 900)
    tab.show()
    try:
        tab.load(nested_archive, ThatecRunReader.describe(nested_archive),
                 Hdf5RunReader.points(nested_archive, include_details=False))
        wait_until(app, lambda: not tab._read_tasks)
        tab.y_axis_combo.setCurrentIndex(tab.y_axis_combo.findData("keithley.A.current"))
        b_min, b_max = tab._range_combos["keithley.B.current"]
        b_min.setCurrentIndex(b_min.findData(-.005))
        b_max.setCurrentIndex(b_max.findData(-.005))
        tab.load_button.click()
        wait_until(app, lambda: not tab._read_tasks and tab.heatmap._data is not None)
        raw = tab.heatmap._data.copy()
        assert raw.shape == (41, 201)  # B remains one exact selected value
        initial_reads = len(calls)
        tab.heatmap.plot.setRange(xRange=(1.2e6, 1.8e6), yRange=(.0023, .0035), padding=0)
        viewport = np.asarray(tab.heatmap.plot.viewRange())
        from app.ui.results.processing import ResultProcessing
        tab.processing_controls.set_state(ResultProcessing("subtract_reference_signed", 1), emit=True)
        wait_until(app, lambda: not tab._read_tasks)
        baseline = Hdf5RunReader.reference(nested_archive, 1)
        expected_reference = np.power(10., (raw - 30.) / 10.) - np.power(
            10., (np.asarray(baseline.powers_dbm) - 30.) / 10.)
        np.testing.assert_allclose(tab.heatmap._data, expected_reference, atol=1e-24)
        assert len(calls) == initial_reads
        np.testing.assert_allclose(tab.heatmap.plot.viewRange(), viewport)
        assert tab.heatmap.show_absolute.isEnabled() and tab.heatmap.show_absolute.isVisibleTo(tab)
        tab.heatmap.show_absolute.click()
        np.testing.assert_allclose(tab.heatmap._data, np.abs(expected_reference), atol=1e-24)
        np.testing.assert_allclose(tab.heatmap.plot.viewRange(), viewport)
        assert tab.heatmap.export_metadata["display_transform"] == "absolute_difference"
        assert tab.heatmap.export_metadata["value_unit"] == "W"
        assert len(calls) == initial_reads
        tab.heatmap.show_absolute.click()
        np.testing.assert_allclose(tab.heatmap._data, expected_reference, atol=1e-24)
        tab.processing_controls.set_state(ResultProcessing(), emit=True)
        wait_until(app, lambda: not tab._read_tasks)
        np.testing.assert_array_equal(tab.heatmap._data, raw)
        assert not tab.heatmap.show_absolute.isEnabled()
        assert len(calls) == initial_reads
        np.testing.assert_allclose(tab.heatmap.plot.viewRange(), viewport)
        tab.map_controls.set_state(MapProcessing(component="median_power"), emit=True)
        wait_until(app, lambda: not tab._read_tasks)
        expected = np.power(10, (raw - 30) / 10)
        expected -= np.median(expected, axis=0)
        np.testing.assert_allclose(tab.heatmap._data, expected, atol=1e-24)
        assert len(calls) == initial_reads
        assert tab.heatmap.export_metadata["value_unit"] == "W"
        tab.map_controls.set_state(replace(tab.map_controls.state, view="input"), emit=True)
        wait_until(app, lambda: not tab._read_tasks)
        np.testing.assert_array_equal(tab.heatmap._data, raw)
        tab.map_controls.set_state(replace(tab.map_controls.state, view="component"), emit=True)
        wait_until(app, lambda: not tab._read_tasks)
        assert len(calls) == initial_reads
        destination = tmp_path / "map.csv"
        tab.heatmap._export_csv(destination)
        write_analysis_manifest(destination, tab.heatmap.export_metadata)
        manifest = json.loads(Path(str(destination) + ".analysis.json").read_text(encoding="utf-8"))
        assert manifest["map_processing"]["view"] == "component"
        b_range = manifest["coordinate_selection"]["filters"]["keithley.B.current"]
        assert b_range[0] == b_range[1] and b_range[0] in {-.005, -.004}
        assert not manifest["source_data_modified"]
        assert hashlib.sha256(nested_archive.read_bytes()).hexdigest() == before
        # An externally updated file must invalidate the numeric source cache.
        import os
        stat = nested_archive.stat()
        os.utime(nested_archive, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        reads_before_refresh = len(calls)
        tab.load_button.click()
        wait_until(app, lambda: not tab._read_tasks)
        assert len(calls) > reads_before_refresh
        assert len(tab._source_caches) <= 2
    finally:
        tab.close()
        assert tab._read_pool.waitForDone(5000)
        tab.deleteLater()
        app.processEvents()


def test_read_file_loads_spectra_off_gui_and_axis_and_filter_changes_use_memory(
    nested_archive, shell_qt_application, monkeypatch,
):
    app = shell_qt_application
    run = ThatecRunReader.describe(nested_archive)
    points = Hdf5RunReader.points(nested_archive, include_details=False)
    tab = HeatmapResultsTab()
    tab.resize(820, 700)
    tab.show()
    resume_read = Event()
    try:
        tab.load(nested_archive, run, points)
        wait_until(app, lambda: not tab._read_tasks)
        tab.y_axis_combo.setCurrentIndex(tab.y_axis_combo.findData("keithley.A.current"))
        open_file = ThatecRunReader._open
        reads = []

        def read(*args, **kwargs):
            assert QThread.currentThread() != app.thread()
            reads.append(1)
            return open_file(*args, **kwargs)

        monkeypatch.setattr(ThatecRunReader, "_open", read)
        read_rows = ThatecRunReader.read_spectral_rows

        def read_with_pause(*args, progress=None, **kwargs):
            def report(completed, total):
                progress(completed, total)
                if 0 < completed < total and not resume_read.is_set():
                    assert resume_read.wait(12), "Progress was not shown while reading"

            return read_rows(*args, progress=report, **kwargs)

        monkeypatch.setattr(ThatecRunReader, "read_spectral_rows", read_with_pause)
        tab.read_file_button.click()
        progress_bar = tab.heatmap_state.read_progress
        wait_until(app, lambda: 0 < progress_bar.value() < progress_bar.maximum())
        assert progress_bar.isVisibleTo(tab) and progress_bar.geometry().width() > 100
        assert progress_bar.maximum() == sum(row.shape[0] for row in tab._spectrum_rows)
        caption = tab.heatmap_state.description
        assert f"{progress_bar.value():,} / {progress_bar.maximum():,}" in caption.text()
        assert caption.contentsRect().width() >= caption.fontMetrics().horizontalAdvance(caption.text())
        resume_read.set()
        wait_until(app, lambda: tab.heatmap._data is not None and not tab._read_tasks)
        assert not progress_bar.isVisibleTo(tab)
        assert tab.read_file_button.isVisibleTo(tab)
        assert tab.controls_card.rect().contains(tab.read_file_button.mapTo(tab.controls_card, tab.read_file_button.rect().bottomRight()))
        assert tab.read_file_button.text() == "File in memory"
        assert len(reads) == 1
        count = tab.heatmap._data.size
        tab.y_axis_combo.setCurrentIndex(tab.y_axis_combo.findData("keithley.B.current"))
        wait_until(app, lambda: tab.heatmap._data is not None and not tab._read_tasks)
        assert tab.heatmap._data.size != count
        minimum, _ = tab._range_combos["keithley.A.current"]
        minimum.setCurrentIndex(min(minimum.currentIndex() + 1, minimum.count() - 1))
        wait_until(app, lambda: tab.heatmap._data is not None and not tab._read_tasks)
        assert tab.heatmap.export_metadata["coordinate_selection"]["filters"]["keithley.A.current"][0] == minimum.currentData()
        assert len(reads) == 1
    finally:
        resume_read.set()
        tab.close()
        assert tab._read_pool.waitForDone(5000)
        tab.deleteLater()
        app.processEvents()


def test_map_settings_validate_frequency_units_without_accepting_bad_values(shell_qt_application, tmp_path):
    app = shell_qt_application
    dialog = MapSettingsDialog(None, MapProcessing(component="stationary_stripes_db"))
    dialog.show()
    app.processEvents()
    assert dialog.grab().save(str(tmp_path / "results-map-settings.png"))
    dialog.widths["line_max_width_hz"].setText("6 mV")
    dialog._save()
    assert dialog.result() == 0 and dialog.error.text()
    dialog.widths["line_max_width_hz"].setText("4 MHz")
    dialog.stripe_bands.setText("797 .. 824 mV")
    dialog._save()
    assert dialog.result() == 0 and dialog.error.text()
    dialog.stripe_bands.setText("797 MHz .. 824 MHz")
    dialog.bands.setText("600 MHz .. 800 MHz; 1 GHz .. 1.2 GHz")
    dialog._save()
    assert dialog.result() == 1, dialog.error.text()
    assert dialog.state.protected_bands_hz == ((600e6, 800e6), (1e9, 1.2e9))
    assert dialog.state.stripe_bands_hz == ((797e6, 824e6),)
    dialog.close()
    dialog.deleteLater()
    app.processEvents()


def test_map_colour_only_is_valid_without_a_frequency_axis():
    from app.ui.results.heatmap_tab import _HeatmapPayload, _process_map_payload
    values = np.array([[1., 2.], [3., 100.]])
    payload = _HeatmapPayload(values, np.array([.001, .002]), np.array([10., 20.]),
        "Current", "A", "Frequency", "Hz", "Power", "dBm", (1., 100.), 0, np.zeros((2, 2), int))
    derived = _process_map_payload(payload, MapProcessing(colour_range="robust"), None)
    np.testing.assert_array_equal(derived.matrix, values)
    assert derived.levels[1] < 100.
    with pytest.raises(ValueError, match="Frequency"):
        _process_map_payload(payload, MapProcessing(component="median_power"), None)


def test_export_cannot_overwrite_a_measurement_archive(tmp_path):
    from app.ui.results.analysis_export import ensure_derived_destination
    source = tmp_path / "measurement.h5"
    source.write_bytes(b"immutable measurement")
    with pytest.raises(ValueError, match="read-only"):
        ensure_derived_destination(source, source)
    assert source.read_bytes() == b"immutable measurement"
