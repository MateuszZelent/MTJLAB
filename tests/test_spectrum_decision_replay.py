"""Chronological replay of actual queued CPU processing changes."""

import hashlib
from dataclasses import replace

import h5py
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.ui.correction_controller import (
    CorrectionSessionRequest, SpectrumCorrectionController,
)
from app.domain.errors import ExecutionError, ProcessingCancelled
from app.domain.spectrum_correction import CorrectionConfig, SpectrumFrameRole
from app.domain.spectrum_decisions import SpectrumDecisionOperation as Operation, SpectrumProcessingChange
from app.spectrum.replay import replay_quantitative_session
from app.storage.spectrum_decision_store import ROOT
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_spectrum_correction_controller import frame, wait_until
from tests.test_spectrum_correction_store import fixture_profile


def recorded_session(path, *, interrupted_reference=False):
    application = QApplication.instance() or QApplication([])
    controller = SpectrumCorrectionController()
    completed, failed = [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    context, first = fixture_profile()
    second = replace(first, profile_id="second-explicit-profile", mean_w=first.mean_w * 1.1)

    def submit(operation, action):
        previous = sum(name == operation for name, _ in completed)
        action()
        wait_until(application, lambda: bool(failed) or sum(name == operation for name, _ in completed) > previous,
                   timeout=30 if operation == "stop" else 8)
        assert not failed
        return next(value for name, value in reversed(completed) if name == operation)

    def change(operation, **kwargs):
        return submit("processing_change", lambda: controller.apply_processing_change(
            SpectrumProcessingChange(operation, **kwargs)))

    try:
        submit("start", lambda: controller.start_session(CorrectionSessionRequest(
            context, CorrectionConfig(minimum_reference_sweeps=3, maximum_reference_age_s=10),
            path, "fixture: true", "ANRITSU,SIM",
            profile=None if interrupted_reference else first,
            reference_state="signal absent test fixture" if interrupted_reference else None,
        )))
        if interrupted_reference:
            submit("frame", lambda: controller.ingest(*frame(context, 11, first.mean_w, SpectrumFrameRole.REFERENCE)))
            submit("stop", lambda: controller.stop_session("aborted"))
            return []
        views = []
        for index in (11, 12):
            views.append(submit("frame", lambda i=index: controller.ingest(*frame(context, i, first.mean_w * .9)))["view"])
        change(Operation.RESET_SEGMENT)
        views.append(submit("frame", lambda: controller.ingest(*frame(context, 13, first.mean_w * .9)))["view"])
        change(Operation.SET_PROFILE, profile=second)
        views.append(submit("frame", lambda: controller.ingest(*frame(context, 14, first.mean_w * .9)))["view"])
        change(Operation.BEGIN_REFERENCE, reference_state="second reference fixture")
        submit("frame", lambda: controller.ingest(*frame(context, 15, second.mean_w, SpectrumFrameRole.REFERENCE)))
        change(Operation.CANCEL_REFERENCE)
        views.append(submit("frame", lambda: controller.ingest(*frame(context, 16, first.mean_w * .9)))["view"])
        submit("snapshot", lambda: controller.request_snapshot(200.0))
        submit("snapshot", lambda: controller.request_snapshot(201.0))
        # A trailing reset is a decision even without another raw checkpoint.
        change(Operation.RESET_SEGMENT)
        submit("stop", controller.stop_session)
        return views
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


def test_multiple_profiles_resets_and_cancel_replay_every_committed_checkpoint(tmp_path):
    path = tmp_path / "chronological.h5"
    views = recorded_session(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    replayed = list(replay_quantitative_session(path))
    assert len(replayed) == 6 and replayed[4].result is None
    results = [record.result for record in replayed if record.result is not None]
    assert [result.count for result in results] == [1, 2, 1, 1, 1]
    assert len({result.processing_generation for result in results}) == 4
    with h5py.File(path, "r") as file:
        from app.storage.spectrum_decision_store import iter_decisions

        status = [record for record in iter_decisions(file) if record["operation"] == "status_at"]
        assert len(status) == 1  # Repeated preview ticks do not flush another decision.
        assert status[0]["parameters"] == {"at_s": 200.0, "quality": "stale"}
        assert status[0]["before_point_index"] == len(replayed)
    for result, view in zip(results, views, strict=True):
        np.testing.assert_array_equal(result.values_w, view.corrected.values_w)
        assert np.all(result.values_w < 0)
        assert result.profile_weights == view.corrected.profile_weights
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with pytest.raises(ProcessingCancelled):
        list(replay_quantitative_session(path, cancellation_check=lambda: True))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid


def test_interrupted_reference_without_profile_replays_raw_prefix(tmp_path):
    path = tmp_path / "interrupted-reference.h5"
    recorded_session(path, interrupted_reference=True)
    records = list(replay_quantitative_session(path))
    assert len(records) == 1 and records[0].contributed and records[0].result is None


@pytest.mark.parametrize("damage", ["missing_history", "unknown_schema", "missing_marker",
                                    "missing_event", "hash", "profile"])
def test_corrupted_decision_history_never_falls_back_to_legacy(tmp_path, damage):
    path = tmp_path / "damaged.h5"
    recorded_session(path)
    with h5py.File(path, "r+") as file:
        if damage == "missing_history":
            del file[ROOT]
        elif damage == "unknown_schema":
            file["run"].attrs["spectrum_processing_decision_schema"] = "future-schema"
        elif damage == "missing_marker":
            del file["run"].attrs["spectrum_processing_decision_schema"]
        elif damage == "missing_event":
            del file[f"{ROOT}/records/00000002"]
        elif damage == "hash":
            file[f"{ROOT}/records/00000002"].attrs["sha256"] = "0" * 64
        else:
            del file["spectrum_processing_v1/profiles/second-explicit-profile"]
    with pytest.raises(ExecutionError):
        list(replay_quantitative_session(path))
