from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import threading
import unittest

from app.devices.anritsu import AnritsuAdapter, SpectrumConfig
from app.devices.keithley import KeithleyAdapter, KeithleySourceRequest
from app.devices.rigol import (
    RigolAdapter,
    RigolBurstConfig,
    RigolChannelConfig,
    RigolFrequencySweepConfig,
    RigolModulationConfig,
)
from app.devices.visa import FakeVisaSession, FakeVisaSessionFactory
from app.domain.errors import SafetyViolation
from app.domain.models import DeviceState
from app.domain.models import ApplicationState
from app.engine.compiler import ExecutionPlan, PlanAction
from app.engine.runner import RecipeRunner
from app.settings.models import StationSettings
from tests.helpers import loaded_settings, simulation_settings


@dataclass
class MemoryWriter:
    points: list[object] = field(default_factory=list)
    events: list[tuple[str, dict[str, object], str]] = field(default_factory=list)
    status: str | None = None

    def append(self, point: object, trace: object = None) -> int:
        self.points.append((point, trace))
        return len(self.points) - 1

    def close(self, status: str) -> None:
        self.status = status

    def append_event(self, name: str, data: dict[str, object], *, severity: str = "info") -> None:
        self.events.append((name, data, severity))


class AdapterAndRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = simulation_settings()

    def test_rigol_cannot_enable_output_with_unapproved_profile(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SOUR1:VOLT:LOW?": "-0.001",
                ":SOUR1:FUNC?": "SQU",
                ":SOUR1:FREQ?": "1000",
                ":SOUR1:VOLT:HIGH?": "0.001",
                ":SYST:ERR?": "0,No error",
                ":OUTP1?": "OFF",
            }
        )
        adapter = RigolAdapter(self.settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_channel(RigolChannelConfig(1, "SQU", 1000, 0.001, -0.001, dut_min_impedance_ohm=50))
        with self.assertRaises(SafetyViolation):
            adapter.set_output(1, True)

    def test_connect_forces_outputs_off_before_optional_probe_or_error_cleanup(self) -> None:
        rigol_session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SOUR1:MOD?": "OFF",
                ":SOUR1:SWE:STAT?": "OFF",
                ":SOUR1:BURS:STAT?": "OFF",
                ":SOUR1:PHAS?": "0",
            }
        )
        rigol = RigolAdapter(self.settings, session_factory=FakeVisaSessionFactory(rigol_session))
        rigol.connect()
        first_probe = rigol_session.writes.index(":SOUR1:MOD?")
        self.assertLess(rigol_session.writes.index(":OUTP1 OFF"), first_probe)
        self.assertLess(rigol_session.writes.index(":OUTP2 OFF"), first_probe)

        keithley_session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
            }
        )
        keithley = KeithleyAdapter(self.settings, session_factory=FakeVisaSessionFactory(keithley_session))
        keithley.connect()
        clear_errors = keithley_session.writes.index("errorqueue.clear()")
        self.assertLess(keithley_session.writes.index("smua.source.output = smua.OUTPUT_OFF"), clear_errors)
        self.assertLess(keithley_session.writes.index("smub.source.output = smub.OUTPUT_OFF"), clear_errors)

    def test_keithley_rejects_unconfirmed_output_state(self) -> None:
        raw = deepcopy(self.settings.model_dump(mode="python"))
        raw["profile"]["state"] = "approved"
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smub.source.output)": "0",
            }
        )
        adapter = KeithleyAdapter(settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_source(KeithleySourceRequest("B", "current", .001, .067))
        adapter.arm_output("B")

        with self.assertRaisesRegex(Exception, "nie potwierdził"):
            adapter.set_output("B", True)

    def test_device_state_remains_on_when_another_channel_is_disabled(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)

        keithley_session = FakeVisaSession(
            responses={"*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0", "print(errorqueue.count)": "0"}
        )
        keithley = KeithleyAdapter(settings, session_factory=FakeVisaSessionFactory(keithley_session))
        keithley.connect()
        keithley.configure_source(KeithleySourceRequest("B", "current", 0.001, 0.067))
        keithley.arm_output("B")
        keithley.set_output("B", True)
        keithley.set_output("A", False)
        self.assertEqual(keithley.state, DeviceState.OUTPUT_ON)

        rigol_session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SYST:ERR?": "0,No error",
                ":SOUR1:VOLT:LOW?": "-0.001",
                ":SOUR1:FUNC?": "SQU",
                ":SOUR1:FREQ?": "1000",
                ":SOUR1:VOLT:HIGH?": "0.001",
                ":OUTP1?": lambda _command: "ON" if ":OUTP1 ON" in rigol_session.writes else "OFF",
                ":OUTP2?": "OFF",
            }
        )
        rigol = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(rigol_session))
        rigol.connect()
        rigol.configure_channel(RigolChannelConfig(1, "SQU", 1000, 0.001, -0.001, dut_min_impedance_ohm=50))
        rigol.arm_output(1)
        rigol.set_output(1, True)
        rigol.set_output(2, False)
        self.assertEqual(rigol.state, DeviceState.OUTPUT_ON)

    def test_keithley_measurement_trip_forces_all_outputs_off(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smub.measure.iv())": "0.001\t0.2",
            }
        )
        adapter = KeithleyAdapter(settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_source(KeithleySourceRequest("B", "current", 0.001, 0.067))
        adapter.arm_output("B")
        adapter.set_output("B", True)

        with self.assertRaises(SafetyViolation):
            adapter.measure("B")

        self.assertIn("smua.source.output = smua.OUTPUT_OFF", session.writes)
        self.assertIn("smub.source.output = smub.OUTPUT_OFF", session.writes)
        self.assertEqual(adapter.state, DeviceState.FAULT)

    def test_failed_emergency_shutdown_marks_each_device_state_unknown(self) -> None:
        rigol_session = FakeVisaSession(
            responses={"*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08"}
        )
        keithley_session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
            }
        )
        anritsu_session = FakeVisaSession(responses={"*IDN?": "ANRITSU,MS2830A,123456,1.0"})
        adapters = (
            (RigolAdapter(self.settings, session_factory=FakeVisaSessionFactory(rigol_session)), rigol_session),
            (KeithleyAdapter(self.settings, session_factory=FakeVisaSessionFactory(keithley_session)), keithley_session),
            (AnritsuAdapter(self.settings, session_factory=FakeVisaSessionFactory(anritsu_session)), anritsu_session),
        )
        for adapter, session in adapters:
            adapter.connect()
            session.closed = True  # simulate a transport loss immediately before E-STOP
            adapter.emergency_off()
            self.assertEqual(adapter.state, DeviceState.UNKNOWN)

    def test_anritsu_live_trace_has_inclusive_frequency_axis(self) -> None:
        values = ",".join(str(-50 + index / 100) for index in range(101))
        session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "INST?": "SPECT",
                "FREQ:STAR?": "1000000",
                "FREQ:STOP?": "2000000",
                "DISP:WIND:TRAC:Y:RLEV?": "0",
                "SWE:POIN?": "101",
                "TRAC? TRAC1": values,
                "FORM?": "ASC,0",
                "*OPC?": "1",
            }
        )
        adapter = AnritsuAdapter(self.settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_spectrum(SpectrumConfig(1e6, 2e6, 0, 101))
        adapter.start_live()
        trace = adapter.fetch_trace()
        self.assertTrue(adapter.live)
        self.assertEqual(trace.frequencies_hz[0], 1e6)
        self.assertEqual(trace.frequencies_hz[-1], 2e6)
        self.assertEqual(len(trace.powers_dbm), 101)

    def test_anritsu_reads_current_configuration_without_writes_or_acquisition_unlock(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "INST?": "SPECT",
                "FREQ:STAR?": "1000000",
                "FREQ:STOP?": "4000000000",
                "DISP:WIND:TRAC:Y:RLEV?": "-10",
                "SWE:POIN?": "1001",
            }
        )
        locked_settings = simulation_settings(anritsu_enabled=False)
        adapter = AnritsuAdapter(locked_settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()

        snapshot = adapter.read_current_configuration()

        self.assertEqual(snapshot.start_hz, 1e6)
        self.assertEqual(snapshot.stop_hz, 4e9)
        self.assertEqual(snapshot.reference_level_dbm, -10)
        self.assertEqual(snapshot.points, 1001)
        self.assertEqual(snapshot.instrument_mode, "SPECT")
        self.assertEqual(
            session.writes,
            [
                "*IDN?",
                "INST?",
                "FREQ:STAR?",
                "FREQ:STOP?",
                "DISP:WIND:TRAC:Y:RLEV?",
                "SWE:POIN?",
            ],
        )
        self.assertTrue(all("?" in command for command in session.writes))

    def test_anritsu_passive_live_reads_locked_profile_without_writes(self) -> None:
        values = ",".join(str(-70 + index / 100) for index in range(101))
        session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "INST?": "SPECT",
                "FORM?": "ASC,0",
                "FREQ:STAR?": "1000000",
                "FREQ:STOP?": "2000000",
                "DISP:WIND:TRAC:Y:RLEV?": "0",
                "SWE:POIN?": "101",
                "TRAC? TRAC1": values,
            }
        )
        adapter = AnritsuAdapter(
            simulation_settings(anritsu_enabled=False),
            session_factory=FakeVisaSessionFactory(session),
        )
        adapter.connect()

        adapter.start_live()
        trace = adapter.fetch_current_trace()

        self.assertEqual(len(trace.powers_dbm), 101)
        self.assertTrue(adapter.live)
        self.assertTrue(all("?" in command for command in session.writes))

    def test_anritsu_rejects_frequency_outside_the_approved_profile(self) -> None:
        session = FakeVisaSession(responses={"*IDN?": "ANRITSU,MS2830A,123456,1.0"})
        adapter = AnritsuAdapter(self.settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        with self.assertRaises(SafetyViolation):
            adapter.configure_spectrum(SpectrumConfig(1e6, 101e9, 0, 101))

    def test_anritsu_opc_query_uses_and_restores_hard_visa_deadline(self) -> None:
        session = FakeVisaSession(
            responses={"*IDN?": "ANRITSU,MS2830A,123456,1.0"},
            timeout=10_000,
        )

        def opc(_command: str) -> str:
            self.assertGreater(session.timeout, 0)
            self.assertLessEqual(session.timeout, 50)
            return "1"

        session.responses["*OPC?"] = opc
        adapter = AnritsuAdapter(self.settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.wait_complete(deadline_s=0.05)
        self.assertEqual(session.timeout, 10_000)

    def test_rigol_advanced_configuration_forces_output_off(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SYST:ERR?": "0,No error",
                ":OUTP1?": "OFF",
                ":SOUR1:MOD?": "OFF",
                ":SOUR1:SWE:STAT?": "OFF",
                ":SOUR1:BURS:STAT?": "OFF",
                ":SOUR1:PHAS?": "0",
            }
        )
        adapter = RigolAdapter(self.settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_modulation(RigolModulationConfig(1, True, "AM", rate_hz=1000, parameter=50))
        adapter.configure_frequency_sweep(
            RigolFrequencySweepConfig(1, True, 100, 1000, 1.0, steps=10)
        )
        adapter.configure_burst(RigolBurstConfig(1, True, cycles=3, period_s=0.01))
        self.assertIn(":OUTP1 OFF", session.writes)
        self.assertIn(":SOUR1:MOD:TYPE AM", session.writes)
        self.assertIn(":SOUR1:SWE:STAT ON", session.writes)
        self.assertIn(":SOUR1:BURS ON", session.writes)

    def test_compliance_writes_a_partial_checkpoint_then_faults_safely(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smub.measure.iv())": "0.001\t0.067",
            }
        )
        keithley = KeithleyAdapter(self.settings, session_factory=FakeVisaSessionFactory(session))
        keithley.connect()
        rigol = RigolAdapter(self.settings, session_factory=FakeVisaSessionFactory(FakeVisaSession()))
        anritsu = AnritsuAdapter(self.settings, session_factory=FakeVisaSessionFactory(FakeVisaSession()))
        plan = ExecutionPlan(
            recipe_name="compliance",
            actions=(
                PlanAction("setup", "configure_keithley", {"request": KeithleySourceRequest("B", "current", .001, .067)}, {"keithley.B.current": .001}),
                PlanAction("measure", "measure_keithley", {"channel": "B"}, {}),
            ),
            total_points=0,
            sha256="compliance",
            recipe_source="schema_version: 1\n",
        )
        writer = MemoryWriter()
        result = RecipeRunner(rigol=rigol, keithley=keithley, anritsu=anritsu, writer=writer).run(plan)  # type: ignore[arg-type]
        self.assertEqual(result.stored_points, 1)
        self.assertIsNotNone(result.error)
        self.assertEqual(writer.status, "faulted")
        point, trace = writer.points[0]
        self.assertEqual(point.status, "compliance")
        self.assertIsNone(trace)
        self.assertIn("smub.source.output = smub.OUTPUT_OFF", session.writes)

    def test_keithley_whitelisted_sense_and_manual_ranges_are_validated_and_written(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
            }
        )
        adapter = KeithleyAdapter(self.settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_source(
            KeithleySourceRequest(
                "B",
                "current",
                0.001,
                0.067,
                sense_mode="4wire",
                source_autorange=False,
                source_range_si=0.01,
                measure_voltage_autorange=False,
                measure_voltage_range_si=0.067,
                measure_current_autorange=False,
                measure_current_range_si=0.01,
            )
        )
        self.assertIn("smub.source.autorangei = smub.AUTORANGE_OFF", session.writes)
        self.assertIn("smub.source.rangei = 0.01", session.writes)
        self.assertIn("smub.measure.rangev = 0.067", session.writes)
        self.assertIn("smub.sense = smub.SENSE_4WIRE", session.writes)

    def test_keithley_measure_only_configures_measurement_path_not_source_range(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
            }
        )
        adapter = KeithleyAdapter(self.settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_source(
            KeithleySourceRequest(
                "B",
                "measure_only",
                0,
                0,
                sense_mode="4wire",
                measure_voltage_autorange=False,
                measure_voltage_range_si=0.067,
            )
        )
        self.assertIn("smub.measure.rangev = 0.067", session.writes)
        self.assertIn("smub.sense = smub.SENSE_4WIRE", session.writes)
        self.assertNotIn("smub.source.rangev =", "\n".join(session.writes))
        with self.assertRaises(SafetyViolation):
            adapter.configure_source(
                KeithleySourceRequest("B", "measure_only", 0, 0, source_autorange=False, source_range_si=0.01)
            )

    def test_rigol_requires_one_shot_arm_before_enabling_output(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SOUR1:VOLT:LOW?": "-0.001",
                ":SOUR1:FUNC?": "SQU",
                ":SOUR1:FREQ?": "1000",
                ":SOUR1:VOLT:HIGH?": "0.001",
                ":SYST:ERR?": "0,No error",
            }
        )
        session.responses[":OUTP1?"] = lambda _command: "ON" if ":OUTP1 ON" in session.writes else "OFF"
        adapter = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_channel(RigolChannelConfig(1, "SQU", 1000, .001, -.001, dut_min_impedance_ohm=50))
        with self.assertRaises(SafetyViolation):
            adapter.set_output(1, True)
        adapter.arm_output(1)
        self.assertTrue(adapter.set_output(1, True))

    def test_runner_stores_one_checkpoint_for_a_spectrum(self) -> None:
        rigol_session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SOUR1:VOLT:LOW?": "-0.001",
                ":SOUR1:FUNC?": "SQU",
                ":SOUR1:FREQ?": "1000",
                ":SOUR1:VOLT:HIGH?": "0.001",
                ":OUTP1?": "OFF",
                ":SYST:ERR?": "0,No error",
            }
        )
        keithley_session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smub.measure.iv())": "0.001\t0.01",
            }
        )
        values = ",".join(str(-50 + index / 100) for index in range(101))
        anritsu_session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "INST?": "SPECT",
                "FREQ:STAR?": "1000000",
                "FREQ:STOP?": "2000000",
                "DISP:WIND:TRAC:Y:RLEV?": "0",
                "SWE:POIN?": "101",
                "TRAC? TRAC1": values,
                "*OPC?": "1",
            }
        )
        rigol = RigolAdapter(self.settings, session_factory=FakeVisaSessionFactory(rigol_session))
        keithley = KeithleyAdapter(self.settings, session_factory=FakeVisaSessionFactory(keithley_session))
        anritsu = AnritsuAdapter(self.settings, session_factory=FakeVisaSessionFactory(anritsu_session))
        rigol.connect()
        keithley.connect()
        anritsu.connect()
        plan = ExecutionPlan(
            recipe_name="test",
            actions=(
                PlanAction("anritsu", "configure_anritsu", {"config": SpectrumConfig(1e6, 2e6, 0, 101)}, {}),
                PlanAction("keithley", "configure_keithley", {"request": KeithleySourceRequest("B", "current", .001, .067)}, {"keithley.B.current": .001}),
                PlanAction("rigol", "configure_rigol", {"config": RigolChannelConfig(1, "SQU", 1000, .001, -.001, dut_min_impedance_ohm=50)}, {"rigol.1.high_level": .001}),
                PlanAction("measure", "measure_keithley", {"channel": "B"}, {}),
                PlanAction("trace", "acquire_spectrum", {"trace": "TRAC1"}, {}),
                PlanAction("ramp", "ramp_keithley_to_zero", {"channel": "B", "deadline_s": 1.0}, {}),
            ),
            total_points=1,
            sha256="test",
            recipe_source="schema_version: 1\n",
        )
        writer = MemoryWriter()
        result = RecipeRunner(rigol=rigol, keithley=keithley, anritsu=anritsu, writer=writer).run(plan)  # type: ignore[arg-type]
        self.assertEqual(result.stored_points, 1)
        self.assertEqual(writer.status, "completed")
        self.assertEqual(len(writer.points), 1)
        self.assertEqual(writer.events[-1][0], "run_completed")
        self.assertIn("INIT:IMM", anritsu_session.writes)

    def test_operator_stop_runs_finally_ramp_and_closes_as_aborted(self) -> None:
        from app.devices.simulators import SimulatedVisaFactory, simulated_station_settings

        settings = simulated_station_settings(loaded_settings())
        rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
        keithley = KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley"))
        anritsu = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
        for device in (rigol, keithley, anritsu):
            device.connect()
        plan = ExecutionPlan(
            recipe_name="operator-stop",
            actions=(
                PlanAction(
                    "keithley-config",
                    "configure_keithley",
                    {"request": KeithleySourceRequest("B", "current", 0.001, 0.067)},
                    {},
                ),
                PlanAction("wait", "wait", {"duration_s": 1.0}, {}),
                PlanAction(
                    "ramp",
                    "ramp_keithley_to_zero",
                    {"channel": "B", "deadline_s": 1.0},
                    {},
                    is_finally=True,
                ),
            ),
            total_points=0,
            sha256="operator-stop",
            recipe_source="schema_version: 1\n",
        )
        writer = MemoryWriter()
        runner = RecipeRunner(rigol=rigol, keithley=keithley, anritsu=anritsu, writer=writer)  # type: ignore[arg-type]
        timer = threading.Timer(0.02, runner.request_stop)
        timer.start()
        try:
            result = runner.run(plan)
        finally:
            timer.cancel()

        self.assertEqual(result.state, ApplicationState.SAFE)
        self.assertEqual(writer.status, "aborted")
        self.assertEqual(keithley.state, DeviceState.OUTPUT_OFF)
        self.assertIn("safe_finally_finished", tuple(event[0] for event in writer.events))


if __name__ == "__main__":
    unittest.main()
