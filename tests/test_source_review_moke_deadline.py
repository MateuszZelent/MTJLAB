"""MOKE send/receive and ramp steps share the caller's operation budget."""

import pytest

from app.devices.moke_box import transport as transport_module
from app.domain.errors import DeviceError
from tests.test_moke_voltage_control import controlled_adapter, plan_for, mutations


class Socket:
    def __init__(self, clock):
        self.clock = clock
        self.timeout = 5
        self.calls = []

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        self.calls.append(("send", self.timeout))
        self.clock[0] += .4

    def recv(self, count):
        self.calls.append(("recv", self.timeout))
        self.clock[0] += .4
        return b"x" * count


def test_tcp_operation_budget_includes_send_and_all_records(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(transport_module.time, "monotonic", lambda: clock[0])
    transport = transport_module.MokeBoxTcpTransport()
    transport._socket = socket = Socket(clock)
    transport._timeout_s = 5
    with transport.operation_timeout(1):
        transport.send(b"request")
        assert transport.recv_exact(4) == b"xxxx"
        with transport.operation_timeout(30):
            assert transport.recv_exact(4) == b"xxxx"
            with pytest.raises(TimeoutError):
                transport.send(b"expired")
            with pytest.raises(TimeoutError):
                transport.recv_exact(4)
    assert [kind for kind, _ in socket.calls] == ["send", "recv", "recv"]
    assert [timeout for _, timeout in socket.calls] == pytest.approx([1, .6, .2])
    assert transport._operation_deadline is None
    assert socket.timeout == 5


def test_short_operation_budget_does_not_accelerate_ramp():
    adapter, transport, profile = controlled_adapter(simulation=False, minimum_settling_s=0)
    plan = plan_for(profile)
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    transport.sent.clear()
    with pytest.raises(DeviceError, match="deadline"):
        with adapter.operation_timeout(.01):
            adapter.ramp_vout(profile.channel, .2)
    assert mutations(transport) == []
    assert not adapter.safe_target_confirmed
    assert not adapter.connected
    assert adapter._operation_deadline is None
