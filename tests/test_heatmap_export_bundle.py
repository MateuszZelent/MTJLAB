"""Per-DUT export location, unit-safe view ranges and standalone HDF5 reload."""

import hashlib
import json
from pathlib import Path

import h5py
import numpy as np
import pytest
from PySide6.QtCore import QThread
from PySide6.QtGui import QImage

from app.storage import Hdf5RunReader
from app.storage.heatmap_analysis_store import SCHEMA, read_heatmap_analysis, write_heatmap_analysis
from app.ui.results.analysis_export import heatmap_export_path
from app.ui.results.heatmap_ranges import HeatmapRangeDialog, parse_axis_value
from app.ui.results.page import ResultsPage
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_heatmap_coordinates import _write_current_sweep
from tests.test_spectrum_correction_controller import wait_until


def metadata(source):
    return {"source_file": str(source), "axes": {"x": {"label": "Frequency", "unit": "Hz"},
            "y": {"label": "Current", "unit": "A"}}, "value_unit": "W",
            "spectrum_processing": {"operation": "subtract_reference_signed", "reference_index": 1,
                                    "reference_sweep": None, "modes": ["denoise"]},
            "map_processing": {"component": "median_power"}}


@pytest.mark.parametrize("bundle", [True, False])
def test_default_export_mirrors_source_dut_and_sweep_name(tmp_path, bundle):
    root = tmp_path / "sample" / "devices" / "R20C4"
    source = root / "measurements" / "sweeps"
    source = source / "12_20261009T120000Z_current" / "data.h5" if bundle else source / "old_sweep.h5"
    target = heatmap_export_path(metadata(source))
    assert target.parent == root / "postprocessing" / "sweeps" / (source.parent.name if bundle else source.stem)
    assert "subtract_reference" in target.name
    target.parent.mkdir(parents=True)
    target.with_suffix(".h5").touch()
    assert heatmap_export_path(metadata(source)) != target
    changed = metadata(source)
    changed["spectrum_processing"]["reference_sweep"] = 2
    assert heatmap_export_path(changed) != target


def test_derived_snapshot_roundtrip_units_gaps_identity_and_no_overwrite(tmp_path):
    path = tmp_path / "derived.h5"
    values = np.array([[1e-9, -2e-9, np.nan], [3e-9, 0., 4e-9]])
    checkpoints = np.array([[10, 10, -1], [20, 20, 20]])
    info = metadata(tmp_path / "data.h5")
    write_heatmap_analysis(path, values, [1e6, 2e6, 3e6], [.002, .003], checkpoints, info)
    stored = read_heatmap_analysis(path)
    np.testing.assert_array_equal(stored.values, values)
    np.testing.assert_array_equal(stored.checkpoints, checkpoints)
    assert stored.metadata == info
    assert Hdf5RunReader.summary(path).status == "postprocessed"
    before = path.read_bytes()
    with pytest.raises(OSError):
        write_heatmap_analysis(path, values, [1e6, 2e6, 3e6], [.002, .003], checkpoints, info)
    assert path.read_bytes() == before
    with h5py.File(path, "r+") as file:
        file["heatmap/x"].attrs["unit"] = "A"
    with pytest.raises(ValueError, match="units"):
        read_heatmap_analysis(path)


@pytest.mark.parametrize("fault", ["infinite", "axes", "checkpoint", "metadata", "view"])
def test_invalid_exports_do_not_create_files(tmp_path, fault):
    values = np.ones((2, 3))
    checkpoints = np.zeros((2, 3), dtype=int)
    x = [1e6, 2e6, 3e6]
    info = metadata(tmp_path / "source.h5")
    if fault == "infinite":
        values[0, 0] = np.inf
    elif fault == "axes":
        x = [1e6, 1e6, 3e6]
    elif fault == "checkpoint":
        checkpoints = checkpoints[:, :2]
    elif fault == "metadata":
        info.pop("axes")
    else:
        info["view_ranges"] = [[np.nan, 1], [0, 1]]
    target = tmp_path / "invalid.h5"
    with pytest.raises(ValueError):
        write_heatmap_analysis(target, values, x, [.002, .003], checkpoints, info)
    assert not target.exists()


def test_source_archive_cannot_be_replaced_by_a_derived_export(tmp_path):
    source = tmp_path / "source.h5"
    source.write_bytes(b"immutable source")
    with pytest.raises(ValueError, match="read-only"):
        write_heatmap_analysis(source, np.ones((2, 3)), [1, 2, 3], [1, 2],
                               np.zeros((2, 3), dtype=int), metadata(source))
    assert source.read_bytes() == b"immutable source"


def test_partial_export_is_rejected(tmp_path):
    path = tmp_path / "partial.h5"
    with h5py.File(path, "x") as file:
        file.attrs.update(schema=SCHEMA, status="writing")
    with pytest.raises(ValueError, match="incomplete"):
        read_heatmap_analysis(path)


def test_cropped_single_nonuniform_cell_keeps_its_original_width(tmp_path, shell_qt_application):
    from app.ui.results.heatmap_tab import HeatmapPlotWidget
    path = tmp_path / "one-bin.h5"
    info = metadata(tmp_path / "source.h5")
    values = np.array([[1e-9], [np.nan]])
    write_heatmap_analysis(path, values, [3], [.002, .003], np.array([[4], [5]]), info,
                           x_edges=[2, 5.5], y_edges=[.0015, .0025, .0035])
    stored = read_heatmap_analysis(path)
    plot = HeatmapPlotWidget()
    try:
        plot.set_data(stored.values, stored.x_values, stored.y_values,
                      x_cell_edges=stored.x_edges, y_cell_edges=stored.y_edges)
        np.testing.assert_array_equal(plot._x_edges, [2, 5.5])
        assert plot._cell_indices(5.1, .002) == (0, 0)
    finally:
        plot.close()
        plot.deleteLater()
        shell_qt_application.processEvents()


def test_failed_hdf5_write_removes_only_its_new_partial_file(tmp_path, monkeypatch):
    target = tmp_path / "failed.h5"
    def fail(*args, **kwargs):
        raise OSError("disk write failed")
    monkeypatch.setattr(h5py.Group, "create_dataset", fail)
    with pytest.raises(OSError, match="disk write"):
        write_heatmap_analysis(target, np.ones((2, 3)), [1, 2, 3], [1, 2],
                               np.zeros((2, 3), dtype=int), metadata(tmp_path / "source.h5"))
    assert not target.exists()


@pytest.mark.parametrize("text,unit,value", [("1.5 GHz", "Hz", 1.5e9), ("2,5 mA", "A", .0025),
                                           ("2 mA", "mA", 2), ("-3", "", -3)])
def test_manual_ranges_convert_to_the_actual_axis_unit(text, unit, value):
    assert parse_axis_value(text, unit) == pytest.approx(value)


@pytest.mark.parametrize("text,unit", [("1 mV", "Hz"), ("1", "Hz"), ("nan", ""), ("1e400 A", "A")])
def test_manual_ranges_reject_wrong_dimensions_and_nonfinite(text, unit):
    with pytest.raises(ValueError):
        parse_axis_value(text, unit)


@pytest.mark.parametrize("width", [1200, 650])
def test_png_hdf5_export_and_results_reload_are_rendered_off_worker_boundary(tmp_path, shell_qt_application, monkeypatch, width):
    app = shell_qt_application
    root = tmp_path / "sample" / "devices" / "R20C4"
    source = root / "measurements" / "sweeps" / "1_20261009T120000Z_current" / "data.h5"
    source.parent.mkdir(parents=True)
    _write_current_sweep(source)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    page = ResultsPage(str(root))
    page.resize(width, 950)
    page.show()
    import app.ui.results.heatmap_tab as module
    exports = []
    def choose(_parent, _title, default, _filters):
        exports.append(Path(default))
        return default, "PNG (*.png)"
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", choose)
    original_write = module.write_heatmap_analysis
    def write(*args, **kwargs):
        assert QThread.currentThread() != app.thread()
        return original_write(*args, **kwargs)
    monkeypatch.setattr(module, "write_heatmap_analysis", write)
    try:
        page._on_file_selected(source)
        wait_until(app, lambda: page._result_task is None and page._thatec_run is not None)
        page.result_tabs.setCurrentIndex(page._heatmap_index)
        tab = page.heatmap_tab
        wait_until(app, lambda: not tab._read_tasks)
        tab.load_button.click()
        wait_until(app, lambda: not tab._read_tasks and tab.heatmap._data is not None)
        plot = tab.heatmap
        raw = plot._data.copy()
        plot.plot.setRange(xRange=(1e6, 2e6), yRange=(.0009, .0021), padding=0)
        plot.export()
        wait_until(app, lambda: plot._export_task is None)
        image = exports[0]
        assert image.parent == root / "postprocessing" / "sweeps" / source.parent.name
        assert not QImage(str(image)).isNull()
        derived = image.with_suffix(".h5")
        stored = read_heatmap_analysis(derived)
        np.testing.assert_array_equal(stored.values, raw[1:, :2])
        np.testing.assert_array_equal(stored.x_values, [1e6, 2e6])
        np.testing.assert_array_equal(stored.y_values, [.001, .002])
        np.testing.assert_array_equal(stored.x_edges, [.5e6, 1.5e6, 2.5e6])
        manifest = json.loads(Path(str(image) + ".analysis.json").read_text(encoding="utf-8"))
        assert not manifest["source_data_modified"]
        page._on_file_selected(derived)
        wait_until(app, lambda: page._result_task is None and page.heatmap_tab.heatmap._data is not None)
        np.testing.assert_array_equal(page.heatmap_tab.heatmap._data, stored.values)
        assert page.result_tabs.stack.currentIndex() == page._heatmap_index
        assert not page.metadata_panel.pythat_button.isEnabled()
        assert plot.range_button.isVisible() and plot.range_button.height() >= 28
        assert page.grab().save(str(tmp_path / f"derived-heatmap-{width}.png"))
        dialog = HeatmapRangeDialog(plot, plot._axis_metadata, plot.plot.viewRange())
        dialog.show()
        app.processEvents()
        assert dialog.grab().save(str(tmp_path / f"heatmap-ranges-{width}.png"))
        assert all(field.height() >= 28 for field in dialog.fields.values())
        dialog.fields["x", "minimum"].setText("1 mV")
        dialog._save()
        assert dialog.result() == 0 and dialog.error.text()
        dialog.fields["x", "minimum"].setText("1.1 MHz")
        dialog.fields["x", "maximum"].setText("1.9 MHz")
        dialog.fields["y", "minimum"].setText("1 mA")
        dialog.fields["y", "maximum"].setText("1.8 mA")
        dialog._save()
        assert dialog.result() == 1, dialog.error.text()
        np.testing.assert_allclose(dialog.ranges, [(1.1e6, 1.9e6), (.001, .0018)])
        dialog.close()
        dialog.deleteLater()
        page._on_file_selected(source)
        wait_until(app, lambda: page._result_task is None and page._thatec_run is not None)
        assert tab.processing_controls.isEnabled() and tab.map_controls.isEnabled()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    finally:
        assert page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()
