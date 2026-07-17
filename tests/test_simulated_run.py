from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json

import h5py

from app.devices.anritsu import AnritsuAdapter
from app.devices.keithley import KeithleyAdapter
from app.devices.rigol import RigolAdapter
from app.devices.simulators import SimulatedVisaFactory, simulated_station_settings
from app.engine import RecipeCompiler, RecipeRunner
from app.recipes import load_recipe, parse_recipe_text
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
            events: list[tuple[str, dict[str, object]]] = []
            telemetry: list[tuple[str, dict[str, object]]] = []
            result = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                writer=writer,
                on_event=lambda name, data: events.append((name, data)),
                on_telemetry=lambda name, data: telemetry.append((name, data)),
            ).run(plan)
            self.assertIsNone(result.error)
            self.assertEqual(result.stored_points, 1)
            with h5py.File(target, "r") as file:
                self.assertEqual(file["run"].attrs["status"], "completed")
                self.assertEqual(len(file["spectra/0/power_dbm"]), 101)
                self.assertIn("keithley.B.current_a", file["points/0/measurements_json"].asstr()[()])
                self.assertIn("run_started", tuple(file["events/name"].asstr()[:]))
            self.assertEqual(Hdf5RunReader.detail(target).events[-1].name, "run_completed")
            point_event = next(data for name, data in events if name == "point_stored")
            self.assertEqual(point_event["spectrum_points"], 101)
            self.assertIn("keithley.B.current_a", point_event["measurements_si"])
            preview = next(data for name, data in telemetry if name == "spectrum_preview")
            self.assertEqual(preview["source_points"], 101)
            self.assertEqual(len(preview["frequency_hz"]), 101)

    def test_explicitly_armed_energized_point_runs_only_with_approved_permissions(self) -> None:
        recipe_source = """\
schema_version: 1
name: energized-simulation-smoke
dut_limits:
  keithley:
    B:
      current: {min: "0 A", max: "2 mA"}
      voltage: {min: "-67 mV", max: "67 mV"}
      max_abs_power: "134 uW"
  rigol:
    1:
      minimum_impedance: "50 ohm"
      max_abs_current: "20 uA"
      max_abs_power: "20 nW"
  anritsu:
    max_expected_input: "-10 dBm"
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
            with h5py.File(root / "result.h5", "r") as file:
                metadata = json.loads(file["points/0/metadata_json"].asstr()[()])
            safety = metadata["safety_context"]
            self.assertEqual(safety["rigol.1"]["minimum_impedance_ohm"], 50.0)
            self.assertEqual(safety["keithley.B"]["current_max_a"], 0.002)
            self.assertEqual(safety["anritsu"]["max_expected_input_dbm"], -10.0)

    def test_nested_sweep_round_trips_all_cartesian_points_through_pythat(self) -> None:
        from PyThat import MeasurementTree

        recipe_source = """\
schema_version: 1
name: two-by-two-storage-contract
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
    - id: current-sweep
      type: sweep
      target: keithley.B.current
      start: "1 mA"
      stop: "2 mA"
      points: 2
      children:
        - id: keithley-config
          type: configure_keithley
          channel: B
          mode: current
          level: "${keithley.B.current}"
          compliance: "67 mV"
        - id: rigol-sweep
          type: sweep
          target: rigol.1.high_level
          start: "1 mV"
          stop: "2 mV"
          points: 2
          children:
            - id: rigol-config
              type: configure_rigol
              channel: 1
              waveform: SQU
              frequency: "1 kHz"
              high_level: "${rigol.1.high_level}"
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
        plan = RecipeCompiler(settings).compile(parse_recipe_text(recipe_source))
        self.assertEqual(plan.total_points, 4)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
            keithley = KeithleyAdapter(
                settings, session_factory=SimulatedVisaFactory("keithley")
            )
            anritsu = AnritsuAdapter(
                settings, session_factory=SimulatedVisaFactory("anritsu")
            )
            for device in (rigol, keithley, anritsu):
                device.connect()
            target = root / "cartesian.h5"
            writer = Hdf5RunWriter(
                target,
                recipe_source=recipe_source,
                settings_source="simulation: true\n",
                plan_hash=plan.sha256,
                device_idn={},
                expected_points=plan.total_points,
            )
            result = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                writer=writer,
            ).run(plan)
            self.assertIsNone(result.error)
            self.assertEqual(result.stored_points, 4)

            with redirect_stdout(StringIO()):
                tree = MeasurementTree(target, index=True, override=True)
            self.assertEqual(tree.dataset.sizes["Keithley B current"], 2)
            self.assertEqual(tree.dataset.sizes["Rigol CH1 high level"], 2)
            self.assertEqual(tree.dataset.sizes["Frequency"], 101)
            self.assertEqual(
                tree.dataset.coords["Keithley B current"].values.tolist(),
                [0.001, 0.002],
            )
            self.assertEqual(
                tree.dataset.coords["Rigol CH1 high level"].values.tolist(),
                [0.001, 0.002],
            )
            self.assertEqual(tree.dataset.coords["Keithley B current"].attrs["units"], "A")
            self.assertEqual(tree.dataset.coords["Rigol CH1 high level"].attrs["units"], "V")
            self.assertEqual(
                set(tree.dataset["Spectrum"].dims),
                {"Keithley B current", "Rigol CH1 high level", "Frequency"},
            )
            self.assertEqual(tree.dataset["Spectrum"].size, 4 * 101)

    def test_full_100_by_20_run_writes_exactly_2000_complete_spectra(self) -> None:
        """Acceptance proof for the production plan's cardinality and storage contract."""

        from PyThat import MeasurementTree

        recipe_source = """\
schema_version: 1
name: full-100-by-20-storage-acceptance
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
    - id: current-sweep
      type: sweep
      target: keithley.B.current
      start: "1 mA"
      stop: "10 mA"
      points: 100
      children:
        - id: keithley-config
          type: configure_keithley
          channel: B
          mode: current
          level: "${keithley.B.current}"
          compliance: "67 mV"
        - id: rigol-sweep
          type: sweep
          target: rigol.1.high_level
          start: "1 mV"
          stop: "3 mV"
          points: 20
          children:
            - id: rigol-config
              type: configure_rigol
              channel: 1
              waveform: SQU
              frequency: "1 kHz"
              high_level: "${rigol.1.high_level}"
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
        plan = RecipeCompiler(settings).compile(parse_recipe_text(recipe_source))
        self.assertEqual(plan.total_points, 2000)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
            keithley = KeithleyAdapter(
                settings, session_factory=SimulatedVisaFactory("keithley")
            )
            anritsu = AnritsuAdapter(
                settings, session_factory=SimulatedVisaFactory("anritsu")
            )
            for device in (rigol, keithley, anritsu):
                device.connect()
            target = root / "full-2000.h5"
            writer = Hdf5RunWriter(
                target,
                recipe_source=recipe_source,
                settings_source="simulation: true\n",
                plan_hash=plan.sha256,
                device_idn={},
                expected_points=plan.total_points,
            )
            result = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                writer=writer,
            ).run(plan)

            self.assertIsNone(result.error)
            self.assertEqual(result.stored_points, 2000)
            with h5py.File(target, "r") as file:
                self.assertEqual(file["run"].attrs["status"], "completed")
                self.assertFalse(bool(file.attrs["measurement running"]))
                self.assertEqual(len(file["points"]), 2000)
                self.assertEqual(len(file["spectra"]), 2000)
                self.assertTrue(all(len(file[f"spectra/{index}/power_dbm"]) == 101 for index in range(2000)))

            with redirect_stdout(StringIO()):
                tree = MeasurementTree(target, index=True, override=True)
            self.assertEqual(tree.dataset.sizes["Keithley B current"], 100)
            self.assertEqual(tree.dataset.sizes["Rigol CH1 high level"], 20)
            self.assertEqual(tree.dataset.sizes["Frequency"], 101)
            self.assertEqual(tree.dataset["Spectrum"].size, 2000 * 101)


if __name__ == "__main__":
    unittest.main()
