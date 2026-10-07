"""One operation budget must shrink across successive physical VISA calls."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.devices import base


class Session:
    timeout = 5000

    def __init__(self, clock):
        self.clock = clock
        self.timeouts = []

    def query(self, command):
        self.timeouts.append(self.timeout)
        self.clock[0] += .4
        return command

    write = query
    query_binary_values = query


@pytest.mark.parametrize("method", ["query", "write", "query_binary_values"])
def test_deadline_shrinks_and_prevents_late_dispatch(monkeypatch, method):
    clock = [0.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: clock[0])
    session = Session(clock)
    adapter = SimpleNamespace(_session=session)
    with base.DeviceAdapter.operation_timeout(adapter, 1):
        with base.DeviceAdapter.io_timeout(adapter, .8):
            for _ in range(3):
                assert getattr(adapter._session, method)("command") == "command"
            with pytest.raises(TimeoutError, match="before VISA dispatch"):
                getattr(adapter._session, method)("must not send")
        assert adapter._session.timeout == 5000
    assert session.timeouts == [800, 600, 200]
    assert adapter._session is session and session.timeout == 5000


def test_nested_scope_cannot_extend_parent_and_restores_after_error(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: clock[0])
    session = Session(clock)
    adapter = SimpleNamespace(_session=session)
    with pytest.raises(TimeoutError):
        with base.DeviceAdapter.operation_timeout(adapter, .5):
            with base.DeviceAdapter.operation_timeout(adapter, 30):
                adapter._session.query("one")
                adapter._session.query("two")
                adapter._session.query("expired")
    assert session.timeouts == [500, 100]
    assert adapter._session is session and session.timeout == 5000


def test_scope_does_not_resurrect_disconnected_session():
    adapter = SimpleNamespace(_session=Session([0.0]))
    with base.DeviceAdapter.operation_timeout(adapter, 1):
        adapter._session = None
    assert adapter._session is None


def test_connect_and_identity_share_enclosing_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: clock[0])
    session = Session(clock)
    def open_session(*args):
        clock[0] += .75
        return session
    factory = SimpleNamespace(open=Mock(side_effect=open_session))
    adapter = SimpleNamespace(_session=None)
    with base.DeviceAdapter.operation_timeout(adapter, 1):
        adapter._session = base.DeviceAdapter._open_session(adapter, factory, "resource", "backend", 5000)
        adapter._session.query("*IDN?")
        with pytest.raises(TimeoutError):
            adapter._session.query("later")
    assert factory.open.call_args.args[-1] == 1000
    assert session.timeouts == [250]
    assert adapter._session is session


def test_late_open_is_closed_and_not_exposed(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(base.time, "monotonic", lambda: clock[0])
    session = SimpleNamespace(close=Mock())
    def open_session(*args):
        clock[0] = 2
        return session
    adapter = SimpleNamespace(_session=None)
    with base.DeviceAdapter.operation_timeout(adapter, 1):
        with pytest.raises(TimeoutError, match="Opening VISA"):
            base.DeviceAdapter._open_session(adapter, SimpleNamespace(open=open_session), "r", "b", 5000)
    session.close.assert_called_once()
    assert adapter._session is None
