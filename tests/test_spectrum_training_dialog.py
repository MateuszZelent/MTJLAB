"""Reference-only model training: shown UI, explicit units and worker lifecycle."""

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import h5py
import pytest
from PySide6.QtCore import QPoint

from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.devices.anritsu_ms2830a.ui.training_dialog import SpectrumTrainingDialog, parse_region_text
from app.storage.hdf5_reader import Hdf5RunReader
from app.storage.thatec_validator import ThatecCompatibilityValidator
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_interference_training import source_archive
from tests.test_spectrum_resonance_dialog import application, destroy


def populate(dialog, tmp_path):
    source = tmp_path / "reference.h5"
    source_archive(source)
    dialog.reference.setText(str(source))
    dialog.output.setText(str(tmp_path / "model.h5"))
    dialog.model_id.setText("ui-reference-model")
    dialog.nuisance.setText("1.26 MHz .. 1.74 MHz")
    dialog.controls.setText("1 MHz .. 2 MHz")
    dialog.protected.setText("1.47 MHz .. 1.53 MHz")
    dialog.sigma_input.setText("1 pW")
    dialog.frame_cap.setValue(12)
    return source


@pytest.mark.parametrize("theme,size", [("light", (1000, 900)), ("dark", (750, 700))])
@pytest.mark.parametrize("qualified", [False, True])
def test_shown_training_saves_real_model_without_inferred_qualification(theme, size, qualified, tmp_path):
    app = application()
    apply_application_theme(app, theme)
    dialog = SpectrumTrainingDialog()
    try:
        dialog.resize(*size)
        dialog.show()
        assert not dialog.controls_qualified.isChecked()
        source = populate(dialog, tmp_path)
        before = source.read_bytes()
        if qualified:
            dialog.controls_qualified.setChecked(True)
            dialog.evidence.setText("Synthetic control masks only; no laboratory qualification")
        dialog.start.click()
        assert dialog._busy and not dialog.start.isEnabled()
        assert dialog.progress.isVisible() and dialog.cancel.isEnabled()
        assert "Reading REF" in dialog.status.text()
        wait_until(app, lambda: not dialog._busy, timeout=25)
        path = tmp_path / "model.h5"
        assert path.exists(), dialog.status.text()
        (model,) = Hdf5RunReader.interference_calibrations(path)
        assert model.signal_control_regions_qualified is qualified
        assert model.basis_w.shape == (201, 2)
        assert not json.loads(model.training_provenance_json)["qualification_inferred"]
        assert source.read_bytes() == before
        assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
        assert "2 local components; 40 reference sweeps" in dialog.result_label.text()
        assert "Held-out validation" in dialog.result_label.text()
        if qualified:
            assert "recorded from your evidence" in dialog.result_label.text()
        else:
            assert "cannot be used for live correction" in dialog.result_label.text()
        assert dialog.start.isEnabled() and not dialog.cancel.isEnabled()
        app.processEvents()
        for control in (dialog.start, dialog.cancel, dialog.close_button, dialog.result_label, dialog.status):
            assert control.isVisible() and control.height() >= control.fontMetrics().height()
            origin = control.mapTo(dialog, QPoint(0, 0))
            assert dialog.rect().contains(origin)
            assert dialog.rect().contains(origin + QPoint(control.width() - 1, control.height() - 1))
        directory = Path("artifacts/spectrum-training-ui")
        directory.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(directory / f"result-{theme}-{size[0]}-{qualified}.png"))
        dialog.reject()
        wait_until(app, lambda: not dialog.isVisible(), timeout=10)
    finally:
        destroy(dialog, app)
        apply_application_theme(app, "light")


def test_regions_preserve_units_and_decimal_comma_without_ambiguous_separators():
    assert parse_region_text("1,26 MHz .. 1,74 MHz; 1.8 MHz .. 2 MHz", required=True) == [
        ["1,26 MHz", "1,74 MHz"], ["1.8 MHz", "2 MHz"]]
    assert parse_region_text("") == []
    with pytest.raises(ValueError):
        parse_region_text("", required=True)


@pytest.mark.parametrize("text", ["1 .. 2", "1 V .. 2 V", "-1 MHz .. 2 MHz", "2 MHz .. 1 MHz",
                                  "1 MHz, 2 MHz", "1 MHz .. 2 MHz .. 3 MHz", "1 MHz .. 2 MHz;",
                                  ";".join(["1 MHz .. 2 MHz"] * 33)],
                         ids=["unitless", "dimension", "negative", "order", "separator", "triple", "empty", "cap"])
def test_invalid_regions_rejected(text):
    with pytest.raises(ValueError):
        parse_region_text(text)


def test_input_and_worker_errors_preserve_sources_and_existing_model(tmp_path):
    app = application()
    dialog = SpectrumTrainingDialog()
    try:
        dialog.show()
        dialog.start.click()
        assert "Cannot start" in dialog.status.text() and not dialog._busy
        source = populate(dialog, tmp_path)
        before = source.read_bytes()
        dialog.sigma_input.setText("1 V")
        dialog.start.click()
        assert "Cannot start" in dialog.status.text() and not dialog._busy
        dialog.sigma_input.setText("0 W")
        dialog.start.click()
        assert "positive power" in dialog.status.text()
        dialog.sigma_input.setText("1 pW")
        dialog.controls_qualified.setChecked(True)
        dialog.start.click()
        assert "evidence" in dialog.status.text() and not dialog._busy
        dialog.controls_qualified.setChecked(False)
        dialog.frame_cap.setValue(3)
        dialog.start.click()
        assert "Cannot start" in dialog.status.text() and not dialog._busy
        dialog.frame_cap.setValue(12)
        dialog.nuisance.setText("3 MHz .. 4 MHz")
        dialog.start.click()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        assert "Training failed" in dialog.status.text()
        assert not (tmp_path / "model.h5").exists()
        dialog.nuisance.setText("1.26 MHz .. 1.74 MHz")
        path = tmp_path / "model.h5"
        path.write_bytes(b"existing model")
        dialog.start.click()
        wait_until(app, lambda: not dialog._busy, timeout=20)
        assert "Training failed" in dialog.status.text()
        assert path.read_bytes() == b"existing model" and source.read_bytes() == before
    finally:
        destroy(dialog, app)


def test_close_pending_training_drains_worker_without_mutating_reference(tmp_path):
    app = application()
    dialog = SpectrumTrainingDialog()
    try:
        dialog.show()
        source = populate(dialog, tmp_path)
        before = source.read_bytes()
        dialog.start.click()
        dialog.close()
        wait_until(app, lambda: not dialog.isVisible(), timeout=20)
        assert not dialog._controller._thread.isRunning()
        assert source.read_bytes() == before
        path = tmp_path / "model.h5"
        if path.exists():
            with h5py.File(path, "r") as archive:
                assert archive.attrs["run_status"] == "aborted"
    finally:
        destroy(dialog, app)


def test_workspace_opens_one_visible_training_dialog_offline():
    app = application()
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=False)
    requests = []
    workspace.request_device.connect(lambda *args: requests.append(args))
    try:
        workspace.resize(1100, 900)
        workspace.show()
        app.processEvents()
        assert workspace.train_model.isEnabled()
        workspace.train_model.click()
        dialog = workspace._training_dialog
        assert dialog is not None and dialog.isVisible()
        workspace.train_model.click()
        assert workspace._training_dialog is dialog
        dialog.reject()
        wait_until(app, lambda: workspace._training_dialog is None, timeout=10)
        workspace._running = True
        workspace.set_available(False)
        assert not workspace.train_model.isEnabled()
        workspace._open_model_training()
        assert workspace._training_dialog is None and requests == []
    finally:
        workspace.shutdown()
        workspace.deleteLater()
        app.processEvents()
