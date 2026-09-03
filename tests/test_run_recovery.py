from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import h5py

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.keithley_2600 import KeithleyAdapter
from app.devices.rigol_dg1000z import RigolAdapter
from app.devices.simulators import SimulatedVisaFactory, simulated_station_settings
from app.domain.errors import ExecutionError
from app.engine import RecipeCompiler, RecipeRunner, RunRecoveryManager
from app.recipes import parse_recipe_text
from app.settings.models import StationSettings
from app.storage import Hdf5RunWriter, ThatecCompatibilityValidator
from tests.helpers import loaded_settings


RECOVERY_RECIPE = """\
schema_version: 1
name: recovery-two-point
dut_limits:
  keithley:
    B:
      current: {min: "0 A", max: "2 mA"}
      voltage: {min: "-67 mV", max: "67 mV"}
      max_abs_power: "134 uW"
  anritsu:
    max_expected_input: "-10 dBm"
root:
  id: root
  type: sequence
  children:
    - id: anritsu-config
      type: configure_anritsu
      start_frequency: "1 MHz"
      stop_frequency: "2 MHz"
      reference_level: "0 dBm"
      points: 101
    - id: first-spectrum
      type: acquire_spectrum
      trace: TRAC1
    - id: keithley-config
      type: configure_keithley
      channel: B
      mode: current
      level: "1 mA"
      compliance: "67 mV"
    - id: keithley-on
      type: set_keithley_output
      channel: B
      enabled: true
    - id: keithley-measure
      type: measure_keithley
      channel: B
    - id: second-spectrum
      type: acquire_spectrum
      trace: TRAC1
"""


NESTED_AXIS_RECIPE = """\
schema_version: 1
name: nested-recovery
root:
  id: root
  type: sequence
  children:
    - id: current-axis
      type: sweep
      target: keithley.B.current
      start: 0 A
      stop: 1 mA
      points: 2
      children:
        - id: frequency-axis
          type: sweep
          target: rigol.1.frequency
          start: 1 kHz
          stop: 2 kHz
          points: 2
          children:
            - id: settle
              type: wait
              duration: 1 ms
finally: []
"""


REVERSED_NESTED_AXIS_RECIPE = NESTED_AXIS_RECIPE.replace(
    "    - id: current-axis\n"
    "      type: sweep\n"
    "      target: keithley.B.current\n"
    "      start: 0 A\n"
    "      stop: 1 mA\n"
    "      points: 2\n"
    "      children:\n"
    "        - id: frequency-axis\n"
    "          type: sweep\n"
    "          target: rigol.1.frequency\n"
    "          start: 1 kHz\n"
    "          stop: 2 kHz\n"
    "          points: 2\n",
    "    - id: frequency-axis\n"
    "      type: sweep\n"
    "      target: rigol.1.frequency\n"
    "      start: 1 kHz\n"
    "      stop: 2 kHz\n"
    "      points: 2\n"
    "      children:\n"
    "        - id: current-axis\n"
    "          type: sweep\n"
    "          target: keithley.B.current\n"
    "          start: 0 A\n"
    "          stop: 1 mA\n"
    "          points: 2\n",
)


class RunRecoveryTests(unittest.TestCase):
    @staticmethod
    def _settings() -> StationSettings:
        raw = deepcopy(
            simulated_station_settings(loaded_settings()).model_dump(mode="python")
        )
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        return StationSettings.model_validate(raw)

    @staticmethod
    def _adapters(settings: StationSettings) -> tuple[RigolAdapter, KeithleyAdapter, AnritsuAdapter]:
        return (
            RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol")),
            KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley")),
            AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu")),
        )

    def test_resume_truncates_unsafe_tail_replays_configuration_and_completes(self) -> None:
        settings = self._settings()
        plan = RecipeCompiler(settings).compile(parse_recipe_text(RECOVERY_RECIPE))
        settings_source = "simulation: true\n"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recoverable.h5"
            rigol, keithley, anritsu = self._adapters(settings)
            keithley.connect()
            anritsu.connect()
            writer = Hdf5RunWriter(
                path,
                recipe_source=RECOVERY_RECIPE,
                settings_source=settings_source,
                plan_hash=plan.sha256,
                device_idn={},
                expected_points=plan.total_points,
            )
            first_result = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                writer=writer,
            ).run(plan)
            self.assertEqual(first_result.stored_points, 2)
            with h5py.File(path, "r+") as h5:
                h5["run"].attrs["status"] = "faulted"

            checkpoint = RunRecoveryManager().inspect(path, plan)
            self.assertEqual(checkpoint.stored_points, 1)
            self.assertEqual(checkpoint.committed_points_found, 2)
            self.assertEqual(
                tuple(action.kind for action in checkpoint.prelude_actions),
                ("configure_anritsu",),
            )

            writer = Hdf5RunWriter.resume(
                path,
                recipe_source=RECOVERY_RECIPE,
                settings_source=settings_source,
                plan_hash=plan.sha256,
                checkpoint_count=checkpoint.stored_points,
                expected_points=plan.total_points,
            )
            rigol, keithley, anritsu = self._adapters(settings)
            keithley.connect()
            anritsu.connect()
            resumed = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                writer=writer,
            ).run(
                plan,
                start_action_index=checkpoint.next_action_index,
                stored_points=checkpoint.stored_points,
                recovery_prelude=checkpoint.prelude_actions,
            )
            self.assertIsNone(resumed.error)
            self.assertEqual(resumed.stored_points, 2)

            with h5py.File(path, "r") as h5:
                self.assertEqual(h5["run"].attrs["status"], "completed")
                self.assertEqual(int(h5["run"].attrs["resume_count"]), 1)
                self.assertEqual(len(h5["points"]), 2)
                events = tuple(h5["events/name"].asstr()[:])
                self.assertIn("run_resumed", events)
                self.assertIn("recovery_prelude_finished", events)
            report = ThatecCompatibilityValidator().validate(path, require_pythat=True)
            self.assertTrue(report.valid, report.errors)
            self.assertEqual(dict(report.dimensions)["Acquisition"], 2)

    def test_recovery_rejects_completed_run(self) -> None:
        settings = self._settings()
        plan = RecipeCompiler(settings).compile(parse_recipe_text(RECOVERY_RECIPE))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "completed.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source=RECOVERY_RECIPE,
                settings_source="simulation: true\n",
                plan_hash=plan.sha256,
                device_idn={},
            )
            writer.close("completed")
            with self.assertRaisesRegex(ExecutionError, "completed run"):
                RunRecoveryManager().inspect(path, plan)

    def test_recovery_rejects_changed_axis_nesting_order(self) -> None:
        settings = self._settings()
        original = RecipeCompiler(settings).compile(
            parse_recipe_text(NESTED_AXIS_RECIPE)
        )
        reversed_plan = RecipeCompiler(settings).compile(
            parse_recipe_text(REVERSED_NESTED_AXIS_RECIPE)
        )
        self.assertNotEqual(original.sha256, reversed_plan.sha256)
        self.assertEqual(original.total_points, reversed_plan.total_points)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "changed-axis-nesting.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source=NESTED_AXIS_RECIPE,
                settings_source="simulation: true\n",
                plan_hash=original.sha256,
                device_idn={},
            )
            writer.close("faulted")

            with self.assertRaisesRegex(ExecutionError, "plan hash"):
                RunRecoveryManager().inspect(path, reversed_plan)


if __name__ == "__main__":
    unittest.main()

