"""Async reference-only training preserves raw sources and explicit qualifications."""

from dataclasses import replace
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import h5py
import pytest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.ui.correction_controller import (
    SpectrumCorrectionController, SpectrumDifferenceRequest, SpectrumInterferenceTrainingRequest,
)
from app.domain.errors import ExecutionError
from app.storage.hdf5_reader import Hdf5RunReader
from app.storage.interference_training_store import train_from_specification
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_interference_training import source_archive


def request(tmp_path):
    source = tmp_path / "reference.h5"
    source_archive(source)
    spec = {"model_id": "async-reference-model", "nuisance_regions": [["1.26 MHz", "1.74 MHz"]],
            "control_regions": [["1 MHz", "2 MHz"]], "protected_regions": [["1.47 MHz", "1.53 MHz"]],
            "control_sigma": "1 pW", "components": 2, "maximum_training_frames": 12}
    return SpectrumInterferenceTrainingRequest(source, tmp_path / "model.h5", json.dumps(spec))


def test_worker_trains_self_contained_model_without_inferred_qualification(tmp_path):
    app = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    before = payload.reference_path.read_bytes()
    controller = SpectrumCorrectionController()
    completed, failed, progress = [], [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    controller.processing_progress.connect(lambda *args: progress.append(args))
    try:
        controller.train_interference_archive(payload)
        with pytest.raises(ValueError, match="pending"):
            controller.train_interference_archive(payload)
        with pytest.raises(ValueError, match="pending"):
            controller.difference_archives(SpectrumDifferenceRequest(payload.reference_path,
                tmp_path / "signal.h5", tmp_path / "difference.json", 2, 1e6, 2e6))
        wait_until(app, lambda: bool(completed) or bool(failed), timeout=20)
        assert not failed
        assert completed[0][0] == "train_interference"
        path, calibration = completed[0][1]
        assert path == payload.destination and path.exists()
        assert calibration.basis_w.shape == (201, 2)
        assert not calibration.signal_control_regions_qualified
        assert not json.loads(calibration.training_provenance_json)["qualification_inferred"]
        assert progress == [("train_interference", 0, 0)]
        assert not controller._offline_busy
        assert payload.reference_path.read_bytes() == before
        assert Hdf5RunReader.interference_calibrations(path)[0].content_hash == calibration.content_hash
        assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        app.processEvents()


def test_pending_training_cancels_without_publication_and_can_retry(tmp_path):
    app = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    controller = SpectrumCorrectionController()
    completed, failed, cancelled = [], [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    controller.cancelled.connect(cancelled.append)
    try:
        controller.train_interference_archive(payload)
        controller.cancel_processing()
        wait_until(app, lambda: bool(cancelled) or bool(failed), timeout=20)
        assert cancelled == ["train_interference"] and not failed
        assert not payload.destination.exists() and not controller._offline_busy
        controller.train_interference_archive(payload)
        wait_until(app, lambda: bool(completed) or bool(failed), timeout=20)
        assert not failed and payload.destination.exists()
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        app.processEvents()


def test_training_error_preserves_existing_artifact_and_releases_worker(tmp_path):
    app = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    payload.destination.write_bytes(b"existing model")
    controller = SpectrumCorrectionController()
    failed, completed = [], []
    controller.failed.connect(lambda *args: failed.append(args))
    controller.completed.connect(lambda *args: completed.append(args))
    try:
        controller.train_interference_archive(payload)
        wait_until(app, lambda: bool(failed), timeout=20)
        assert payload.destination.read_bytes() == b"existing model"
        assert not controller._offline_busy
        controller.train_interference_archive(replace(payload, destination=tmp_path / "new-model.h5"))
        wait_until(app, lambda: bool(completed), timeout=20)
        assert completed[0][1][0].exists()
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        app.processEvents()


def test_completed_reference_with_pending_checkpoint_cannot_train(tmp_path):
    payload = request(tmp_path)
    with h5py.File(payload.reference_path, "r+") as file:
        file["_pending"].create_group("uncommitted")
    with pytest.raises(ExecutionError, match="completed raw reference"):
        train_from_specification(payload.reference_path, payload.destination, json.loads(payload.specification_json))
    assert not payload.destination.exists()


@pytest.mark.parametrize("text", ["[]", "not JSON", " " * 65537], ids=["nonobject", "invalid-json", "oversized"])
def test_training_request_rejects_nonobject_or_unbounded_json(tmp_path, text):
    with pytest.raises(ValueError):
        SpectrumInterferenceTrainingRequest(tmp_path / "ref.h5", tmp_path / "model.h5", text)
