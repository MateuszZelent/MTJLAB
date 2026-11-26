"""UI and Fluent shell rendering/regression tests for Rigol MTJ Load Calculator."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import h5py
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.devices.rigol_dg1000z.ui.calculator_page import RigolLoadCalculatorPage
from app.devices.rigol_dg1000z.ui.page import RigolConfigurationSnapshot
from app.security.access import AccessPolicy
from app.ui.design_system import apply_application_theme
from app.ui.dialogs import StationMessageBox
from app.ui.results.file_browser import FileBrowserPanel
from app.ui.shell.main_window import MainWindow
from tests.helpers import simulation_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SWEEP_CSV = REPO_ROOT / "artifacts" / "sweep-r19c7-02" / "keithley-a.csv"


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_hdf5_slice(path: Path, kb_a: float, moke_v: float, r_ohm: float = 115.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(path, "w") as handle:
        run = handle.create_group("run")
        run.attrs["status"] = "completed"
        run.attrs["created_at_utc"] = "2026-11-25T10:00:00Z"
        run.attrs["sample_id"] = "S1"
        run.attrs["sample_name"] = "W1"
        run.attrs["sample_coordinate_label"] = "R19C7"
        pts = handle.create_group("points")
        for idx, curr in enumerate((-2e-3, -1e-3, 0.0, 1e-3, 2e-3)):
            grp = pts.create_group(str(idx))
            grp.attrs["complete"] = True
            grp.attrs["status"] = "ok"
            grp.create_dataset(
                "setpoints_json",
                data=json.dumps(
                    {
                        "keithley.A.current": curr,
                        "keithley.B.current": kb_a,
                        "moke_box.vout0.voltage": moke_v,
                    }
                ),
            )
            grp.create_dataset(
                "measurements_json",
                data=json.dumps(
                    {
                        "keithley.A.current_a": curr,
                        "keithley.A.voltage_v": curr * r_ohm,
                        "keithley.A.resistance_ohm": r_ohm,
                        "keithley.A.resistance_valid": float(abs(curr) > 1e-9),
                        "keithley.A.compliance_detected": 0.0,
                        "keithley.B.current_a": kb_a,
                        "moke_box.vout_voltage_v": moke_v,
                    }
                ),
            )
            grp.create_dataset("metadata_json", data="{}")
            grp.create_dataset("device_states_json", data="{}")
    return path


def test_calculator_page_csv_dc_waveform_fixed_r_and_export(
    qapp: QApplication, tmp_path: Path
) -> None:
    snapshots = {
        1: RigolConfigurationSnapshot(
            channel=1,
            waveform="SIN",
            frequency="10 kHz",
            level_mode="Amplitude + Offset",
            high_level="400 mV",
            low_level="-200 mV",
            output_load="HIGHZ",
        ),
        2: RigolConfigurationSnapshot(
            channel=2,
            waveform="DC",
            high_level="250 mV",
            low_level="250 mV",
            output_load="50",
        ),
    }
    page = RigolLoadCalculatorPage(
        settings=simulation_settings(),
        initial_directory=tmp_path,
        catalogue_root=tmp_path,
        rigol_snapshot_provider=lambda ch: snapshots[ch],
    )
    try:
        page.resize(1280, 820)
        page.show()
        qapp.processEvents()

        # Uses the exact shared Results FileBrowserPanel
        assert isinstance(page.file_browser, FileBrowserPanel)

        # 1. Load real keithley-a.csv artifact
        assert page.load_csv_file(SAMPLE_SWEEP_CSV) is True
        qapp.processEvents()
        assert page.active_curve is not None
        assert "54" in page.reference_summary_label.text()
        assert "-75" in page.reference_summary_label.text() or "−75" in page.reference_summary_label.text()

        # Enter HighZ setting 843.95 mV -> ~589.2 mV MTJ, ~5.095 mA
        page.set_load_mode("HIGHZ")
        page.set_signal_mode("dc")
        page.dc_direction_combo.setCurrentIndex(0)  # Rigol setting
        page.dc_value_edit.setText("843.95 mV")
        qapp.processEvents()
        assert page.last_dc_result is not None
        assert page.last_dc_result.point.mtj_voltage_v == pytest.approx(0.58921, abs=1e-3)
        assert page.last_dc_result.point.mtj_current_a == pytest.approx(5.0948e-3, abs=2e-5)

        # Out-of-range value fails closed with banner error (no extrapolation)
        page.dc_value_edit.setText("5 V")
        qapp.processEvents()
        assert page.last_dc_result is None
        assert page.banner.last_severity == "error"
        assert "outside" in page.banner.last_message.lower()

        # 2. Import CH1 AC sine waveform from Rigol card
        assert page.import_from_rigol_channel(1) is True
        qapp.processEvents()
        assert page.signal_mode == "waveform"
        assert page.last_waveform_result is not None
        assert page.last_waveform_result.request.waveform == "SIN"
        assert page.last_waveform_result.nominal.mtj_voltage_max_v > 0.25
        assert page.last_waveform_result.nominal.mtj_current_rms_a > 0.0

        # 3. Switch to Fixed R mode using curve stats button
        page.use_curve_r_range_button.click()
        qapp.processEvents()
        assert page.source_mode == "resistor"
        assert page.last_waveform_result is not None
        assert page.last_waveform_result.resistance_range is not None

        # Default MTJ bounds (±670 mV) auto-populate Rigol V_set span and conservative limit
        page.r_nominal_edit.setText("114 ohm")
        page.r_min_edit.setText("108 ohm")
        page.r_max_edit.setText("120 ohm")
        page.set_load_mode("HIGHZ")
        qapp.processEvents()
        assert page.mtj_v_min_edit.text().strip() == "-670 mV"
        assert page.mtj_v_max_edit.text().strip() == "670 mV"
        assert page.last_limit_estimate is not None
        assert page.last_limit_estimate.rigol_symmetric_limit_v == pytest.approx(0.9491, abs=1e-9)
        assert "949" in page.resistor_span_max_edit.text()

        # Switching to LOAD 50 ohm halves the auto-calculated Rigol V_set span and limit
        page.set_load_mode("50")
        qapp.processEvents()
        assert page.last_limit_estimate is not None
        assert page.last_limit_estimate.rigol_symmetric_limit_v == pytest.approx(0.4745, abs=1e-9)
        assert "474" in page.resistor_span_max_edit.text()

        # 4. Export conversion CSV
        out_csv = tmp_path / "rigol_conversion.csv"
        exported = page.export_conversion_table(out_csv)
        assert exported == out_csv
        assert out_csv.is_file()

        # 5. Theme switching and narrow layout geometry (under full Fluent stylesheet)
        apply_application_theme(qapp, "dark")
        page.apply_theme("dark")
        page.resize(820, 560)
        qapp.processEvents()
        assert page.width() == 820
        assert page.voltage_plot.width() > 200
        assert page.voltage_plot.height() >= 150
        assert page.current_plot.width() > 200
        assert page.current_plot.height() >= 150
        assert page.main_splitter.orientation() == Qt.Orientation.Vertical

        apply_application_theme(qapp, "light")
        page.apply_theme("light")
        page.resize(1360, 880)
        qapp.processEvents()
        assert page.main_splitter.orientation() == Qt.Orientation.Horizontal
    finally:
        page.shutdown()
        page.close()


def test_calculator_page_results_browser_selects_moke_and_keithley_b_for_active_target(
    qapp: QApplication, tmp_path: Path
) -> None:
    dut_dir = tmp_path / "W1" / "devices" / "R19C7" / "measurements" / "sweeps"
    _write_hdf5_slice(dut_dir / "1_20261125T100000Z_s1" / "data.h5", kb_a=0.054, moke_v=-0.075, r_ohm=110.0)
    _write_hdf5_slice(dut_dir / "2_20261125T100500Z_s2" / "data.h5", kb_a=0.020, moke_v=-0.010, r_ohm=122.0)

    page = RigolLoadCalculatorPage(
        settings=simulation_settings(),
        initial_directory=dut_dir.parent.parent,
        catalogue_root=tmp_path,
    )
    try:
        page.resize(1280, 820)
        page.show()
        page.set_sample_context(dut_dir.parent.parent, tmp_path)
        for _ in range(300):
            qapp.processEvents()
            if not page.is_scanning and page.kb_combo.count() == 2:
                break
            QTest.qWait(15)
        assert page.kb_combo.count() == 2

        # Select specific MOKE Box setting (-75 mV) -> filters Keithley B choices to 54 mA (110 ohm)
        target_moke_idx = next(
            i for i in range(page.moke_combo.count()) if "-75" in page.moke_combo.itemText(i)
        )
        page.moke_combo.setCurrentIndex(target_moke_idx)
        qapp.processEvents()
        assert page.kb_combo.count() == 1
        assert page.active_curve is not None
        assert page.active_curve.keithley_b_setpoint_a == pytest.approx(0.054)
        assert page.active_curve.resistance_stats is not None
        assert page.active_curve.resistance_stats.mean_ohm == pytest.approx(110.0)

        # Switch to -10 mV MOKE setting -> switches to 20 mA (122 ohm), reflecting field-dependent R!
        other_moke_idx = next(
            i for i in range(page.moke_combo.count()) if "-10" in page.moke_combo.itemText(i)
        )
        page.moke_combo.setCurrentIndex(other_moke_idx)
        qapp.processEvents()
        assert page.active_curve is not None
        assert page.active_curve.keithley_b_setpoint_a == pytest.approx(0.020)
        assert page.active_curve.resistance_stats is not None
        assert page.active_curve.resistance_stats.mean_ohm == pytest.approx(122.0)
    finally:
        page.shutdown()
        page.close()


def test_main_window_rigol_calculator_route_rendering_and_read_only_safety(
    qapp: QApplication,
) -> None:
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.resize(1360, 880)
        window.show()
        qapp.processEvents()

        assert "rigol_calculator" in window.navigation_routes
        rigol_item = window.navigationInterface.widget("rigolPageHost")
        calc_item = window.navigationInterface.widget("rigol_calculatorPageHost")
        assert rigol_item is not None
        assert calc_item is not None
        rigol_item.setExpanded(False, ani=False)
        qapp.processEvents()
        assert calc_item.isVisible() is False

        rigol_item.setExpanded(True, ani=False)
        qapp.processEvents()
        assert calc_item.isVisible() is True

        # Shortcut button on RigolPage navigates to rigol_calculator
        window._navigate_to("rigol")
        qapp.processEvents()
        window.rigol_page.load_calculator_button.click()
        qapp.processEvents()

        host = window.navigation_routes["rigol_calculator"]
        assert window.stackedWidget.currentWidget() is host
        calc_page = window.rigol_calculator_page
        assert isinstance(calc_page, RigolLoadCalculatorPage)
        assert calc_page.isVisible()
        assert calc_page.width() > 600
        assert calc_page.height() > 400
        assert calc_page.voltage_plot.width() > 250
        assert calc_page.voltage_plot.height() > 120

        # Theme switch propagates to dark plot canvas
        window._set_theme_mode("dark", persist=False)
        qapp.processEvents()
        assert calc_page._theme == "dark"

        # Importing from Rigol card works without touching controller
        assert calc_page.import_from_rigol_channel(1) is True
        qapp.processEvents()

        # Simulation mode disables Set Rigol Voltage Limit button with clear reason
        assert calc_page.set_rigol_limit_button.isEnabled() is False
        assert "simulation" in calc_page.set_rigol_limit_button.toolTip().lower()

        # Calculator remains interactive during execution read-only lock
        window._set_run_ui_locked(True)
        qapp.processEvents()
        assert calc_page.dc_value_edit.isEnabled()
        assert calc_page.load_csv_button.isEnabled()
        assert calc_page.set_rigol_limit_button.isEnabled() is False
        window._set_run_ui_locked(False)
    finally:
        window.close()


def test_main_window_rigol_calculator_stages_voltage_limit_safely(
    qapp: QApplication, tmp_path: Path
) -> None:
    cfg_path = tmp_path / "settings.yml"
    username = AccessPolicy._operating_system_username()
    template_text = (REPO_ROOT / "app" / "resources" / "settings.template.yml").read_text(
        encoding="utf-8"
    )
    template_text = template_text.replace(
        "  user_roles: {}",
        f"  user_roles:\n    {json.dumps(username)}:\n    - \"engineer\"",
        1,
    )
    cfg_path.write_text(template_text, encoding="utf-8")
    window = MainWindow(str(cfg_path), simulation=False)
    try:
        window.resize(1360, 880)
        window.show()
        window._navigate_to("rigol_calculator")
        qapp.processEvents()

        calc_page = window.rigol_calculator_page
        calc_page.set_source_mode("resistor")
        calc_page.r_nominal_edit.setText("114 ohm")
        calc_page.r_min_edit.setText("108 ohm")
        calc_page.r_max_edit.setText("120 ohm")
        calc_page.set_load_mode("HIGHZ")
        calc_page.mtj_v_min_edit.setText("-670 mV")
        calc_page.mtj_v_max_edit.setText("670 mV")
        qapp.processEvents()

        assert calc_page.set_rigol_limit_button.isEnabled() is True
        original_yaml = cfg_path.read_text(encoding="utf-8")

        # 1. Cancel leaves settings untouched
        with patch.object(
            StationMessageBox, "question", return_value=StationMessageBox.StandardButton.Cancel
        ):
            calc_page.set_rigol_limit_button.click()
            qapp.processEvents()
        assert window.settings_page._dirty is False
        assert cfg_path.read_text(encoding="utf-8") == original_yaml

        # 2. Confirm stages combined_voltage_limit=949.1 mV without writing disk silently
        with patch.object(
            StationMessageBox, "question", return_value=StationMessageBox.StandardButton.Yes
        ):
            calc_page.set_rigol_limit_button.click()
            qapp.processEvents()
        assert window.settings_page._dirty is True
        staged_val = window.settings_page._raw["devices"]["rigol"]["safety"]["channels"]["1"][
            "lab_limits"
        ]["combined_voltage_limit"]
        assert "949.1" in str(staged_val)
        assert cfg_path.read_text(encoding="utf-8") == original_yaml  # Never silently writes disk!
        assert "949.1" in calc_page.rigol_limit_summary_label.text()
    finally:
        window.close()
