"""Read-only VISA resource discovery and conservative instrument identification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

import pyvisa


@dataclass(frozen=True, slots=True)
class DiscoveredInstrument:
    resource: str
    backend: str
    idn: str | None
    device: str | None
    error: str | None = None


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
