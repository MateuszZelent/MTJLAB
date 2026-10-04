"""Async held-out REF validation: immutable regions, cancellation and safe reuse."""

from dataclasses import replace
import json
import os
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.ui.correction_controller import (
    CorrectionSessionRequest, SpectrumCorrectionController, SpectrumInterferenceTrainingRequest,
    SpectrumInterferenceValidationRequest,
)
from app.domain.spectrum_correction import CorrectionConfig
from app.storage.interference_training_store import train_interference_archive
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_correction_store import fixture_profile
from tests.test_spectrum_interference_training import parameters, source_archive
from tests.test_spectrum_interference_validation import save_unseen


def request(tmp_path):
    training, model, reference = (tmp_path / name for name in ("training.h5", "model.h5", "reference.h5"))
    x, _context, _profile, _rows, nuisance = source_archive(training)
    train_interference_archive(training, model, **parameters(x, nuisance))
    save_unseen(reference)
    return SpectrumInterferenceValidationRequest(model, reference, tmp_path / "validation.json",
        model_id="learned-lines", validation_regions_json=json.dumps([["1.48 MHz", "1.52 MHz"]]))


def test_worker_validates_disjoint_sources_without_qualifying_model(tmp_path):
    app = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    paths = [payload.model_path, payload.reference_path]
    before = [path.read_bytes() for path in paths]
    controller = SpectrumCorrectionController()
    completed, failed, progress = [], [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    controller.processing_progress.connect(lambda *args: progress.append(args))
    try:
        controller.validate_interference_archive(payload)
        with pytest.raises(ValueError, match="pending"):
            controller.validate_interference_archive(payload)
        with pytest.raises(ValueError, match="pending"):
            controller.train_interference_archive(SpectrumInterferenceTrainingRequest(
                payload.reference_path, tmp_path / "other.h5", "{}"))
        context, profile = fixture_profile()
        with pytest.raises(ValueError, match="pending"):
            controller.start_session(CorrectionSessionRequest(context, CorrectionConfig(), tmp_path / "not-opened.h5",
                "synthetic: true", "NO_VISA", profile=profile))
        wait_until(app, lambda: bool(completed) or bool(failed), timeout=20)
        assert not failed and completed[0][0] == "validate_interference"
        path, report = completed[0][1]
        assert path == payload.destination
        assert json.loads(path.read_text(encoding="utf-8")) == report
        assert report["accepted_fits"] == 12 and report["rejected_fits"] == 0
        assert not report["qualification_inferred"]
        assert report["model_error_on_accepted_fits"]["unused_region_rms_w"] < 1e-23
        assert report["static_error_on_same_accepted_fits"]["unused_region_rms_w"] > 1e-11
        assert progress == [("validate_interference", 0, 0)]
        assert not controller._offline_busy
        assert [path.read_bytes() for path in paths] == before
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_validation_cancels_during_source_read_without_publication(tmp_path, monkeypatch, action):
    from app.storage import interference_validation_store as module

    app = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    before = [path.read_bytes() for path in (payload.model_path, payload.reference_path)]
    original = module.file_sha256
    entered, proceed = Event(), Event()

    def delayed_hash(*args, **kwargs):
        entered.set()
        if not proceed.wait(5):
            raise RuntimeError("Test release timed out")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "file_sha256", delayed_hash)
    controller = SpectrumCorrectionController()
    completed, failed, cancelled = [], [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    controller.cancelled.connect(cancelled.append)
    try:
        controller.validate_interference_archive(payload)
        wait_until(app, entered.is_set)
        if action == "close":
            assert not controller.close(wait_ms=0)
        else:
            controller.cancel_processing()
        proceed.set()
        wait_until(app, lambda: bool(cancelled) or bool(failed), timeout=15)
        assert cancelled == ["validate_interference"] and not failed
        assert not payload.destination.exists() and not controller._offline_busy
        assert [path.read_bytes() for path in (payload.model_path, payload.reference_path)] == before
        if action == "close":
            wait_until(app, lambda: not controller._thread.isRunning())
        else:
            controller.validate_interference_archive(payload)
            wait_until(app, lambda: bool(completed) or bool(failed), timeout=20)
            assert not failed and payload.destination.exists()
    finally:
        proceed.set()
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        app.processEvents()


def test_existing_report_error_releases_worker_for_retry(tmp_path):
    app = QApplication.instance() or QApplication([])
    payload = request(tmp_path)
    payload.destination.write_bytes(b"existing report")
    controller = SpectrumCorrectionController()
    completed, failed = [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    try:
        controller.validate_interference_archive(payload)
        wait_until(app, lambda: bool(failed), timeout=20)
        assert failed[0][0] == "validate_interference" and not controller._offline_busy
        assert payload.destination.read_bytes() == b"existing report"
        failed.clear()
        retry = replace(payload, destination=tmp_path / "new.json", validation_regions_json=None)
        controller.validate_interference_archive(retry)
        wait_until(app, lambda: bool(completed) or bool(failed), timeout=20)
        assert not failed and retry.destination.exists()
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("regions", ["{}", "[]", "[", '[[1, 2]]',
                                     json.dumps([["1 MHz", "2 MHz"]] * 33), " " * 65537],
                         ids=["object", "empty", "invalid-json", "unitless", "count", "bytes"])
def test_validation_request_rejects_mutable_or_unbounded_region_input(tmp_path, regions):
    with pytest.raises(ValueError):
        SpectrumInterferenceValidationRequest(tmp_path / "model.h5", tmp_path / "ref.h5",
                                              tmp_path / "report.json", validation_regions_json=regions)
