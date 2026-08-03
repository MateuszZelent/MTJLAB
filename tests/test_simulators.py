from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.devices.anritsu_ms2830a import AnritsuAdapter, SpectrumConfig
from app.devices.keithley_2600 import KeithleyAdapter, KeithleySourceRequest
from app.devices.keithley_2600 import module as keithley_module
from app.devices.rigol_dg1000z import RigolAdapter, RigolChannelConfig, RigolOutputConfig
from app.devices.simulators import SimulatorFault, SimulatedVisaFactory, simulated_station_settings
from app.domain.errors import ConnectionError, DeviceError, SafetyViolation
from app.domain.models import DeviceState
from app.engine.compiler import ExecutionPlan, PlanAction
from app.engine.runner import RecipeRunner
from app.settings.models import StationSettings
from app.storage import Hdf5RunReader, Hdf5RunWriter
from tests.helpers import loaded_settings


class SimulatorTests(unittest.TestCase):
    def test_every_simulator_exposes_deterministic_transport_faults(self) -> None:
        for device in ("rigol", "keithley", "anritsu"):
            with self.subTest(device=device, fault="normal"):
                session = SimulatedVisaFactory(device).open("SIM", "sim", 1234)
                self.assertIn("SIM000001", session.query("*IDN?"))
                self.assertEqual(session.timeout, 1234)
            with self.subTest(device=device, fault="timeout"):
                session = SimulatedVisaFactory(
                    device,
                    fault=SimulatorFault(timeout_prefixes=frozenset({"*IDN?"})),
                ).open("SIM", "sim", 1234)
                with self.assertRaisesRegex(DeviceError, "timeout"):
                    session.query("*IDN?")
            with self.subTest(device=device, fault="malformed"):
                session = SimulatedVisaFactory(
                    device,
                    fault=SimulatorFault(malformed_response_prefixes=frozenset({"*IDN?"})),
                ).open("SIM", "sim", 1234)
                self.assertEqual(session.query("*IDN?"), "MALFORMED_RESPONSE")
            with self.subTest(device=device, fault="device_error"):
                session = SimulatedVisaFactory(
                    device,
                    fault=SimulatorFault(device_error_prefixes=frozenset({"*IDN?"})),
                ).open("SIM", "sim", 1234)
                with self.assertRaisesRegex(DeviceError, "device error"):
                    session.query("*IDN?")
            with self.subTest(device=device, fault="disconnect"):
                session = SimulatedVisaFactory(
                    device,
                    fault=SimulatorFault(disconnect_prefixes=frozenset({"*IDN?"})),
                ).open("SIM", "sim", 1234)
                with self.assertRaisesRegex(ConnectionError, "disconnected"):
                    session.query("*IDN?")

    def test_every_simulator_has_a_deterministic_error_queue(self) -> None:
        commands = {
            "rigol": ":SOUR1:FREQ 1000",
            "keithley": "smub.source.leveli = 0.001",
            "anritsu": "FREQ:STAR 1000000HZ",
        }
        queue_queries = {
            "rigol": ":SYST:ERR?",
            "keithley": "print(errorqueue.next())",
            "anritsu": "SYST:ERR?",
        }
        for device, command in commands.items():
            with self.subTest(device=device):
                session = SimulatedVisaFactory(
                    device,
                    command_errors={command: "901,simulated command error"},
                ).open("SIM", "sim", 1000)
                session.write(command)
                self.assertEqual(session.query(queue_queries[device]), "901,simulated command error")

    def test_keithley_noise_model_is_repeatable_and_non_constant(self) -> None:
        def readings() -> list[str]:
            session = SimulatedVisaFactory(
                "keithley",
                keithley_resistance_ohm=100.0,
                keithley_noise_fraction=0.01,
            ).open("SIM", "sim", 1000)
            session.write("smub.source.func = smub.OUTPUT_DCAMPS")
            session.write("smub.source.leveli = 0.001")
            session.write("smub.source.output = smub.OUTPUT_ON")
            return [session.query("print(smub.measure.iv())") for _ in range(3)]

        first = readings()
        self.assertEqual(first, readings())
        self.assertEqual(len(set(first)), 3)

    def test_keithley_noise_model_rejects_invalid_scale(self) -> None:
        for invalid in (-0.1, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                SimulatedVisaFactory("keithley", keithley_noise_fraction=invalid)

    def test_keithley_simulator_accepts_signed_scientific_setpoints(self) -> None:
        raw = deepcopy(
            simulated_station_settings(loaded_settings()).model_dump(mode="python")
        )
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        settings = StationSettings.model_validate(raw)
        keithley = KeithleyAdapter(
            settings, session_factory=SimulatedVisaFactory("keithley")
        )
        keithley.connect()
        keithley.configure_source(
            KeithleySourceRequest("A", "current", 100e-6, 0.1)
        )

        actual = keithley.update_source_level(
            "A", mode="current", level_si=88.8888888889e-6
        )

        self.assertAlmostEqual(actual, 88.8888888889e-6)

    def test_all_simulated_instruments_support_safe_core_operations(self) -> None:
        settings = simulated_station_settings(loaded_settings())
        self.assertFalse(settings.rigol.safety.allow_output_enable)
        self.assertFalse(settings.keithley.safety.allow_output_enable)
        rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
        keithley = KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley"))
        anritsu = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
        self.assertIn("DG1032Z", rigol.connect().idn)
        self.assertIn("2602A", keithley.connect().idn)
        self.assertIn("MS2830A", anritsu.connect().idn)
        rigol.configure_channel(
            RigolChannelConfig(1, "SQU", 1000, 0.001, -0.001)
        )
        rigol.configure_output(
            RigolOutputConfig(1, output_load="HIGHZ", polarity="INV", sync_enabled=True, sync_delay_s=0.001)
        )
        keithley.configure_source(KeithleySourceRequest("B", "current", 0.001, 0.067))
        anritsu.configure_spectrum(SpectrumConfig(1e6, 2e6, 0, 101))
        trace = anritsu.fetch_trace()
        self.assertEqual(len(trace.powers_dbm), 101)
        self.assertEqual(trace.frequencies_hz[-1], 2e6)

    def test_keithley_simulator_supports_read_only_configuration_snapshot(self) -> None:
        settings = simulated_station_settings(loaded_settings())
        keithley = KeithleyAdapter(
            settings, session_factory=SimulatedVisaFactory("keithley")
        )
        keithley.connect()

        readback = keithley.read_configuration()

        self.assertEqual(
            tuple(channel.channel for channel in readback.channels), ("A", "B")
        )
        self.assertTrue(all(not channel.output_enabled for channel in readback.channels))
        self.assertTrue(
            all(channel.output_off_mode == "high_impedance" for channel in readback.channels)
        )
        self.assertTrue(all(channel.source_mode == "current" for channel in readback.channels))

    def test_keithley_simulator_isolates_one_dut_and_measurement_restores_normal(
        self,
    ) -> None:
        settings = simulated_station_settings(loaded_settings())
        keithley = KeithleyAdapter(
            settings, session_factory=SimulatedVisaFactory("keithley")
        )
        keithley.connect()
        keithley.set_dut_output_off_mode("A", "normal")
        keithley.set_dut_output_off_mode("B", "high_impedance")

        isolated = keithley.read_configuration()

        self.assertEqual(isolated.channels[0].output_off_mode, "normal")
        self.assertEqual(isolated.channels[1].output_off_mode, "high_impedance")
        measurement = keithley.measure("B")
        restored = keithley.read_configuration()
        self.assertTrue(measurement.measurement_path_connected)
        self.assertEqual(restored.channels[0].output_off_mode, "normal")
        self.assertEqual(restored.channels[1].output_off_mode, "normal")

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

    def test_anritsu_simulated_live_produces_new_frames(self) -> None:
        settings = simulated_station_settings(loaded_settings())
        anritsu = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
        anritsu.connect()
        anritsu.configure_spectrum(SpectrumConfig(1e6, 2e6, 0, 101))
        anritsu.start_live(ensure_continuous=True)

        first = anritsu.fetch_current_trace()
        second = anritsu.fetch_current_trace()

        self.assertNotEqual(first.powers_dbm, second.powers_dbm)

    def test_rigol_dc_uses_offset_without_minimum_ac_amplitude(self) -> None:
        settings = simulated_station_settings(loaded_settings())
        rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
        rigol.connect()
        estimate = rigol.configure_channel(
            RigolChannelConfig(1, "DC", 1.0, 0.001, 0.001)
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

        with self.assertRaisesRegex(DeviceError, "invalid trace response"):
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
        keithley.set_output("B", True)

        measurement = keithley.measure("B")

        self.assertTrue(measurement.compliance_detected)
        self.assertTrue(measurement.compliance_stop_required)
        self.assertEqual(keithley.state, DeviceState.COMPLIANCE)

    def test_keithley_per_channel_compliance_preserves_other_output(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        raw["devices"]["keithley"]["safety"]["channels"]["B"]["enabled"] = True
        settings = StationSettings.model_validate(raw)
        keithley = KeithleyAdapter(
            settings,
            session_factory=SimulatedVisaFactory(
                "keithley", keithley_resistance_ohm=67.0
            ),
        )
        keithley.connect()
        keithley.configure_source(
            KeithleySourceRequest("A", "current", 0.0005, 0.067)
        )
        keithley.set_output("A", True)
        safe_a = keithley.measure("A")
        self.assertFalse(safe_a.compliance_detected)
        keithley.configure_source(
            KeithleySourceRequest("A", "current", 0.001, 0.067)
        )
        keithley.configure_source(
            KeithleySourceRequest("B", "voltage", 0.05, 0.003)
        )
        keithley.set_output("A", True)
        keithley.set_output("B", True)

        measurement = keithley.measure("A")

        self.assertTrue(measurement.compliance_stop_required)
        self.assertFalse(keithley._output_states["A"])
        self.assertTrue(keithley._output_states["B"])
        with self.assertRaisesRegex(SafetyViolation, "compliance"):
            keithley.set_output("A", True)
        self.assertTrue(keithley._output_states["B"])

        restored = keithley.recover_from_compliance("A", "restore_previous")

        self.assertEqual(restored["channel"], "A")
        self.assertEqual(restored["choice"], "restore_previous")
        self.assertTrue(restored["outputs_confirmed_off"])
        self.assertFalse(keithley._output_states["A"])
        self.assertTrue(keithley._output_states["B"])
        self.assertAlmostEqual(keithley.last_source_request("A").level_si, 0.0005)

    def test_keithley_per_channel_recovery_keep_off_never_enables_output(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        raw["devices"]["keithley"]["safety"]["channels"]["B"]["enabled"] = True
        settings = StationSettings.model_validate(raw)
        keithley = KeithleyAdapter(
            settings,
            session_factory=SimulatedVisaFactory(
                "keithley", keithley_resistance_ohm=67.0
            ),
        )
        keithley.connect()
        keithley.configure_source(KeithleySourceRequest("A", "current", 0.001, 0.067))
        keithley.set_output("A", True)
        keithley.measure("A")

        recovered = keithley.recover_from_compliance("A", "keep_off")

        self.assertEqual(recovered["choice"], "keep_off")
        self.assertTrue(recovered["outputs_confirmed_off"])
        self.assertFalse(keithley._output_states["A"])

    def test_keithley_continue_policy_keeps_output_on_and_blocks_increase(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        raw["devices"]["keithley"]["safety"]["channels"]["B"]["enabled"] = True
        raw["devices"]["keithley"]["safety"]["stop_on_compliance"] = False
        settings = StationSettings.model_validate(raw)
        keithley = KeithleyAdapter(
            settings,
            session_factory=SimulatedVisaFactory(
                "keithley", keithley_resistance_ohm=67.0
            ),
        )
        keithley.connect()
        keithley.configure_source(
            KeithleySourceRequest("A", "voltage", 0.05, 0.001)
        )
        keithley.configure_source(
            KeithleySourceRequest("B", "current", 0.001, 0.067)
        )
        keithley.set_output("A", True)
        keithley.set_output("B", True)

        measurement = keithley.measure("B")

        self.assertTrue(measurement.compliance_detected)
        self.assertFalse(measurement.compliance_stop_required)
        self.assertTrue(measurement.output_enabled)
        self.assertTrue(keithley._output_states["A"])
        self.assertTrue(keithley._output_states["B"])
        with self.assertRaisesRegex(SafetyViolation, "increasing"):
            keithley.update_source_level("B", mode="current", level_si=0.002)
        self.assertTrue(keithley._output_states["B"])

        keithley.update_source_level("B", mode="current", level_si=0.0005)
        cleared = keithley.measure("B")
        self.assertFalse(cleared.compliance_detected)
        self.assertEqual(keithley.state, DeviceState.OUTPUT_ON)
        self.assertTrue(keithley._output_states["B"])

    def test_keithley_enabling_stop_policy_after_warning_latches_only_channel(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["stop_on_compliance"] = False
        settings = StationSettings.model_validate(raw)
        keithley = KeithleyAdapter(
            settings,
            session_factory=SimulatedVisaFactory(
                "keithley", keithley_resistance_ohm=67.0
            ),
        )
        keithley.connect()
        keithley.configure_source(
            KeithleySourceRequest("B", "current", 0.001, 0.067)
        )
        keithley.set_output("B", True)
        warning = keithley.measure("B")
        self.assertTrue(warning.compliance_detected)
        self.assertTrue(keithley._output_states["B"])

        keithley.set_compliance_policy("B", True)

        self.assertFalse(keithley._output_states["B"])
        with self.assertRaisesRegex(SafetyViolation, "compliance"):
            keithley.set_output("B", True)

    def test_keithley_compliance_policy_dispatch_is_channel_scoped(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        settings = StationSettings.model_validate(raw)
        keithley = KeithleyAdapter(
            settings,
            session_factory=SimulatedVisaFactory("keithley"),
        )
        with patch.object(keithley, "set_compliance_policy", return_value=False) as policy:
            result = keithley_module._dispatch(
                keithley,
                "set_compliance_policy",
                ("B", False),
            )

        self.assertFalse(result)
        policy.assert_called_once_with("B", False)

    def test_keithley_recover_dispatch_carries_channel_and_choice(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        settings = StationSettings.model_validate(raw)
        keithley = KeithleyAdapter(
            settings,
            session_factory=SimulatedVisaFactory("keithley"),
        )
        with patch.object(
            keithley,
            "recover_from_compliance",
            return_value={"channel": "A", "choice": "keep_off"},
        ) as recover:
            result = keithley_module._dispatch(
                keithley,
                "recover_from_compliance",
                ("A", "keep_off"),
            )

        self.assertEqual(result, {"channel": "A", "choice": "keep_off"})
        recover.assert_called_once_with("A", "keep_off")

    def test_keithley_compliance_can_recover_without_reconnecting(self) -> None:
        raw = deepcopy(simulated_station_settings(loaded_settings()).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        keithley = KeithleyAdapter(
            settings,
            session_factory=SimulatedVisaFactory("keithley", keithley_resistance_ohm=67.0),
        )
        identity = keithley.connect()
        keithley.configure_source(KeithleySourceRequest("B", "current", 0.001, 0.067))
        keithley.set_output("B", True)
        measurement = keithley.measure("B")

        self.assertTrue(measurement.compliance_stop_required)
        self.assertEqual(keithley.state, DeviceState.COMPLIANCE)
        with self.assertRaisesRegex(SafetyViolation, "compliance"):
            keithley.set_output("B", True)
        recovery = keithley.recover_from_compliance("B", "keep_off")

        self.assertEqual(recovery["state"], DeviceState.OUTPUT_OFF.value)
        self.assertTrue(recovery["outputs_confirmed_off"])
        self.assertIs(keithley.identity, identity)
        self.assertEqual(keithley.state, DeviceState.OUTPUT_OFF)

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
            with self.assertRaisesRegex(SafetyViolation, "disabled in the station profile"):
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
                    {"config": RigolChannelConfig(1, "SQU", 1000, 0.001, -0.001)},
                    {},
                ),
                PlanAction("keithley-on", "set_keithley_output", {"channel": "B", "enabled": True}, {}),
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

