from __future__ import annotations

import unittest

from app.devices.visa import FakeVisaSession, _ManagedVisaSession


class _ManagerStub:
    def close(self) -> None:
        pass


class VisaTrafficLoggingTests(unittest.TestCase):
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
