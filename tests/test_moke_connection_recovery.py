"""Only read-only connection probes may retry a rejected VOUT response."""

import time

import pytest

from app.devices.moke_box.adapter import MokeBoxAdapter
from app.devices.moke_box.models import MokeBoxConfig
from app.devices.moke_box.protocol import MokeFrame, readback_vout
from app.domain.errors import ConnectionError, DeviceError
from tests.test_moke_protocol import _vout_response


class Sessions:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.events = []
        self.sent = []
        self.timeout_caps = []

    def connect(self, endpoint, timeout_s):
        self.events.append("connect")
        self.timeout_caps.append(timeout_s)
        self.response = next(self.replies)

    def send(self, raw):
        self.sent.append(raw)

    def recv_exact(self, count):
        assert count == 32
        return self.response

    def close(self):
        self.events.append("close")


def duplicate_reply():
    raw = _vout_response()
    return raw[:4] + raw[:4] + raw[8:]


def test_rejected_startup_reply_reopens_once_and_sends_only_readback(caplog):
    transport = Sessions((duplicate_reply(), _vout_response()))
    adapter = MokeBoxAdapter(MokeBoxConfig("test:10001"), transport)
    adapter.connect()
    assert adapter.connected
    assert transport.events == ["connect", "close", "connect"]
    assert transport.sent == [readback_vout(), readback_vout()]
    assert transport.timeout_caps[1] < transport.timeout_caps[0]
    assert "duplicate VOUT0" in caplog.text
    assert duplicate_reply().hex(" ") in caplog.text


@pytest.mark.parametrize("bad,reason", [
    (duplicate_reply(), "duplicate VOUT0"),
    (MokeFrame(1, 2, 0, 128, 0).encode() + _vout_response()[4:], "origin=1"),
    (MokeFrame(0, 1, 0, 128, 0).encode() + _vout_response()[4:], "type=1"),
])
def test_repeated_bad_reply_reports_full_bytes_and_rejects_control(bad, reason):
    transport = Sessions((bad, bad))
    adapter = MokeBoxAdapter(MokeBoxConfig("test:10001"), transport)
    with pytest.raises(ConnectionError) as failure:
        adapter.connect()
    assert reason in str(failure.value)
    assert bad.hex(" ") in str(failure.value)
    assert "Previous connection probe" in str(failure.value)
    assert transport.events == ["connect", "close", "connect", "close"]
    assert not adapter.connected
    assert transport.sent == [readback_vout(), readback_vout()]


def test_bad_readback_after_connection_is_not_automatically_retried():
    transport = Sessions((_vout_response(),))
    adapter = MokeBoxAdapter(MokeBoxConfig("test:10001"), transport)
    adapter.connect()
    transport.response = duplicate_reply()
    with pytest.raises(DeviceError, match="duplicate VOUT0"):
        adapter.read_vouts()
    assert transport.events == ["connect", "close"]
    assert transport.sent == [readback_vout(), readback_vout()]
    assert not adapter.connected


def test_probe_retry_does_not_restart_deadline():
    transport = Sessions((duplicate_reply(), _vout_response()))
    adapter = MokeBoxAdapter(MokeBoxConfig("test:10001", timeout_s=.01), transport)
    began = time.monotonic()
    with pytest.raises(ConnectionError, match="deadline"):
        adapter.connect()
    assert time.monotonic() - began < .1
    assert transport.timeout_caps == pytest.approx([.01], abs=.002)
    assert transport.sent == [readback_vout()]
