"""Bounded 2D denoising of derived map planes, never recorded spectra."""

import json
from dataclasses import asdict, replace

import numpy as np
import pytest

from app.ui.results.map_processing import MapProcessing, analyse_map
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414


def analyse(values, *, state, unit="W", transpose=False, cancelled=None, **kwargs):
    # Deliberately non-uniform axes: image windows count samples, not Hz or A.
    frequencies = 1e9 + np.arange(values.shape[1], dtype=float) ** 1.2 * 1e6
    coordinates = np.arange(values.shape[0], dtype=float) ** 1.4 * 1e-3
    return analyse_map(values.T if transpose else values, unit=unit,
                       frequencies_hz=frequencies, coordinate_values=coordinates,
                       frequency_axis=0 if transpose else 1, state=state,
                       cancelled=cancelled, **kwargs)


def test_median_uses_a_joint_2d_neighbourhood_and_keeps_signed_watts():
    # A separable row/column median gives a different centre for this fixture.
    values = np.array([[0., 1., 9.], [2., 3., 8.], [4., 7., 6.]]) * 1e-9 - 4e-9
    result = analyse(values, state=MapProcessing(image_filter="median"))
    assert result.unit == "W"
    assert result.values[1, 1] == pytest.approx(0., abs=1e-24)
    assert np.any(result.values < 0)


def test_gaussian_uses_both_explicit_sigmas_without_clipping_negative_watts():
    values = np.full((3, 3), -4e-9)
    values[1, 1] = 8e-9
    # With axial weight 1/2 and corner weight 1/4, the sum of weights is 4.
    sigma = 1 / np.sqrt(2 * np.log(2))
    result = analyse(values, state=MapProcessing(image_filter="gaussian",
        image_frequency_sigma=sigma, image_sweep_sigma=sigma))
    assert result.unit == "W"
    assert result.values[1, 1] == pytest.approx(-1e-9)
    assert np.all(result.values < 0)


@pytest.mark.parametrize("kind", ["median", "gaussian"])
def test_controlled_noise_is_reduced_without_erasing_the_moving_resonance(kind):
    rng = np.random.default_rng(147)
    frequency = np.arange(171)
    centre = np.linspace(45, 120, 91)
    clean = 2e-9 + 10e-9 * np.exp(-.5 * ((frequency - centre[:, None]) / 9) ** 2)
    noisy = clean + rng.normal(0, 1.8e-9, clean.shape)
    if kind == "median":
        noisy[rng.random(clean.shape) < .025] += 20e-9
    before = noisy.copy()
    state = MapProcessing(image_filter=kind, image_frequency_window=5, image_sweep_window=5)
    result = analyse(noisy, state=state)
    assert np.mean((result.values - clean) ** 2) < .4 * np.mean((noisy - clean) ** 2)
    assert np.median(np.max(result.values, axis=1)) > 10e-9
    assert np.max(np.abs(np.argmax(result.values, axis=1) - centre)) < 6
    np.testing.assert_array_equal(noisy, before)


@pytest.mark.parametrize("kind", ["median", "gaussian"])
@pytest.mark.parametrize("unit", ["W", "dB", "dBm"])
def test_holes_missing_rows_columns_and_tile_edges_stay_gaps(kind, unit):
    values = np.full((137, 149), -30. if unit == "dBm" else -2.)
    values[64, 64] = np.nan
    values[40:46] = np.nan
    values[:, 95:101] = np.nan
    values[120:, 120:] = np.nan  # includes entirely missing filter windows
    before = values.copy()
    result = analyse(values, unit=unit, state=MapProcessing(image_filter=kind,
        image_frequency_window=7, image_sweep_window=5))
    assert result.unit == unit
    np.testing.assert_array_equal(np.isnan(result.values), np.isnan(values))
    np.testing.assert_allclose(result.values[np.isfinite(values)], values[np.isfinite(values)])
    np.testing.assert_array_equal(values, before)


@pytest.mark.parametrize("kind", ["median", "gaussian"])
def test_dbm_filtering_and_removed_preview_use_watts_even_for_even_finite_counts(kind):
    power = np.array([[1., 1., 1.], [1., 1., 9.], [9., 9., 9.]]) * 1e-6
    power[0, 0] = np.nan  # median of 1 and 9 microW is 5 microW, not -25.23 dBm
    dbm = 10 * np.log10(power / 1e-3)
    state = MapProcessing(image_filter=kind)
    result = analyse(dbm, unit="dBm", state=state)
    removed = analyse(dbm, unit="dBm", state=replace(state, view="removed"))
    assert result.unit == "dBm" and removed.unit == "W"
    if kind == "median":
        assert result.values[1, 1] == pytest.approx(10 * np.log10(5e-3))
    else:
        q = np.exp(-.5)
        expected_w = (1 + 20 * q + 19 * q * q) * 1e-6
        expected_w /= 1 + 4 * q + 3 * q * q
        assert result.values[1, 1] == pytest.approx(10 * np.log10(expected_w / 1e-3))
    np.testing.assert_allclose(removed.values + 1e-3 * 10 ** (result.values / 10),
                               power, rtol=1e-13, atol=1e-20)
    assert np.any(removed.values < 0) and np.any(removed.values > 0)
    assert any("W" in note and "dBm" in note for note in removed.notes)


@pytest.mark.parametrize("kind", ["median", "gaussian"])
@pytest.mark.parametrize("unit", ["W", "dB"])
def test_signed_inputs_and_removed_noise_stay_in_their_domain(kind, unit):
    values = np.full((9, 11), -4.)
    values[4, 5] = 8.
    state = MapProcessing(image_filter=kind)
    result = analyse(values, unit=unit, state=state)
    removed = analyse(values, unit=unit, state=replace(state, view="removed"))
    assert result.unit == removed.unit == unit
    assert np.any(result.values < 0)
    np.testing.assert_allclose(result.values + removed.values, values)


@pytest.mark.parametrize("kind", ["median", "gaussian"])
@pytest.mark.parametrize("view", ["result", "removed"])
def test_transpose_and_anisotropic_windows_are_invariant(kind, view):
    values = np.random.default_rng(7).normal(size=(71, 133))
    values[63:66, 63:66] = np.nan
    state = MapProcessing(image_filter=kind, image_frequency_window=7, image_sweep_window=3,
        image_frequency_sigma=1.7, image_sweep_sigma=.6, view=view)
    direct = analyse(values, state=state)
    transposed = analyse(values, state=state, transpose=True)
    np.testing.assert_array_equal(transposed.values.T, direct.values)
    assert transposed.unit == direct.unit


@pytest.mark.parametrize("kind", ["median", "gaussian"])
def test_input_preview_bypasses_inapplicable_filters_and_off_is_exact(kind):
    values = np.array([[1e-12, np.nan, -7e-12], [-3e-12, 0., 5e-12]])
    state = MapProcessing(image_filter=kind, component="median_db", mask_lines=True, view="input")
    np.testing.assert_array_equal(analyse(values, state=state).values, values)
    off = replace(state, image_filter="none", component="none", mask_lines=False, view="result")
    assert not off.active
    np.testing.assert_array_equal(analyse(values, state=off).values, values)
    assert MapProcessing(image_filter=kind).active


@pytest.mark.parametrize("kind", ["median", "gaussian"])
def test_one_sample_windows_are_exact_and_do_not_require_three_sweep_points(kind):
    values = np.array([[-3., 1., 7., np.nan]])
    result = analyse(values, unit="dB", state=MapProcessing(image_filter=kind,
        image_frequency_window=1, image_sweep_window=1))
    np.testing.assert_array_equal(result.values, values)


@pytest.mark.parametrize("kind", ["median", "gaussian"])
def test_component_subtraction_precedes_smoothing_and_removed_is_stage_local(kind):
    values = np.random.default_rng(4).uniform(1e-9, 4e-9, size=(15, 17))
    original = values.copy()
    dbm = 10 * np.log10(values / 1e-3)
    base = MapProcessing(component="median_power")
    baseline = analyse(dbm, unit="dBm", state=base)
    state = replace(base, image_filter=kind)
    result = analyse(dbm, unit="dBm", state=state)
    expected = analyse(baseline.values, state=MapProcessing(image_filter=kind))
    removed = analyse(dbm, unit="dBm", state=replace(state, view="removed"))
    component = analyse(dbm, unit="dBm", state=replace(state, view="component"))
    assert result.unit == removed.unit == component.unit == "W"
    np.testing.assert_allclose(result.values, expected.values, atol=1e-24)
    np.testing.assert_allclose(result.values + removed.values, baseline.values, atol=1e-24)
    np.testing.assert_allclose(result.component, np.median(values, axis=0))
    np.testing.assert_allclose(component.values, np.broadcast_to(result.component, values.shape))
    np.testing.assert_array_equal(values, original)


@pytest.mark.parametrize("kwargs", [
    {"image_filter": "unknown"},
    {"image_frequency_window": 0}, {"image_sweep_window": -3},
    {"image_frequency_window": 4}, {"image_sweep_window": 33},
    {"image_frequency_window": 3.0}, {"image_sweep_window": True},
    {"image_frequency_window": float("nan")}, {"image_sweep_window": "3"},
    {"image_frequency_sigma": 0}, {"image_sweep_sigma": -1},
    {"image_frequency_sigma": float("inf")}, {"image_sweep_sigma": float("nan")},
])
def test_invalid_image_settings_are_rejected_even_when_off(kwargs):
    with pytest.raises(ValueError, match="(?i)(image|window|sigma)"):
        MapProcessing(**kwargs)


def test_removed_preview_requires_an_enabled_image_filter():
    with pytest.raises(ValueError, match="(?i)(image|denois)"):
        analyse(np.ones((5, 7)), state=MapProcessing(view="removed"))


@pytest.mark.parametrize("kind", ["median", "gaussian"])
def test_cancellation_interrupts_large_filters_without_modifying_input(kind):
    values = np.random.default_rng(2).normal(size=(257, 263))
    before = values.copy()
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 5

    with pytest.raises(InterruptedError, match="cancelled"):
        analyse(values, state=MapProcessing(image_filter=kind, image_frequency_window=31,
            image_sweep_window=31), cancelled=cancelled)
    assert calls == 5
    np.testing.assert_array_equal(values, before)


def test_all_settings_survive_the_existing_asdict_manifest():
    from app.ui.results.analysis_export import processing_manifest

    state = MapProcessing(image_filter="gaussian", image_frequency_window=7,
        image_sweep_window=5, image_frequency_sigma=1.5, image_sweep_sigma=.75, view="removed")
    manifest = json.loads(json.dumps(processing_manifest(state), allow_nan=False))
    assert manifest == json.loads(json.dumps(asdict(state)))
    assert manifest["image_filter"] == "gaussian"
    assert manifest["image_frequency_window"] == 7 and manifest["image_sweep_window"] == 5
    assert manifest["image_frequency_sigma"] == 1.5 and manifest["image_sweep_sigma"] == .75
    assert manifest["view"] == "removed"


@pytest.mark.parametrize("kind", ["median", "gaussian"])
def test_tile_boundaries_match_independent_joint_neighbourhoods(kind):
    values = np.random.default_rng(8).normal(size=(132, 137))
    values[62, 64] = np.nan
    state = MapProcessing(image_filter=kind, image_frequency_window=7, image_sweep_window=3,
        image_frequency_sigma=1.7, image_sweep_sigma=.6)
    result = analyse(values, state=state)
    for row, column in ((0, 0), (63, 64), (64, 63), (127, 128), (131, 136)):
        row_indices = np.arange(max(0, row - 1), min(values.shape[0], row + 2))
        column_indices = np.arange(max(0, column - 3), min(values.shape[1], column + 4))
        neighbours = values[np.ix_(row_indices, column_indices)]
        if kind == "median":
            expected = np.nanmedian(neighbours)
        else:
            weights = np.exp(-.5 * (((row_indices[:, None] - row) / .6) ** 2 +
                                    ((column_indices - column) / 1.7) ** 2))
            weights[~np.isfinite(neighbours)] = 0
            expected = np.nansum(weights * neighbours) / np.sum(weights)
        assert result.values[row, column] == pytest.approx(expected, abs=1e-14)


@pytest.mark.parametrize("kind", ["median", "gaussian"])
def test_line_masking_precedes_image_filters_and_raw_companion_is_immutable(kind):
    frequencies = np.linspace(1e9, 1.2e9, 101)
    raw = np.full((11, 101), -80.)
    raw[:, 50] = -60.
    signed = np.full(raw.shape, -2e-10)
    signed[:, 50] = 8e-10
    raw_before, signed_before = raw.copy(), signed.copy()
    result = analyse_map(signed, unit="W", frequencies_hz=frequencies,
        coordinate_values=np.arange(11), frequency_axis=1, line_input_dbm=raw,
        state=MapProcessing(image_filter=kind, mask_lines=True))
    assert result.masked_frequencies_hz == (frequencies[50],)
    assert np.all(np.isnan(result.values[:, 50]))
    np.testing.assert_allclose(result.values[:, :50], -2e-10)
    np.testing.assert_allclose(result.values[:, 51:], -2e-10)
    np.testing.assert_array_equal(raw, raw_before)
    np.testing.assert_array_equal(signed, signed_before)


def test_payload_processing_preserves_axis_checkpoint_and_baseline_metadata():
    from app.ui.results.heatmap_tab import _HeatmapPayload, _process_map_payload

    values = np.arange(35, dtype=float).reshape(5, 7) * 1e-9 - 2e-8
    values[1, 2] = np.nan
    checkpoints = np.arange(35).reshape(5, 7)
    checkpoints[1, 2] = -1
    payload = _HeatmapPayload(values, np.linspace(1e9, 2e9, 7), np.arange(5) * .001,
        "Frequency", "Hz", "Current", "A", "Signed power", "W", (-2e-8, 2e-8),
        1, checkpoints, ("existing provenance",), ("recorded reference",))
    original = payload.matrix.copy()
    result = _process_map_payload(payload, MapProcessing(image_filter="gaussian"), 1)
    assert result.x_values is payload.x_values and result.y_values is payload.y_values
    assert result.cell_checkpoints is checkpoints and result.missing_checkpoints == 1
    assert (result.x_label, result.x_unit, result.y_label, result.y_unit) == (
        "Frequency", "Hz", "Current", "A")
    assert result.baselines == payload.baselines and result.z_unit == "W"
    assert result.processing_notes[0] == "existing provenance"
    assert any("2D" in note for note in result.processing_notes)
    np.testing.assert_array_equal(payload.matrix, original)


@pytest.mark.parametrize("width", [650, 1200])
@pytest.mark.parametrize("dark", [False, True])
def test_rendered_fluent_image_controls_are_editable_and_reset_exactly(
    shell_qt_application, tmp_path, width, dark,
):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    from qfluentwidgets import DoubleSpinBox, SpinBox, qconfig

    from app.ui.design_system import apply_application_theme
    from app.ui.results.map_controls import MapProcessingControls

    app = shell_qt_application
    previous_theme = qconfig.theme.value.lower()
    apply_application_theme(app, "dark" if dark else "light")
    host = QWidget()
    layout = QVBoxLayout(host)
    controls = MapProcessingControls(host)
    layout.addWidget(controls)
    layout.addStretch(1)
    host.resize(width, 900)
    host.show()
    emitted = []
    controls.changed.connect(emitted.append)
    try:
        for _ in range(3):
            app.processEvents()
        assert not controls.state.active
        assert controls.image_filter.currentData() == "none"
        assert all(not spin.isEnabled() for spin in controls.image_windows.values())
        controls.image_filter.setCurrentIndex(controls.image_filter.findData("gaussian"))
        controls.image_windows["image_frequency_window"].setValue(7)
        controls.image_windows["image_sweep_window"].setValue(5)
        controls.image_sigmas["image_frequency_sigma"].setValue(1.5)
        controls.image_sigmas["image_sweep_sigma"].setValue(.75)
        app.processEvents()
        assert emitted[-1] == MapProcessing(image_filter="gaussian", image_frequency_window=7,
            image_sweep_window=5, image_frequency_sigma=1.5, image_sweep_sigma=.75)
        for spin in controls.image_windows.values():
            assert isinstance(spin, SpinBox)
        for spin in controls.image_sigmas.values():
            assert isinstance(spin, DoubleSpinBox) and spin.isEnabled()
        for spin in (*controls.image_windows.values(), *controls.image_sigmas.values()):
            assert spin.lineEdit().width() >= spin.fontMetrics().horizontalAdvance(spin.text()) + 8
        visible = (controls.image_filter, *controls.image_windows.values(),
                   *controls.image_sigmas.values())
        for widget in visible:
            assert widget.isVisible() and widget.width() > 0 and widget.height() > 0
            top_left = widget.mapTo(host, QPoint(0, 0))
            bottom_right = widget.mapTo(host, widget.rect().bottomRight())
            assert host.rect().contains(top_left) and host.rect().contains(bottom_right)
        assert "bins" in controls.image_note.text() and "points" in controls.image_note.text()
        assert "Narrow peaks" in controls.image_note.text() and "per spectrum" in controls.image_note.text()
        assert host.grab().save(str(tmp_path / f"map-image-{'dark' if dark else 'light'}-{width}.png"))
        controls.view.setCurrentIndex(controls.view.findData("removed"))
        assert emitted[-1].view == "removed"
        controls.image_filter.setCurrentIndex(controls.image_filter.findData("none"))
        assert emitted[-1].view == "result"
        controls.reset.click()
        assert emitted[-1] == MapProcessing() and not controls.state.active
        controls.image_filter.setCurrentIndex(controls.image_filter.findData("median"))
        assert all(not spin.isEnabled() for spin in controls.image_sigmas.values())
    finally:
        host.close()
        host.deleteLater()
        app.processEvents()
        apply_application_theme(app, previous_theme)


def test_even_window_edits_remain_visible_errors_without_emitting_bad_state(shell_qt_application):
    from app.ui.results.map_controls import MapProcessingControls

    app = shell_qt_application
    controls = MapProcessingControls()
    controls.set_state(MapProcessing(image_filter="median"))
    controls.show()
    app.processEvents()
    emitted = []
    controls.changed.connect(emitted.append)
    try:
        spin = controls.image_windows["image_frequency_window"]
        spin.setValue(4)
        assert controls.state.image_frequency_window == 3 and not emitted
        assert controls.image_error.isVisible() and "odd" in controls.image_error.text()
        spin.setValue(5)
        assert emitted[-1].image_frequency_window == 5
        assert not controls.image_error.isVisible()
    finally:
        controls.close()
        controls.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("action", ["input", "off"])
def test_rejected_window_edit_cannot_block_input_preview_or_turning_filter_off(
    shell_qt_application, action,
):
    from app.ui.results.map_controls import MapProcessingControls

    app = shell_qt_application
    controls = MapProcessingControls()
    controls.set_state(MapProcessing(image_filter="gaussian"))
    controls.show()
    app.processEvents()
    emitted = []
    controls.changed.connect(emitted.append)
    try:
        controls.image_windows["image_frequency_window"].setValue(4)
        assert controls.image_error.isVisible()
        if action == "input":
            controls.view.setCurrentIndex(controls.view.findData("input"))
            assert emitted and emitted[-1].view == "input"
        else:
            controls.image_filter.setCurrentIndex(controls.image_filter.findData("none"))
            assert emitted and not emitted[-1].active
        assert controls.state.image_frequency_window == 3
        assert not controls.image_error.isVisible()
    finally:
        controls.close()
        controls.deleteLater()
        app.processEvents()
