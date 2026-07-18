"""Read-only VISA resource discovery and conservative instrument identification."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

import pyvisa

from app.devices.moke_box.protocol import (
    MokeFrame,
    MokeResponseType,
    MokeTarget,
    readback_vout,
)


@dataclass(frozen=True, slots=True)
class DiscoveredInstrument:
    resource: str
    backend: str
    idn: str | None
    device: str | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredTcpEndpoint:
    """One host that accepted a TCP connection during a bounded LAN scan."""

    host: str
    port: int
    moke_verified: bool | None = None
    verification_detail: str | None = None
    tx_bytes: bytes = b""
    rx_bytes: bytes = b""

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"


def detect_local_ipv4_address(
    *, socket_factory: Callable[[int, int], object] | None = None
) -> str:
    """Return the IPv4 source address selected by the active default route.

    UDP ``connect`` selects a route locally; this helper sends no payload or
    application command. The returned address is used only to prefill a scan.
    """

    factory = socket_factory or socket.socket
    probe = None
    try:
        probe = factory(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("1.1.1.1", 53))
        address = str(probe.getsockname()[0])
        parsed = ipaddress.IPv4Address(address)
        if parsed.is_unspecified or parsed.is_loopback:
            raise OSError("no routable IPv4 address was selected")
        return address
    except OSError as exc:
        raise OSError("Could not determine the active local IPv4 address.") from exc
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass


def suggested_scan_cidr(address: str, *, prefix_length: int = 24) -> str:
    """Return the containing IPv4 network for a conservative scan default."""

    try:
        return str(ipaddress.ip_network(f"{address}/{prefix_length}", strict=False))
    except ValueError as exc:
        raise ValueError("Cannot derive a scan network from the local IPv4 address.") from exc


def identify_device(idn: str) -> str | None:
    """Map an IDN response to a supported adapter without fuzzy guessing."""

    value = idn.upper()
    if "RIGOL" in value and ("DG1032" in value or "DG1022" in value):
        return "rigol"
    if "KEITHLEY" in value and any(model in value for model in ("2602", "2600")):
        return "keithley"
    if "ANRITSU" in value and "MS2830" in value:
        return "anritsu"
    return None


def discover_visa_resources(
    backends: Iterable[str] = ("system", "@py"),
    *,
    timeout_ms: int = 750,
    manager_factory: Callable[[str], object] | None = None,
) -> tuple[DiscoveredInstrument, ...]:
    """List resources and issue only ``*IDN?`` using a bounded timeout.

    Discovery deliberately does not instantiate an adapter and sends no reset,
    clear, output or configuration command. One unreachable instrument does not
    hide other resources.
    """

    if timeout_ms < 50 or timeout_ms > 5_000:
        raise ValueError("VISA discovery timeout must be between 50 and 5000 ms.")
    factory = manager_factory or _resource_manager
    found: list[DiscoveredInstrument] = []
    seen: set[tuple[str, str]] = set()
    for backend in dict.fromkeys(backends):
        manager = None
        try:
            manager = factory(backend)
            resources = tuple(manager.list_resources())
        except Exception as exc:
            found.append(DiscoveredInstrument("—", backend, None, None, f"Backend unavailable: {exc}"))
            continue
        try:
            for resource in resources:
                key = (backend, str(resource))
                if key in seen:
                    continue
                seen.add(key)
                session = None
                try:
                    session = manager.open_resource(str(resource), open_timeout=timeout_ms)
                    session.timeout = timeout_ms
                    # IEEE-488.2 IDN is line-oriented. These settings improve
                    # compatibility with serial VISA resources without sending
                    # any state-changing command.
                    session.read_termination = "\n"
                    session.write_termination = "\n"
                    idn = str(session.query("*IDN?")).strip()
                    found.append(DiscoveredInstrument(str(resource), backend, idn, identify_device(idn)))
                except Exception as exc:
                    found.append(DiscoveredInstrument(str(resource), backend, None, None, str(exc)))
                finally:
                    if session is not None:
                        try:
                            session.close()
                        except Exception:
                            pass
        finally:
            try:
                manager.close()
            except Exception:
                pass
    return tuple(found)


def _resource_manager(backend: str) -> object:
    return pyvisa.ResourceManager() if backend == "system" else pyvisa.ResourceManager(backend)


def discover_tcp_endpoints(
    network: str,
    port: int,
    *,
    timeout_s: float = 0.5,
    max_hosts: int = 1_024,
    allow_non_private: bool = False,
    verify_moke: bool = False,
    progress_callback: Callable[[int, int, str], None] | None = None,
    activity_callback: Callable[[str, str, str], None] | None = None,
    cancellation_requested: Callable[[], bool] | None = None,
    connector: Callable[[tuple[str, int], float], object] | None = None,
) -> tuple[DiscoveredTcpEndpoint, ...]:
    """Find open instances of one TCP port within an explicitly supplied LAN.

    The scan intentionally avoids protocol probes. Publicly-routed ranges need
    an explicit caller opt-in; this covers campus networks using public IPs.
    """

    try:
        subnet = ipaddress.ip_network(network.strip(), strict=False)
    except ValueError as exc:
        raise ValueError("TCP scan network must be a valid IPv4 CIDR, e.g. 192.168.1.0/24.") from exc
    if subnet.version != 4:
        raise ValueError("TCP scans support IPv4 networks only.")
    if not subnet.is_private and (not allow_non_private or not subnet.is_global):
        raise ValueError(
            "TCP scans outside private IPv4 ranges require explicit confirmation."
        )
    if not 1 <= port <= 65_535:
        raise ValueError("TCP port must be in 1..65535.")
    if not 0.01 <= timeout_s <= 2.0:
        raise ValueError("TCP scan timeout must be in 0.01..2 seconds.")
    hosts = tuple(str(host) for host in subnet.hosts())
    if not hosts:
        raise ValueError("TCP scan network contains no usable host addresses.")
    if len(hosts) > max_hosts:
        raise ValueError(f"TCP scan is limited to {max_hosts} hosts; use a narrower subnet.")
    return _scan_tcp_hosts(
        hosts, port, timeout_s=timeout_s, connector=connector, verify_moke=verify_moke,
        progress_callback=progress_callback, activity_callback=activity_callback,
        cancellation_requested=cancellation_requested,
    )


def discover_tcp_ip_range(
    start: str,
    end: str,
    port: int,
    *,
    timeout_s: float = 0.5,
    max_hosts: int = 1_024,
    allow_non_private: bool = False,
    verify_moke: bool = False,
    progress_callback: Callable[[int, int, str], None] | None = None,
    activity_callback: Callable[[str, str, str], None] | None = None,
    cancellation_requested: Callable[[], bool] | None = None,
    connector: Callable[[tuple[str, int], float], object] | None = None,
) -> tuple[DiscoveredTcpEndpoint, ...]:
    """Find one open TCP port over an inclusive, explicit IPv4 address range."""

    try:
        first, last = ipaddress.IPv4Address(start.strip()), ipaddress.IPv4Address(end.strip())
    except ipaddress.AddressValueError as exc:
        raise ValueError("TCP scan range requires two valid IPv4 addresses.") from exc
    if int(first) > int(last):
        raise ValueError("TCP scan range start must not be after its end.")
    if not (first.is_private and last.is_private) and not allow_non_private:
        raise ValueError(
            "TCP scans outside private IPv4 ranges require explicit confirmation."
        )
    if not 1 <= port <= 65_535:
        raise ValueError("TCP port must be in 1..65535.")
    if not 0.01 <= timeout_s <= 2.0:
        raise ValueError("TCP scan timeout must be in 0.01..2 seconds.")
    count = int(last) - int(first) + 1
    if count > max_hosts:
        raise ValueError(f"TCP scan is limited to {max_hosts} hosts; use a narrower range.")
    hosts = tuple(str(ipaddress.IPv4Address(value)) for value in range(int(first), int(last) + 1))
    return _scan_tcp_hosts(
        hosts, port, timeout_s=timeout_s, connector=connector, verify_moke=verify_moke,
        progress_callback=progress_callback, activity_callback=activity_callback,
        cancellation_requested=cancellation_requested,
    )


def _scan_tcp_hosts(
    hosts: tuple[str, ...],
    port: int,
    *,
    timeout_s: float,
    connector: Callable[[tuple[str, int], float], object] | None,
    verify_moke: bool,
    progress_callback: Callable[[int, int, str], None] | None,
    activity_callback: Callable[[str, str, str], None] | None,
    cancellation_requested: Callable[[], bool] | None,
) -> tuple[DiscoveredTcpEndpoint, ...]:
    """Probe a prevalidated finite host list without speaking an application protocol."""

    open_connection = connector or socket.create_connection

    def probe(host: str) -> DiscoveredTcpEndpoint | None:
        connection = None
        try:
            if cancellation_requested is not None and cancellation_requested():
                if activity_callback is not None:
                    activity_callback(host, "cancelled", "")
                return None
            if activity_callback is not None:
                activity_callback(host, "scanning", "")
            connection = open_connection((host, port), timeout_s)
            if cancellation_requested is not None and cancellation_requested():
                if activity_callback is not None:
                    activity_callback(host, "cancelled", "")
                return None
            if not verify_moke:
                if activity_callback is not None:
                    activity_callback(host, "open", "Not requested")
                return DiscoveredTcpEndpoint(host, port)
            tx_bytes = readback_vout()
            try:
                rx_bytes = _verify_moke_readback(connection)
            except Exception as exc:
                if activity_callback is not None:
                    activity_callback(host, "open", str(exc))
                return DiscoveredTcpEndpoint(
                    host,
                    port,
                    False,
                    str(exc),
                    tx_bytes,
                    getattr(exc, "received", b""),
                )
            if activity_callback is not None:
                activity_callback(host, "open", "MOKE Box verified")
            return DiscoveredTcpEndpoint(
                host,
                port,
                True,
                "Readback VOUT returned all 8 AD5362 channels.",
                tx_bytes,
                rx_bytes,
            )
        except OSError:
            if activity_callback is not None:
                activity_callback(host, "closed", "")
            return None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    found: list[DiscoveredTcpEndpoint] = []
    workers = min(64, len(hosts))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tcp-discovery") as executor:
        futures = {executor.submit(probe, host): host for host in hosts}
        completed = 0
        for future in as_completed(futures):
            host = futures[future]
            result = future.result()
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, len(hosts), host)
            if result is not None:
                found.append(result)
    return tuple(sorted(found, key=lambda result: tuple(map(int, result.host.split(".")))))


class _MokeReadbackError(OSError):
    """Readback failure retaining any bytes received for diagnostics."""

    def __init__(self, message: str, received: bytes = b"") -> None:
        super().__init__(message)
        self.received = received


def _verify_moke_readback(connection: object) -> bytes:
    """Verify the documented, read-only MOKE VOUT response on one connection."""

    sendall = getattr(connection, "sendall", None)
    recv = getattr(connection, "recv", None)
    if not callable(sendall) or not callable(recv):
        raise _MokeReadbackError("TCP connection does not support MOKE readback verification.")
    sendall(readback_vout())
    chunks: list[bytes] = []
    remaining = 32
    while remaining:
        chunk = recv(remaining)
        if not chunk:
            raise _MokeReadbackError(
                "Connection closed during MOKE VOUT readback.", b"".join(chunks)
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    try:
        frames = tuple(
            MokeFrame.decode(raw[index:index + 4]) for index in range(0, 32, 4)
        )
    except Exception as exc:
        raise _MokeReadbackError(f"Invalid MOKE VOUT response: {exc}", raw) from exc
    channels = {
        frame.channel
        for frame in frames
        if frame.origin in {MokeTarget.MAIN_BOX, MokeTarget.OPT2}
        and frame.record_type == MokeResponseType.AD5362
    }
    if channels != set(range(8)) or len({frame.channel for frame in frames}) != 8:
        raise _MokeReadbackError(
            "Response is not the documented eight-channel MOKE VOUT readback.", raw
        )
    return raw
