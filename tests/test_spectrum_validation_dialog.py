"""Shown held-out REF diagnostic with real reports and explicit rejected fits."""

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint

from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.devices.anritsu_ms2830a.ui.validation_dialog import SpectrumValidationDialog
from app.storage.interference_training_store import train_interference_archive
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_interference_training import parameters, source_archive
from tests.test_spectrum_interference_validation import save_unseen
from tests.test_spectrum_resonance_dialog import application, destroy


def populate(dialog, tmp_path, *, out_of_range=False):
    training, model, reference = (tmp_path / name for name in ("train.h5", "model.h5", "ref.h5"))
    x, _context, _profile, _rows, nuisance = source_archive(training)
    train_interference_archive(training, model, **parameters(x, nuisance))
    save_unseen(reference, out_of_range=out_of_range)
    dialog.model.setText(str(model))
    dialog.reference.setText(str(reference))
    dialog.output.setText(str(tmp_path / "validation.json"))
    return model, reference


@pytest.mark.parametrize("theme,size", [("light", (1000, 850)), ("dark", (750, 700))])
@pytest.mark.parametrize("out_of_range", [False, True])
def test_shown_validation_counts_rejections_without_automatic_approval(theme, size, out_of_range, tmp_path):
    app = application()
    apply_application_theme(app, theme)
    dialog = SpectrumValidationDialog()
    try:
        dialog.resize(*size)
        dialog.show()
        assert dialog.regions.text() == "" and dialog.model_id.text() == ""
        paths = populate(dialog, tmp_path, out_of_range=out_of_range)
        before = [path.read_bytes() for path in paths]
        dialog.start.click()
        assert dialog._busy and not dialog.start.isEnabled()
        assert dialog.progress.isVisible() and dialog.cancel.isEnabled()
        assert "checking disjointness" in dialog.status.text()
        wait_until(app, lambda: not dialog._busy, timeout=25)
        path = tmp_path / "validation.json"
        assert path.exists(), dialog.status.text()
        report = json.loads(path.read_text(encoding="utf-8"))
        assert not report["qualification_inferred"]
        assert [path.read_bytes() for path in paths] == before
        assert "12 REF sweeps" in dialog.result_label.text()
        assert "No automatic model approval" in dialog.result_label.text()
        if out_of_range:
            assert "0 accepted / 12 rejected" in dialog.result_label.text()
            assert "RMS unavailable" in dialog.result_label.text()
            assert report["model_error_on_accepted_fits"] is None
        else:
            assert "12 accepted / 0 rejected" in dialog.result_label.text()
            assert "static reference" in dialog.result_label.text()
            assert report["model_error_on_accepted_fits"]["unused_region_rms_w"] < 1e-23
        app.processEvents()
        for control in (dialog.start, dialog.cancel, dialog.close_button, dialog.status, dialog.result_label):
            assert control.isVisible() and control.height() >= control.fontMetrics().height()
            origin = control.mapTo(dialog, QPoint(0, 0))
            assert dialog.rect().contains(origin)
            assert dialog.rect().contains(origin + QPoint(control.width() - 1, control.height() - 1))
        directory = Path("artifacts/spectrum-validation-ui")
        directory.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(directory / f"result-{theme}-{size[0]}-{out_of_range}.png"))
        dialog.reject()
        wait_until(app, lambda: not dialog.isVisible(), timeout=10)
    finally:
        destroy(dialog, app)
        apply_application_theme(app, "light")


def test_invalid_regions_training_reuse_and_existing_report_are_visible(tmp_path):
    app = application()
    dialog = SpectrumValidationDialog()
    try:
        dialog.show()
        dialog.start.click()
        assert "Cannot start" in dialog.status.text() and not dialog._busy
        model, reference = populate(dialog, tmp_path)
        before = [path.read_bytes() for path in (model, reference)]
        dialog.regions.setText("1 V .. 2 V")
        dialog.start.click()
        assert "Cannot start" in dialog.status.text() and not dialog._busy
        dialog.regions.setText("1 MHz .. 1.1 MHz")
        dialog.start.click()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        assert "Validation failed" in dialog.status.text() and "participate" in dialog.status.text()
        assert not (tmp_path / "validation.json").exists()
        dialog.regions.setText("1,48 MHz .. 1,52 MHz")
        dialog.reference.setText(str(tmp_path / "train.h5"))
        dialog.start.click()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        assert "reuse" in dialog.status.text()
        dialog.reference.setText(str(reference))
        output = tmp_path / "validation.json"
        output.write_bytes(b"existing report")
        dialog.start.click()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        assert "Validation failed" in dialog.status.text()
        assert output.read_bytes() == b"existing report"
        assert [path.read_bytes() for path in (model, reference)] == before
        dialog.output.setText(str(tmp_path / "new.json"))
        dialog.start.click()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        assert (tmp_path / "new.json").exists(), dialog.status.text()
    finally:
        destroy(dialog, app)


def test_close_pending_validation_cancels_without_report_or_source_mutation(tmp_path):
    app = application()
    dialog = SpectrumValidationDialog()
    try:
        dialog.show()
        paths = populate(dialog, tmp_path)
        before = [path.read_bytes() for path in paths]
        dialog.start.click()
        dialog.close()
        wait_until(app, lambda: not dialog.isVisible(), timeout=20)
        assert not dialog._controller._thread.isRunning()
        assert not (tmp_path / "validation.json").exists()
        assert [path.read_bytes() for path in paths] == before
    finally:
        destroy(dialog, app)


def test_workspace_opens_one_validation_dialog_without_instrument_connection():
    app = application()
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=False)
    requests = []
    workspace.request_device.connect(lambda *args: requests.append(args))
    try:
        workspace.resize(1100, 900)
        workspace.show()
        app.processEvents()
        assert workspace.validate_model.isEnabled()
        workspace.validate_model.click()
        dialog = workspace._validation_dialog
        assert dialog is not None and dialog.isVisible()
        workspace.validate_model.click()
        assert workspace._validation_dialog is dialog
        dialog.reject()
        wait_until(app, lambda: workspace._validation_dialog is None, timeout=10)
        workspace._profile_io_busy = True
        workspace.set_available(False)
        assert not workspace.validate_model.isEnabled()
        workspace._open_model_validation()
        assert workspace._validation_dialog is None and requests == []
    finally:
        workspace.shutdown()
        workspace.deleteLater()
        app.processEvents()
