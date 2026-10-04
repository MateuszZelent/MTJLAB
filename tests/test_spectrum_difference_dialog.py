"""Shown global difference dialog: worker results, units, errors and close."""

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint

pytest.importorskip("scipy")

from app.devices.anritsu_ms2830a.ui.difference_dialog import SpectrumDifferenceDialog
from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.ui.design_system import apply_application_theme
from tests.test_spectrum_resonance_dialog import application, destroy
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_resonance_bootstrap_store import archives
from tests.helpers import simulation_settings


def populate(dialog, tmp_path):
    ref, signal = archives(tmp_path)
    dialog.reference.setText(str(ref))
    dialog.signal.setText(str(signal))
    dialog.output.setText(str(tmp_path / "difference.json"))
    dialog.search_start.setText("1 MHz")
    dialog.search_stop.setText("2 MHz")
    dialog.blocks.setValue(2)


@pytest.mark.parametrize("theme,size", [("light", (1000, 900)), ("dark", (750, 700))])
@pytest.mark.parametrize("qualified", [False, True])
def test_shown_comparison_reports_real_archives_without_laboratory_claims(theme, size, qualified, tmp_path):
    app = application()
    apply_application_theme(app, theme)
    dialog = SpectrumDifferenceDialog()
    try:
        dialog.resize(*size)
        dialog.show()
        assert not dialog.independent.isChecked() and not dialog.exchangeable.isChecked()
        populate(dialog, tmp_path)
        if qualified:
            dialog.independent.setChecked(True)
            dialog.exchangeable.setChecked(True)
            dialog.evidence.setText("Synthetic API assumptions only; no laboratory qualification")
        dialog.start.click()
        assert dialog._busy and not dialog.start.isEnabled()
        assert dialog.progress.isVisible() and dialog.cancel.isEnabled()
        assert "Reading closed archives" in dialog.status.text()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        path = tmp_path / "difference.json"
        assert path.exists(), dialog.status.text()
        report = json.loads(path.read_text(encoding="utf-8"))
        assert not report["laboratory_qualified"] and not report["false_alarm_rate_qualified"]
        if qualified:
            assert "Global difference detected" in dialog.result_label.text()
            assert "p = 0.001" in dialog.result_label.text()
        else:
            assert report["p_value"] is None
            assert "p-value unavailable" in dialog.result_label.text()
        assert "24 REF / 24 SIGNAL" in dialog.result_label.text()
        assert "non-detection does not prove absence" in dialog.result_label.text()
        assert dialog.start.isEnabled() and not dialog.cancel.isEnabled()
        app.processEvents()
        for control in (dialog.start, dialog.cancel, dialog.close_button, dialog.result_label, dialog.status):
            assert control.isVisible() and control.height() >= control.fontMetrics().height()
            origin = control.mapTo(dialog, QPoint(0, 0))
            assert dialog.rect().contains(origin)
            assert dialog.rect().contains(origin + QPoint(control.width() - 1, control.height() - 1))
        directory = Path("artifacts/spectrum-difference-ui")
        directory.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(directory / f"result-{theme}-{size[0]}-{qualified}.png"))
        dialog.reject()
        wait_until(app, lambda: not dialog.isVisible(), timeout=10)
    finally:
        destroy(dialog, app)
        apply_application_theme(app, "light")


def test_validation_and_existing_report_errors_are_visible(tmp_path):
    app = application()
    dialog = SpectrumDifferenceDialog()
    try:
        dialog.show()
        dialog.start.click()
        assert "Choose REF, SIGNAL" in dialog.status.text() and not dialog._busy
        populate(dialog, tmp_path)
        dialog.search_start.setText("1")
        dialog.start.click()
        assert "Cannot start" in dialog.status.text() and not dialog._busy
        dialog.search_start.setText("3 MHz")
        dialog.start.click()
        assert "Search stop" in dialog.status.text() and not dialog._busy
        dialog.search_start.setText("1 MHz")
        dialog.independent.setChecked(True)
        dialog.start.click()
        assert "evidence" in dialog.status.text() and not dialog._busy
        dialog.independent.setChecked(False)
        dialog.permutations.setValue(99)
        dialog.start.click()
        assert "resolvable" in dialog.status.text() and not dialog._busy
        dialog.permutations.setValue(999)
        output = tmp_path / "difference.json"
        output.write_bytes(b"existing report")
        dialog.start.click()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        assert "Comparison failed" in dialog.status.text()
        assert output.read_bytes() == b"existing report"
    finally:
        destroy(dialog, app)


def test_close_during_comparison_cancels_without_mutating_sources(tmp_path):
    app = application()
    dialog = SpectrumDifferenceDialog()
    try:
        dialog.show()
        populate(dialog, tmp_path)
        paths = [tmp_path / "reference.h5", tmp_path / "signal.h5"]
        before = [path.read_bytes() for path in paths]
        dialog.independent.setChecked(True)
        dialog.exchangeable.setChecked(True)
        dialog.evidence.setText("Synthetic assumptions only")
        dialog.permutations.setValue(99999)
        dialog.start.click()
        dialog.close()
        wait_until(app, lambda: not dialog.isVisible(), timeout=15)
        assert not dialog._controller._thread.isRunning()
        assert not (tmp_path / "difference.json").exists()
        assert [path.read_bytes() for path in paths] == before
    finally:
        destroy(dialog, app)


def test_workspace_opens_one_visible_comparison_without_instrument_connection():
    app = application()
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=False)
    requests = []
    workspace.request_device.connect(lambda *args: requests.append(args))
    try:
        workspace.resize(1100, 900)
        workspace.show()
        app.processEvents()
        assert workspace.compare_recorded.isEnabled()
        workspace.compare_recorded.click()
        dialog = workspace._difference_dialog
        assert dialog is not None and dialog.isVisible()
        workspace.compare_recorded.click()
        assert workspace._difference_dialog is dialog
        dialog.reject()
        wait_until(app, lambda: workspace._difference_dialog is None, timeout=10)
        assert requests == []
    finally:
        workspace.shutdown()
        workspace.deleteLater()
        app.processEvents()
