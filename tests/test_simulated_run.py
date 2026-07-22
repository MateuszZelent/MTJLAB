from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
import json

import h5py

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.anritsu_ms2830a.adapter import SpectrumConfig
from app.devices.keithley_2600 import KeithleyAdapter
from app.devices.keithley_2600 import KeithleySourceRequest
from app.devices.moke_box import MokeBoxAdapter, MokeBoxConfig
from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
from app.devices.registry import built_in_device_registry
from app.devices.rigol_dg1000z import RigolAdapter
from app.devices.rigol_dg1000z.adapter import RigolChannelConfig
from app.devices.simulators import (
    AnritsuSimulator,
    KeithleySimulator,
    RigolSimulator,
    SimulatedVisaFactory,
    simulated_station_settings,
)
from app.devices.simulation import SimulationContext
from app.devices.visa import FakeVisaSessionFactory
from app.engine import ExecutionMode, RecipeCompiler, RecipeRunner
from app.engine.compiler import ExecutionPlan, PlanAction
from app.recipes import load_recipe, parse_recipe_text
from app.storage import Hdf5RunWriter
from app.storage.hdf5_reader import Hdf5RunReader
from app.storage.pythat_reader import read_pythat_run_data
from app.storage.pythat_bridge import open_measurement_tree
from app.settings.models import StationSettings
from tests.helpers import loaded_settings


class SimulatedRunTests(unittest.TestCase):
    def test_dry_run_programs_rigol_and_keithley_sweeps_and_stores_spectra(self) -> None:
        """Dry run changes real device settings but never sends OUTPUT ON."""

        recipe = parse_recipe_text(
            """\
schema_version: 1
name: dry-run-device-programming
root:
  id: sequence
  type: sequence
  children:
    - id: analyzer
      type: configure_anritsu
      start_frequency: "1 MHz"
      stop_frequency: "2 MHz"
      reference_level: "0 dBm"
      points: 101
      trace: TRAC1
    - id: rigol
      type: configure_rigol
      channel: 1
      waveform: SIN
      frequency: "1 kHz"
      high_level: "1 mV"
      low_level: "-1 mV"
      output_load: HIGHZ
      dut_min_impedance: "50 ohm"
    - id: keithley
      type: configure_keithley
      channel: A
      mode: current
      level: "200 uA"
      compliance: "100 mV"
    - id: rigol-on-in-tree
      type: set_rigol_output
      channel: 1
      enabled: true
    - id: keithley-on-in-tree
      type: set_keithley_output
      channel: A
      enabled: true
    - id: rigol-frequency-sweep
      type: sweep
      target: rigol.1.frequency
      start: "1 kHz"
      stop: "2 kHz"
      points: 2
      spacing: linear
      children:
        - id: update-rigol-frequency
          type: update_rigol_frequency
          channel: 1
          frequency: "${rigol.1.frequency}"
        - id: store-spectrum
          type: acquire_spectrum
          trace: TRAC1
"""
        )
        raw = deepcopy(
            simulated_station_settings(loaded_settings()).model_dump(mode="python")
        )
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        settings = StationSettings.model_validate(raw)
        plan = RecipeCompiler(settings, outputs_forced_off=True).compile(recipe)
        rigol_session = RigolSimulator()
        keithley_session = KeithleySimulator()
        anritsu_session = AnritsuSimulator()
        rigol = RigolAdapter(
            settings, session_factory=FakeVisaSessionFactory(rigol_session)
        )
        keithley = KeithleyAdapter(
            settings, session_factory=FakeVisaSessionFactory(keithley_session)
        )
        anritsu = AnritsuAdapter(
            settings, session_factory=FakeVisaSessionFactory(anritsu_session)
        )
        for device in (rigol, keithley, anritsu):
            device.connect()
        events: list[tuple[str, dict[str, object]]] = []

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "dry-run-programming.h5"
            writer = Hdf5RunWriter(
                target,
                recipe_source=recipe.source_text,
                settings_source="simulation: true\n",
                plan_hash=plan.sha256,
                device_idn={},
                expected_points=plan.total_points,
                simulation_metadata={
                    "enabled": True,
                    "execution_mode": ExecutionMode.DRY_RUN.value,
                    "outputs_forced_off": True,
                },
            )
            result = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                writer=writer,
                on_event=lambda name, data: events.append((name, data)),
                execution_mode=ExecutionMode.DRY_RUN,
            ).run(plan)

            self.assertEqual(result.state.value, "safe")
            self.assertEqual(result.stored_points, 2)
            self.assertEqual(rigol_session.frequency[1], 2_000.0)
            self.assertEqual(keithley_session.level["smua"], 200e-6)
            self.assertTrue(any(command.startswith(":SOUR1:FREQ") for command in rigol_session.commands))
            self.assertTrue(any("smua.source.leveli" in command for command in keithley_session.commands))
            self.assertFalse(any(command.upper() == ":OUTP1 ON" for command in rigol_session.commands))
            self.assertFalse(any("OUTPUT_ON" in command for command in keithley_session.commands))
            self.assertFalse(rigol_session.output[1])
            self.assertFalse(keithley_session.output["smua"])
            self.assertEqual(len(Hdf5RunReader.points(target)), 2)
            with h5py.File(target, "r") as file:
                self.assertEqual(len(file["spectra"]), 2)
            self.assertEqual(
                sum(name == "dry_run_output_action_suppressed" for name, _ in events),
                2,
            )

    def test_dry_run_keeps_outputs_off_and_stores_anritsu_raw_processed(self) -> None:
        recipe_path = (
            Path(__file__).resolve().parents[1]
            / "recipes"
            / "keithley_a_100ua_anritsu_raw_processed_10.yml"
        )
        recipe = load_recipe(recipe_path)
        raw = deepcopy(
            simulated_station_settings(loaded_settings()).model_dump(mode="python")
        )
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        settings = StationSettings.model_validate(raw)
        compiled = RecipeCompiler(settings).compile(recipe)
        plan = replace(
            compiled,
            actions=tuple(
                replace(action, payload={**action.payload, "duration_s": 0.0})
                if action.kind == "wait"
                else action
                for action in compiled.actions
            ),
        )
        context = SimulationContext(seed=2_026_0721)
        rigol = RigolAdapter(
            settings,
            session_factory=SimulatedVisaFactory("rigol", context=context),
        )
        keithley = KeithleyAdapter(
            settings,
            session_factory=SimulatedVisaFactory("keithley", context=context),
        )
        anritsu = AnritsuAdapter(
            settings,
            session_factory=SimulatedVisaFactory("anritsu", context=context),
        )
        for device in (rigol, keithley, anritsu):
            device.connect()
        events: list[tuple[str, dict[str, object]]] = []

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "dry-run-keithley-a-100ua.h5"
            writer = Hdf5RunWriter(
                target,
                recipe_source=recipe.source_text,
                settings_source="simulation: true\n",
                plan_hash=plan.sha256,
                device_idn={},
                expected_points=plan.total_points,
                simulation_metadata={
                    **context.metadata(("rigol", "keithley", "anritsu")),
                    "execution_mode": ExecutionMode.DRY_RUN.value,
                    "outputs_forced_off": True,
                },
            )
            result = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                writer=writer,
                on_event=lambda name, data: events.append((name, data)),
                execution_mode=ExecutionMode.DRY_RUN,
            ).run(plan)

            self.assertIsNone(result.error)
            self.assertEqual(result.stored_points, 10)
            self.assertFalse(any(rigol._output_states.values()))
            self.assertFalse(any(keithley._output_states.values()))
            self.assertFalse(anritsu._sg_output_enabled)
            self.assertEqual(
                sum(
                    name == "action_started"
                    and data.get("node_id") == "keithley-a-current-point"
                    for name, data in events
                ),
                10,
            )
            self.assertTrue(
                any(name == "dry_run_output_action_suppressed" for name, _ in events)
            )
            detail = Hdf5RunReader.detail(target)
            self.assertTrue(detail.simulation_metadata["outputs_forced_off"])
            self.assertEqual(
                detail.simulation_metadata["execution_mode"],
                ExecutionMode.DRY_RUN.value,
            )
            points = Hdf5RunReader.points(target)
            self.assertEqual(len(points), 10)
            self.assertTrue(all(point.metadata["outputs_forced_off"] for point in points))
            with h5py.File(target, "r") as file:
                self.assertEqual(len(file["spectra"]), 10)
                self.assertIn("reference/power_dbm", file)
                for index in range(10):
                    spectrum = file[f"spectra/{index}"]
                    self.assertEqual(len(spectrum["power_dbm"]), 5001)
                    self.assertEqual(len(spectrum["processed_values"]), 5001)
                    self.assertEqual(spectrum.attrs["processed_unit"], "dB")
                    self.assertEqual(
                        spectrum.attrs["processing_operation"], "difference_db"
                    )

    def test_keithley_a_100ua_recipe_runs_ten_raw_and_processed_points(self) -> None:
        recipe_path = (
            Path(__file__).resolve().parents[1]
            / "recipes"
            / "keithley_a_100ua_anritsu_raw_processed_10.yml"
        )
        recipe = load_recipe(recipe_path)
        raw = deepcopy(
            simulated_station_settings(loaded_settings()).model_dump(mode="python")
        )
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        settings = StationSettings.model_validate(raw)
        compiled = RecipeCompiler(settings).compile(recipe)
        plan = replace(
            compiled,
            actions=tuple(
                replace(action, payload={**action.payload, "duration_s": 0.0})
                if action.kind == "wait"
                else action
                for action in compiled.actions
            ),
        )
        self.assertEqual((plan.total_points, plan.total_spectra), (10, 10))

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "keithley-a-100ua.h5"
            rigol = RigolAdapter(
                settings, session_factory=SimulatedVisaFactory("rigol")
            )
            keithley = KeithleyAdapter(
                settings, session_factory=SimulatedVisaFactory("keithley")
            )
            anritsu = AnritsuAdapter(
                settings, session_factory=SimulatedVisaFactory("anritsu")
            )
            for device in (rigol, keithley, anritsu):
                device.connect()
            writer = Hdf5RunWriter(
                target,
                recipe_source=recipe.source_text,
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
            self.assertEqual(result.stored_points, 10)
            self.assertFalse(keithley._output_states["A"])
            self.assertEqual(Hdf5RunReader.detail(target).summary.status, "completed")
            with h5py.File(target, "r") as file:
                self.assertEqual(len(file["spectra"]), 10)
                self.assertIn("reference/power_dbm", file)
                for index in range(10):
                    spectrum = file[f"spectra/{index}"]
                    self.assertEqual(len(spectrum["power_dbm"]), 5001)
                    self.assertEqual(len(spectrum["processed_values"]), 5001)
                    self.assertEqual(spectrum.attrs["processed_unit"], "dB")
                    self.assertEqual(
                        spectrum.attrs["processing_operation"], "difference_db"
                    )

    def test_rigol_frequency_sweep_stores_one_reference_per_processed_point(self) -> None:
        recipe_path = (
            Path(__file__).resolve().parents[1]
            / "recipes"
            / "rigol_frequency_anritsu_reference_10.yml"
        )
        raw = deepcopy(
            simulated_station_settings(loaded_settings()).model_dump(mode="python")
        )
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        recipe = load_recipe(recipe_path)
        plan = RecipeCompiler(settings).compile(recipe)
        self.assertEqual((plan.total_points, plan.total_spectra), (10, 10))
        self.assertEqual(
            sum(action.kind == "acquire_reference" for action in plan.actions), 10
        )

        context = SimulationContext(seed=1032)
        rigol = RigolAdapter(
            settings, session_factory=SimulatedVisaFactory("rigol", context=context)
        )
        keithley = KeithleyAdapter(
            settings, session_factory=SimulatedVisaFactory("keithley", context=context)
        )
        anritsu = AnritsuAdapter(
            settings, session_factory=SimulatedVisaFactory("anritsu", context=context)
        )
        for device in (rigol, keithley, anritsu):
            device.connect()

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "per-point-reference.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source=recipe.source_text,
                settings_source="simulation: true\n",
                plan_hash=plan.sha256,
                device_idn={},
                expected_points=10,
                simulation_metadata=context.metadata(("rigol", "anritsu")),
            )
            result = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                writer=writer,
            ).run(plan)

            self.assertIsNone(result.error)
            self.assertEqual(result.stored_points, 10)
            self.assertFalse(rigol._output_states[1])
            self.assertFalse(keithley._output_states["A"])
            self.assertFalse(keithley._output_states["B"])
            references = Hdf5RunReader.references(path)
            self.assertEqual(tuple(reference.index for reference in references), tuple(range(10)))
            points = Hdf5RunReader.points(path)
            for index, point in enumerate(points):
                self.assertAlmostEqual(
                    point.setpoints["rigol.1.frequency"],
                    100e3 + index * (30e6 - 100e3) / 9,
                    places=6,
                )
            for index in range(10):
                spectrum = Hdf5RunReader.spectrum(path, index)
                self.assertIsNotNone(spectrum)
                assert spectrum is not None
                self.assertEqual(spectrum.reference_index, index)
                self.assertEqual(len(spectrum.powers_dbm), 1001)
                self.assertEqual(len(spectrum.processed_values or ()), 1001)
                self.assertEqual(spectrum.processing_operation, "difference_db")

    def test_lakeshore_recipe_compiles_runs_and_round_trips(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["lakeshore_gaussmeter"].update(
            {"enabled": True, "resource": "SIM::LAKESHORE::INSTR"}
        )
        settings = StationSettings.model_validate(raw)
        recipe = parse_recipe_text(
            """\
schema_version: 1
name: lakeshore-round-trip
root:
  id: field
  type: measure_lakeshore_field
"""
        )
        plan = RecipeCompiler(settings).compile(recipe)
        context = SimulationContext(seed=475)
        rigol = RigolAdapter(
            settings, session_factory=SimulatedVisaFactory("rigol", context=context)
        )
        keithley = KeithleyAdapter(
            settings, session_factory=SimulatedVisaFactory("keithley", context=context)
        )
        anritsu = AnritsuAdapter(
            settings, session_factory=SimulatedVisaFactory("anritsu", context=context)
        )
        lakeshore = built_in_device_registry().get("lakeshore_gaussmeter").create_adapter(
            settings, simulation=True
        )
        for device in (rigol, keithley, anritsu, lakeshore):
            device.connect()

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lakeshore.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source=recipe.source_text,
                settings_source="simulation: true\n",
                plan_hash=plan.sha256,
                device_idn={"lakeshore_gaussmeter": "SIM475"},
                expected_points=1,
            )
            result = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                lakeshore=lakeshore,  # type: ignore[arg-type]
                writer=writer,
            ).run(plan)

            self.assertIsNone(result.error)
            self.assertEqual(result.stored_points, 1)
            point = Hdf5RunReader.points(path)[0]
            self.assertIn("lakeshore.field_t", point.measurements)
            self.assertEqual(read_pythat_run_data(path).dimensions["Checkpoint"], 1)

    def test_four_device_simulation_saves_full_state_for_every_spectrum(self) -> None:
        settings = simulated_station_settings(loaded_settings())
        context = SimulationContext(seed=2_026_0718)
        rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol", context=context))
        keithley = KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley", context=context))
        anritsu = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu", context=context))
        moke = MokeBoxAdapter(
            MokeBoxConfig(endpoint="SIM::MOKE::INSTR", expected_model="MOKE SIM"),
            SimulatedMokeBoxTransport(context),
        )
        for device in (rigol, keithley, anritsu, moke):
            device.connect()
        plan = ExecutionPlan(
            recipe_name="four-device-simulation",
            actions=(
                PlanAction("rigol", "configure_rigol", {"config": RigolChannelConfig(1, "SIN", 1_000.0, 0.001, -0.001)}, {}),
                PlanAction("keithley", "configure_keithley", {"request": KeithleySourceRequest("B", "current", 0.001, 0.067)}, {}),
                PlanAction("anritsu", "configure_anritsu", {"config": SpectrumConfig(1e6, 2e6, 0.0, 101)}, {}),
                PlanAction("moke", "measure_moke_hall", {"checkpoint": False}, {}),
                PlanAction("spectrum", "acquire_spectrum", {"trace": "TRAC1", "average_count": 1}, {}),
            ),
            total_points=1,
            sha256="four-device-simulation",
            recipe_source="schema_version: 1\nname: four-device-simulation\n",
            required_devices=frozenset({"rigol", "keithley", "anritsu", "moke_box"}),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "four-device.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source=plan.recipe_source,
                settings_source="simulation: true\n",
                plan_hash=plan.sha256,
                device_idn={"rigol": "SIM", "keithley": "SIM", "anritsu": "SIM", "moke_box": "MOKE SIM"},
                simulation_metadata=context.metadata(("rigol", "keithley", "anritsu", "moke_box")),
            )
            result = RecipeRunner(rigol=rigol, keithley=keithley, anritsu=anritsu, moke_box=moke, writer=writer).run(plan)
            self.assertIsNone(result.error)
            spectra = [point for point in Hdf5RunReader.points(path) if point.has_spectrum]
            self.assertEqual(len(spectra), 1)
            self.assertEqual(set(spectra[0].device_states), {"rigol", "keithley", "anritsu", "moke_box"})
            self.assertEqual(Hdf5RunReader.detail(path).simulation_metadata["seed"], 2_026_0718)

    def test_requested_10_by_100_recipe_runs_all_1000_processed_spectra(self) -> None:
        recipe_path = (
            Path(__file__).resolve().parents[1]
            / "recipes"
            / "keithley_b_rigol_frequency_anritsu_reference_10x100.yml"
        )
        recipe = load_recipe(recipe_path)
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        raw["devices"]["rigol"]["safety"]["channels"]["1"]["lab_limits"]["frequency"]["max"] = "30 MHz"
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        limits = raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]
        limits["source_current"] = {
            "min": "0 A",
            "max": "150 mA",
            "max_abs": "150 mA",
        }
        limits["measured_current_trip"] = {"min": "-1 mA", "max": "151 mA"}
        limits["max_abs_power"] = "10.05 mW"
        settings = StationSettings.model_validate(raw)
        plan = RecipeCompiler(settings).compile(recipe)
        self.assertEqual(plan.total_points, 1000)
        self.assertEqual(plan.total_spectra, 1000)

        keithley_on = next(
            index
            for index, action in enumerate(plan.actions)
            if action.node_id == "keithley-b-output-on"
        )
        rigol_on = next(
            index
            for index, action in enumerate(plan.actions)
            if action.node_id == "rigol-ch1-output-on"
        )
        first_finally = next(
            index for index, action in enumerate(plan.actions) if action.is_finally
        )
        energized_actions = plan.actions[max(keithley_on, rigol_on) + 1 : first_finally]
        self.assertNotIn(
            "configure_keithley", {action.kind for action in energized_actions}
        )
        self.assertNotIn("configure_rigol", {action.kind for action in energized_actions})

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
            target = root / "requested-10x100.h5"
            writer = Hdf5RunWriter(
                target,
                recipe_source=recipe.source_text,
                settings_source="simulation: requested-range-qualification\n",
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
            self.assertEqual(result.stored_points, 1000)
            self.assertFalse(rigol._output_states[1])
            self.assertFalse(keithley._output_states["B"])
            with h5py.File(target, "r") as file:
                self.assertEqual(file["run"].attrs["status"], "completed")
                self.assertEqual(len(file["spectra"]), 1000)
                self.assertEqual(len(file["reference/power_dbm"]), 1001)
                first = file["spectra/0"]
                last = file["spectra/999"]
                for spectrum in (first, last):
                    self.assertEqual(len(spectrum["power_dbm"]), 1001)
                    self.assertEqual(len(spectrum["processed_values"]), 1001)
                    self.assertEqual(spectrum.attrs["processed_unit"], "dB")
                    self.assertEqual(
                        spectrum.attrs["processing_operation"], "difference_db"
                    )

    def test_reference_processed_cartesian_sweep_keeps_outputs_on_and_stores_both_spectra(self) -> None:
        recipe_source = """\
schema_version: 1
name: reference-processed-cartesian
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
    - {id: rigol-off-initial, type: set_rigol_output, channel: 1, enabled: false}
    - {id: keithley-off-initial, type: set_keithley_output, channel: B, enabled: false}
    - id: anritsu-config
      type: configure_anritsu
      start_frequency: "1 MHz"
      stop_frequency: "2 MHz"
      reference_level: "0 dBm"
      points: 101
    - {id: reference, type: acquire_reference, trace: TRAC1}
    - id: keithley-config
      type: configure_keithley
      channel: B
      mode: current
      level: "0 A"
      compliance: "67 mV"
    - id: rigol-config
      type: configure_rigol
      channel: 1
      waveform: SIN
      frequency: "1 kHz"
      high_level: "1 mV"
      low_level: "-1 mV"
      output_load: HIGHZ
      dut_min_impedance: "50 ohm"
    - {id: keithley-on, type: set_keithley_output, channel: B, enabled: true}
    - {id: rigol-on, type: set_rigol_output, channel: 1, enabled: true}
    - id: current
      type: sweep
      target: keithley.B.current
      start: "0 A"
      stop: "2 mA"
      points: 2
      children:
        - id: current-point
          type: update_keithley_level
          channel: B
          mode: current
          level: "${keithley.B.current}"
        - id: frequency
          type: sweep
          target: rigol.1.frequency
          start: "1 kHz"
          stop: "3 kHz"
          points: 3
          children:
            - id: frequency-point
              type: update_rigol_frequency
              channel: 1
              frequency: "${rigol.1.frequency}"
            - id: spectrum
              type: acquire_spectrum
              trace: TRAC1
              reference_operation: difference_db
              store_raw: true
              store_processed: true
finally:
  - {id: rigol-off, type: set_rigol_output, channel: 1, enabled: false}
  - {id: keithley-zero, type: ramp_keithley_to_zero, channel: B, deadline: "10 s"}
  - {id: keithley-off, type: set_keithley_output, channel: B, enabled: false}
"""
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        plan = RecipeCompiler(settings).compile(parse_recipe_text(recipe_source))
        self.assertEqual(plan.total_points, 6)

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
            target = root / "reference-processed.h5"
            writer = Hdf5RunWriter(
                target,
                recipe_source=recipe_source,
                settings_source="simulation: true\n",
                plan_hash=plan.sha256,
                device_idn={},
                expected_points=plan.total_points,
            )
            events: list[tuple[str, dict[str, object]]] = []
            result = RecipeRunner(
                rigol=rigol,
                keithley=keithley,
                anritsu=anritsu,
                writer=writer,
                on_event=lambda name, data: events.append((name, data)),
            ).run(plan)

            self.assertIsNone(result.error)
            self.assertEqual(result.stored_points, 6)
            self.assertFalse(rigol._output_states[1])
            self.assertFalse(keithley._output_states["B"])
            self.assertEqual(sum(name == "reference_stored" for name, _ in events), 1)
            with h5py.File(target, "r") as file:
                self.assertIn("reference/power_dbm", file)
                self.assertEqual(len(file["spectra"]), 6)
                for index in range(6):
                    spectrum = file[f"spectra/{index}"]
                    self.assertEqual(len(spectrum["power_dbm"]), 101)
                    self.assertEqual(len(spectrum["processed_values"]), 101)
                    self.assertEqual(spectrum.attrs["processed_unit"], "dB")
                    self.assertEqual(
                        spectrum.attrs["processing_operation"], "difference_db"
                    )

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

    def test_energized_point_runs_with_limits_and_output_permissions(self) -> None:
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
    - id: on-keithley
      type: set_keithley_output
      channel: B
      enabled: true
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

            tree = open_measurement_tree(target)
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

            tree = open_measurement_tree(target)
            self.assertEqual(tree.dataset.sizes["Keithley B current"], 100)
            self.assertEqual(tree.dataset.sizes["Rigol CH1 high level"], 20)
            self.assertEqual(tree.dataset.sizes["Frequency"], 101)
            self.assertEqual(tree.dataset["Spectrum"].size, 2000 * 101)


if __name__ == "__main__":
    unittest.main()

