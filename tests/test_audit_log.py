from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from app.audit import AuditLogReader, AuditLogger
from app.domain.errors import ConfigurationError


class AuditLogTests(unittest.TestCase):
    def test_schema_sequence_redaction_and_close_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logger = AuditLogger(
                temporary,
                profile_id="station-a",
                simulation=False,
                application_version="test",
                actor="LAB\\alice",
                actor_roles=("engineer",),
            )
            logger.record(
                "VISA assignment saved",
                category="configuration",
                event_type="visa_assignment",
                correlation_id="run-123",
                context={
                    "resource": "GPIB0::23::INSTR",
                    "api_token": "must-not-leak",
                    "nested": {"password": "must-not-leak", "value": float("inf")},
                },
                critical=True,
            )
            path = logger.path
            logger.close()

            records = AuditLogReader.read(path)
            self.assertEqual([record["sequence"] for record in records], [0, 1, 2])
            self.assertEqual(records[0]["event_type"], "session_started")
            self.assertEqual(records[-1]["event_type"], "session_closed")
            self.assertEqual(records[1]["correlation_id"], "run-123")
            self.assertEqual(records[1]["actor"], "LAB\\alice")
            self.assertEqual(records[1]["actor_roles"], ["engineer"])
            context = records[1]["context"]
            self.assertEqual(context["api_token"], "<redacted>")
            self.assertEqual(context["nested"]["password"], "<redacted>")
            self.assertEqual(context["nested"]["value"], "inf")
            self.assertNotIn("must-not-leak", path.read_text(encoding="utf-8"))

    def test_concurrent_writers_keep_a_contiguous_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logger = AuditLogger(temporary, profile_id="station-a", simulation=True)
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(lambda index: logger.record(f"event {index}"), range(100)))
            path = logger.path
            logger.close()

            records = AuditLogReader.read(path)
            self.assertEqual(len(records), 102)
            self.assertEqual(
                [record["sequence"] for record in records],
                list(range(102)),
            )

    def test_reader_rejects_a_corrupted_or_non_contiguous_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "corrupt.jsonl"
            first = {
                "schema_version": 1,
                "session_id": "session",
                "sequence": 0,
            }
            second = {**first, "sequence": 2}
            path.write_text(
                json.dumps(first) + "\n" + json.dumps(second) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "Non-contiguous"):
                AuditLogReader.read(path)

    def test_closed_logger_rejects_new_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logger = AuditLogger(temporary, profile_id="station-a", simulation=False)
            logger.close()
            with self.assertRaises(RuntimeError):
                logger.record("too late")


if __name__ == "__main__":
    unittest.main()
