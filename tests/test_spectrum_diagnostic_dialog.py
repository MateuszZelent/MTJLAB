"""Shown raw REF diagnostics: units, unavailable states and worker teardown."""

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint

from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.devices.anritsu_ms2830a.ui.diagnostic_dialog import SpectrumDiagnosticDialog
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_reference_diagnostics import archive
from tests.test_spectrum_resonance_dialog import application, destroy


def populate(dialog, tmp_path):
    source, _context, _profile = archive(tmp_path)
    dialog.reference.setText(str(source))
    dialog.output.setText(str(tmp_path / "diagnostic.json"))
    dialog.bins.setText("0, 1, 2")
    return source


@pytest.mark.parametrize("theme,size", [("light", (1000, 900)), ("dark", (750, 700))])
@pytest.mark.parametrize("duration", ["1 s", "4 s"])
def test_shown_diagnostics_display_real_tables_or_explicit_unavailability(theme, size, duration, tmp_path):
    app = application()
    apply_application_theme(app, theme)
    dialog = SpectrumDiagnosticDialog()
    try:
        dialog.resize(*size)
        dialog.show()
        assert callable(dialog.metric)  # Preserve QPaintDevice's native paint metric method.
        assert dialog.duration.text() == "1 s" and dialog.bins.text() == ""
        source = populate(dialog, tmp_path)
        before = source.read_bytes()
        dialog.duration.setText(duration)
        dialog.start.click()
        assert dialog._busy and not dialog.start.isEnabled()
        assert dialog.progress.isVisible() and dialog.cancel.isEnabled()
        assert "verifying its profile" in dialog.status.text()
        wait_until(app, lambda: not dialog._busy, timeout=25)
        path = tmp_path / "diagnostic.json"
        assert path.exists(), dialog.status.text()
        report = json.loads(path.read_text(encoding="utf-8"))
        assert report["raw_profile_verified"] and report["qualified_ttl_s"] is None
        assert not report["sweep_independence_inferred"]
        assert source.read_bytes() == before
        assert "132 raw sweeps" in dialog.result_label.text()
        assert "No independence, TTL or CI qualification" in dialog.result_label.text()
        assert dialog.bin_selector.isEnabled()
        dialog.bin_selector.setCurrentIndex(1)
        if duration == "1 s":
            assert "32 complete time blocks" in dialog.result_label.text()
            assert dialog.table.rowCount() == len(report["allan_tau_s"])
            assert dialog.table.horizontalHeaderItem(1).text() == "Allan variance (W²)"
            assert float(dialog.table.item(0, 1).text()) == pytest.approx(report["allan_variance_w2"][0][1], rel=1e-5)
            assert int(dialog.table.item(0, 2).text()) == report["allan_pair_counts"][0]
            dialog.metric_selector.setCurrentIndex(dialog.metric_selector.findData("acf"))
            assert dialog.table.rowCount() == len(report["correlation_lag_s"])
            assert float(dialog.table.item(0, 1).text()) == 1
            dialog.bin_selector.setCurrentIndex(0)
            assert dialog.table.rowCount() == 0
            assert "undefined for a constant-power bin" in dialog.table_status.text()
            dialog.bin_selector.setCurrentIndex(1)
            dialog.metric_selector.setCurrentIndex(dialog.metric_selector.findData("allan"))
        else:
            assert "insufficient_blocks" in dialog.result_label.text()
            assert dialog.table.rowCount() == 0 and report["allan_variance_w2"] is None
            assert "No gap interpolation" in dialog.table_status.text()
            for column in range(dialog.table.columnCount()):
                label = dialog.table.horizontalHeaderItem(column).text()
                assert dialog.table.columnWidth(column) >= dialog.table.fontMetrics().horizontalAdvance(label)
        app.processEvents()
        for control in (dialog.start, dialog.cancel, dialog.close_button, dialog.result_label,
                        dialog.status, dialog.table, dialog.table_status):
            assert control.isVisible() and control.height() >= control.fontMetrics().height()
            origin = control.mapTo(dialog, QPoint(0, 0))
            assert dialog.rect().contains(origin)
            assert dialog.rect().contains(origin + QPoint(control.width() - 1, control.height() - 1))
        directory = Path("artifacts/spectrum-diagnostic-ui")
        directory.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(directory / f"result-{theme}-{size[0]}-{duration[0]}.png"))
        dialog.reject()
        wait_until(app, lambda: not dialog.isVisible(), timeout=10)
    finally:
        destroy(dialog, app)
        apply_application_theme(app, "light")


def test_units_bin_errors_existing_report_and_retry_are_visible(tmp_path):
    app = application()
    dialog = SpectrumDiagnosticDialog()
    try:
        dialog.show()
        dialog.start.click()
        assert "Cannot start" in dialog.status.text() and not dialog._busy
        source = populate(dialog, tmp_path)
        before = source.read_bytes()
        dialog.duration.setText("1 Hz")
        dialog.start.click()
        assert "Cannot start" in dialog.status.text() and not dialog._busy
        dialog.duration.setText("1 s")
        dialog.bins.setText("1, 1")
        dialog.start.click()
        assert "Cannot start" in dialog.status.text() and not dialog._busy
        dialog.bins.setText("100")
        dialog.start.click()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        assert "Diagnostics failed" in dialog.status.text()
        assert not (tmp_path / "diagnostic.json").exists()
        dialog.bins.setText("1 2")
        output = tmp_path / "diagnostic.json"
        output.write_bytes(b"existing report")
        dialog.start.click()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        assert "Diagnostics failed" in dialog.status.text()
        assert output.read_bytes() == b"existing report" and source.read_bytes() == before
        dialog.output.setText(str(tmp_path / "new.json"))
        dialog.start.click()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        assert (tmp_path / "new.json").exists(), dialog.status.text()
    finally:
        destroy(dialog, app)


def test_close_pending_analysis_cancels_without_report_or_raw_mutation(tmp_path):
    app = application()
    dialog = SpectrumDiagnosticDialog()
    try:
        dialog.show()
        source = populate(dialog, tmp_path)
        before = source.read_bytes()
        dialog.start.click()
        dialog.close()
        wait_until(app, lambda: not dialog.isVisible(), timeout=20)
        assert not dialog._controller._thread.isRunning()
        assert not (tmp_path / "diagnostic.json").exists() and source.read_bytes() == before
    finally:
        destroy(dialog, app)


def test_workspace_opens_one_diagnostic_dialog_offline_and_blocks_recording():
    app = application()
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=False)
    requests = []
    workspace.request_device.connect(lambda *args: requests.append(args))
    try:
        workspace.resize(1100, 900)
        workspace.show()
        app.processEvents()
        assert workspace.diagnose_reference.isEnabled()
        workspace.diagnose_reference.click()
        dialog = workspace._diagnostic_dialog
        assert dialog is not None and dialog.isVisible()
        workspace.diagnose_reference.click()
        assert workspace._diagnostic_dialog is dialog
        dialog.reject()
        wait_until(app, lambda: workspace._diagnostic_dialog is None, timeout=10)
        workspace._running = True
        workspace.set_available(False)
        assert not workspace.diagnose_reference.isEnabled()
        workspace._open_reference_diagnostics()
        assert workspace._diagnostic_dialog is None and requests == []
    finally:
        workspace.shutdown()
        workspace.deleteLater()
        app.processEvents()
