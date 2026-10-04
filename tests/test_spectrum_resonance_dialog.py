"""Shown Fluent analysis dialog, real archives, explicit units and cooperative close."""

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

pytest.importorskip("scipy", reason="Optional qualification dependencies are required")

from app.devices.anritsu_ms2830a.ui.resonance_dialog import SpectrumResonanceDialog
from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.ui.design_system import apply_application_theme
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_resonance_bootstrap_store import archives
from tests.helpers import simulation_settings


def application():
    app = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Segoe UI", 10))
    return app


def populate(dialog, tmp_path):
    reference, signal = archives(tmp_path)
    dialog.reference.setText(str(reference))
    dialog.signal.setText(str(signal))
    dialog.output.setText(str(tmp_path / "report.json"))
    dialog.center.setText("1.5 MHz")
    dialog.fwhm_input.setText("100 kHz")
    dialog.blocks.setValue(2)
    dialog.resamples.setValue(200)


def destroy(dialog, app):
    dialog.shutdown()
    wait_until(app, lambda: not dialog._controller._thread.isRunning(), timeout=10)
    dialog.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("theme,size", [("light", (1000, 900)), ("dark", (750, 700))])
def test_shown_dialog_analyzes_real_archives_without_inferred_ci(theme, size, tmp_path):
    app = application()
    apply_application_theme(app, theme)
    dialog = SpectrumResonanceDialog()
    try:
        dialog.resize(*size)
        dialog.show()
        app.processEvents()
        assert dialog.start.isVisible() and dialog.start.width() > 100
        assert dialog.close_button.isVisible() and dialog.close_button.height() > 20
        assert not any(control.isChecked() for control in (dialog.independent, dialog.equivalent, dialog.stationary))
        populate(dialog, tmp_path)
        dialog.start.click()
        assert dialog._busy and not dialog.start.isEnabled()
        assert dialog.progress.isVisible() and dialog.cancel.isEnabled()
        assert "Reading closed archives" in dialog.status.text()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        report_path = tmp_path / "report.json"
        assert report_path.exists(), dialog.status.text()
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["status"] == "unqualified" and report["confidence_intervals"] is None
        assert "No confidence intervals (unqualified)" in dialog.result_label.text()
        assert "24 REF / 24 SIGNAL" in dialog.result_label.text()
        assert "W·Hz" in dialog.result_label.text()
        assert dialog.start.isEnabled() and not dialog.cancel.isEnabled()
        app.processEvents()
        assert dialog.result_label.isVisible() and dialog.result_label.height() > 70
        directory = Path("artifacts/spectrum-resonance-ui")
        directory.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(directory / f"result-{theme}-{size[0]}.png"))
        dialog.reject()
        wait_until(app, lambda: not dialog.isVisible(), timeout=10)
        assert not dialog._controller._thread.isRunning()
    finally:
        destroy(dialog, app)
        apply_application_theme(app, "light")


def test_units_evidence_and_existing_output_errors_are_visible(tmp_path):
    app = application()
    dialog = SpectrumResonanceDialog()
    try:
        dialog.show()
        dialog.start.click()
        assert "Choose REF, SIGNAL" in dialog.status.text() and not dialog._busy
        populate(dialog, tmp_path)
        dialog.center.setText("1.5")
        dialog.start.click()
        assert "Cannot start" in dialog.status.text() and not dialog._busy
        dialog.center.setText("1.5 MHz")
        dialog.independent.setChecked(True)
        dialog.start.click()
        assert "evidence" in dialog.status.text() and not dialog._busy
        dialog.independent.setChecked(False)
        output = tmp_path / "report.json"
        output.write_bytes(b"existing user report")
        dialog.start.click()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        assert "Analysis failed" in dialog.status.text()
        assert output.read_bytes() == b"existing user report"
        assert dialog.start.isEnabled()
    finally:
        destroy(dialog, app)


def test_close_during_pending_analysis_cancels_and_preserves_sources(tmp_path):
    app = application()
    dialog = SpectrumResonanceDialog()
    try:
        dialog.show()
        populate(dialog, tmp_path)
        paths = [tmp_path / "reference.h5", tmp_path / "signal.h5"]
        before = [path.read_bytes() for path in paths]
        dialog.independent.setChecked(True)
        dialog.equivalent.setChecked(True)
        dialog.stationary.setChecked(True)
        dialog.evidence.setText("Synthetic assumptions only")
        dialog.resamples.setValue(10000)
        dialog.start.click()
        assert dialog._busy
        dialog.close()
        wait_until(app, lambda: not dialog.isVisible(), timeout=15)
        assert not dialog._controller._thread.isRunning()
        assert not (tmp_path / "report.json").exists()
        assert all(path.read_bytes() == content for path, content in zip(paths, before, strict=True))
    finally:
        destroy(dialog, app)


def test_workspace_opens_visible_offline_dialog_without_device_connection_or_commands():
    app = application()
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=False)
    requests = []
    workspace.request_device.connect(lambda *args: requests.append(args))
    try:
        workspace.resize(1100, 900)
        workspace.show()
        app.processEvents()
        assert workspace.analyze_resonance.isEnabled()

        workspace.analyze_resonance.click()
        dialog = workspace._resonance_dialog
        assert dialog is not None and dialog.isVisible()
        assert dialog.width() > 500 and dialog.height() > 500
        dialog.reject()
        wait_until(app, lambda: workspace._resonance_dialog is None, timeout=10)
        assert workspace._resonance_dialog is None and not requests
    finally:
        assert workspace.shutdown()
        workspace.deleteLater()
        app.processEvents()
