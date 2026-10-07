"""Reject oversized discovery before enumeration; hash closed archives in chunks."""

import hashlib
import ipaddress
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.devices import discovery
from app.storage.moke_calibration_store import MokeCalibrationRunStore


def test_oversized_network_is_rejected_before_enumerating_hosts(monkeypatch):
    def forbidden(_self):
        pytest.fail("Oversized subnet was enumerated")
    monkeypatch.setattr(ipaddress.IPv4Network, "hosts", forbidden)
    with pytest.raises(ValueError, match="limited to 1024 hosts"):
        discovery.discover_tcp_endpoints("10.0.0.0/8", 10001)


@pytest.mark.parametrize("network,limit,expected", [
    ("192.168.1.0/30", 2, ("192.168.1.1", "192.168.1.2")),
    ("192.168.1.0/31", 2, ("192.168.1.0", "192.168.1.1")),
    ("192.168.1.0/32", 1, ("192.168.1.0",)),
])
def test_usable_host_count_includes_point_to_point_and_single_address(monkeypatch, network, limit, expected):
    seen = []
    def capture(hosts, *_args, **_kwargs):
        seen.append(hosts)
        return ()
    monkeypatch.setattr(discovery, "_scan_tcp_hosts", capture)
    assert discovery.discover_tcp_endpoints(network, 10001, max_hosts=limit) == ()
    assert seen == [expected]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, float("inf")])
def test_discovery_requires_a_finite_integer_host_budget(limit):
    with pytest.raises(ValueError, match="positive integer"):
        discovery.discover_tcp_endpoints("192.168.1.0/24", 10001, max_hosts=limit)
    with pytest.raises(ValueError, match="positive integer"):
        discovery.discover_tcp_ip_range("192.168.1.1", "192.168.1.2", 10001, max_hosts=limit)


def test_calibration_close_hashes_finalized_file_without_read_bytes(tmp_path, monkeypatch):
    path = tmp_path / "calibration.h5"
    data = b"measurement" * 100_000
    closed = []
    def close(status):
        closed.append(status)
        path.write_bytes(data)
    store = object.__new__(MokeCalibrationRunStore)
    store.path, store._closed = path, False
    store._writer = SimpleNamespace(close=close)
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("whole-file allocation"))
    assert store.close("completed") == hashlib.sha256(data).hexdigest()
    assert store.close("completed") == hashlib.sha256(data).hexdigest()
    assert closed == ["completed"]


def test_failed_calibration_close_never_returns_a_hash(tmp_path):
    store = object.__new__(MokeCalibrationRunStore)
    store.path, store._closed = tmp_path / "failed.h5", False
    def fail(_status):
        raise OSError("flush failed")
    store._writer = SimpleNamespace(close=fail)
    with pytest.raises(OSError, match="flush failed"):
        store.close("completed")
    assert not store._closed
