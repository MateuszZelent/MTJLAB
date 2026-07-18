from __future__ import annotations

import unittest

from app.devices.discovery import discover_tcp_endpoints, discover_tcp_ip_range, discover_visa_resources, identify_device


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


class TcpConnection:
    def __init__(self) -> None:
        self.closed = False

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

    def test_tcp_discovery_returns_only_hosts_with_open_requested_port(self) -> None:
        opened: list[TcpConnection] = []

        def connector(address: tuple[str, int], timeout: float) -> TcpConnection:
            self.assertEqual(address[1], 10001)
            self.assertEqual(timeout, 0.1)
            if address[0] != "192.168.50.2":
                raise OSError("connection refused")
            connection = TcpConnection()
            opened.append(connection)
            return connection

        results = discover_tcp_endpoints(
            "192.168.50.0/30", 10001, timeout_s=0.1, connector=connector
        )

        self.assertEqual([result.endpoint for result in results], ["192.168.50.2:10001"])
        self.assertTrue(opened[0].closed)

    def test_tcp_discovery_rejects_public_or_unbounded_networks_without_opt_in(self) -> None:
        with self.assertRaises(ValueError):
            discover_tcp_endpoints("8.8.8.0/24", 10001)
        with self.assertRaises(ValueError):
            discover_tcp_endpoints("192.168.0.0/16", 10001)

    def test_tcp_discovery_allows_explicitly_confirmed_campus_network(self) -> None:
        result = discover_tcp_endpoints(
            "131.246.221.118/32",
            10001,
            allow_non_private=True,
            connector=lambda _address, _timeout: TcpConnection(),
        )
        self.assertEqual([endpoint.endpoint for endpoint in result], ["131.246.221.118:10001"])

    def test_tcp_discovery_supports_inclusive_ip_range(self) -> None:
        results = discover_tcp_ip_range(
            "131.246.221.117", "131.246.221.119", 10001,
            allow_non_private=True,
            connector=lambda address, _timeout: (
                TcpConnection() if address[0].endswith(".118") else (_ for _ in ()).throw(OSError())
            ),
        )
        self.assertEqual([endpoint.endpoint for endpoint in results], ["131.246.221.118:10001"])


if __name__ == "__main__":
    unittest.main()
