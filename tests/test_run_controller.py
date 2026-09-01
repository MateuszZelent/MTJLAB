from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

# This module creates only QtCore objects, but offscreen avoids a platform
# plugin dependency if the suite is executed on a headless CI worker.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import h5py
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from app.devices.base import DeviceAdapter
from app.devices.simulators import simulated_station_settings
from app.domain.models import DeviceIdentity, DeviceState
from app.engine import RecipeCompiler
from app.engine.compiler import ExecutionPlan
from app.recipes import load_recipe
from app.settings.models import StationSettings
from app.storage import Hdf5RunReader
from app.ui.run_worker import RunController, RunTelemetryCoalescer
from app.ui.workers import DeviceController
from tests.helpers import loaded_settings


class _RunLeaseAdapter(DeviceAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.connect_count = 0
        self.emergency_off_count = 0
        self.disconnect_count = 0

    def connect(self) -> DeviceIdentity:
        if self._state is DeviceState.DISCONNECTED:
            self.connect_count += 1
            self._state = DeviceState.VERIFIED
            self._identity = DeviceIdentity("SIM::LEASE", "LEASE,MODEL,1,1")
        assert self._identity is not None
        return self._identity

    def disconnect(self) -> None:
        self.disconnect_count += 1
        self._state = DeviceState.DISCONNECTED

    def emergency_off(self) -> None:
        self.emergency_off_count += 1
        self._state = DeviceState.OUTPUT_OFF


class RunControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_run_telemetry_coalescer_keeps_critical_events_and_latest_preview(self) -> None:
        forwarded: list[tuple[str, dict[str, object]]] = []
        coalescer = RunTelemetryCoalescer(
            lambda name, data: forwarded.append((name, data)),
            interval_s=60.0,
        )

        coalescer.submit("spectrum_preview", {"point_index": 0})
        coalescer.submit("spectrum_preview", {"point_index": 1})
        coalescer.submit("spectrum_preview", {"point_index": 2})
        coalescer.submit("point_stored", {"stored_points": 3})
        coalescer.submit("watchdog_timeout", {"node_id": "slow"})

        self.assertEqual(
            forwarded,
            [
                ("spectrum_preview", {"point_index": 0}),
                ("point_stored", {"stored_points": 3}),
                ("watchdog_timeout", {"node_id": "slow"}),
            ],
        )
        coalescer.flush()
        self.assertEqual(
            forwarded[-1], ("spectrum_preview", {"point_index": 2})
        )

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
                timeout.start(15_000)
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

    def test_demo_run_worker_persists_forced_off_mode_and_anritsu_spectrum(self) -> None:
        recipe_source = """\
schema_version: 1
name: qthread-demo-outputs-off
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
      channel: A
      mode: current
      level: "100 uA"
      compliance: "100 mV"
    - id: keithley-on
      type: set_keithley_output
      channel: A
      enabled: true
    - id: spectrum
      type: acquire_spectrum
      trace: TRAC1
finally:
  - id: keithley-off
    type: set_keithley_output
    channel: A
    enabled: false
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = simulated_station_settings(loaded_settings()).model_dump(mode="python")
            raw["storage"]["output_directory"] = str(root / "measurements")
            raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
            settings = StationSettings.model_validate(raw)
            recipe_path = root / "demo-recipe.yml"
            recipe_path.write_text(recipe_source, encoding="utf-8")
            plan = RecipeCompiler(settings, outputs_forced_off=True).compile(
                load_recipe(recipe_path)
            )

            controller = RunController()
            finished: list[object] = []
            failures: list[str] = []
            events: list[tuple[str, object]] = []
            loop = QEventLoop()
            timeout = QTimer()
            timeout.setSingleShot(True)
            controller.finished.connect(
                lambda result: (finished.append(result), loop.quit())
            )
            controller.failed.connect(
                lambda error: (failures.append(error), loop.quit())
            )
            controller.event.connect(lambda name, data: events.append((name, data)))
            timeout.timeout.connect(
                lambda: (
                    failures.append("timeout"),
                    controller.request_stop(),
                    loop.quit(),
                )
            )
            try:
                controller.start(
                    settings,
                    root / "settings.yml",
                    plan,
                    simulation=True,
                    outputs_forced_off=True,
                )
                timeout.start(15_000)
                loop.exec()
                timeout.stop()
                self.application.processEvents()

                self.assertFalse(failures)
                self.assertEqual(len(finished), 1)
                self.assertTrue(
                    any(name == "dry_run_output_action_suppressed" for name, _ in events)
                )
                files = list((root / "measurements").glob("*.h5"))
                self.assertEqual(len(files), 1)
                detail = Hdf5RunReader.detail(files[0])
                self.assertEqual(detail.summary.status, "completed")
                self.assertTrue(detail.simulation_metadata["outputs_forced_off"])
                self.assertEqual(
                    detail.simulation_metadata["execution_mode"],
                    "dry_run",
                )
                self.assertEqual(detail.summary.spectrum_count, 1)
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

    def test_watchdog_timeout_automatically_requests_out_of_band_estop_once(self) -> None:
        controller = RunController()
        settings = simulated_station_settings(loaded_settings())
        controller._run_settings = settings
        controller._run_simulation = True
        with patch.object(controller, "request_emergency_stop") as emergency:
            controller._worker_event("watchdog_timeout", {"node_id": "slow"})
            controller._worker_event("watchdog_timeout", {"node_id": "slow"})

        emergency.assert_called_once_with(settings, simulation=True)

    def test_run_controller_reuses_a_provided_connected_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = simulated_station_settings(loaded_settings()).model_dump(mode="python")
            raw["storage"]["output_directory"] = str(root / "measurements")
            settings = StationSettings.model_validate(raw)
            plan = ExecutionPlan(
                "leased-session",
                (),
                0,
                "a" * 64,
                "name: leased-session\n",
                frozenset({"anritsu"}),
                0,
            )
            adapter = _RunLeaseAdapter()
            device_controller = DeviceController(adapter)
            connected = QEventLoop()
            device_controller.result.connect(
                lambda operation, _result: operation == "connect" and connected.quit()
            )
            device_controller.call("connect")
            QTimer.singleShot(2_000, connected.quit)
            connected.exec()
            self.assertEqual(adapter.connect_count, 1)

            controller = RunController()
            finished: list[object] = []
            failures: list[str] = []
            loop = QEventLoop()
            controller.finished.connect(lambda result: (finished.append(result), loop.quit()))
            controller.failed.connect(lambda error: (failures.append(error), loop.quit()))
            try:
                controller.start(
                    settings,
                    root / "settings.yml",
                    plan,
                    simulation=True,
                    outputs_forced_off=True,
                    device_controllers={"anritsu": device_controller},
                )
                QTimer.singleShot(5_000, loop.quit)
                loop.exec()
                self.assertFalse(failures)
                self.assertEqual(len(finished), 1)
                self.assertEqual(adapter.connect_count, 1)
                self.assertGreaterEqual(adapter.emergency_off_count, 1)
                self.assertGreaterEqual(adapter.disconnect_count, 1)
            finally:
                controller.close()
                device_controller.close()


if __name__ == "__main__":
    unittest.main()
