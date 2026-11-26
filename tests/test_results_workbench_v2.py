"""Focused unit, functional, and rendered-geometry regression tests for Results Browser v2."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QMainWindow

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.spectrum.spintronic_fits import (
    DEFAULT_GAMMA_GHZ_PER_T,
    extract_heatmap_ridge,
    fit_gilbert_damping,
    fit_kittel_dispersion,
    fit_spectrum_resonance,
    kittel_frequency_hz,
    magnetic_field_scale_to_tesla,
)
from app.storage import Hdf5RunWriter
from app.ui.design_system import apply_application_theme
from app.ui.measurement_tree import (
    MeasurementTreeDelegate,
    MeasurementTreeModel,
    MeasurementTreeView,
    TreeInteractionMode,
)
from app.ui.results import ResultsPage
from app.ui.results.command_palette import ResultsCommandPalette
from app.ui.shell.page_host import FluentPageHost
from tests.shell_test_isolation import (
    isolated_shell_persistence,  # noqa: F401
)
from tests.shell_test_isolation import (
    shell_qt_application as _shell_qt_application,
)
from tests.test_spectrum_correction_controller import wait_until

shell_qt_application = _shell_qt_application


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lorentzian_dbm(
    freqs_hz: np.ndarray,
    *,
    f0_hz: float,
    fwhm_hz: float,
    peak_w: float = 2e-6,
    floor_w: float = 1e-11,
) -> np.ndarray:
    gamma = fwhm_hz / 2.0
    power_w = floor_w + peak_w * (gamma**2) / ((freqs_hz - f0_hz) ** 2 + gamma**2)
    return 10.0 * np.log10(power_w) + 30.0


def _write_fmr_and_mtj_run(path: Path) -> None:
    """Create a rich synthetic FMR + MTJ hysteresis HDF5 run (strictly read-only after close)."""
    recipe_source = (
        "schema_version: 1\n"
        "name: FMR Field x Current Sweep\n"
        "metadata:\n"
        "  operator: Lab Operator\n"
        "  sample_id: W42-R2C4\n"
        "steps:\n"
        "  - type: sweep\n"
        "    name: Magnetic Field Sweep\n"
        "    parameter: moke.field\n"
        "    start: 0.02\n"
        "    stop: 0.12\n"
        "    points: 6\n"
        "    steps:\n"
        "      - type: acquire_spectrum\n"
        "        name: Anritsu FMR Trace\n"
    )
    writer = Hdf5RunWriter(
        path,
        recipe_source=recipe_source,
        settings_source="profile:\n  id: fmr\n  name: FMR Lab\n",
        plan_hash="c" * 64,
        device_idn={"anritsu": "ANRITSU,MS2830A", "keithley": "KEITHLEY,2636B"},
        simulation_metadata={
            "enabled": True,
            "execution_mode": "simulation",
            "outputs_forced_off": False,
        },
    )
    freqs_hz = np.linspace(1.0e9, 10.0e9, 181)
    # Two-pass field sweep so MTJ hysteresis metrics (Rp, Rap, TMR, Hc) are also exercisable
    fields_t = (0.02, 0.04, 0.06, 0.08, 0.10, 0.12)
    currents_a = (0.001, 0.002)
    idx = 0
    for current_a in currents_a:
        for field_t in fields_t:
            f0 = float(
                kittel_frequency_hz([field_t], m_eff_t=1.2, h_k_t=0.015)[0]
            )
            fwhm = 0.06e9 + 2.0 * 0.012 * f0
            powers = _lorentzian_dbm(freqs_hz, f0_hz=f0, fwhm_hz=fwhm)
            # Add a synthetic stationary EMI spur at 3.85 GHz
            powers[np.argmin(np.abs(freqs_hz - 3.85e9))] += 3.0
            # Synthetic MTJ resistance switching from ~100 Ohm (P) to ~220 Ohm (AP)
            r_ohm = 100.0 if field_t >= 0.07 else 220.0
            voltage_v = current_a * r_ohm
            writer.append(
                MeasurementPoint(
                    index=idx,
                    setpoints={
                        "keithley.A.current": current_a,
                        "keithley.B.voltage": field_t,
                    },
                    measurements={
                        "keithley.A.voltage_v": voltage_v,
                        "keithley.A.current_a": current_a,
                        "keithley.A.resistance_ohm": r_ohm,
                    },
                ),
                SpectrumTrace(
                    frequencies_hz=tuple(float(f) for f in freqs_hz),
                    powers_dbm=tuple(float(p) for p in powers),
                    acquired_at_utc=datetime.now(UTC),
                    trace_name="TRAC1",
                ),
                device_states={"anritsu": {"rbw_hz": 100_000.0}},
            )
            idx += 1
    writer.close("completed")


# ---------------------------------------------------------------------------
# 1. Pure SI Spintronic Physics Unit Tests
# ---------------------------------------------------------------------------


def test_fit_spectrum_resonance_recovers_lorentzian_and_gaussian_si_metrics():
    freqs_hz = np.linspace(3.0e9, 7.0e9, 401)
    f0_true = 4.85e9
    fwhm_true = 0.16e9
    peak_w = 5e-6
    floor_w = 1e-11
    dbm = _lorentzian_dbm(
        freqs_hz, f0_hz=f0_true, fwhm_hz=fwhm_true, peak_w=peak_w, floor_w=floor_w
    )

    lor = fit_spectrum_resonance(freqs_hz, dbm, unit="dBm", shape="lorentzian")
    assert lor.fit.center_hz == pytest.approx(f0_true, rel=1e-3)
    assert lor.fit.fwhm_hz == pytest.approx(fwhm_true, rel=2e-2)
    assert lor.q_factor == pytest.approx(f0_true / fwhm_true, rel=2e-2)
    assert lor.r_squared > 0.999
    # Lorentzian integral area in linear power is (pi/2) * amplitude * FWHM [W*Hz]
    expected_area_w_hz = (math.pi / 2.0) * peak_w * fwhm_true
    assert lor.fit.finite_window_area_w_hz == pytest.approx(expected_area_w_hz, rel=6e-2)

    # Gaussian synthetic trace
    sigma = fwhm_true / (2.0 * math.sqrt(2.0 * math.log(2.0)))
    g_w = floor_w + peak_w * np.exp(-0.5 * ((freqs_hz - f0_true) / sigma) ** 2)
    g_dbm = 10.0 * np.log10(g_w) + 30.0
    gau = fit_spectrum_resonance(freqs_hz, g_dbm, unit="dBm", shape="gaussian")
    assert gau.fit.center_hz == pytest.approx(f0_true, rel=1e-3)
    assert gau.fit.fwhm_hz == pytest.approx(fwhm_true, rel=2e-2)
    assert gau.r_squared > 0.999


def test_kittel_dispersion_and_gilbert_damping_recover_true_parameters():
    fields_t = np.linspace(0.02, 0.30, 15)
    m_eff_true = 1.18
    h_k_true = 0.0185
    freqs_hz = kittel_frequency_hz(
        fields_t,
        m_eff_t=m_eff_true,
        h_k_t=h_k_true,
        gamma_ghz_per_t=DEFAULT_GAMMA_GHZ_PER_T,
    )
    kfit = fit_kittel_dispersion(fields_t, freqs_hz)
    assert kfit.m_eff_t == pytest.approx(m_eff_true, rel=1e-3)
    assert kfit.h_k_t == pytest.approx(h_k_true, abs=2e-4)
    assert kfit.r_squared > 0.9999

    alpha_true = 0.0114
    df0_true = 45e6
    fwhms_hz = df0_true + 2.0 * alpha_true * freqs_hz
    gfit = fit_gilbert_damping(freqs_hz, fwhms_hz)
    assert gfit.alpha_eff == pytest.approx(alpha_true, rel=1e-4)
    assert gfit.inhomogeneous_fwhm_hz == pytest.approx(df0_true, rel=1e-4)
    assert gfit.r_squared > 0.9999

    assert magnetic_field_scale_to_tesla("T") == pytest.approx(1.0)
    assert magnetic_field_scale_to_tesla("mT") == pytest.approx(1e-3)
    assert magnetic_field_scale_to_tesla("V") is None


def test_extract_heatmap_ridge_tracks_fmr_mode_and_excludes_emi_bands():
    freqs_hz = np.linspace(1.0e9, 14.0e9, 261)
    fields_t = np.linspace(0.03, 0.15, 7)
    true_f0 = kittel_frequency_hz(fields_t, m_eff_t=1.1, h_k_t=0.015)
    matrix = np.vstack(
        [_lorentzian_dbm(freqs_hz, f0_hz=float(f0), fwhm_hz=0.15e9) for f0 in true_f0]
    )
    # Inject a huge stationary EMI spur at 2.40 GHz that would fool naive argmax
    spur_idx = int(np.argmin(np.abs(freqs_hz - 2.40e9)))
    matrix[:, spur_idx] = -10.0

    sweeps, tracked = extract_heatmap_ridge(
        matrix,
        freqs_hz,
        fields_t,
        frequency_axis=1,
        exclude_bands_hz=[(2.35e9, 2.45e9)],
    )
    assert sweeps.size == fields_t.size
    assert np.allclose(tracked, true_f0, atol=0.1e9)


# ---------------------------------------------------------------------------
# 2. Rendered PySide6 Workbench v2 Geometry & Functional Tests
# ---------------------------------------------------------------------------


def _mount_hosted_results(output_dir: Path, width: int = 1440, height: int = 900):
    window = QMainWindow()
    page = ResultsPage(str(output_dir))
    host = FluentPageHost(page, window)
    window.setCentralWidget(host)
    window.resize(width, height)
    window.show()
    return window, host, page


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_results_v2_workbench_rendered_geometry_fit_wipe_slices_and_readonly_hdf5(
    tmp_path: Path, shell_qt_application, theme: str
):
    app = shell_qt_application
    apply_application_theme(app, theme)
    run_path = tmp_path / "fmr_mtj_sweep.h5"
    _write_fmr_and_mtj_run(run_path)
    digest_before = _sha256(run_path)

    window, host, page = _mount_hosted_results(tmp_path, width=1440, height=900)
    try:
        page.spectrum_tab.spectrum_plot.apply_theme(theme)
        page.heatmap_tab.apply_theme(theme)
        page.scalar_tab.apply_theme(theme)
        wait_until(app, lambda: page.file_browser._refresh_task is None)
        assert page.runs.topLevelItemCount() == 1
        page.runs.setCurrentItem(page.runs.topLevelItem(0))
        wait_until(app, lambda: page._result_task is None and page._thatec_run is not None)
        app.processEvents()

        # --- Overview KPI Strip & Header ---
        assert page.metadata_panel.kpi_strip.isVisibleTo(window)
        assert "12" in page.metadata_panel._kpi_cards["points"][0].text()
        assert "Completed" in page.run_status_label.text()
        assert host.scroll_area.horizontalScrollBar().maximum() == 0

        # --- Sweep Tree: shared MeasurementTreeView + truthful historical states ---
        page.result_tabs.setCurrentIndex(page._tree_index)
        app.processEvents()
        assert isinstance(page.sweep_tree.measurement_tree, MeasurementTreeView)
        assert isinstance(page.sweep_tree.tree_model, MeasurementTreeModel)
        assert isinstance(page.sweep_tree.measurement_tree.itemDelegate(), MeasurementTreeDelegate)
        assert page.sweep_tree.measurement_tree._interaction_mode is TreeInteractionMode.READ_ONLY
        assert page.sweep_tree.open_node_button.isVisibleTo(window)
        # Historical states must NOT remain READY/WAITING on a completed run
        phases = {
            state.get("phase")
            for state in page.sweep_tree.tree_model._states.values()
            if isinstance(state, dict)
        }
        assert phases
        assert "ready" not in phases
        assert "waiting" not in phases
        assert phases <= {"recorded", "applied"}
        sweep_requests: list[tuple[object, object]] = []
        page.open_sweep_requested.connect(lambda p, r: sweep_requests.append((p, r)))
        page.sweep_tree.open_node_button.click()
        app.processEvents()
        assert len(sweep_requests) == 1

        # --- Spectrum Tab: data-first above the fold + Fit + A/B Wipe + Scrubber + Inspector ---
        page.result_tabs.setCurrentIndex(page._spectrum_index)
        app.processEvents()
        page.points.setCurrentIndex(page.points.model().index(0, 0))
        wait_until(app, lambda: not page.spectrum_tab._read_tasks)
        app.processEvents()

        spec_plot = page.spectrum_tab.spectrum_plot
        spec_top = spec_plot.mapTo(page, QPoint(0, 0)).y()
        assert spec_plot.isVisibleTo(window)
        assert spec_top < 260, f"Spectrum plot pushed below fold (y={spec_top})"
        assert spec_plot.height() >= 260
        assert host.scroll_area.horizontalScrollBar().maximum() == 0

        # Curve Fit toggle & readout
        page.spectrum_tab.fit_button.setChecked(True)
        app.processEvents()
        assert "f₀" in page.spectrum_tab.fit_readout_label.text()
        assert "FWHM" in page.spectrum_tab.fit_readout_label.text()
        assert "R²" in page.spectrum_tab.fit_readout_label.text()

        # Switch to Gaussian model
        page.spectrum_tab.fit_model_combo.setCurrentIndex(1)
        app.processEvents()
        assert "FWHM" in page.spectrum_tab.fit_readout_label.text()

        # A/B Wipe curtain toggle
        page.spectrum_tab.ab_wipe_button.setChecked(True)
        app.processEvents()
        assert spec_plot._ab_enabled is True
        assert spec_plot._ab_line.isVisible()

        # Checkpoint scrubber steps smoothly
        assert page.spectrum_tab.checkpoint_slider.maximum() == 11
        page.spectrum_tab.next_button.click()
        wait_until(app, lambda: not page.spectrum_tab._read_tasks)
        app.processEvents()
        assert "2 / 12" in page.spectrum_tab.position_label.text()

        # Inspector drawer toggles cleanly
        assert page.spectrum_tab.inspector_drawer.isVisibleTo(window)
        page.spectrum_tab.toggle_inspector()
        app.processEvents()
        assert not page.spectrum_tab.inspector_drawer.isVisibleTo(window)
        page.spectrum_tab.toggle_inspector()
        app.processEvents()
        assert page.spectrum_tab.inspector_drawer.isVisibleTo(window)

        # --- Heatmap Tab: 2D Map + Live 1D Slice + P(H) Cut + Kittel/Ridge ---
        page.result_tabs.setCurrentIndex(page._heatmap_index)
        app.processEvents()
        page.heatmap_tab.load_button.click()
        wait_until(
            app,
            lambda: page.heatmap_tab.heatmap._data is not None
            and not page.heatmap_tab._read_tasks,
        )
        app.processEvents()

        hm = page.heatmap_tab.heatmap
        hm_top = hm.mapTo(page, QPoint(0, 0)).y()
        assert hm.isVisibleTo(window)
        assert hm_top < 260, f"Heatmap plot pushed below fold (y={hm_top})"
        assert hm.height() >= 220
        assert page.heatmap_tab.slice_panel.isVisibleTo(window)
        assert len(page.heatmap_tab.slice_curve.getData()[0]) > 10

        # Toggle vertical cut P(H) & hover cell
        page.heatmap_tab.vcut_toggle_button.setChecked(True)
        page.heatmap_tab._update_cross_sections(1, 30)
        app.processEvents()
        assert page.heatmap_tab.vcut_panel.isVisibleTo(window)
        assert len(page.heatmap_tab.vcut_curve.getData()[0]) >= 2

        # Toggle Kittel / Ridge dispersion overlay
        page.heatmap_tab.kittel_button.setChecked(True)
        app.processEvents()
        assert page.heatmap_tab.kittel_readout_label.text()
        assert hm._dispersion_curve.isVisible()

        # --- Scalar Tab: dY/dX and Log Y ---
        page.result_tabs.setCurrentIndex(page._scalar_index)
        wait_until(app, lambda: page.scalar_tab._task is None)
        app.processEvents()
        assert page.scalar_tab.plot.isVisibleTo(window)
        page.scalar_tab.derivative_button.setChecked(True)
        app.processEvents()
        assert "d(" in page.scalar_tab.plot.plotItem.getAxis("left").labelText
        page.scalar_tab.derivative_button.setChecked(False)
        page.scalar_tab.log_y_button.setChecked(True)
        app.processEvents()
        assert page.scalar_tab.plot.plotItem.ctrl.logYCheck.isChecked()

        # --- Command Palette (Ctrl+K) ---
        palette = ResultsCommandPalette(page)
        palette.search.setText("Overview")
        app.processEvents()
        assert palette.list_widget.count() >= 1
        palette._execute_item(palette.list_widget.item(0))
        app.processEvents()
        assert page.result_tabs.stack.currentIndex() == page._overview_index
        palette.close()

        # --- Read-Only HDF5 Integrity Guarantee ---
        assert _sha256(run_path) == digest_before
    finally:
        window.close()
        apply_application_theme(app, "light")


def test_scalar_tab_computes_mtj_figures_of_merit_bar(tmp_path: Path, shell_qt_application):
    """When a resistance-vs-field hysteresis sweep is loaded, the hairline FoM bar shows Rp/Rap/TMR."""
    app = shell_qt_application
    run_path = tmp_path / "mtj_rh_loop.h5"
    writer = Hdf5RunWriter(
        run_path,
        recipe_source="name: mtj-rh\n",
        settings_source="profile:\n  id: mtj\n  name: MTJ\n",
        plan_hash="d" * 64,
        device_idn={},
    )
    # Full hysteresis loop: forward (-0.05 -> +0.05) and backward (+0.05 -> -0.05)
    fields = [-0.05, -0.03, -0.01, 0.01, 0.03, 0.05, 0.03, 0.01, -0.01, -0.03, -0.05]
    resistances = [100.0, 102.0, 105.0, 220.0, 225.0, 226.0, 224.0, 221.0, 103.0, 101.0, 100.0]
    for i, (h_val, r_val) in enumerate(zip(fields, resistances)):
        writer.append(
            MeasurementPoint(
                index=i,
                setpoints={"magnet.field_t": h_val},
                measurements={"keithley.A.resistance_ohm": r_val},
            )
        )
    writer.close("completed")

    window, _host, page = _mount_hosted_results(tmp_path, width=1280, height=760)
    try:
        wait_until(app, lambda: page.file_browser._refresh_task is None)
        page.runs.setCurrentItem(page.runs.topLevelItem(0))
        wait_until(app, lambda: page._result_task is None)
        page.result_tabs.setCurrentIndex(page._scalar_index)
        wait_until(app, lambda: page.scalar_tab._task is None)
        app.processEvents()

        fom_text = page.scalar_tab.fom_label.text()
        assert "Rp" in fom_text
        assert "Rap" in fom_text
        assert "TMR" in fom_text
    finally:
        window.close()


def test_results_v2_narrow_desktop_1024_has_no_horizontal_overflow(
    tmp_path: Path, shell_qt_application
):
    app = shell_qt_application
    run_path = tmp_path / "narrow.h5"
    _write_fmr_and_mtj_run(run_path)
    window, host, page = _mount_hosted_results(tmp_path, width=1024, height=700)
    try:
        wait_until(app, lambda: page.file_browser._refresh_task is None)
        page.runs.setCurrentItem(page.runs.topLevelItem(0))
        wait_until(app, lambda: page._result_task is None)
        app.processEvents()

        for tab_idx in (
            page._overview_index,
            page._tree_index,
            page._scalar_index,
            page._spectrum_index,
            page._heatmap_index,
        ):
            page.result_tabs.setCurrentIndex(tab_idx)
            app.processEvents()
            assert host.scroll_area.horizontalScrollBar().maximum() == 0

        # Toggle FileBrowser filters panel open and verify zero clipped controls
        page.file_browser.filters_toggle_button.setChecked(True)
        app.processEvents()
        assert page.file_browser.filters_panel.isVisibleTo(window)
    finally:
        window.close()


def test_file_browser_breadcrumbs_consolidate_run_bundles_and_quick_scopes(
    tmp_path: Path, shell_qt_application
):
    """Run bundles (data.h5 + sidecars) become 1-click rows with collapsed sidecars and breadcrumbs."""
    from app.ui.results.file_browser import FileBrowserPanel

    app = shell_qt_application
    catalogue = tmp_path / "Cat"
    dut_dir = catalogue / "W42_Vortex" / "devices" / "R19C7"
    run3_dir = dut_dir / "measurements" / "sweeps" / "3_20261125T102512Z_FMR_Sweep"
    run3_dir.mkdir(parents=True)
    run3_h5 = run3_dir / "data.h5"
    _write_fmr_and_mtj_run(run3_h5)
    (run3_dir / "metadata.json").write_text('{"status": "completed"}', encoding="utf-8")
    (run3_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (run3_dir / "recipe.yml").write_text("name: FMR\n", encoding="utf-8")
    (run3_dir / "plan.json").write_text("{}", encoding="utf-8")
    ref_dir = run3_dir / "references"
    ref_dir.mkdir()
    (ref_dir / "ref0000_bg.json").write_text("{}", encoding="utf-8")

    run2_dir = dut_dir / "measurements" / "sweeps" / "2_20261124T235100Z_TMR_Loop"
    run2_dir.mkdir(parents=True)
    w2 = Hdf5RunWriter(
        run2_dir / "data.h5",
        recipe_source="name: TMR\n",
        settings_source="schema_version: 1\n",
        plan_hash="h2",
        device_idn={},
    )
    w2.close("aborted")
    (run2_dir / "metadata.json").write_text('{"status": "aborted"}', encoding="utf-8")

    post_dir = dut_dir / "postprocessing" / "sweeps" / "3_20261125T102512Z_FMR_Sweep"
    post_dir.mkdir(parents=True)
    w_post = Hdf5RunWriter(
        post_dir / "derived_map.h5",
        recipe_source="name: Derived\n",
        settings_source="schema_version: 1\n",
        plan_hash="p1",
        device_idn={},
    )
    w_post.close("completed")

    browser = FileBrowserPanel(str(catalogue), catalogue_tree=True)
    try:
        browser.set_sample_context(dut_dir, catalogue)
        wait_until(app, lambda: browser._refresh_task is None)
        app.processEvents()

        # 1. Breadcrumbs show Samples -> Sample -> R19C7 (skipping boilerplate "devices")
        crumb_texts = [item.text for item in browser.breadcrumb_bar.items]
        assert crumb_texts[0] == "Samples"
        assert crumb_texts[-1] == "R19C7"
        assert "devices" not in [t.casefold() for t in crumb_texts]

        # 2. Measurements appears BEFORE Post-processing, and Post-processing is collapsed by default
        assert browser.runs.topLevelItemCount() == 2
        top0 = browser.runs.topLevelItem(0)
        top1 = browser.runs.topLevelItem(1)
        assert top0.text(0).startswith("Measurements")
        assert top0.isExpanded()
        assert top1.text(0).startswith("Post-processing")
        assert not top1.isExpanded()

        # 3. Under Measurements -> Sweeps, each run bundle is a single clickable row with collapsed Bundle files
        sweeps_group = top0.child(0)
        assert sweeps_group.text(0).startswith("Sweeps")
        assert sweeps_group.childCount() == 2
        run_labels = [sweeps_group.child(i).text(0) for i in range(sweeps_group.childCount())]
        assert any("#3 · FMR Sweep" in lbl for lbl in run_labels)
        assert any("#2 · TMR Loop" in lbl for lbl in run_labels)
        run3_item = next(
            sweeps_group.child(i)
            for i in range(sweeps_group.childCount())
            if "#3" in sweeps_group.child(i).text(0)
        )
        assert not run3_item.isExpanded()
        assert run3_item.childCount() == 1
        assert run3_item.child(0).text(0).startswith("Bundle files")
        assert not run3_item.child(0).isExpanded()

        # 4. Clicking the run row directly selects data.h5
        browser.runs.setCurrentItem(run3_item)
        app.processEvents()
        assert browser.selected_path == run3_h5.resolve()

        # 5. Quick Scope: Resumable shows ONLY the aborted run #2
        browser._on_scope_changed("resumable")
        app.processEvents()
        assert len(browser._filtered_summaries) == 1
        assert browser._filtered_summaries[0].status == "aborted"

        # 6. Clicking the root "Samples" breadcrumb navigates back to catalogue root
        root_key = next(iter(browser._breadcrumb_paths))
        browser._on_breadcrumb_clicked(root_key)
        wait_until(app, lambda: browser._refresh_task is None)
        assert browser.output_dir == catalogue.resolve()
    finally:
        browser.close()
