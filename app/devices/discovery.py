"""Read-only VISA resource discovery and conservative instrument identification."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

import pyvisa


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

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"


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
    timeout_s: float = 0.15,
    max_hosts: int = 1_024,
    allow_non_private: bool = False,
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
        hosts, port, timeout_s=timeout_s, connector=connector
    )


def discover_tcp_ip_range(
    start: str,
    end: str,
    port: int,
    *,
    timeout_s: float = 0.15,
    max_hosts: int = 1_024,
    allow_non_private: bool = False,
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
    return _scan_tcp_hosts(hosts, port, timeout_s=timeout_s, connector=connector)


def _scan_tcp_hosts(
    hosts: tuple[str, ...],
    port: int,
    *,
    timeout_s: float,
    connector: Callable[[tuple[str, int], float], object] | None,
) -> tuple[DiscoveredTcpEndpoint, ...]:
    """Probe a prevalidated finite host list without speaking an application protocol."""

    open_connection = connector or socket.create_connection

    def probe(host: str) -> DiscoveredTcpEndpoint | None:
        connection = None
        try:
            connection = open_connection((host, port), timeout_s)
            return DiscoveredTcpEndpoint(host, port)
        except OSError:
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
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                found.append(result)
    return tuple(sorted(found, key=lambda result: tuple(map(int, result.host.split(".")))))
