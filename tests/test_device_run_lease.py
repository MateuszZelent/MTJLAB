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


if __name__ == "__main__":
    unittest.main()

