from __future__ import annotations

import unittest

from app.devices.discovery import discover_visa_resources, identify_device


class DiscoverySession:
    def __init__(self, idn: str) -> None:
        self.idn = idn
        self.timeout = 0
        self.read_termination = None
        self.write_termination = None
        self.queries: list[str] = []
        self.closed = False

    def query(self, command: str) -> str:
        self.queries.append(command)
        return self.idn

    def close(self) -> None:
        self.closed = True


class DiscoveryManager:
    def __init__(self, resources: dict[str, DiscoverySession]) -> None:
        self.resources = resources
        self.closed = False

    def list_resources(self) -> tuple[str, ...]:
        return tuple(self.resources)

    def open_resource(self, resource: str, *, open_timeout: int) -> DiscoverySession:
        if open_timeout != 250:
            raise AssertionError("unexpected discovery timeout")
        return self.resources[resource]

    def close(self) -> None:
        self.closed = True


class VisaDiscoveryTests(unittest.TestCase):
    def test_supported_identities_are_classified_conservatively(self) -> None:
        self.assertEqual(identify_device("RIGOL TECHNOLOGIES,DG1032Z,SN,1"), "rigol")
        self.assertEqual(identify_device("KEITHLEY INSTRUMENTS,2602A,SN,1"), "keithley")
        self.assertEqual(identify_device("ANRITSU,MS2830A,SN,1"), "anritsu")
        self.assertIsNone(identify_device("ACME,POWER-SUPPLY,SN,1"))

    def test_discovery_sends_only_idn_and_closes_every_resource(self) -> None:
        sessions = {
            "USB0::RIGOL::INSTR": DiscoverySession("RIGOL TECHNOLOGIES,DG1032Z,SN,1"),
            "TCPIP0::KEITHLEY::INSTR": DiscoverySession("KEITHLEY INSTRUMENTS,2602A,SN,1"),
        }
        manager = DiscoveryManager(sessions)
        results = discover_visa_resources(
            ("system",), timeout_ms=250, manager_factory=lambda _backend: manager
        )

        self.assertEqual([result.device for result in results], ["rigol", "keithley"])
        self.assertTrue(manager.closed)
        for session in sessions.values():
            self.assertEqual(session.queries, ["*IDN?"])
            self.assertEqual(session.timeout, 250)
            self.assertTrue(session.closed)

    def test_unavailable_backend_is_reported_without_raising(self) -> None:
        results = discover_visa_resources(
            ("broken",), manager_factory=lambda _backend: (_ for _ in ()).throw(RuntimeError("missing"))
        )
        self.assertEqual(len(results), 1)
        self.assertIn("Backend unavailable", results[0].error or "")


if __name__ == "__main__":
    unittest.main()
