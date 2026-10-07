"""Asynchronous persistence failure must never acknowledge durable success."""
from types import SimpleNamespace
from unittest.mock import Mock
import queue

import pytest

from app.audit import AuditLogger
from app.ui.shell import MainWindow
from tests.shell_test_isolation import isolated_shell_persistence as isolated_shell_persistence
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


@pytest.mark.parametrize("operation", ["write", "flush", "fsync"])
def test_disk_failure_propagates_and_latches_health(tmp_path, monkeypatch, operation):
    logger = AuditLogger(tmp_path, profile_id="test", simulation=True)
    stream = logger._stream
    wrapper = Mock(wraps=stream)
    logger._stream = wrapper
    if operation == "fsync":
        monkeypatch.setattr("app.audit.logger.os.fsync", Mock(side_effect=OSError("disk fault")))
    else:
        getattr(wrapper, operation).side_effect = OSError("disk fault")
    try:
        with pytest.raises(RuntimeError, match="disk fault"):
            logger.record("must be durable", wait_durable=True)
        with pytest.raises(RuntimeError, match="disk fault"):
            logger.check_health()
        with pytest.raises(RuntimeError, match="disk fault"):
            logger.record("later mutation")
        with pytest.raises(RuntimeError, match="disk fault"):
            logger.flush()
    finally:
        with pytest.raises(RuntimeError, match="disk fault"):
            logger.close()
        assert not logger._worker.is_alive()
        assert stream.closed


def test_full_queue_raises_without_blocking_put(tmp_path, monkeypatch):
    logger = AuditLogger(tmp_path, profile_id="test", simulation=True)
    blocking = Mock(side_effect=AssertionError("blocking put must not be used"))
    with monkeypatch.context() as patch:
        patch.setattr(logger._queue, "put", blocking)
        patch.setattr(logger._queue, "put_nowait", Mock(side_effect=queue.Full))
        with pytest.raises(RuntimeError, match="queue is full"):
            logger.record("overflow")
        blocking.assert_not_called()
    with pytest.raises(RuntimeError, match="queue is full"):
        logger.close()
    assert not logger._worker.is_alive()


def test_durability_wait_timeout_is_not_success(tmp_path):
    logger = AuditLogger(tmp_path, profile_id="test", simulation=True)
    with pytest.raises(RuntimeError, match="timed out"):
        logger._wait_durable(SimpleNamespace(wait=lambda **_kwargs: False))
    with pytest.raises(RuntimeError, match="timed out"):
        logger.close()


def test_wait_durable_forces_fsync_even_without_critical_flag(tmp_path, monkeypatch):
    logger = AuditLogger(tmp_path, profile_id="test", simulation=True)
    import os
    fsync = Mock(wraps=os.fsync)
    monkeypatch.setattr("app.audit.logger.os.fsync", fsync)
    logger.record("durable", wait_durable=True)
    assert fsync.call_count == 1
    logger.close()


def test_shell_detects_async_failure_and_keeps_leased_output_off_available(monkeypatch):
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.show()
        application.processEvents()
        assert window.isVisible() and window.width() >= 820
        stopped = Mock()
        monkeypatch.setattr(window._run_controller, "request_stop", stopped)
        window._audit._fail(OSError("asynchronous disk failure"))
        assert window._audit_healthy  # not yet projected by the GUI timer
        with pytest.raises(Exception, match="audit log is unavailable"):
            window._assert_audit_ready_for_run()
        assert not window._audit_healthy
        stopped.assert_called_once()
        with pytest.raises(Exception, match="audit log is unavailable"):
            window._guard_manual_operation("keithley", "set_output", ("A", True))
        window._leased_run_devices.add("keithley")
        window._guard_manual_operation("keithley", "set_output", ("A", False))
        window._guard_manual_operation("keithley", "ramp_to_zero", "A")
    finally:
        window.close()
        application.processEvents()
