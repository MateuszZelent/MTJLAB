from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from copy import deepcopy

import h5py

from app.devices.anritsu import AnritsuAdapter
from app.devices.keithley import KeithleyAdapter
from app.devices.rigol import RigolAdapter
from app.devices.simulators import SimulatedVisaFactory, simulated_station_settings
from app.engine import RecipeCompiler, RecipeRunner
from app.recipes import load_recipe
from app.storage import Hdf5RunWriter
from app.storage.hdf5_reader import Hdf5RunReader
from app.settings.models import StationSettings
from tests.helpers import loaded_settings


class SimulatedRunTests(unittest.TestCase):
    def test_recipe_compiles_runs_and_writes_one_hdf5_checkpoint(self) -> None:
        recipe_source = """\
schema_version: 1
name: simulation-smoke
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
    - id: measure
      type: measure_keithley
      channel: B
    - id: spectrum
      type: acquire_spectrum
      trace: TRAC1
"""
        settings = simulated_station_settings(loaded_settings())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recipe_path = root / "recipe.yml"
            recipe_path.write_text(recipe_source, encoding="utf-8")
            plan = RecipeCompiler(settings).compile(load_recipe(recipe_path))
            rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
            keithley = KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley"))
            anritsu = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
            identities = {
                "rigol": rigol.connect().idn,
                "keithley": keithley.connect().idn,
                "anritsu": anritsu.connect().idn,
            }
            target = root / "result.h5"
            writer = Hdf5RunWriter(
                target,
                recipe_source=recipe_source,
                settings_source="simulation: true\n",
                plan_hash=plan.sha256,
                device_idn=identities,
            )
            result = RecipeRunner(rigol=rigol, keithley=keithley, anritsu=anritsu, writer=writer).run(plan)
            self.assertIsNone(result.error)
            self.assertEqual(result.stored_points, 1)
            with h5py.File(target, "r") as file:
                self.assertEqual(file["run"].attrs["status"], "completed")
                self.assertEqual(len(file["spectra/0/power_dbm"]), 101)
                self.assertIn("keithley.B.current_a", file["points/0/measurements_json"].asstr()[()])
                self.assertIn("run_started", tuple(file["events/name"].asstr()[:]))
            self.assertEqual(Hdf5RunReader.detail(target).events[-1].name, "run_completed")

    def test_explicitly_armed_energized_point_runs_only_with_approved_permissions(self) -> None:
        recipe_source = """\
schema_version: 1
name: energized-simulation-smoke
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
    - id: keithley-config
      type: configure_keithley
      channel: B
      mode: current
      level: "1 mA"
      compliance: "67 mV"
    - id: rigol-config
      type: configure_rigol
      channel: 1
      waveform: SQU
      frequency: "1 kHz"
      high_level: "1 mV"
      low_level: "-1 mV"
      output_load: HIGHZ
      dut_min_impedance: "50 ohm"
    - id: arm-keithley
      type: arm_keithley_output
      channel: B
    - id: on-keithley
      type: set_keithley_output
      channel: B
      enabled: true
    - id: arm-rigol
      type: arm_rigol_output
      channel: 1
    - id: on-rigol
      type: set_rigol_output
      channel: 1
      enabled: true
    - id: measure
      type: measure_keithley
      channel: B
    - id: spectrum
      type: acquire_spectrum
      trace: TRAC1
finally:
  - id: rigol-off
    type: set_rigol_output
    channel: 1
    enabled: false
  - id: keithley-ramp
    type: ramp_keithley_to_zero
    channel: B
"""
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recipe_path = root / "recipe.yml"
            recipe_path.write_text(recipe_source, encoding="utf-8")
            plan = RecipeCompiler(settings).compile(load_recipe(recipe_path))
            rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
            keithley = KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley"))
            anritsu = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
            for device in (rigol, keithley, anritsu):
                device.connect()
            writer = Hdf5RunWriter(root / "result.h5", recipe_source=recipe_source, settings_source="simulation: true\n", plan_hash=plan.sha256, device_idn={})
            result = RecipeRunner(rigol=rigol, keithley=keithley, anritsu=anritsu, writer=writer).run(plan)
            self.assertIsNone(result.error)
            self.assertEqual(result.stored_points, 1)


if __name__ == "__main__":
    unittest.main()
