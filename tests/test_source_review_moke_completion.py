"""One terminal outcome is emitted only after every lease release attempt."""

from contextlib import nullcontext
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.devices.moke_box.ui.field_control import MokeFieldWorker, MokeFieldWorkflow


@pytest.mark.parametrize("operation_fails", [False, True])
@pytest.mark.parametrize("cleanup_failure", [None, "release", "live_close"])
def test_terminal_outcome_follows_cleanup(tmp_path, operation_fails, cleanup_failure):
    events = []
    moke = Mock()
    moke.interruption_event = Event()
    moke.get_control_profile.return_value = SimpleNamespace(ramp_timeout_s=1)
    moke.io_timeout.return_value = nullcontext()
    result = object()
    moke.stop_vout.return_value = result
    if operation_fails:
        moke.stop_vout.side_effect = RuntimeError("operation failed")
        moke.emergency_off.side_effect = RuntimeError("shutdown failed")
    def release_moke():
        events.append("release moke")
        if cleanup_failure == "release":
            raise RuntimeError("release failed")
    moke.release.side_effect = release_moke
    reference = Mock()
    reference.interruption_event = Event()
    reference.release.side_effect = lambda: events.append("release reference")
    live = Mock()
    if cleanup_failure == "live_close":
        live.close.side_effect = RuntimeError("close failed")
    worker = MokeFieldWorker("zero", 0, {"reference": reference, "moke_box": moke}, tmp_path, Event(), live)
    successes, failures = [], []
    worker.succeeded.connect(lambda value: (successes.append(value), events.append("success")))
    worker.failed.connect(lambda error: (failures.append(error), events.append("failure")))
    worker.finished.connect(lambda: events.append("finished"))
    worker.run()
    assert events[:2] == ["release moke", "release reference"]
    assert events[-1] == "finished"
    assert len(successes) + len(failures) == 1
    if operation_fails or cleanup_failure:
        assert not successes
        if operation_fails:
            assert "operation failed" in failures[0] and "shutdown failed" in failures[0]
        if cleanup_failure:
            assert ("release failed" if cleanup_failure == "release" else "close failed") in failures[0]
    else:
        assert successes == [result]


def test_failed_initial_reservation_reports_release_error_too():
    lease = Mock()
    lease.release.side_effect = RuntimeError("release failed")
    reference = Mock()
    reference.acquire_run_lease.return_value = lease
    controller = Mock()
    controller.acquire_run_lease.side_effect = RuntimeError("acquire failed")
    workflow = SimpleNamespace(busy=False, _external_controlled=False, _pause_live=None,
        _approve=Mock(), _controller=controller, _reference=reference, _failed=Mock())
    MokeFieldWorkflow._start_job(workflow, "calibration", object())
    lease.release.assert_called_once()
    workflow._failed.assert_called_once()
    message = workflow._failed.call_args.args[0]
    assert "acquire failed" in message and "release failed" in message


def test_incomplete_lease_set_still_releases_owned_instrument(tmp_path):
    reference = Mock()
    worker = MokeFieldWorker("zero", 0, {"reference": reference}, tmp_path, Event())
    failures, finished = [], []
    worker.failed.connect(failures.append)
    worker.finished.connect(lambda: finished.append(True))
    worker.run()
    reference.release.assert_called_once()
    assert len(failures) == 1 and "moke_box" in failures[0]
    assert finished == [True]
