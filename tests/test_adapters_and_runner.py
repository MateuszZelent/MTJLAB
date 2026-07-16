from __future__ import annotations

from dataclasses import dataclass, field
import unittest

from app.devices.anritsu import AnritsuAdapter, SpectrumConfig
from app.devices.keithley import KeithleyAdapter, KeithleySourceRequest
from app.devices.rigol import RigolAdapter, RigolChannelConfig
from app.devices.visa import FakeVisaSession, FakeVisaSessionFactory
from app.domain.errors import SafetyViolation
from app.engine.compiler import ExecutionPlan, PlanAction
from app.engine.runner import RecipeRunner
from tests.helpers import simulation_settings


@dataclass
class MemoryWriter:
    points: list[object] = field(default_factory=list)
    status: str | None = None

    def append(self, point: object, trace: object = None) -> int:
        self.points.append((point, trace))
        return len(self.points) - 1

    def close(self, status: str) -> None:
        self.status = status


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

    def test_anritsu_live_trace_has_inclusive_frequency_axis(self) -> None:
        values = ",".join(str(-50 + index / 100) for index in range(101))
        session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "FREQ:START?": "1000000",
                "FREQ:STOP?": "2000000",
                "SWE:POIN?": "101",
                "TRAC? TRAC1": values,
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
                "print(smub.measure.v())": "0.01",
                "print(smub.measure.i())": "0.001",
            }
        )
        values = ",".join(str(-50 + index / 100) for index in range(101))
        anritsu_session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "FREQ:START?": "1000000",
                "FREQ:STOP?": "2000000",
                "SWE:POIN?": "101",
                "TRAC? TRAC1": values,
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


if __name__ == "__main__":
    unittest.main()
