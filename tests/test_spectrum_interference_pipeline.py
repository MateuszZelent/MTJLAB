"""Real worker/raw archive/replay with bounded nuisance fits, no VISA."""

import os
from dataclasses import replace
import json

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import h5py
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.ui.correction_controller import CorrectionSessionRequest, SpectrumCorrectionController
from app.domain.spectrum_correction import CorrectionConfig, CorrectionQuality, TemporalAverageMode
from app.domain.spectrum_decisions import SpectrumDecisionOperation, SpectrumProcessingChange
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.spectrum.realtime_processor import RealtimeSpectrumProcessor
from app.spectrum.replay import replay_quantitative_session
from app.storage.hdf5_reader import Hdf5RunReader
from tests.test_spectrum_interference_store import writer
from tests.test_spectrum_correction_controller import frame, wait_until
from tests.test_spectrum_interference_store import fixture


@pytest.mark.parametrize("mode", list(TemporalAverageMode))
@pytest.mark.parametrize("reinstall", [False, True])
def test_worker_archives_rejected_fit_and_replays_protected_signed_signal(tmp_path, mode, reinstall):
    application = QApplication.instance() or QApplication([])
    x, profile, calibration = fixture()
    path = tmp_path / "model-session.h5"
    config = CorrectionConfig(average_mode=mode, window_frames=3)
    controller = SpectrumCorrectionController(queue_frames=4)
    completed, failures = [], []
    controller.completed.connect(lambda name, result: completed.append((name, result)))
    controller.failed.connect(lambda *args: failures.append(args))

    def submit(operation, action):
        before = sum(name == operation for name, _ in completed)
        action()
        wait_until(application, lambda: bool(failures) or sum(name == operation for name, _ in completed) > before)
        assert not failures
        return next(value for name, value in reversed(completed) if name == operation)

    signal = np.where(np.abs(x) < .3, np.where(x < 0, -2e-10, 3e-10), 0)
    coefficients = [(0.2, .05), (.8, -.1), (3, 0), (.4, .1), (.6, 0)]
    try:
        submit("start", lambda: controller.start_session(CorrectionSessionRequest(
            calibration.context, config, path, "synthetic: true", "SYNTHETIC;NO_VISA",
            profile=profile, simulation_mode=True, interference_calibration=calibration,
        )))
        for index, values in enumerate(coefficients):
            if reinstall and index == 3:
                # Explicitly return to mean subtraction, then reinstall the
                # model at the same raw boundary. Replay must follow both
                # decisions, including their averaging-generation resets.
                submit("processing_change", lambda: controller.apply_processing_change(
                    SpectrumProcessingChange(SpectrumDecisionOperation.SET_PROFILE, profile=profile)))
                submit("processing_change", lambda: controller.apply_processing_change(
                    SpectrumProcessingChange(SpectrumDecisionOperation.SET_MODEL, calibration=calibration)))
            watts = calibration.baseline_w + calibration.basis_w @ values + signal
            envelope, trace = frame(calibration.context, index, watts)
            acknowledgement = submit("frame", lambda: controller.ingest(envelope, trace))
            assert acknowledgement["accepted"] is (index != 2)
            assert acknowledgement["committed_point_count"] == index + 1
        preview = submit("snapshot", controller.request_snapshot)
        np.testing.assert_allclose(preview.corrected.values_w, signal, atol=2e-23)
        assert preview.corrected.standard_uncertainty_w is None
        assert preview.corrected.quality == CorrectionQuality.UNQUALIFIED
        assert preview.corrected.interference_model_hash == calibration.content_hash
        np.testing.assert_allclose(preview.corrected.interference_last_coefficients, coefficients[-1], atol=1e-14)
        submit("stop", lambda: controller.stop_session("aborted"))
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()
    replay = list(replay_quantitative_session(path))
    assert [item.contributed for item in replay] == [True, True, False, True, True]
    assert replay[2].result is None
    assert Hdf5RunReader.spectrum_correction(path, 2) is None
    assert Hdf5RunReader.summary(path).point_count == 5
    assert replay[-1].result.count == (2 if reinstall else 3 if mode == TemporalAverageMode.WINDOW else 4)
    np.testing.assert_allclose(replay[-1].result.values_w, signal, atol=2e-23)
    with h5py.File(path, "r") as file:
        assert len(file["spectrum_processing_v1/interference_models"]) == 1
        assert len(file["_pending"]) == 0
    with h5py.File(path, "r+") as file:
        group = file["spectra/4/correction_v1"]
        metadata = json.loads(group.attrs["metadata_json"])
        metadata["interference_last_coefficients"][0] += .01  # Still inside calibrated bounds.
        group.attrs["metadata_json"] = json.dumps(metadata)
    before = path.read_bytes()
    with pytest.raises(ExecutionError, match="provenance"):
        list(replay_quantitative_session(path))
    assert path.read_bytes() == before


def test_installation_resets_average_refresh_clears_model_and_failed_install_is_atomic():
    _x, profile, calibration = fixture()
    processor = RealtimeSpectrumProcessor(calibration.context, CorrectionConfig(average_mode=TemporalAverageMode.BLOCK))
    processor.set_background_profile(profile)
    first, trace = frame(calibration.context, 0, profile.mean_w)
    assert processor.ingest(first, trace.powers_dbm)
    before = processor.snapshot()
    with pytest.raises(ValueError, match="qualified"):
        processor.set_interference_calibration(replace(calibration, signal_control_regions_qualified=False))
    assert processor.snapshot().processing_generation == before.processing_generation
    processor.set_interference_calibration(calibration)
    assert processor.snapshot() is None
    second, trace = frame(calibration.context, 1, profile.mean_w)
    assert processor.ingest(second, trace.powers_dbm)
    assert processor.snapshot().interference_model_id == calibration.model_id
    processor.set_background_profile(profile)
    third, trace = frame(calibration.context, 2, profile.mean_w)
    assert processor.ingest(third, trace.powers_dbm)
    assert processor.snapshot().interference_model_id is None
    assert processor.snapshot().algorithm_version == "signed-reference-v1"


def test_model_memory_limit_is_checked_before_operator_construction(monkeypatch):
    _x, profile, calibration = fixture()
    config = CorrectionConfig(working_memory_limit_bytes=20 * calibration.baseline_w.size * 8)
    processor = RealtimeSpectrumProcessor(calibration.context, config)
    processor.set_background_profile(profile)
    import app.spectrum.realtime_processor as module

    def forbidden(_calibration):
        raise AssertionError("QR allocated before memory validation")

    monkeypatch.setattr(module, "calibrated_interference_model", forbidden)
    with pytest.raises(ValueError, match="memory"):
        processor.set_interference_calibration(calibration)


@pytest.mark.parametrize("damage", ["missing_model", "model_hash", "coefficients"])
def test_stored_correction_dependencies_reject_tampering(tmp_path, damage):
    _x, profile, calibration = fixture()
    processor = RealtimeSpectrumProcessor(calibration.context, CorrectionConfig())
    processor.set_background_profile(profile)
    processor.set_interference_calibration(calibration)
    envelope, trace = frame(calibration.context, 0, profile.mean_w + calibration.basis_w @ [.4, .1])
    assert processor.ingest(envelope, trace.powers_dbm)
    result = processor.snapshot()
    path = tmp_path / "result.h5"
    run = writer(path)
    try:
        run.store_background_profile(calibration.context, profile)
        with pytest.raises(ExecutionError, match="uncommitted interference"):
            run.append(MeasurementPoint(0, {}, {}), trace, acquisition_envelope=envelope, corrected_frame=result)
        run.store_interference_calibration(calibration)
        with pytest.raises(ExecutionError, match="identity or coefficients"):
            run.append(MeasurementPoint(0, {}, {}), trace, acquisition_envelope=envelope,
                       corrected_frame=replace(result, interference_model_hash="0" * 64))
        run.append(MeasurementPoint(0, {}, {}), trace, acquisition_envelope=envelope, corrected_frame=result)
    finally:
        run.close("aborted")
    assert Hdf5RunReader.spectrum_correction(path, 0).interference_model_id == calibration.model_id
    with h5py.File(path, "r+") as file:
        if damage == "missing_model":
            del file["spectrum_processing_v1/interference_models/line-model"]
        else:
            group = file["spectra/0/correction_v1"]
            metadata = json.loads(group.attrs["metadata_json"])
            if damage == "model_hash":
                metadata["interference_model_hash"] = "0" * 64
            else:
                metadata["interference_last_coefficients"] = [4, 0]
            group.attrs["metadata_json"] = json.dumps(metadata)
    with pytest.raises(ExecutionError):
        Hdf5RunReader.spectrum_correction(path, 0)
