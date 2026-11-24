"""Linked generator representations must not filter a sweep down to one trace."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.storage import Hdf5RunReader, Hdf5RunWriter, ThatecRunReader
from app.ui.design_system import apply_application_theme
from app.ui.results.heatmap_coordinates import (
    HeatmapCoordinates,
    HeatmapDimension,
    HeatmapRequest,
    build_heatmap_coordinates,
    read_heatmap_matrix,
)
from app.ui.results.heatmap_tab import HeatmapResultsTab
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.fixture
def dc_archive(tmp_path):
    path = tmp_path / "linked-dc.h5"
    recipe = Path("recipes/example_keithley_a_10ma_rigol_dc_0_600mv_fixed_moke.yml").read_text(encoding="utf-8")
    writer = Hdf5RunWriter(path, recipe_source=recipe, settings_source="schema_version: 1\n",
                          plan_hash="linked-dc", device_idn={}, expected_points=41)
    for index in range(41):
        level_v = index * .015
        writer.append(
            MeasurementPoint(index=index, setpoints={
                "rigol.1.offset": level_v, "rigol.1.high_level": level_v,
                "rigol.1.low_level": level_v,
            }, measurements={}),
            SpectrumTrace((1e6, 2e6, 3e6), (-80. + index / 4, -70. - index / 2, -75.),
                          datetime.now(UTC), "TRAC1"),
        )
    writer.close("completed")
    return path


@pytest.mark.parametrize("axis_id,theme,size", [
    ("rigol.1.offset", "light", (1440, 1000)),
    ("rigol.1.high_level", "dark", (1440, 1000)),
    ("rigol.1.low_level", "light", (1440, 1000)),
    ("rigol.1.low_level", "dark", (760, 900)),
])
def test_dc_map_keeps_all_41_spectra_for_each_voltage_representation(
    dc_archive, shell_qt_application, tmp_path, axis_id, theme, size,
):
    app = shell_qt_application
    before = hashlib.sha256(dc_archive.read_bytes()).hexdigest()
    apply_application_theme(app, theme)
    tab = HeatmapResultsTab()
    try:
        tab.resize(*size)
        tab.show()
        tab.load(dc_archive, ThatecRunReader.describe(dc_archive),
                 Hdf5RunReader.points(dc_archive, include_details=False))
        wait_until(app, lambda: not tab._read_tasks)
        default_axis = tab.y_axis_combo.currentData()
        tab.y_axis_combo.setCurrentIndex(tab.y_axis_combo.findData(axis_id))
        tab.load_button.click()
        wait_until(app, lambda: not tab._read_tasks)
        assert tab.heatmap._data is not None, tab.info_label.text()
        assert tab.heatmap._data.shape == (41, 3)
        np.testing.assert_allclose(tab.heatmap._y_values, np.arange(41) * .015)
        np.testing.assert_allclose(tab.heatmap._data[:, 0], -80 + np.arange(41) / 4)
        assert tab.heatmap._cell_checkpoint_indices[:, 0].tolist() == list(range(41))
        assert default_axis == "rigol.1.offset"
        for lower, upper in tab._range_combos.values():
            assert lower.currentData() == 0.
            assert upper.currentData() == pytest.approx(.6)
        for _ in range(3):
            app.processEvents()
        y_axis = tab.heatmap.plot.getAxis("left")
        assert y_axis.autoSIPrefixScale == 1000.
        assert "mV" in y_axis.label.toPlainText()
        assert tab.heatmap.plot.viewRange()[1] == pytest.approx((-.0075, .6075))
        assert tab.heatmap.plot.isVisible()
        assert tab.heatmap.plot.width() > 400 and tab.heatmap.plot.height() > 100
        assert tab.grab().save(str(tmp_path / f"linked-dc-{axis_id}-{theme}-{size[0]}.png"))
        assert hashlib.sha256(dc_archive.read_bytes()).hexdigest() == before
    finally:
        tab.close()
        tab.deleteLater()
        app.processEvents()
        apply_application_theme(app, "dark")


@pytest.mark.parametrize("order", ["recipe", "alphabetical"])
@pytest.mark.parametrize("axis_id", ["rigol.1.amplitude", "rigol.1.high_level", "rigol.1.low_level"])
def test_ac_defaults_slice_independent_offset_without_pinning_derived_levels(axis_id, order):
    from app.ui.results.heatmap_coordinates import default_heatmap_ranges

    # Two independently swept controls, plus three equivalent recorded views.
    coordinates = HeatmapCoordinates((
        HeatmapDimension("frequency", "Frequency", "Hz", (), True),
        HeatmapDimension("rigol.1.offset", "Offset", "V", (0., 0., 0., .1, .1, .1)),
        HeatmapDimension("rigol.1.amplitude", "Amplitude", "V", (.2, .4, .6, .2, .4, .6)),
        HeatmapDimension("rigol.1.high_level", "High", "V", (.1, .2, .3, .2, .3, .4)),
        HeatmapDimension("rigol.1.low_level", "Low", "V", (-.1, -.2, -.3, 0., -.1, -.2)),
    ), 6)
    if order == "alphabetical":
        coordinates = replace(coordinates, dimensions=tuple(sorted(coordinates.dimensions, key=lambda d: d.id)))
    ranges = default_heatmap_ranges(coordinates, "frequency", axis_id)
    assert ranges["rigol.1.offset"].minimum == ranges["rigol.1.offset"].maximum == 0.
    # The selected representation retains all three distinct carrier levels.
    retained = [i for i in range(6) if all(r.minimum <= d.values[i] <= r.maximum
                for d in coordinates.dimensions if not d.is_frequency
                for r in (ranges[d.id],))]
    assert retained == [0, 1, 2]


def test_empty_spectrum_coordinate_controls_remain_readable(dc_archive, shell_qt_application):
    app = shell_qt_application
    run = ThatecRunReader.describe(dc_archive)
    row = replace(next(r for r in run.rows.values() if len(r.shape) == 2), shape=(0, 3), timestamp_count=0)
    coordinates = build_heatmap_coordinates(dc_archive, run, row)
    tab = HeatmapResultsTab()
    try:
        tab.resize(1280, 900)
        tab.show()
        tab._accept_coordinates(row, (coordinates, ()))
        app.processEvents()
        assert not tab._range_combos
        assert tab.y_axis_combo.currentData() == "measurement.checkpoint"
    finally:
        tab.close()
        tab.deleteLater()
        app.processEvents()


def test_two_physical_axes_do_not_pin_coordinates_derived_from_both():
    from app.ui.results.heatmap_coordinates import default_heatmap_ranges

    coordinates = HeatmapCoordinates((
        HeatmapDimension("frequency", "Frequency", "Hz", (), True),
        HeatmapDimension("rigol.1.offset", "Offset", "V", (0., 0., .1, .1)),
        HeatmapDimension("rigol.1.amplitude", "Amplitude", "V", (.2, .4, .2, .4)),
        HeatmapDimension("rigol.1.high_level", "High", "V", (.1, .2, .2, .3)),
    ), 4)
    ranges = default_heatmap_ranges(coordinates, "rigol.1.offset", "rigol.1.amplitude")
    assert ranges["rigol.1.high_level"].minimum == .1
    assert ranges["rigol.1.high_level"].maximum == .3


def test_near_zero_linked_values_cannot_stall_default_selection(monkeypatch):
    from app.ui.results import heatmap_coordinates as module

    coordinates = HeatmapCoordinates((
        HeatmapDimension("frequency", "Frequency", "Hz", (), True),
        HeatmapDimension("rigol.1.amplitude", "Amplitude", "V", (.2, .2)),
        HeatmapDimension("rigol.1.offset", "Offset", "V", (-1.0000000005e-15, 0.)),
    ), 2)
    original = np.isclose
    calls = 0

    def guarded_comparison(*args, **kwargs):
        nonlocal calls
        calls += 1
        # A watchdog makes the regression fail instead of hanging pytest.
        assert calls < 12, "Default selection did not terminate near zero."
        return original(*args, **kwargs)

    monkeypatch.setattr(module.np, "isclose", guarded_comparison)
    ranges = module.default_heatmap_ranges(coordinates, "frequency", "rigol.1.amplitude")
    assert ranges["rigol.1.offset"].minimum == -1.0000000005e-15
    assert ranges["rigol.1.offset"].maximum == 0.


def test_dc_alias_ranges_remain_editable_and_keep_exact_source_indices(dc_archive):
    from app.ui.results.heatmap_coordinates import HeatmapRange, default_heatmap_ranges

    run = ThatecRunReader.describe(dc_archive)
    row = next(r for r in run.rows.values() if len(r.shape) == 2)
    coordinates = build_heatmap_coordinates(dc_archive, run, row, Hdf5RunReader.points(dc_archive))
    ranges = default_heatmap_ranges(coordinates, "rigol.1.offset", "frequency")
    ranges["rigol.1.high_level"] = HeatmapRange(.15, .3)
    matrix = read_heatmap_matrix(dc_archive, row, coordinates,
                                 HeatmapRequest("rigol.1.offset", "frequency", ranges))
    np.testing.assert_allclose(matrix.x_values, np.arange(10, 21) * .015)
    assert matrix.cell_checkpoints[0].tolist() == list(range(10, 21))
