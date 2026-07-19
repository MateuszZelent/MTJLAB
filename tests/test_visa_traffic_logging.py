from __future__ import annotations

import unittest

from pyvisa.constants import Parity, StopBits

from app.devices.visa import FakeVisaSession, _ManagedVisaSession


class _ManagerStub:
    def close(self) -> None:
        pass


class VisaTrafficLoggingTests(unittest.TestCase):
    def test_serial_configuration_is_applied_to_underlying_visa_session_and_logged(self) -> None:
        messages: list[str] = []
        raw = FakeVisaSession()
        session = _ManagedVisaSession(raw, _ManagerStub(), messages.append)

        session.read_termination = "\r\n"
        session.write_termination = "\r\n"
        session.baud_rate = 57_600
        session.data_bits = 7
        session.parity = Parity.odd
        session.stop_bits = StopBits.one

        self.assertEqual(raw.read_termination, "\r\n")
        self.assertEqual(raw.write_termination, "\r\n")
        self.assertEqual(raw.baud_rate, 57_600)
        self.assertEqual(raw.data_bits, 7)
        self.assertEqual(raw.parity, Parity.odd)
        self.assertEqual(raw.stop_bits, StopBits.one)
        log = "\n".join(messages)
        self.assertIn("CONFIG baud_rate=57600", log)
        self.assertIn("CONFIG data_bits=7", log)
        self.assertIn("CONFIG parity=", log)

    def test_spectrum_payload_is_suppressed_but_summary_is_logged(self) -> None:
        payload = "-80.125,-79.5,-81.75"
        messages: list[str] = []
        session = _ManagedVisaSession(
            FakeVisaSession(responses={"TRAC? TRAC1": payload}),
            _ManagerStub(),
            messages.append,
        )

        response = session.query("TRAC? TRAC1")

        self.assertEqual(response, payload)
        log = "\n".join(messages)
        self.assertIn("TX QUERY 'TRAC? TRAC1'", log)
        self.assertIn("spectrum data suppressed; 3 point(s)", log)
        self.assertNotIn("-80.125", log)
        self.assertNotIn("-79.5", log)
        self.assertNotIn("-81.75", log)

    def test_short_non_trace_response_remains_visible(self) -> None:
        messages: list[str] = []
        session = _ManagedVisaSession(
            FakeVisaSession(responses={"*IDN?": "ANRITSU,MS2830A,123,1.0"}),
            _ManagerStub(),
            messages.append,
        )

        session.query("*IDN?")

        self.assertIn("'ANRITSU,MS2830A,123,1.0'", "\n".join(messages))


if __name__ == "__main__":
    unittest.main()
