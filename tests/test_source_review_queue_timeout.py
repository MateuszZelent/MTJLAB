"""A timed-out queued mutation must never reach the instrument later."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ui.workers import DeviceController, InstrumentWorker, RunDeviceAdapter, _RunCall


def test_expired_queued_mutation_is_never_dispatched():
    request = _RunCall("set_output", ("A", True))
    worker = SimpleNamespace(_invoke_member=Mock(), _publish_state=Mock(), _run_access=None)
    with pytest.raises(TimeoutError, match="Cancelled before dispatch"):
        DeviceController._wait_for_run_call(request, 0.001)
    InstrumentWorker.invoke_for_run(worker, request)
    worker._invoke_member.assert_not_called()
    worker._publish_state.assert_not_called()


def test_in_flight_timeout_is_reported_as_uncertain_not_cancelled():
    request = _RunCall("set_output", ("B", True))
    assert request.try_start()
    with pytest.raises(TimeoutError, match="already dispatched.*uncertain"):
        DeviceController._wait_for_run_call(request, 0.001)
    assert not request.completed.is_set()
    assert not request.cancel_pending()


def test_state_publication_error_still_completes_request():
    request = _RunCall("read_configuration")
    worker = SimpleNamespace(_invoke_member=Mock(), _run_access=None,
                             _publish_state=Mock(side_effect=RuntimeError("state unavailable")))
    InstrumentWorker.invoke_for_run(worker, request)
    assert request.completed.is_set()
    with pytest.raises(RuntimeError, match="state unavailable"):
        DeviceController._wait_for_run_call(request, 0.001)


def test_dispatch_error_is_not_replaced_by_state_publication_error():
    request = _RunCall("configure")
    worker = SimpleNamespace(_invoke_member=Mock(side_effect=ValueError("configuration failed")),
                             _run_access=None,
                             _publish_state=Mock(side_effect=RuntimeError("state unavailable")))
    InstrumentWorker.invoke_for_run(worker, request)
    with pytest.raises(ValueError, match="configuration failed"):
        DeviceController._wait_for_run_call(request, 0.001)


def test_operation_budget_is_separate_and_shared_by_all_calls(monkeypatch):
    from app.ui import workers

    clock = [100.0]
    monkeypatch.setattr(workers.time, "monotonic", lambda: clock[0])
    controller = Mock()
    proxy = RunDeviceAdapter(controller)
    with proxy.operation_timeout(120), proxy.io_timeout(5):
        proxy.acquire_single_sweep("TRAC1")
        assert controller.call_for_run.call_args.kwargs["timeout_s"] == 5
        assert controller.call_for_run.call_args.kwargs["wait_timeout_s"] == 120
        clock[0] += 40
        proxy.acquire_single_sweep("TRAC1")
        assert controller.call_for_run.call_args.kwargs["wait_timeout_s"] == 80
        with proxy.operation_timeout(200):
            assert proxy._remaining_operation_s() == 80
        clock[0] += 81
        with pytest.raises(TimeoutError, match="before dispatch"):
            proxy.acquire_single_sweep("TRAC1")
        assert controller.call_for_run.call_count == 2
    assert proxy._remaining_operation_s() is None


def test_controller_wait_does_not_derive_from_per_command_timeout():
    controller = SimpleNamespace(_run_access=SimpleNamespace(assert_open=Mock()), run_request=Mock(),
                                 _wait_for_run_call=Mock(return_value="done"))
    assert DeviceController.call_for_run(controller, "acquire", timeout_s=0.01,
                                         wait_timeout_s=120) == "done"
    request, budget = controller._wait_for_run_call.call_args.args
    assert budget == 120
    assert request.timeout_s == 0.01
    assert request.deadline_monotonic is not None


def test_queued_deadline_expires_without_waiter_cancellation(monkeypatch):
    from app.ui import workers
    monkeypatch.setattr(workers.time, "monotonic", lambda: 12.0)
    request = _RunCall("set_output", ("A", True), deadline_monotonic=11.0)
    worker = SimpleNamespace(_invoke_member=Mock(), _publish_state=Mock(), _run_access=None)
    InstrumentWorker.invoke_for_run(worker, request)
    assert request.completed.is_set()
    worker._invoke_member.assert_not_called()
    with pytest.raises(TimeoutError, match="expired before dispatch"):
        DeviceController._wait_for_run_call(request, .01)


def test_queue_delay_reduces_actual_adapter_io_budget(monkeypatch):
    from app.ui import workers
    from app.devices.base import DeviceAdapter
    from tests.test_source_review_operation_deadline import Session

    clock = [100.0]
    monkeypatch.setattr(workers.time, "monotonic", lambda: clock[0])
    session = Session(clock)
    class Adapter:
        operation_timeout = DeviceAdapter.operation_timeout
        io_timeout = DeviceAdapter.io_timeout

        def acquire(self):
            self._session.query("first")
            self._session.query("second")
            self._session.query("must not send")

    adapter = Adapter()
    adapter._session = session
    controller = SimpleNamespace(
        _run_access=SimpleNamespace(assert_open=Mock()), run_request=Mock(),
        _wait_for_run_call=Mock(),
    )
    DeviceController.call_for_run(controller, "acquire", timeout_s=5, wait_timeout_s=1)
    request = controller.run_request.emit.call_args.args[0]
    clock[0] += .5  # Half of the operation budget was spent waiting in the queue.
    worker = SimpleNamespace(_adapter=adapter, _run_access=None, _publish_state=Mock())
    worker._invoke_member = lambda req: InstrumentWorker._invoke_member(worker, req)
    InstrumentWorker.invoke_for_run(worker, request)
    assert request.completed.is_set() and isinstance(request.error, TimeoutError)
    assert len(session.timeouts) == 2
    assert session.timeouts[0] == 500
    assert 99 <= session.timeouts[1] <= 101
    assert adapter._session is session and session.timeout == 5000
