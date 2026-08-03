from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.ui.analysis_worker import (
    SpectrumAnalysisController,
    SpectrumAnalysisRequest,
)
from app.spectrum import clean_spectrum_dbm as real_clean_spectrum_dbm


class SpectrumAnalysisWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_analysis_runs_off_gui_thread_and_coalesces_to_newest_frame(self) -> None:
        controller = SpectrumAnalysisController()
        completed: list[int] = []
        gui_ticks: list[float] = []
        worker_threads: list[QThread] = []
        controller.result.connect(lambda outcome: completed.append(outcome.generation))

        def slow_cleanup(*args: object, **kwargs: object) -> object:
            worker_threads.append(QThread.currentThread())
            time.sleep(0.05)
            return real_clean_spectrum_dbm(*args, **kwargs)

        def request(generation: int) -> SpectrumAnalysisRequest:
            return SpectrumAnalysisRequest(
                generation=generation,
                frequencies_hz=(1.0, 2.0, 3.0, 4.0, 5.0),
                powers_dbm=(-80.0, -79.0, -30.0, -79.0, -80.0),
                mode="raw",
                history_dbm=(),
                detect_peaks=False,
            )

        try:
            with patch(
                "app.devices.anritsu_ms2830a.ui.analysis_worker.clean_spectrum_dbm",
                side_effect=slow_cleanup,
            ):
                controller.submit(request(1))
                controller.submit(request(2))
                controller.submit(request(3))
                QTimer.singleShot(5, lambda: gui_ticks.append(time.monotonic()))
                deadline = time.monotonic() + 3.0
                while len(completed) < 2 and time.monotonic() < deadline:
                    self.application.processEvents()
                    QTest.qWait(10)

            self.assertEqual(completed, [1, 3])
            self.assertTrue(gui_ticks, "GUI timer must fire while CPU analysis is running")
            self.assertTrue(worker_threads)
            self.assertTrue(
                all(thread is not self.application.thread() for thread in worker_threads)
            )
        finally:
            controller.close()
        self.assertFalse(controller._thread.isRunning())

    def test_analysis_outcome_echoes_display_source_identity(self) -> None:
        controller = SpectrumAnalysisController()
        outcomes: list[object] = []
        controller.result.connect(outcomes.append)
        request = SpectrumAnalysisRequest(
            generation=4,
            frequencies_hz=(1.0, 2.0, 3.0, 4.0, 5.0),
            powers_dbm=(-80.0, -79.0, -30.0, -79.0, -80.0),
            mode="raw",
            history_dbm=(),
            detect_peaks=False,
            source_key="processed",
            frame_id=17,
            source_unit="dB",
            provenance=("raw", "reference", "difference_db"),
        )
        try:
            controller.submit(request)
            deadline = time.monotonic() + 3.0
            while not outcomes and time.monotonic() < deadline:
                self.application.processEvents()
                QTest.qWait(10)
            self.assertEqual(len(outcomes), 1)
            outcome = outcomes[0]
            self.assertEqual(outcome.source_key, "processed")
            self.assertEqual(outcome.frame_id, 17)
            self.assertEqual(outcome.source_unit, "dB")
            self.assertEqual(outcome.provenance, ("raw", "reference", "difference_db"))
            self.assertEqual(outcome.cleanup.unit, "dB")
        finally:
            controller.close()


if __name__ == "__main__":
    unittest.main()
