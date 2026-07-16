from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from app.devices.anritsu import AnritsuAdapter, SpectrumConfig
from app.devices.keithley import KeithleyAdapter, KeithleySourceRequest
from app.devices.rigol import RigolAdapter, RigolChannelConfig, RigolOutputConfig
from app.devices.simulators import SimulatorFault, SimulatedVisaFactory, simulated_station_settings
from app.domain.errors import DeviceError, SafetyViolation
from app.domain.models import DeviceState
from app.engine.compiler import ExecutionPlan, PlanAction
from app.engine.runner import RecipeRunner
from app.settings.models import StationSettings
from app.storage import Hdf5RunReader, Hdf5RunWriter
from tests.helpers import loaded_settings


class SimulatorTests(unittest.TestCase):
    def test_all_simulated_instruments_support_safe_core_operations(self) -> None:
        settings = simulated_station_settings(loaded_settings())
        self.assertFalse(settings.outputs_locked)
        rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
        keithley = KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley"))
        anritsu = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
        self.assertIn("DG1032Z", rigol.connect().idn)
        self.assertIn("2602A", keithley.connect().idn)
        self.assertIn("MS2830A", anritsu.connect().idn)
        rigol.configure_channel(
            RigolChannelConfig(1, "SQU", 1000, 0.001, -0.001, dut_min_impedance_ohm=50)
        )
        rigol.configure_output(
            RigolOutputConfig(1, output_load="HIGHZ", polarity="INV", sync_enabled=True, sync_delay_s=0.001)
        )
        keithley.configure_source(KeithleySourceRequest("B", "current", 0.001, 0.067))
        anritsu.configure_spectrum(SpectrumConfig(1e6, 2e6, 0, 101))
        trace = anritsu.fetch_trace()
        self.assertEqual(len(trace.powers_dbm), 101)
        self.assertEqual(trace.frequencies_hz[-1], 2e6)

    def test_anritsu_trace_timeout_is_reported_without_hardware(self) -> None:
        settings = simulated_station_settings(loaded_settings())
        anritsu = AnritsuAdapter(
            settings,
            session_factory=SimulatedVisaFactory(
                "anritsu", fault=SimulatorFault(timeout_prefixes=frozenset({"TRAC?"}))
            ),
        )
        anritsu.connect()
        anritsu.configure_spectrum(SpectrumConfig(1e6, 2e6, 0, 101))

        with self.assertRaisesRegex(DeviceError, "timeout"):
            anritsu.fetch_trace()

        anritsu.emergency_off()
        anritsu.disconnect()

    def test_rigol_dc_uses_offset_without_minimum_ac_amplitude(self) -> None:
        settings = simulated_station_settings(loaded_settings())
        rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
        rigol.connect()
        estimate = rigol.configure_channel(
            RigolChannelConfig(1, "DC", 1.0, 0.001, 0.001, dut_min_impedance_ohm=50)
        )
        self.assertGreater(estimate.peak_absolute_current_a, 0)
        rigol.disconnect()

    def test_anritsu_malformed_trace_response_is_a_device_error(self) -> None:
        settings = simulated_station_settings(loaded_settings())
        anritsu = AnritsuAdapter(
            settings,
            session_factory=SimulatedVisaFactory(
                "anritsu",
                fault=SimulatorFault(malformed_response_prefixes=frozenset({"TRAC?"})),
            ),
        )
        anritsu.connect()
        anritsu.configure_spectrum(SpectrumConfig(1e6, 2e6, 0, 101))

        with self.assertRaisesRegex(DeviceError, "nieprawidłową odpowiedź"):
            anritsu.fetch_trace()

    def test_keithley_compliance_turns_output_off_in_simulation(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        keithley = KeithleyAdapter(
            settings,
            session_factory=SimulatedVisaFactory("keithley", keithley_resistance_ohm=67.0),
        )
        keithley.connect()
        keithley.configure_source(KeithleySourceRequest("B", "current", 0.001, 0.067))
        keithley.arm_output("B")
        keithley.set_output("B", True)

        measurement = keithley.measure("B")

        self.assertTrue(measurement.compliance_detected)
        self.assertTrue(measurement.compliance_stop_required)
        self.assertEqual(keithley.state, DeviceState.COMPLIANCE)

    def test_keithley_simulator_clips_source_at_programmed_compliance(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        keithley = KeithleyAdapter(
            settings,
            session_factory=SimulatedVisaFactory("keithley", keithley_resistance_ohm=1_000.0),
        )
        keithley.connect()
        keithley.configure_source(KeithleySourceRequest("B", "current", 0.001, 0.067))
        keithley.arm_output("B")
        keithley.set_output("B", True)

        measurement = keithley.measure("B")

        self.assertAlmostEqual(measurement.voltage_v, 0.067)
        self.assertAlmostEqual(measurement.current_a, 0.000067)
        self.assertTrue(measurement.compliance_detected)

    def test_keithley_simulator_exposes_error_queue_to_adapter(self) -> None:
        settings = simulated_station_settings(loaded_settings())
        keithley = KeithleyAdapter(
            settings,
            session_factory=SimulatedVisaFactory(
                "keithley",
                keithley_command_errors={
                    "smub.source.leveli": "901,simulated source error",
                },
            ),
        )
        keithley.connect()

        with self.assertRaisesRegex(DeviceError, "901,simulated source error"):
            keithley.configure_source(KeithleySourceRequest("B", "current", 0.001, 0.067))

    def test_disabled_device_cannot_open_a_session(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["rigol"]["enabled"] = False
        raw["devices"]["keithley"]["enabled"] = False
        raw["devices"]["anritsu"]["enabled"] = False
        settings = StationSettings.model_validate(raw)

        for adapter in (
            RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol")),
            KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley")),
            AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu")),
        ):
            with self.assertRaisesRegex(SafetyViolation, "wyłączony w profilu"):
                adapter.connect()

    def test_rigol_rejects_non_finite_manual_configuration(self) -> None:
        settings = simulated_station_settings(loaded_settings())
        rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
        rigol.connect()

        with self.assertRaisesRegex(SafetyViolation, "phase"):
            rigol.configure_channel(
                RigolChannelConfig(
                    1,
                    "SQU",
                    1_000,
                    0.001,
                    -0.001,
                    phase_deg=float("nan"),
                    dut_min_impedance_ohm=50,
                )
            )

    def test_trace_fault_safely_turns_off_previously_enabled_outputs(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
        keithley = KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley"))
        anritsu = AnritsuAdapter(
            settings,
            session_factory=SimulatedVisaFactory(
                "anritsu", fault=SimulatorFault(device_error_prefixes=frozenset({"TRAC?"}))
            ),
        )
        for device in (rigol, keithley, anritsu):
            device.connect()
        plan = ExecutionPlan(
            recipe_name="fault-safe-shutdown",
            actions=(
                PlanAction("anritsu", "configure_anritsu", {"config": SpectrumConfig(1e6, 2e6, 0, 101)}, {}),
                PlanAction(
                    "keithley-config",
                    "configure_keithley",
                    {"request": KeithleySourceRequest("B", "current", 0.001, 0.067)},
                    {},
                ),
                PlanAction(
                    "rigol-config",
                    "configure_rigol",
                    {"config": RigolChannelConfig(1, "SQU", 1000, 0.001, -0.001, dut_min_impedance_ohm=50)},
                    {},
                ),
                PlanAction("keithley-arm", "arm_keithley_output", {"channel": "B"}, {}),
                PlanAction("keithley-on", "set_keithley_output", {"channel": "B", "enabled": True}, {}),
                PlanAction("rigol-arm", "arm_rigol_output", {"channel": 1}, {}),
                PlanAction("rigol-on", "set_rigol_output", {"channel": 1, "enabled": True}, {}),
                PlanAction("spectrum", "acquire_spectrum", {"trace": "TRAC1"}, {}),
            ),
            total_points=1,
            sha256="fault-safe-shutdown",
            recipe_source="schema_version: 1\n",
        )
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "result.h5"
            writer = Hdf5RunWriter(
                target,
                recipe_source="schema_version: 1\n",
                settings_source="simulation: true\n",
                plan_hash=plan.sha256,
                device_idn={},
            )
            result = RecipeRunner(rigol=rigol, keithley=keithley, anritsu=anritsu, writer=writer).run(plan)

            self.assertIsNotNone(result.error)
            self.assertEqual(rigol.state, DeviceState.OUTPUT_OFF)
            self.assertEqual(keithley.state, DeviceState.OUTPUT_OFF)
            self.assertEqual(Hdf5RunReader.detail(target).summary.status, "faulted")


if __name__ == "__main__":
    unittest.main()
