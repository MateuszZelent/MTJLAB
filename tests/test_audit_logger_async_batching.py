"""Verify audit redaction, JSON encoding, and batched flushing stay off the caller thread."""

from __future__ import annotations

import threading
from unittest.mock import Mock

import app.audit.logger as audit_module
from app.audit import AuditLogger, AuditLogReader


def test_audit_redaction_and_json_encoding_run_on_writer_thread(tmp_path, monkeypatch) -> None:
    caller_thread = threading.current_thread()
    redaction_threads: list[threading.Thread] = []
    original_redact = audit_module._redacted_json_value

    def spy_redact(value: object, *, key: str = "") -> object:
        redaction_threads.append(threading.current_thread())
        return original_redact(value, key=key)

    monkeypatch.setattr(audit_module, "_redacted_json_value", spy_redact)
    logger = AuditLogger(tmp_path, profile_id="bench", simulation=True)
    try:
        logger.record(
            "traffic sample",
            category="traffic",
            event_type="visa_rx",
            context={"api_token": "secret-value", "payload": "TRAC1"},
        )
        logger.flush()
    finally:
        path = logger.path
        logger.close()

    assert redaction_threads
    assert all(thread is not caller_thread for thread in redaction_threads)
    records = AuditLogReader.read(path)
    assert records[1]["context"]["api_token"] == "<redacted>"


def test_audit_batches_non_critical_flushes_and_immediately_flushes_safety(tmp_path) -> None:
    logger = AuditLogger(tmp_path, profile_id="bench", simulation=True)
    stream = logger._stream
    wrapper = Mock(wraps=stream)
    logger._stream = wrapper
    try:
        for index in range(40):
            logger.record(
                f"traffic {index}",
                category="traffic",
                event_type="visa_tx",
                context={"step": index},
            )
        logger.flush()
        batched_flushes = wrapper.flush.call_count
        # 40 traffic records + 1 explicit flush must not perform 40 individual flushes.
        assert batched_flushes < 15

        before_safety = wrapper.flush.call_count
        logger.record(
            "Emergency stop asserted",
            severity="critical",
            category="safety",
            event_type="emergency_stop",
            critical=True,
            wait_durable=True,
        )
        assert wrapper.flush.call_count >= before_safety + 1
    finally:
        path = logger.path
        logger.close()

    records = AuditLogReader.read(path)
    assert len(records) == 43  # session_started + 40 traffic + emergency_stop + session_closed
    assert [record["sequence"] for record in records] == list(range(43))
