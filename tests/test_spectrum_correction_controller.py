"""Ordered archival, bounded backpressure and teardown across the Qt worker."""

import os
import json
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import h5py
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.devices.anritsu_ms2830a.ui.correction_controller import (
    CorrectionSessionRequest, CorrectionViewSnapshot, SpectrumCorrectionController,
    SpectrumFinalizationRequest,
)
from app.domain.spectrum_correction import (
    CorrectionConfig, SpectrumAcquisitionContext, SpectrumFrameEnvelope,
    SpectrumFrameRole, SweepEvidence, TemporalAverageMode,
)
from app.storage.hdf5_reader import Hdf5RunReader
from app.domain.errors import ExecutionError
from app.spectrum.replay import replay_quantitative_session
from tests.test_spectrum_correction_store import fixture_profile


def wait_until(application, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.01)
    application.processEvents()
    assert predicate(), "Worker did not complete the expected operation"


def frame(context, index, values_w, role=SpectrumFrameRole.SIGNAL):
    acquired = 100.0 + index
    envelope = SpectrumFrameEnvelope(
        index, "reference" if role == SpectrumFrameRole.REFERENCE else "signal",
        context.context_id, 0, acquired, role=role,
        evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP,
    )
    trace = SpectrumTrace(
        tuple(context.frequencies_hz), tuple(10 * np.log10(values_w) + 30),
        datetime.fromtimestamp(acquired, timezone.utc), "TRAC1",
    )
    return envelope, trace


def test_worker_archives_reference_then_signed_frames_in_order(tmp_path):
    application = QApplication.instance() or QApplication([])
    controller = SpectrumCorrectionController(queue_frames=4)
    completed, failed = [], []
    controller.completed.connect(lambda operation, payload: completed.append((operation, payload)))
    controller.failed.connect(lambda *payload: failed.append(payload))
    context = SpectrumAcquisitionContext([1e6, 2e6, 3e6], "fixture-rms")
    path = tmp_path / "worker.h5"
    config = CorrectionConfig(average_mode=TemporalAverageMode.BLOCK, minimum_reference_sweeps=3)

    def submit_and_wait(operation, action):
        before = sum(name == operation for name, _ in completed)
        action()
        wait_until(application, lambda: bool(failed) or sum(name == operation for name, _ in completed) > before)
        assert not failed
        return next(payload for name, payload in reversed(completed) if name == operation)

    try:
        submit_and_wait("start", lambda: controller.start_session(CorrectionSessionRequest(
            context, config, path, "fixture: true", "ANRITSU,SIM", reference_state="off resonance",
        )))
        reference = np.array([1e-9, 2e-9, 3e-9])
        for index in range(3):
            ack = submit_and_wait("frame", lambda i=index: controller.ingest(*frame(
                context, i, reference, SpectrumFrameRole.REFERENCE,
            )))
            assert ack["view"] is None
        profile = submit_and_wait("reference", controller.finish_reference)
        assert profile.sweep_count == 3
        for index in range(3, 6):
            ack = submit_and_wait("frame", lambda i=index: controller.ingest(*frame(
                context, i, reference + [1e-10, -1e-10, 2e-10],
            )))
            assert ack["committed_point_count"] == index + 1
            assert ack["processing_duration_s"] > 0 and ack["commit_duration_s"] > 0
            assert isinstance(ack["view"], CorrectionViewSnapshot)
            assert ack["view"].corrected.frame_id == index
            assert ack["view"].source_raw.acquired_at_utc.timestamp() == 100 + index
            np.testing.assert_allclose(ack["view"].corrected.values_w, [1e-10, -1e-10, 2e-10], rtol=1e-12)
        view = submit_and_wait("snapshot", controller.request_snapshot)
        assert isinstance(view, CorrectionViewSnapshot)
        assert view.corrected.frame_id == 5 and view.corrected.count == 3
        assert view.source_raw.acquired_at_utc.timestamp() == 105
        np.testing.assert_allclose(view.corrected.values_w, [1e-10, -1e-10, 2e-10], rtol=1e-12)
        rejected = submit_and_wait("frame", lambda: controller.ingest(*frame(context, 5, reference * 2)))
        assert not rejected["accepted"] and rejected["committed_point_count"] == 7
        assert rejected["view"] is None
        submit_and_wait("stop", controller.stop_session)
        assert [Hdf5RunReader.spectrum_acquisition(path, i).frame_id for i in range(7)] == [0, 1, 2, 3, 4, 5, 5]
        assert Hdf5RunReader.spectrum_correction(path, 2) is None
        assert Hdf5RunReader.spectrum_correction(path, 6) is None
        assert Hdf5RunReader.spectrum_correction(path, 5).count == 3
        with h5py.File(path, "r") as file:
            assert file["run"].attrs["status"] == "completed"
            assert len(file["points"]) == 7
        replayed = list(replay_quantitative_session(path))
        assert len(replayed) == 7 and not replayed[6].contributed and replayed[6].result is None
        assert replayed[5].result.count == 3
        np.testing.assert_allclose(replayed[5].result.values_w, view.corrected.values_w, rtol=1e-12)
        with h5py.File(path, "r+") as file:
            file["spectra/5/power_dbm"][1] += 3
        with pytest.raises(ExecutionError, match="signed power differs"):
            list(replay_quantitative_session(path))
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


def test_failed_checkpoint_never_publishes_unstored_correction(tmp_path, monkeypatch):
    from app.storage.hdf5_writer import Hdf5RunWriter

    application = QApplication.instance() or QApplication([])
    controller = SpectrumCorrectionController()
    completed, errors = [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: errors.append(args))
    context, profile = fixture_profile()
    path = tmp_path / "failed-publication.h5"
    append = Hdf5RunWriter.append

    def fail_second_checkpoint(writer, *args, **kwargs):
        if writer.point_count == 1:
            raise ExecutionError("Injected checkpoint failure")
        return append(writer, *args, **kwargs)

    monkeypatch.setattr(Hdf5RunWriter, "append", fail_second_checkpoint)
    try:
        controller.start_session(CorrectionSessionRequest(context, CorrectionConfig(), path,
            "fixture: true", "ANRITSU,SIM", profile=profile))
        wait_until(application, lambda: bool(completed) or bool(errors))
        assert not errors
        controller.ingest(*frame(context, 11, profile.mean_w + [1e-10, -1e-10, 2e-10]))
        wait_until(application, lambda: any(name == "frame" for name, _ in completed) or bool(errors))
        assert not errors
        controller.ingest(*frame(context, 12, profile.mean_w * 2))
        wait_until(application, lambda: bool(errors))
        assert errors == [("frame", "Injected checkpoint failure")]
        acknowledgements = [payload for name, payload in completed if name == "frame"]
        assert len(acknowledgements) == 1
        assert acknowledgements[0]["view"].corrected.frame_id == 11
        controller.request_snapshot()
        wait_until(application, lambda: any(name == "snapshot" for name, _ in completed))
        assert [payload for name, payload in completed if name == "snapshot"] == [None]
        assert Hdf5RunReader.summary(path).point_count == 1
        assert Hdf5RunReader.summary(path).status == "faulted"
        assert Hdf5RunReader.spectrum_correction(path, 0).frame_id == 11
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


def test_mixed_counter_rejection_is_archived_and_replays_consistently(tmp_path):
    application = QApplication.instance() or QApplication([])
    controller = SpectrumCorrectionController()
    completed, errors = [], []
    controller.completed.connect(lambda operation, payload: completed.append((operation, payload)))
    controller.failed.connect(lambda *args: errors.append(args))
    context, profile = fixture_profile()
    path = tmp_path / "mixed-evidence.h5"

    def submit(operation, action):
        previous = sum(name == operation for name, _ in completed)
        action()
        wait_until(application, lambda: bool(errors) or sum(name == operation for name, _ in completed) > previous)
        assert not errors
        return next(value for name, value in reversed(completed) if name == operation)

    try:
        submit("start", lambda: controller.start_session(CorrectionSessionRequest(
            context, CorrectionConfig(average_mode=TemporalAverageMode.BLOCK), path, "fixture: true", "SIM",
            profile=profile, simulation_mode=True,
        )))
        values = profile.mean_w + [1e-10, -1e-10, 2e-10]
        accepted = []
        for index, evidence, sweep_id in ((12, SweepEvidence.INSTRUMENT_COUNTER, "40"),
                                          (13, SweepEvidence.QUALIFIED_SINGLE_SWEEP, "host:opaque"),
                                          (14, SweepEvidence.INSTRUMENT_COUNTER, "40"),
                                          (15, SweepEvidence.INSTRUMENT_COUNTER, "41")):
            envelope, trace = frame(context, index, values * (100 if index == 14 else 1))
            envelope = replace(envelope, evidence=evidence, sweep_id=sweep_id)
            ack = submit("frame", lambda envelope=envelope, trace=trace: controller.ingest(envelope, trace))
            accepted.append(ack["accepted"])
        assert accepted == [True, True, False, True]
        submit("stop", controller.stop_session)
        assert Hdf5RunReader.spectrum_correction(path, 2) is None
        replayed = list(replay_quantitative_session(path))
        assert [item.contributed for item in replayed] == accepted
        assert replayed[-1].result.count == 3
        np.testing.assert_allclose(replayed[-1].result.values_w, [1e-10, -1e-10, 2e-10], rtol=1e-12)
        assert len(replayed) == 4  # Rejected raw is preserved, but does not enter the estimate.
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


def test_full_fifo_rejects_producer_without_dropping_accepted_messages(monkeypatch):
    application = QApplication.instance() or QApplication([])
    controller = SpectrumCorrectionController(queue_frames=2)
    entered, release = Event(), Event()
    received = []
    completed = []

    def execute(operation, payload):
        if operation == "block":
            entered.set()
            assert release.wait(5)
        received.append(operation)
        return payload

    monkeypatch.setattr(controller._worker, "_execute", execute)
    controller.completed.connect(lambda operation, payload: completed.append(operation))
    try:
        controller._submit("block")
        assert entered.wait(3)
        controller._submit("first")
        controller.request_snapshot()
        controller.request_snapshot()  # Coalesced despite full FIFO.
        assert controller.queued_count == 2
        with pytest.raises(BufferError, match="FIFO is full"):
            controller._submit("rejected")
        # Shutdown is placed behind already accepted frames and remains bounded.
        before = time.monotonic()
        assert not controller.close(wait_ms=10)
        assert time.monotonic() - before < 0.2
        release.set()
        assert controller.close(wait_ms=5000)
        wait_until(application, lambda: "shutdown" in completed)
        assert received == ["block", "first", "snapshot", "shutdown"]
        with pytest.raises(ValueError, match="shutting down"):
            controller._submit("too-late")
    finally:
        release.set()
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("mode", [True, False, None])
def test_worker_archives_explicit_backend_mode_without_guessing_from_identity(tmp_path, mode):
    application = QApplication.instance() or QApplication([])
    controller = SpectrumCorrectionController()
    completed, errors = [], []
    controller.completed.connect(lambda operation, payload: completed.append(operation))
    controller.failed.connect(lambda *args: errors.append(args))
    path = tmp_path / "provenance.h5"
    context = SpectrumAcquisitionContext([1e6, 2e6], "provenance")
    try:
        controller.start_session(CorrectionSessionRequest(
            context, CorrectionConfig(), path, "fixture: true", "ANRITSU,SIM",
            simulation_mode=mode,
        ))
        wait_until(application, lambda: "start" in completed or bool(errors))
        assert not errors
        controller.stop_session()
        wait_until(application, lambda: "stop" in completed or bool(errors))
        assert not errors
        metadata = Hdf5RunReader.detail(path).simulation_metadata
        assert metadata["enabled"] is mode
        assert metadata["mode"] == ("unknown" if mode is None else "simulation" if mode else "hardware")
        assert metadata["mode_source"] == ("not_provided" if mode is None else "application_runtime")
        with h5py.File(path, "r") as file:
            assert json.loads(file["run/simulation_json"].asstr()[()]) == metadata
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("mode", ["false", 0, 1, {}])
def test_session_rejects_ambiguous_backend_flags(tmp_path, mode):
    context = SpectrumAcquisitionContext([1e6, 2e6], "provenance")
    with pytest.raises(ValueError, match="explicit boolean"):
        CorrectionSessionRequest(context, CorrectionConfig(), tmp_path / "invalid.h5", "", "", simulation_mode=mode)


@pytest.mark.parametrize("action", ["cancel", "close"])
def test_offline_processing_cancels_without_waiting_for_queued_shutdown(tmp_path, monkeypatch, action):
    application = QApplication.instance() or QApplication([])
    controller = SpectrumCorrectionController()
    entered = Event()
    cancelled, failed = [], []
    controller.cancelled.connect(cancelled.append)
    controller.failed.connect(lambda *args: failed.append(args))

    def slow_finalize(*args, cancellation_check=None, **_selection):
        entered.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            cancellation_check()
            time.sleep(.002)
        raise AssertionError("Offline cancellation token was not delivered")

    monkeypatch.setattr("app.storage.finalized_spectrum_store.finalize_spectrum_archives", slow_finalize)
    request = SpectrumFinalizationRequest(*(tmp_path / name for name in ("signal", "before", "after", "output")))
    try:
        controller.finalize_archives(request)
        assert entered.wait(2)
        if action == "cancel":
            controller.cancel_processing()
            # A second request must not clear the first job's cancellation flag.
            with pytest.raises(ValueError, match="already pending"):
                controller.finalize_archives(request)
        else:
            assert controller.close(wait_ms=500)
        wait_until(application, lambda: bool(cancelled) or bool(failed))
        assert cancelled == ["finalize"] and not failed
        assert not request.destination.exists()
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


def test_worker_finalizes_explicit_profile_and_checkpoint_subset(tmp_path):
    from dataclasses import replace
    import h5py
    from app.storage.spectrum_correction_codec import read_profile, write_profile
    from app.storage.finalized_spectrum_store import replay_finalized_artifact
    from tests.test_spectrum_finalized_store import archives

    application = QApplication.instance() or QApplication([])
    _context, expected, paths = archives(tmp_path)
    with h5py.File(paths[0], "r+") as file:
        root = file["spectrum_processing_v1/profiles"]
        context, profile = read_profile(root["before"])
        decoy = replace(profile, profile_id="000_decoy", mean_w=profile.mean_w * 2)
        group = root.create_group(decoy.profile_id)
        write_profile(group, context, decoy)
        group.attrs["complete"] = True
    destination = tmp_path / "subset.h5"
    request = SpectrumFinalizationRequest(*paths, destination, point_indices=(1, 2),
        before_profile_id="before", after_profile_id="after", signal_profile_id="before")
    controller = SpectrumCorrectionController()
    completed, failed = [], []
    controller.completed.connect(lambda *args: completed.append(args))
    controller.failed.connect(lambda *args: failed.append(args))
    try:
        controller.finalize_archives(request)
        wait_until(application, lambda: bool(completed) or bool(failed), timeout=15)
        assert not failed
        operation, (path, block, _profiles) = completed[0]
        assert operation == "finalize" and path == destination
        assert block.source_frame_ids == (1, 2) and block.result.count == 2
        np.testing.assert_allclose(block.result.values_w, expected, rtol=1e-12)
        replayed = replay_finalized_artifact(destination)
        np.testing.assert_array_equal(replayed.result.values_w, block.result.values_w)
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("indices", [[0, 1], (), (1, 0), (1, 1), (True,)])
def test_finalization_request_rejects_mutable_or_invalid_checkpoint_selection(tmp_path, indices):
    paths = tuple(tmp_path / name for name in ("signal", "before", "after", "output"))
    with pytest.raises(ValueError, match="immutable strictly ordered"):
        SpectrumFinalizationRequest(*paths, point_indices=indices)
