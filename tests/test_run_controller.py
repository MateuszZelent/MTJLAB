from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

# This module creates only QtCore objects, but offscreen avoids a platform
# plugin dependency if the suite is executed on a headless CI worker.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import h5py
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from app.devices.simulators import simulated_station_settings
from app.engine import RecipeCompiler
from app.recipes import load_recipe
from app.settings.models import StationSettings
from app.ui.run_worker import RunController
from tests.helpers import loaded_settings


class RunControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_simulated_run_completes_in_dedicated_qthread(self) -> None:
        recipe_source = """\
schema_version: 1
name: qthread-smoke
root:
  id: root
  type: sequence
  children:
    - id: anritsu
      type: configure_anritsu
      start_frequency: "1 MHz"
      stop_frequency: "2 MHz"
      reference_level: "0 dBm"
      points: 101
    - id: keithley
      type: configure_keithley
      channel: B
      mode: current
      level: "1 mA"
      compliance: "67 mV"
    - id: rigol
      type: configure_rigol
      channel: 1
      waveform: SQU
      frequency: "1 kHz"
      high_level: "1 mV"
      low_level: "-1 mV"
      output_load: HIGHZ
      dut_min_impedance: "50 ohm"
    - id: spectrum
      type: acquire_spectrum
      trace: TRAC1
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = simulated_station_settings(loaded_settings()).model_dump(mode="python")
            raw["storage"]["output_directory"] = str(root / "measurements")
            settings = StationSettings.model_validate(raw)
            recipe_path = root / "recipe.yml"
            recipe_path.write_text(recipe_source, encoding="utf-8")
            plan = RecipeCompiler(settings).compile(load_recipe(recipe_path))

            controller = RunController()
            finished: list[object] = []
            failures: list[str] = []
            loop = QEventLoop()
            timeout = QTimer()
            timeout.setSingleShot(True)
            controller.finished.connect(lambda result: (finished.append(result), loop.quit()))
            controller.failed.connect(lambda error: (failures.append(error), loop.quit()))
            timeout.timeout.connect(lambda: (failures.append("timeout"), controller.request_stop(), loop.quit()))
            try:
                controller.start(settings, root / "settings.yml", plan, simulation=True)
                timeout.start(5_000)
                loop.exec()
                timeout.stop()
                self.application.processEvents()
                self.assertFalse(failures)
                self.assertEqual(len(finished), 1)
                result = finished[0]
                self.assertEqual(result["result"].stored_points, 1)  # type: ignore[index,union-attr]
                self.assertFalse(controller.running)
                files = list((root / "measurements").glob("*.h5"))
                self.assertEqual(len(files), 1)
                with h5py.File(files[0], "r") as file:
                    self.assertEqual(file["run"].attrs["status"], "completed")
                    self.assertIn("SIMULATION", file["run/settings_yaml"].asstr()[()])
            finally:
                controller.close()

    def test_emergency_stop_uses_short_lived_simulated_sessions(self) -> None:
        controller = RunController()
        completed: list[object] = []
        loop = QEventLoop()
        timeout = QTimer()
        timeout.setSingleShot(True)
        controller.emergency_completed.connect(lambda errors: (completed.append(errors), loop.quit()))
        timeout.timeout.connect(loop.quit)
        try:
            controller.request_emergency_stop(simulated_station_settings(loaded_settings()), simulation=True)
            timeout.start(5_000)
            loop.exec()
            timeout.stop()
            self.assertEqual(completed, [()])
        finally:
            controller.close()


if __name__ == "__main__":
    unittest.main()
