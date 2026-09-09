from __future__ import annotations

import os
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from app.devices.base import DeviceAdapter
from app.domain.models import DeviceIdentity, DeviceState
from app.ui.workers import DeviceController
from app.domain.errors import SafetyViolation


class FakeAdapter(DeviceAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.connect_count = 0

    def connect(self) -> DeviceIdentity:
        self.connect_count += 1
        self._state = DeviceState.VERIFIED
        self._identity = DeviceIdentity("SIM::FAKE", "FAKE,MODEL,1,1")
        return self._identity

    def disconnect(self) -> None:
        self._state = DeviceState.DISCONNECTED

    def emergency_off(self) -> None:
        self._state = DeviceState.OUTPUT_OFF

    def read_owner_thread(self) -> int:
        return threading.get_ident()

    def fail_for_run(self) -> None:
        raise RuntimeError("injected run operation failure")


class DeviceRunLeaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def _connect(self, controller: DeviceController) -> None:
        completed: list[str] = []
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        controller.result.connect(lambda operation, _result: (completed.append(operation), loop.quit()))
        timer.timeout.connect(loop.quit)
        try:
            controller.call("connect")
            timer.start(2_000)
            loop.exec()
            self.assertEqual(completed, ["connect"])
        finally:
            timer.stop()
            controller.result.disconnect()

    def test_run_proxy_executes_adapter_calls_in_its_owner_thread(self) -> None:
        controller = DeviceController(FakeAdapter())
        try:
            self._connect(controller)

            proxy = controller.adapter_for_run()

            self.assertEqual(proxy.connect().idn, "FAKE,MODEL,1,1")
            self.assertEqual(proxy.state, DeviceState.VERIFIED)
            self.assertNotEqual(proxy.read_owner_thread(), threading.get_ident())
        finally:
            controller.close()

    def test_run_proxy_preserves_adapter_errors(self) -> None:
        controller = DeviceController(FakeAdapter())
        try:
            with self.assertRaisesRegex(RuntimeError, "injected run operation failure"):
                controller.adapter_for_run().fail_for_run()
        finally:
            controller.close()

    def test_exclusive_lease_blocks_other_proxies_and_expires(self) -> None:
        controller = DeviceController(FakeAdapter())
        try:
            lease = controller.acquire_run_lease()
            self.assertNotEqual(lease.read_owner_thread(), threading.get_ident())
            with self.assertRaises(SafetyViolation):
                controller.adapter_for_run().read_owner_thread()
            with self.assertRaises(SafetyViolation):
                controller.acquire_run_lease()
            lease.release()
            with self.assertRaisesRegex(SafetyViolation, "expired"):
                lease.read_owner_thread()
            controller.adapter_for_run().read_owner_thread()
        finally:
            controller.close()

    def test_emergency_off_interrupts_lease_and_prevents_reenable(self) -> None:
        controller = DeviceController(FakeAdapter())
        try:
            lease = controller.acquire_run_lease()
            controller.adapter_for_run().emergency_off()
            self.assertTrue(lease.interruption_event.is_set())
            with self.assertRaisesRegex(SafetyViolation, "interrupted"):
                lease.connect()
            lease.emergency_off()
            lease.release()
        finally:
            controller.close()

    def test_transport_gate_rechecks_queued_manual_request(self) -> None:
        from app.ui.workers import _RunAccess
        gate = _RunAccess()
        owner, interrupted = gate.acquire()
        with self.assertRaises(SafetyViolation):
            with gate.enter(None, "configure", (object(),)):
                self.fail("manual request reached adapter")
        with gate.enter(owner, "measure", ("A",)):
            with self.assertRaisesRegex(SafetyViolation, "busy"):
                gate.release(owner)
        with gate.enter(None, "set_output", ("B", False)):
            pass
        self.assertTrue(interrupted.is_set())
        with self.assertRaises(SafetyViolation):
            with gate.enter(owner, "set_output", ("A", True)):
                self.fail("enabled after external OFF")
        gate.release(owner)

    def test_controller_close_interrupts_owner_and_rejects_new_calls(self) -> None:
        controller = DeviceController(FakeAdapter())
        lease = controller.acquire_run_lease()
        self.assertTrue(controller.close())
        self.assertTrue(lease.interruption_event.is_set())
        with self.assertRaisesRegex(SafetyViolation, "shutting down"):
            lease.connect()
        with self.assertRaisesRegex(SafetyViolation, "shutting down"):
            _ = lease.state
        with self.assertRaisesRegex(SafetyViolation, "shutting down"):
            controller.acquire_run_lease()

    def test_shutdown_gate_rejects_queued_enable_but_allows_off(self) -> None:
        from app.ui.workers import _RunAccess
        gate = _RunAccess()
        owner, interrupted = gate.acquire()
        gate.begin_shutdown()
        self.assertTrue(interrupted.is_set())
        with self.assertRaisesRegex(SafetyViolation, "shutting down"):
            with gate.enter(owner, "set_output", ("A", True)):
                self.fail("enabled while closing")
        with gate.enter(None, "emergency_off"):
            pass


if __name__ == "__main__":
    unittest.main()
