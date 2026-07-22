from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from copy import deepcopy
from contextlib import nullcontext
import threading
import time
import unittest

from app.devices.anritsu_ms2830a import (
    AdvancedSpectrumConfig,
    AnritsuAdapter,
    SignalGeneratorConfig,
    SpectrumConfig,
)
from app.devices.keithley_2600 import (
    KeithleyAdapter,
    KeithleyRampRequest,
    KeithleySourceRequest,
    build_keithley_ramp_levels,
)
from app.devices.keithley_2600.module import _dispatch as dispatch_keithley
from app.devices.rigol_dg1000z import (
    RigolAdapter,
    RigolBurstConfig,
    RigolChannelConfig,
    RigolFrequencySweepConfig,
    RigolModulationConfig,
)
from app.devices.rigol_dg1000z.module import _dispatch as dispatch_rigol
from app.devices.simulators import RigolSimulator
from app.devices.moke_box.models import MokeHallVoltageReading, hall_field_from_voltage
from app.devices.lakeshore_475.models import (
    FieldUnit, GaussmeterReading, GaussmeterSnapshot, MeasurementMode,
)
from app.devices.visa import FakeVisaSession, FakeVisaSessionFactory
from app.domain.errors import ConfigurationError, DeviceError, SafetyViolation
from app.domain.quick_controls import QuickControlCommand
from app.domain.models import DeviceState
from app.domain.models import ApplicationState
from app.domain.quantities import (
    DIMENSION_CURRENT,
    DIMENSION_FREQUENCY,
    parse_quantity,
)
from app.engine.compiler import ExecutionPlan, PlanAction, RecipeCompiler
from app.engine.runner import ExecutionMode, RecipeRunner
from app.recipes import parse_recipe_text
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


@dataclass
class ShutdownProbe:
    fail: bool = False
    calls: int = 0
    state: DeviceState = DeviceState.OUTPUT_OFF

    def emergency_off(self) -> None:
        self.calls += 1
        if self.fail:
            self.state = DeviceState.UNKNOWN
            raise OSError("injected shutdown failure")
        self.state = DeviceState.OUTPUT_OFF


@dataclass
class RigolShutdownFaultSession(FakeVisaSession):
    """Fault injector that records shutdown attempts which fail before VISA."""

    fail_next_ch1_off_write: bool = False
    fail_next_ch1_output_query: bool = False
    attempted_commands: list[str] = field(default_factory=list)

    def write(self, command: str) -> None:
        self.attempted_commands.append(command)
        if self.fail_next_ch1_off_write and command == ":OUTP1 OFF":
            self.fail_next_ch1_off_write = False
            raise DeviceError("injected CH1 OFF write failure")
        super().write(command)

    def query(self, command: str) -> str:
        self.attempted_commands.append(command)
        if self.fail_next_ch1_output_query and command == ":OUTP1?":
            self.fail_next_ch1_output_query = False
            raise DeviceError("injected CH1 OUTPUT query failure")
        return super().query(command)


@dataclass
class DryRunOutputProbe(ShutdownProbe):
    output_requests: list[tuple[object, bool]] = field(default_factory=list)

    def io_timeout(self, _timeout_s: float):
        return nullcontext()

    def set_output(self, channel: object, enabled: bool) -> bool:
        self.output_requests.append((channel, enabled))
        self.state = DeviceState.OUTPUT_ON if enabled else DeviceState.OUTPUT_OFF
        return enabled

    def set_signal_generator_output(self, enabled: bool) -> bool:
        self.output_requests.append(("SG", enabled))
        self.state = DeviceState.OUTPUT_ON if enabled else DeviceState.OUTPUT_OFF
        return enabled


@dataclass
class HallProbe:
    reading: MokeHallVoltageReading
    reads: int = 0

    def read_hall_voltage(self) -> MokeHallVoltageReading:
        self.reads += 1
        return self.reading


@dataclass
class LakeShoreProbe:
    reading: GaussmeterReading
    reads: int = 0

    def read_measurement(self) -> GaussmeterReading:
        self.reads += 1
        return self.reading


class AdapterAndRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = simulation_settings()

    def test_rigol_failed_live_mutation_invalidates_verified_carrier(self) -> None:
        session = RigolSimulator()
        adapter = RigolAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        adapter.configure_channel(
            RigolChannelConfig(
                1,
                "SIN",
                1_000.0,
                0.001,
                -0.001,
            )
        )
        session.command_errors[":SOUR1:FREQ"] = "-200,Injected update failure"

        with self.assertRaises(DeviceError):
            adapter.update_frequency(1, 2_000.0)

        with self.assertRaisesRegex(SafetyViolation, "Configure and validate"):
            adapter.last_channel_config(1)

    def test_rigol_user_waveform_forces_and_verifies_frequency_mode(self) -> None:
        session = RigolSimulator()
        adapter = RigolAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()

        adapter.configure_channel(
            RigolChannelConfig(
                1,
                "USER",
                1_000.0,
                0.001,
                -0.001,
            )
        )

        mode_write = session.commands.index(":SOUR1:FUNC:ARB:MODE FREQ")
        frequency_write = session.commands.index(":SOUR1:FREQ 1000")
        self.assertLess(mode_write, frequency_write)
        self.assertIn(":SOUR1:FUNC:ARB:MODE?", session.commands)

    def test_dry_run_suppresses_every_output_on_action(self) -> None:
        rigol = DryRunOutputProbe()
        keithley = DryRunOutputProbe()
        anritsu = DryRunOutputProbe()
        writer = MemoryWriter()
        actions = (
            PlanAction(
                "rigol-on", "set_rigol_output", {"channel": 1, "enabled": True}, {}
            ),
            PlanAction(
                "keithley-on",
                "set_keithley_output",
                {"channel": "A", "enabled": True},
                {},
            ),
            PlanAction(
                "anritsu-on", "set_anritsu_sg_output", {"enabled": True}, {}
            ),
        )
        plan = ExecutionPlan(
            "dry-run-output-guard",
            actions,
            0,
            "dry-run-output-guard",
            "schema_version: 1\n",
            required_devices=frozenset({"rigol", "keithley", "anritsu"}),
        )

        result = RecipeRunner(
            rigol=rigol,  # type: ignore[arg-type]
            keithley=keithley,  # type: ignore[arg-type]
            anritsu=anritsu,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
            execution_mode=ExecutionMode.DRY_RUN,
        ).run(plan)

        self.assertEqual(result.state, ApplicationState.SAFE)
        self.assertIsNone(result.error)
        self.assertTrue(rigol.output_requests)
        self.assertTrue(keithley.output_requests)
        self.assertTrue(anritsu.output_requests)
        self.assertFalse(
            any(
                enabled
                for probe in (rigol, keithley, anritsu)
                for _channel, enabled in probe.output_requests
            )
        )
        suppressed = [
            data
            for name, data, _severity in writer.events
            if name == "dry_run_output_action_suppressed"
        ]
        self.assertEqual(len(suppressed), 3)

    def test_dry_run_faults_if_initial_output_off_cannot_be_confirmed(self) -> None:
        rigol = DryRunOutputProbe(fail=True)
        keithley = DryRunOutputProbe()
        anritsu = DryRunOutputProbe()
        writer = MemoryWriter()
        plan = ExecutionPlan(
            "dry-run-output-guard-failure",
            (),
            0,
            "dry-run-output-guard-failure",
            "schema_version: 1\n",
        )

        result = RecipeRunner(
            rigol=rigol,  # type: ignore[arg-type]
            keithley=keithley,  # type: ignore[arg-type]
            anritsu=anritsu,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
            execution_mode=ExecutionMode.DRY_RUN,
        ).run(plan)

        self.assertEqual(result.state, ApplicationState.FAULT)
        self.assertIn("could not confirm all outputs OFF", result.error or "")
        self.assertGreaterEqual(rigol.calls, 2)
        self.assertGreaterEqual(keithley.calls, 2)
        self.assertGreaterEqual(anritsu.calls, 2)
        self.assertEqual(writer.status, "faulted")

    def test_moke_hall_action_stores_voltage_and_derived_field_checkpoint(self) -> None:
        voltage_v = -0.099453926
        moke_box = HallProbe(
            MokeHallVoltageReading(
                voltage_v=voltage_v,
                stddev_v=0.0,
                samples=1,
                raw_codes=(0x7EBA1C,),
                timestamp_utc=datetime.now(timezone.utc),
            )
        )
        writer = MemoryWriter()
        plan = ExecutionPlan(
            recipe_name="moke-hall",
            actions=(
                PlanAction(
                    "hall-at-sweep-point",
                    "measure_moke_hall",
                    {},
                    {"keithley.B.current": 0.001},
                ),
            ),
            total_points=1,
            sha256="moke-hall",
            recipe_source="schema_version: 1\n",
            required_devices=frozenset({"moke_box"}),
        )

        result = RecipeRunner(
            rigol=ShutdownProbe(),  # type: ignore[arg-type]
            keithley=ShutdownProbe(),  # type: ignore[arg-type]
            anritsu=ShutdownProbe(),  # type: ignore[arg-type]
            moke_box=moke_box,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
        ).run(plan)

        self.assertEqual(result.state, ApplicationState.SAFE)
        self.assertEqual(result.stored_points, 1)
        self.assertEqual(moke_box.reads, 1)
        point, trace = writer.points[0]
        self.assertIsNone(trace)
        self.assertAlmostEqual(point.measurements["moke_box.hall1_voltage_v"], voltage_v)
        self.assertAlmostEqual(
            point.measurements["moke_box.hall1_field_t"],
            hall_field_from_voltage(voltage_v),
        )

    def test_lakeshore_action_stores_read_only_measurement_checkpoint(self) -> None:
        snapshot = GaussmeterSnapshot("2", MeasurementMode.RMS, "2", FieldUnit.TESLA, "0", True, "40", datetime.now(timezone.utc))
        lakeshore = LakeShoreProbe(GaussmeterReading.now(mode=MeasurementMode.RMS, unit=FieldUnit.TESLA, snapshot=snapshot, field_t=0.25, frequency_hz=60.0))
        writer = MemoryWriter()
        plan = ExecutionPlan("lake", (PlanAction("field", "measure_lakeshore_field", {}, {}),), 1, "lake", "schema_version: 1\n", required_devices=frozenset({"lakeshore_gaussmeter"}))

        result = RecipeRunner(rigol=ShutdownProbe(), keithley=ShutdownProbe(), anritsu=ShutdownProbe(), lakeshore=lakeshore, writer=writer).run(plan)  # type: ignore[arg-type]

        self.assertEqual(result.state, ApplicationState.SAFE)
        self.assertEqual(lakeshore.reads, 1)
        point, _trace = writer.points[0]
        self.assertEqual(point.measurements["lakeshore.field_t"], 0.25)
        self.assertEqual(point.measurements["lakeshore.frequency_hz"], 60.0)
        self.assertEqual(point.measurements["lakeshore.mode_code"], 2.0)

    def test_rigol_cannot_enable_output_without_output_permission(self) -> None:
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
        adapter.configure_channel(RigolChannelConfig(1, "SQU", 1000, 0.001, -0.001))
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

    def test_rigol_connect_fails_if_output_off_readback_is_not_confirmed(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":OUTP1?": "ON",
                ":OUTP2?": "OFF",
            }
        )
        adapter = RigolAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )

        with self.assertRaisesRegex(DeviceError, "during connection"):
            adapter.connect()

        self.assertTrue(session.closed)
        self.assertEqual(adapter.state, DeviceState.UNKNOWN)

    def test_rigol_disconnect_reports_unconfirmed_output_off_after_closing_visa(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":OUTP1?": "OFF",
                ":OUTP2?": "OFF",
                ":SOUR1:MOD?": "OFF",
                ":SOUR1:SWE:STAT?": "OFF",
                ":SOUR1:BURS:STAT?": "OFF",
                ":SOUR1:PHAS?": "0",
            }
        )
        adapter = RigolAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        session.responses[":OUTP1?"] = "ON"

        with self.assertRaisesRegex(DeviceError, "OUTPUT OFF was not confirmed"):
            adapter.disconnect()

        self.assertTrue(session.closed)
        self.assertEqual(adapter.state, DeviceState.UNKNOWN)

    def test_rigol_emergency_off_attempts_both_channels_after_independent_failures(self) -> None:
        session = RigolShutdownFaultSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":OUTP1?": "OFF",
                ":OUTP2?": "OFF",
            }
        )
        adapter = RigolAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        session.attempted_commands.clear()
        session.fail_next_ch1_off_write = True
        session.fail_next_ch1_output_query = True

        adapter.emergency_off()

        self.assertEqual(
            session.attempted_commands,
            [":OUTP1 OFF", ":OUTP2 OFF", ":OUTP1?", ":OUTP2?"],
        )
        self.assertEqual(adapter.state, DeviceState.UNKNOWN)

    def test_rigol_failed_channel_off_uses_two_channel_confirmed_fallback(self) -> None:
        session = RigolShutdownFaultSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":OUTP1?": "OFF",
                ":OUTP2?": "OFF",
            }
        )
        adapter = RigolAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        session.attempted_commands.clear()
        session.fail_next_ch1_off_write = True

        with self.assertRaisesRegex(DeviceError, "fallback confirmed both outputs OFF"):
            adapter.set_output(1, False)

        self.assertEqual(
            session.attempted_commands,
            [":OUTP1 OFF", ":OUTP1 OFF", ":OUTP2 OFF", ":OUTP1?", ":OUTP2?"],
        )
        self.assertEqual(adapter.state, DeviceState.OUTPUT_OFF)

    def test_rigol_capability_probe_rejects_malformed_success_responses(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":OUTP1?": "OFF",
                ":OUTP2?": "OFF",
                ":SOUR1:MOD?": "unexpected",
                ":SOUR1:SWE:STAT?": "OFF",
                ":SOUR1:BURS:STAT?": "OFF",
                ":SOUR1:PHAS?": "not-a-number",
                ":COUN?": "maybe",
                ":SOUR1:HARM?": "OFF",
                ":COUP?": "OFF,OFF,OFF",
                ":SOUR1:TRACK?": "unknown",
            }
        )
        adapter = RigolAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )

        adapter.connect()

        capabilities = adapter.capabilities
        self.assertIsNotNone(capabilities)
        self.assertFalse(capabilities.supports("modulation"))
        self.assertFalse(capabilities.supports("phase_sync"))
        self.assertFalse(capabilities.supports("counter"))
        self.assertFalse(capabilities.supports("coupling"))
        self.assertFalse(capabilities.supports("tracking"))
        self.assertTrue(capabilities.supports("frequency_sweep"))
        self.assertTrue(capabilities.supports("burst"))
        self.assertTrue(capabilities.supports("harmonics"))

    def test_rigol_rejects_invalid_pulse_timing_before_any_configuration_write(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":OUTP1?": "OFF",
                ":OUTP2?": "OFF",
            }
        )
        adapter = RigolAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        writes_before = len(session.writes)

        with self.assertRaisesRegex(SafetyViolation, "leading edge cannot be below 10 ns"):
            adapter.configure_channel(
                RigolChannelConfig(
                    1,
                    "PULS",
                    1000,
                    0.001,
                    -0.001,
                    pulse_width_s=100e-6,
                    pulse_leading_s=1e-9,
                    pulse_trailing_s=20e-9,
                )
            )

        self.assertEqual(len(session.writes), writes_before)

    def test_limit_updates_keep_confirmed_off_instrument_sessions_open(self) -> None:
        raw = deepcopy(self.settings.model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["channels"]["1"]["lab_limits"]["frequency"][
            "max"
        ] = "900 kHz"
        raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"][
            "source_current"
        ]["max"] = "9 mA"
        raw["devices"]["anritsu"]["safety"]["frequency"]["max"] = "19 GHz"
        updated = StationSettings.model_validate(raw)

        rigol_session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SOUR1:MOD?": "OFF",
                ":SOUR1:SWE:STAT?": "OFF",
                ":SOUR1:BURS:STAT?": "OFF",
                ":SOUR1:PHAS?": "0",
            }
        )
        rigol = RigolAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(rigol_session)
        )
        rigol.connect()

        keithley_session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
            }
        )
        keithley = KeithleyAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(keithley_session)
        )
        keithley.connect()

        anritsu_session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "*OPT?": "020",
                "OUTP?": "0",
            }
        )
        anritsu = AnritsuAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(anritsu_session)
        )
        anritsu.connect()

        rigol.apply_limit_settings(updated)
        keithley.apply_limit_settings(updated)
        anritsu.apply_limit_settings(updated)

        self.assertFalse(rigol_session.closed)
        self.assertFalse(keithley_session.closed)
        self.assertFalse(anritsu_session.closed)
        self.assertEqual(rigol._settings.safety.channels["1"].lab_limits.frequency.max, "900 kHz")
        self.assertEqual(
            keithley._settings.safety.channels["B"].lab_limits.source_current.max,
            "9 mA",
        )
        self.assertEqual(anritsu._settings.safety.frequency.max, "19 GHz")

    def test_limit_update_is_rejected_while_keithley_output_is_on(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        active_settings = StationSettings.model_validate(raw)
        updated_raw = deepcopy(raw)
        updated_raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"][
            "source_current"
        ]["max"] = "9 mA"
        updated = StationSettings.model_validate(updated_raw)
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
            }
        )
        adapter = KeithleyAdapter(
            active_settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        adapter.configure_source(KeithleySourceRequest("B", "current", 0.001, 0.067))
        adapter.set_output("B", True)

        with self.assertRaisesRegex(SafetyViolation, "confirmed OFF"):
            adapter.apply_limit_settings(updated)

        self.assertFalse(session.closed)
        self.assertEqual(
            adapter._settings.safety.channels["B"].lab_limits.source_current.max,
            "10 mA",
        )

    def test_saved_form_defaults_refresh_adapter_before_later_limit_update(self) -> None:
        defaults_raw = deepcopy(self.settings.model_dump(mode="python"))
        defaults_raw["devices"]["rigol"]["safety"]["channels"]["1"][
            "defaults"
        ]["frequency"] = "2 kHz"
        defaults_raw["devices"]["keithley"]["safety"]["channels"]["B"][
            "defaults"
        ]["nplc"] = 2.0
        defaults_raw["devices"]["anritsu"]["safety"]["defaults"][
            "start_frequency"
        ] = "2 MHz"
        with_defaults = StationSettings.model_validate(defaults_raw)
        adapters = (
            RigolAdapter(self.settings),
            KeithleyAdapter(self.settings),
            AnritsuAdapter(self.settings),
        )
        for adapter in adapters:
            adapter.refresh_station_context(with_defaults)

        limits_raw = deepcopy(with_defaults.model_dump(mode="python"))
        limits_raw["devices"]["rigol"]["safety"]["channels"]["1"][
            "lab_limits"
        ]["frequency"]["max"] = "900 kHz"
        limits_raw["devices"]["keithley"]["safety"]["channels"]["B"][
            "lab_limits"
        ]["source_current"]["max"] = "9 mA"
        limits_raw["devices"]["anritsu"]["safety"]["frequency"]["max"] = (
            "19 GHz"
        )
        updated = StationSettings.model_validate(limits_raw)

        for adapter in adapters:
            adapter.apply_limit_settings(updated)

    def test_keithley_rejects_unconfirmed_output_state(self) -> None:
        raw = deepcopy(self.settings.model_dump(mode="python"))
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
        with self.assertRaisesRegex(Exception, "did not confirm"):
            adapter.set_output("B", True)

    def test_keithley_adapter_rejects_five_volts_before_any_source_write(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
            }
        )
        adapter = KeithleyAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        writes_before = tuple(session.writes)

        with self.assertRaisesRegex(SafetyViolation, "outside"):
            adapter.configure_source(
                KeithleySourceRequest("B", "voltage", 5.0, 0.001)
            )

        self.assertEqual(tuple(session.writes), writes_before)

    def test_keithley_output_requires_output_permission(self) -> None:
        raw = deepcopy(simulation_settings(approved=False).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = False
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
            }
        )
        adapter = KeithleyAdapter(
            settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        adapter.configure_source(
            KeithleySourceRequest("B", "current", 0.001, 0.067)
        )

        with self.assertRaisesRegex(SafetyViolation, "disabled"):
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
                ":COUP?": "FREQ:OFF,PHASE:OFF,AMPL:OFF",
                ":SOUR1:TRACK?": "OFF",
                ":SOUR1:MOD?": "OFF",
                ":SOUR1:SWE:STAT?": "OFF",
                ":SOUR1:BURS:STAT?": "OFF",
                ":SOUR1:HARM?": "OFF",
                ":SOUR1:SUM?": "OFF",
                ":OUTP1?": lambda _command: "ON" if ":OUTP1 ON" in rigol_session.writes else "OFF",
                ":OUTP2?": "OFF",
            }
        )
        rigol = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(rigol_session))
        rigol.connect()
        rigol.configure_channel(RigolChannelConfig(1, "SQU", 1000, 0.001, -0.001))
        rigol.set_output(1, True)
        rigol.set_output(2, False)
        self.assertEqual(rigol.state, DeviceState.OUTPUT_ON)

    def test_rigol_level_update_preserves_energized_output_and_reads_back(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession()
        def voltage_state() -> tuple[float, float]:
            amplitude = next(
                (float(item.rsplit(" ", 1)[-1]) for item in reversed(session.writes)
                 if item.startswith(":SOUR1:VOLT ")),
                0.002,
            )
            offset = next(
                (float(item.rsplit(" ", 1)[-1]) for item in reversed(session.writes)
                 if item.startswith(":SOUR1:VOLT:OFFS ")),
                0.0,
            )
            high, low = offset + amplitude / 2, offset - amplitude / 2
            if ":SOUR1:VOLT:HIGH 0.003" in session.writes:
                high = 0.003
            return high, low
        session.responses.update(
            {
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SYST:ERR?": "0,No error",
                ":SOUR1:FUNC?": "SQU",
                ":SOUR1:FREQ?": "1000",
                ":SOUR1:VOLT?": lambda _command: str(
                    voltage_state()[0] - voltage_state()[1]
                ),
                ":SOUR1:VOLT:OFFS?": lambda _command: str(
                    sum(voltage_state()) / 2
                ),
                ":SOUR1:VOLT:HIGH?": lambda _command: (
                    str(voltage_state()[0])
                ),
                ":SOUR1:VOLT:LOW?": lambda _command: str(voltage_state()[1]),
                ":OUTP1?": lambda _command: (
                    "ON" if ":OUTP1 ON" in session.writes else "OFF"
                ),
            }
        )
        adapter = RigolAdapter(
            settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        adapter.configure_channel(
            RigolChannelConfig(
                1, "SQU", 1000, 0.001, -0.001
            )
        )
        adapter.set_output(1, True)

        actual = adapter.update_levels(
            1, high_level_v=0.003, low_level_v=-0.001
        )

        self.assertEqual(actual, (0.003, -0.001))
        self.assertEqual(adapter.state, DeviceState.OUTPUT_ON)
        self.assertIn(":SOUR1:VOLT:HIGH 0.003", session.writes)

    def test_rigol_quick_amplitude_and_offset_are_atomic_and_preserve_output(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession()
        def voltage_state() -> tuple[float, float, float, float]:
            amplitude = next(
                (float(item.rsplit(" ", 1)[-1]) for item in reversed(session.writes)
                 if item.startswith(":SOUR1:VOLT ")),
                0.002,
            )
            offset = next(
                (float(item.rsplit(" ", 1)[-1]) for item in reversed(session.writes)
                 if item.startswith(":SOUR1:VOLT:OFFS ")),
                0.0,
            )
            return amplitude, offset, offset + amplitude / 2, offset - amplitude / 2
        session.responses.update(
            {
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SYST:ERR?": "0,No error",
                ":SOUR1:FUNC?": "SQU",
                ":SOUR1:FREQ?": "1000",
                ":SOUR1:VOLT?": lambda _command: str(voltage_state()[0]),
                ":SOUR1:VOLT:OFFS?": lambda _command: str(voltage_state()[1]),
                ":SOUR1:VOLT:HIGH?": lambda _command: str(voltage_state()[2]),
                ":SOUR1:VOLT:LOW?": lambda _command: str(voltage_state()[3]),
                ":OUTP1?": lambda _command: (
                    "ON" if ":OUTP1 ON" in session.writes else "OFF"
                ),
            }
        )
        adapter = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_channel(
            RigolChannelConfig(1, "SQU", 1000, 0.001, -0.001)
        )
        adapter.set_output(1, True)

        self.assertAlmostEqual(adapter.update_amplitude_vpp(1, 0.004), 0.004)
        self.assertAlmostEqual(adapter.update_offset(1, 0.001), 0.001)
        self.assertEqual(adapter.state, DeviceState.OUTPUT_ON)
        self.assertIn(":SOUR1:VOLT 0.004", session.writes)
        self.assertIn(":SOUR1:VOLT:OFFS 0.001", session.writes)
        writes_before = len(session.writes)
        with self.assertRaises(SafetyViolation):
            adapter.update_amplitude_vpp(1, 1_000.0)
        self.assertEqual(len(session.writes), writes_before)

    def test_quick_setpoints_accept_exact_limits_and_reject_above_without_visa(self) -> None:
        settings = simulation_settings(approved=True)
        rigol_session = FakeVisaSession()
        rigol_session.responses.update(
            {
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SYST:ERR?": "0,No error",
                ":SOUR1:FUNC?": "SIN",
                ":SOUR1:FREQ?": lambda _command: next(
                    (
                        command.rsplit(" ", 1)[-1]
                        for command in reversed(rigol_session.writes)
                        if command.startswith(":SOUR1:FREQ ")
                    ),
                    "1000",
                ),
                ":SOUR1:VOLT:HIGH?": "0.001",
                ":SOUR1:VOLT:LOW?": "-0.001",
                ":OUTP1?": "OFF",
            }
        )
        rigol = RigolAdapter(
            settings, session_factory=FakeVisaSessionFactory(rigol_session)
        )
        rigol.connect()
        rigol.configure_channel(
            RigolChannelConfig(
                1, "SIN", 1000, 0.001, -0.001
            )
        )
        rigol_limit = parse_quantity(
            settings.rigol.safety.channels["1"].lab_limits.frequency.max,
            DIMENSION_FREQUENCY,
        ).si_value
        self.assertEqual(
            dispatch_rigol(
                rigol,
                "quick_setpoint",
                QuickControlCommand("rigol.1.frequency", f"{rigol_limit} Hz"),
            ),
            rigol_limit,
        )
        rigol_writes_at_limit = len(rigol_session.writes)
        with self.assertRaises(SafetyViolation):
            dispatch_rigol(
                rigol,
                "quick_setpoint",
                QuickControlCommand(
                    "rigol.1.frequency", f"{rigol_limit + 1.0} Hz"
                ),
            )
        self.assertEqual(len(rigol_session.writes), rigol_writes_at_limit)

        keithley_session = FakeVisaSession()
        keithley_session.responses.update(
            {
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smub.source.leveli)": lambda _command: next(
                    (
                        command.rsplit(" = ", 1)[-1]
                        for command in reversed(keithley_session.writes)
                        if command.startswith("smub.source.leveli = ")
                    ),
                    "0.001",
                ),
                "print(smub.source.output)": "0",
            }
        )
        keithley = KeithleyAdapter(
            settings, session_factory=FakeVisaSessionFactory(keithley_session)
        )
        keithley.connect()
        keithley.configure_source(
            KeithleySourceRequest("B", "current", 0.001, 0.067)
        )
        keithley_limit = parse_quantity(
            settings.keithley.safety.channels["B"].lab_limits.source_current.max,
            DIMENSION_CURRENT,
        ).si_value
        self.assertEqual(
            dispatch_keithley(
                keithley,
                "quick_setpoint",
                QuickControlCommand(
                    "keithley.B.current", f"{keithley_limit} A"
                ),
            ),
            keithley_limit,
        )
        keithley_writes_at_limit = len(keithley_session.writes)
        with self.assertRaises(SafetyViolation):
            dispatch_keithley(
                keithley,
                "quick_setpoint",
                QuickControlCommand(
                    "keithley.B.current", f"{keithley_limit + 1e-9} A"
                ),
            )
        self.assertEqual(len(keithley_session.writes), keithley_writes_at_limit)

    def test_quick_protocol_boundary_rejects_wrong_or_missing_units_before_visa(self) -> None:
        settings = simulation_settings(approved=True)
        rigol_session = FakeVisaSession()
        keithley_session = FakeVisaSession()
        rigol = RigolAdapter(
            settings, session_factory=FakeVisaSessionFactory(rigol_session)
        )
        keithley = KeithleyAdapter(
            settings, session_factory=FakeVisaSessionFactory(keithley_session)
        )

        for payload in (
            QuickControlCommand("rigol.1.frequency", "1 mA"),
            QuickControlCommand("rigol.1.frequency", "1000"),
        ):
            with self.assertRaises(ValueError):
                dispatch_rigol(rigol, "quick_setpoint", payload)
        for payload in (
            QuickControlCommand("keithley.B.current", "1 V"),
            QuickControlCommand("keithley.B.current", "0.001"),
        ):
            with self.assertRaises(ValueError):
                dispatch_keithley(keithley, "quick_setpoint", payload)
        with self.assertRaisesRegex(ValueError, "explicit-unit command"):
            dispatch_rigol(
                rigol,
                "quick_setpoint",
                ("rigol.1.frequency", 1000.0),
            )

        self.assertEqual(rigol_session.writes, [])
        self.assertEqual(keithley_session.writes, [])

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
        adapter.set_output("B", True)

        with self.assertRaisesRegex(
            SafetyViolation,
            r"channel B measurement safety trip:.*Measured I=0.001 A, "
            r"V=0.2 V.*Both outputs were commanded OFF and confirmed OFF",
        ):
            adapter.measure("B")

        self.assertIn("smua.source.output = smua.OUTPUT_OFF", session.writes)
        self.assertIn("smub.source.output = smub.OUTPUT_OFF", session.writes)
        self.assertEqual(adapter.state, DeviceState.FAULT)

    def test_keithley_connect_applies_and_confirms_high_impedance_off_mode(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
            }
        )
        adapter = KeithleyAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )

        adapter.connect()

        for smu in ("smua", "smub"):
            self.assertIn(
                f"{smu}.source.offmode = {smu}.OUTPUT_HIGH_Z",
                session.writes,
            )
            self.assertIn(
                f"print({smu}.source.offmode == {smu}.OUTPUT_HIGH_Z)",
                session.writes,
            )
        self.assertEqual(adapter.state, DeviceState.OUTPUT_OFF)

    def test_keithley_connect_accepts_scientific_boolean_high_z_confirmation(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,2.1.6",
                "print(errorqueue.count)": "0.00000E+00",
                "print(smua.source.offmode == smua.OUTPUT_HIGH_Z)": "1.00000E+00",
                "print(smub.source.offmode == smub.OUTPUT_HIGH_Z)": "1.00000E+00",
            }
        )

        adapter = KeithleyAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )

        adapter.connect()
        self.assertEqual(adapter.state, DeviceState.OUTPUT_OFF)

    def test_keithley_passive_measure_never_writes_output_on(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smua.measure.iv())": "0\t0",
            }
        )
        adapter = KeithleyAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()

        result = adapter.measure("A")

        self.assertEqual(result.channel, "A")
        self.assertFalse(
            any("source.output" in write and "OUTPUT_ON" in write for write in session.writes)
        )
        self.assertEqual(adapter.state, DeviceState.OUTPUT_OFF)

    def test_keithley_measure_uses_hardware_compliance_and_trips_on_queue_error(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["stop_on_compliance"] = False
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession()
        drained = False

        def error_count(_command: str) -> str:
            return "1" if "print(smub.measure.iv())" in session.writes and not drained else "0"

        def next_error(_command: str) -> str:
            nonlocal drained
            drained = True
            return "-104\tData type error\t20\tnode 1"

        session.responses.update(
            {
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,2.1.6",
                "print(errorqueue.count)": error_count,
                "print(errorqueue.next())": next_error,
                "print(smub.measure.iv())": "0.001\t0.01",
                "print(smub.source.compliance)": "false",
            }
        )
        adapter = KeithleyAdapter(
            settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()

        with self.assertRaisesRegex(DeviceError, "Keithley reported an error"):
            adapter.measure("B")

        self.assertIn("print(smub.source.compliance)", session.writes)
        self.assertIn("smua.source.output = smua.OUTPUT_OFF", session.writes)
        self.assertIn("smub.source.output = smub.OUTPUT_OFF", session.writes)
        self.assertEqual(adapter.state, DeviceState.FAULT)

    def test_keithley_measurement_output_transition_trips_both_channels_off(self) -> None:
        session = FakeVisaSession()

        def output_readback(command: str) -> str:
            smu = command.split(".", 1)[0].removeprefix("print(")
            last_off = max(
                (
                    index
                    for index, write in enumerate(session.writes)
                    if write == f"{smu}.source.output = {smu}.OUTPUT_OFF"
                ),
                default=-1,
            )
            measurement = max(
                (
                    index
                    for index, write in enumerate(session.writes)
                    if write == "print(smua.measure.iv())"
                ),
                default=-1,
            )
            return "1" if smu == "smua" and measurement > last_off else "0"

        session.responses.update(
            {
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smua.measure.iv())": "0\t0",
                "print(smua.source.output)": output_readback,
                "print(smub.source.output)": output_readback,
            }
        )
        adapter = KeithleyAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()

        with self.assertRaisesRegex(DeviceError, "changed an OUTPUT state"):
            adapter.measure("A")

        measurement_index = session.writes.index("print(smua.measure.iv())")
        self.assertIn(
            "smua.source.output = smua.OUTPUT_OFF",
            session.writes[measurement_index + 1 :],
        )
        self.assertIn(
            "smub.source.output = smub.OUTPUT_OFF",
            session.writes[measurement_index + 1 :],
        )
        self.assertFalse(
            any("source.output" in write and "OUTPUT_ON" in write for write in session.writes)
        )
        self.assertEqual(adapter.state, DeviceState.FAULT)

    def test_keithley_compliance_update_preserves_energized_output_and_reads_back(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smub.source.limitv)": lambda _command: (
                    "0.05"
                    if "smub.source.limitv = 0.05" in session.writes
                    else "0.067"
                ),
            }
        )
        adapter = KeithleyAdapter(
            settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        adapter.configure_source(
            KeithleySourceRequest("B", "current", 0.001, 0.067)
        )
        adapter.set_output("B", True)

        actual = adapter.update_source_compliance(
            "B", mode="current", compliance_si=0.05
        )

        self.assertEqual(actual, 0.05)
        self.assertIn("smub.source.limitv = 0.05", session.writes)
        self.assertEqual(adapter.state, DeviceState.OUTPUT_ON)

    def test_keithley_configuration_rejects_nplc_readback_mismatch(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smub.measure.nplc)": "0.5",
            }
        )
        adapter = KeithleyAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()

        with self.assertRaisesRegex(DeviceError, "configuration readback mismatch"):
            adapter.configure_source(
                KeithleySourceRequest(
                    "B", "current", 0.001, 0.067, nplc=1.0
                )
            )

    def test_keithley_configuration_accepts_scientific_numeric_enum_readback(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smub.source.func)": "0.00000E+00",
                "print(smub.source.autorangei)": "1.00000E+00",
                "print(smub.measure.autorangev)": "1.00000E+00",
                "print(smub.measure.autorangei)": "1.00000E+00",
                "print(smub.sense)": "0.00000E+00",
            }
        )
        adapter = KeithleyAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()

        adapter.configure_source(
            KeithleySourceRequest("B", "current", 0.001, 0.067)
        )

        self.assertEqual(adapter.state, DeviceState.OUTPUT_OFF)

    def test_keithley_manual_ranges_verify_selected_2602a_hardware_range(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,2.1.6",
                "print(errorqueue.count)": "0",
                # 67 mV is a range-selection request. The 2602A correctly
                # returns its selected nominal 100 mV hardware range.
                "print(smub.measure.rangev)": "0.1",
            }
        )
        adapter = KeithleyAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()

        adapter.configure_source(
            KeithleySourceRequest(
                "B",
                "current",
                1e-3,
                67e-3,
                measure_voltage_autorange=False,
                measure_voltage_range_si=67e-3,
            )
        )

        self.assertEqual(adapter.state, DeviceState.OUTPUT_OFF)

    def test_keithley_rejects_unrepresentable_nplc_before_tsp_write(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,2.1.6",
                "print(errorqueue.count)": "0",
            }
        )
        adapter = KeithleyAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        writes_before = len(session.writes)

        with self.assertRaisesRegex(SafetyViolation, "0.001 PLC increments"):
            adapter.configure_source(
                KeithleySourceRequest("B", "current", 1e-3, 67e-3, nplc=0.0015)
            )

        self.assertEqual(session.writes[writes_before:], [])

    def test_keithley_enforces_2602a_compliance_minimum_beyond_yaml(self) -> None:
        raw = deepcopy(self.settings.model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"][
            "voltage_compliance"
        ]["min"] = "1 mV"
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,2.1.6",
                "print(errorqueue.count)": "0",
            }
        )
        adapter = KeithleyAdapter(
            settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        writes_before = len(session.writes)

        with self.assertRaisesRegex(SafetyViolation, "between 10 mV and 40 V"):
            adapter.configure_source(
                KeithleySourceRequest("B", "current", 1e-3, 1e-3)
            )

        self.assertEqual(session.writes[writes_before:], [])

    def test_keithley_enforces_2602a_continuous_operating_boundary(self) -> None:
        raw = deepcopy(self.settings.model_dump(mode="python"))
        limits = raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]
        limits["source_current"] = {"min": "-3 A", "max": "3 A", "max_abs": "3 A"}
        limits["voltage_compliance"] = {"min": "10 mV", "max": "40 V"}
        limits["measured_current_trip"] = {"min": "-3 A", "max": "3 A"}
        limits["measured_voltage_trip"] = {"min": "-40 V", "max": "40 V"}
        limits["max_abs_power"] = "120 W"
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,2.1.6",
                "print(errorqueue.count)": "0",
            }
        )
        adapter = KeithleyAdapter(
            settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        writes_before = len(session.writes)

        with self.assertRaisesRegex(SafetyViolation, "above 1 A.*6 V"):
            adapter.configure_source(
                KeithleySourceRequest("B", "current", 2.0, 10.0)
            )

        self.assertEqual(session.writes[writes_before:], [])

    def test_keithley_configuration_verifies_all_settings_without_output_on(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
            }
        )
        adapter = KeithleyAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        writes_before = len(session.writes)

        adapter.configure_source(
            KeithleySourceRequest(
                "B",
                "current",
                500e-6,
                50e-3,
                nplc=0.5,
                settle_time_s=0.2,
                sense_mode="4wire",
                source_autorange=False,
                source_range_si=1e-3,
                measure_voltage_autorange=False,
                measure_voltage_range_si=67e-3,
                measure_current_autorange=False,
                measure_current_range_si=1e-3,
            )
        )

        configuration_traffic = session.writes[writes_before:]
        self.assertEqual(
            configuration_traffic[0],
            "smub.source.output = smub.OUTPUT_OFF",
        )
        self.assertFalse(
            any("OUTPUT_ON" in command for command in configuration_traffic)
        )
        for readback_field in (
            "smub.source.output",
            "smub.measure.nplc",
            "smub.sense",
            "smub.measure.autorangev",
            "smub.measure.autorangei",
            "smub.measure.rangev",
            "smub.measure.rangei",
            "smub.source.func",
            "smub.source.leveli",
            "smub.source.limitv",
            "smub.source.autorangei",
            "smub.source.rangei",
        ):
            self.assertIn(f"print({readback_field})", configuration_traffic)
        self.assertEqual(adapter.state, DeviceState.OUTPUT_OFF)

    def test_keithley_reads_both_channel_configurations_using_queries_only(self) -> None:
        raw = deepcopy(self.settings.model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
            }
        )
        adapter = KeithleyAdapter(
            settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        adapter.configure_source(
            KeithleySourceRequest(
                "A",
                "current",
                500e-6,
                50e-3,
                nplc=0.5,
                sense_mode="4wire",
                source_autorange=False,
                source_range_si=1e-3,
                measure_voltage_autorange=False,
                measure_voltage_range_si=100e-3,
                measure_current_autorange=False,
                measure_current_range_si=1e-3,
            )
        )
        adapter.configure_source(
            KeithleySourceRequest(
                "B",
                "voltage",
                10e-3,
                1e-3,
                nplc=2.0,
                source_autorange=False,
                source_range_si=67e-3,
                measure_voltage_autorange=False,
                measure_voltage_range_si=70e-3,
                measure_current_autorange=False,
                measure_current_range_si=10e-3,
            )
        )
        adapter.set_output("B", True)
        traffic_start = len(session.writes)

        readback = adapter.read_configuration()

        read_traffic = session.writes[traffic_start:]
        self.assertTrue(read_traffic)
        self.assertTrue(all(command.startswith("print(") for command in read_traffic))
        self.assertFalse(any(" = " in command for command in read_traffic))
        channel_a, channel_b = readback.channels
        self.assertEqual(channel_a.channel, "A")
        self.assertFalse(channel_a.output_enabled)
        self.assertEqual(channel_a.output_off_mode, "high_impedance")
        self.assertEqual(channel_a.source_mode, "current")
        self.assertEqual(channel_a.source_level_si, 500e-6)
        self.assertEqual(channel_a.compliance_si, 50e-3)
        self.assertEqual(channel_a.source_range_si, 1e-3)
        self.assertEqual(channel_a.sense_mode, "4wire")
        self.assertEqual(channel_a.nplc, 0.5)
        self.assertEqual(channel_b.channel, "B")
        self.assertTrue(channel_b.output_enabled)
        self.assertEqual(channel_b.source_mode, "voltage")
        self.assertEqual(channel_b.source_level_si, 10e-3)
        self.assertEqual(channel_b.compliance_si, 1e-3)
        self.assertEqual(channel_b.source_range_si, 100e-3)
        self.assertEqual(channel_b.sense_mode, "2wire")
        self.assertEqual(channel_b.nplc, 2.0)
        self.assertEqual(adapter.state, DeviceState.OUTPUT_ON)

    def test_keithley_manual_ramp_queries_actual_level_and_measures_each_step(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smub.source.leveli)": lambda _command: next(
                    (
                        write.split("=", 1)[1].strip()
                        for write in reversed(session.writes)
                        if write.startswith("smub.source.leveli =")
                    ),
                    "0",
                ),
                "print(smub.measure.iv())": "0.0011\t0.01",
            }
        )
        adapter = KeithleyAdapter(settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_source(KeithleySourceRequest("B", "current", 0.001, 0.067))
        adapter.set_output("B", True)

        result = adapter.ramp_to_level(
            KeithleyRampRequest("B", 0.0012, 0.0001, 0.001, 1.0)
        )

        self.assertAlmostEqual(result.start_si, 0.001)
        self.assertAlmostEqual(result.levels_si[-1], 0.0012)
        self.assertEqual(
            session.writes.count("print(smub.measure.iv())"),
            len(result.levels_si),
        )
        self.assertEqual(adapter.state, DeviceState.OUTPUT_ON)
        self.assertAlmostEqual(result.final_measurement.voltage_v, 0.01)

    def test_keithley_quick_active_setpoint_updates_directly_with_readback(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smub.source.leveli)": lambda _command: (
                    "0.0012"
                    if "smub.source.leveli = 0.0012" in session.writes
                    else "0.001"
                ),
                "print(smub.measure.iv())": "0.0011\t0.01",
            }
        )
        adapter = KeithleyAdapter(settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_source(KeithleySourceRequest("B", "current", 0.001, 0.067))
        adapter.set_output("B", True)

        writes_before_limit_check = len(session.writes)
        with self.assertRaises(SafetyViolation):
            adapter.quick_update_source_level(
                "B", mode="current", level_si=1_000.0
            )
        self.assertEqual(len(session.writes), writes_before_limit_check)

        actual = adapter.quick_update_source_level(
            "B", mode="current", level_si=0.0012
        )

        self.assertAlmostEqual(actual, 0.0012)
        self.assertNotIn("print(smub.measure.iv())", session.writes)
        self.assertEqual(adapter.state, DeviceState.OUTPUT_ON)
        writes_before = len(session.writes)
        with self.assertRaisesRegex(SafetyViolation, "Configure Keithley B for voltage"):
            adapter.quick_update_source_level(
                "B", mode="voltage", level_si=0.001
            )
        self.assertEqual(len(session.writes), writes_before)

    def test_keithley_manual_ramp_failure_forces_both_outputs_off(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
                "print(smub.source.leveli)": "0.001",
            }
        )
        adapter = KeithleyAdapter(settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_source(KeithleySourceRequest("B", "current", 0.001, 0.067))
        adapter.set_output("B", True)

        with self.assertRaisesRegex(DeviceError, "source-level readback"):
            adapter.ramp_to_level(
                KeithleyRampRequest("B", 0.0011, 0.0001, 0.001, 1.0)
            )

        self.assertIn("smua.source.output = smua.OUTPUT_OFF", session.writes)
        self.assertIn("smub.source.output = smub.OUTPUT_OFF", session.writes)
        self.assertIn(adapter.state, {DeviceState.FAULT, DeviceState.UNKNOWN})

    def test_keithley_ramp_preview_is_finite_and_respects_point_limit(self) -> None:
        levels = build_keithley_ramp_levels(0.0, 0.001, 0.0003, max_points=10)
        self.assertEqual(len(levels), 4)
        self.assertAlmostEqual(levels[-1], 0.001)
        self.assertTrue(all(right > left for left, right in zip((0.0, *levels), levels)))
        with self.assertRaisesRegex(SafetyViolation, "requires 100"):
            build_keithley_ramp_levels(0.0, 0.01, 0.0001, max_points=99)

    def test_each_keithley_ivp_trip_forces_both_outputs_off(self) -> None:
        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        raw["devices"]["keithley"]["safety"]["stop_on_compliance"] = False
        raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"][
            "max_abs_power"
        ] = "100 uW"
        settings = StationSettings.model_validate(raw)
        cases = (
            ("current", "0.02\t0.01", "measured current"),
            ("voltage", "0.001\t0.2", "measured voltage"),
            ("power", "0.002\t0.06", "Measured power"),
        )
        for label, response, message in cases:
            with self.subTest(boundary=label):
                session = FakeVisaSession(
                    responses={
                        "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                        "print(errorqueue.count)": "0",
                        "print(smub.measure.iv())": response,
                    }
                )
                adapter = KeithleyAdapter(
                    settings, session_factory=FakeVisaSessionFactory(session)
                )
                adapter.connect()
                adapter.configure_source(
                    KeithleySourceRequest("B", "current", 0.001, 0.067)
                )
                adapter.set_output("B", True)

                with self.assertRaisesRegex(SafetyViolation, message):
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

    def test_shutdown_attempts_every_required_device_after_one_failure(self) -> None:
        keithley = ShutdownProbe(fail=True)
        rigol = ShutdownProbe()
        anritsu = ShutdownProbe()
        writer = MemoryWriter()
        plan = ExecutionPlan(
            recipe_name="shutdown-aggregation",
            actions=(PlanAction("wait", "wait", {"duration_s": 0.0}, {}),),
            total_points=0,
            sha256="shutdown-aggregation",
            recipe_source="schema_version: 1\n",
            required_devices=frozenset({"keithley", "rigol", "anritsu"}),
        )

        result = RecipeRunner(
            rigol=rigol,  # type: ignore[arg-type]
            keithley=keithley,  # type: ignore[arg-type]
            anritsu=anritsu,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
        ).run(plan)

        self.assertEqual((keithley.calls, rigol.calls, anritsu.calls), (2, 2, 2))
        self.assertEqual(result.state, ApplicationState.FAULT)
        self.assertEqual(writer.status, "faulted")
        self.assertTrue(any(name == "shutdown_error" for name, _data, _severity in writer.events))

    def test_any_recipe_shutdown_attempts_all_station_outputs(self) -> None:
        keithley = ShutdownProbe()
        rigol = ShutdownProbe()
        anritsu = ShutdownProbe()
        writer = MemoryWriter()
        plan = ExecutionPlan(
            recipe_name="anritsu-only-station-off",
            actions=(PlanAction("wait", "wait", {"duration_s": 0.0}, {}),),
            total_points=0,
            sha256="anritsu-only-station-off",
            recipe_source="schema_version: 1\n",
            required_devices=frozenset({"anritsu"}),
            safe_shutdown_actions=(
                "anritsu.rf_off_and_abort",
                "keithley.outputs_off",
                "rigol.outputs_off",
                "storage.flush_checkpoint",
            ),
        )

        result = RecipeRunner(
            rigol=rigol,  # type: ignore[arg-type]
            keithley=keithley,  # type: ignore[arg-type]
            anritsu=anritsu,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
        ).run(plan)

        self.assertEqual(result.state, ApplicationState.SAFE)
        self.assertEqual((keithley.calls, rigol.calls, anritsu.calls), (1, 1, 1))

    def test_faulted_single_device_recipe_also_attempts_all_station_outputs(self) -> None:
        keithley = ShutdownProbe()
        rigol = ShutdownProbe()
        anritsu = ShutdownProbe()
        writer = MemoryWriter()
        plan = ExecutionPlan(
            recipe_name="faulted-anritsu-only-station-off",
            actions=(PlanAction("broken", "not-supported", {}, {}),),
            total_points=0,
            sha256="faulted-anritsu-only-station-off",
            recipe_source="schema_version: 1\n",
            required_devices=frozenset({"anritsu"}),
        )

        result = RecipeRunner(
            rigol=rigol,  # type: ignore[arg-type]
            keithley=keithley,  # type: ignore[arg-type]
            anritsu=anritsu,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
        ).run(plan)

        self.assertEqual(result.state, ApplicationState.FAULT)
        self.assertEqual((keithley.calls, rigol.calls, anritsu.calls), (1, 1, 1))

    def test_fault_runs_pending_finally_before_independent_station_shutdown(self) -> None:
        from app.devices.simulators import SimulatedVisaFactory, simulated_station_settings

        settings = simulated_station_settings(loaded_settings())
        keithley = KeithleyAdapter(
            settings, session_factory=SimulatedVisaFactory("keithley")
        )
        keithley.connect()
        rigol = ShutdownProbe()
        anritsu = ShutdownProbe()
        writer = MemoryWriter()
        plan = ExecutionPlan(
            recipe_name="fault-runs-finally",
            actions=(
                PlanAction("broken", "not-supported", {}, {}),
                PlanAction(
                    "keithley-b-off",
                    "set_keithley_output",
                    {"channel": "B", "enabled": False},
                    {},
                    is_finally=True,
                ),
            ),
            total_points=0,
            sha256="fault-runs-finally",
            recipe_source="schema_version: 1\n",
        )

        result = RecipeRunner(
            rigol=rigol,  # type: ignore[arg-type]
            keithley=keithley,
            anritsu=anritsu,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
        ).run(plan)

        names = [name for name, _data, _severity in writer.events]
        self.assertEqual(result.state, ApplicationState.FAULT)
        self.assertIn("action_failed", names)
        self.assertIn("safe_finally_finished", names)
        self.assertLess(
            names.index("safe_finally_finished"),
            names.index("shutdown_action_started"),
        )
        self.assertEqual(keithley.state, DeviceState.OUTPUT_OFF)
        self.assertEqual((rigol.calls, anritsu.calls), (1, 1))

    def test_runner_executes_hashed_shutdown_manifest_in_declared_order(self) -> None:
        keithley = ShutdownProbe()
        rigol = ShutdownProbe()
        anritsu = ShutdownProbe()
        writer = MemoryWriter()
        manifest = (
            "rigol.outputs_off",
            "keithley.outputs_off",
            "anritsu.rf_off_and_abort",
            "storage.flush_checkpoint",
        )
        plan = ExecutionPlan(
            recipe_name="ordered-shutdown",
            actions=(PlanAction("wait", "wait", {"duration_s": 0.0}, {}),),
            total_points=0,
            sha256="ordered-shutdown",
            recipe_source="schema_version: 1\n",
            required_devices=frozenset({"keithley", "rigol", "anritsu"}),
            safe_shutdown_actions=manifest,
        )

        result = RecipeRunner(
            rigol=rigol,  # type: ignore[arg-type]
            keithley=keithley,  # type: ignore[arg-type]
            anritsu=anritsu,  # type: ignore[arg-type]
            writer=writer,  # type: ignore[arg-type]
        ).run(plan)

        started = tuple(
            data["action"]
            for name, data, _severity in writer.events
            if name == "shutdown_action_started"
        )
        self.assertEqual(result.state, ApplicationState.SAFE)
        self.assertEqual(started, manifest)
        self.assertEqual((rigol.calls, keithley.calls, anritsu.calls), (1, 1, 1))

    def test_anritsu_live_trace_has_inclusive_frequency_axis(self) -> None:
        values = ",".join(str(-50 + index / 100) for index in range(101))
        session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "*OPT?": "041,008",
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
                "*OPT?": "041,008",
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
        self.assertEqual(adapter.capabilities.hardware_options, ("041", "008"))
        self.assertEqual(
            session.writes,
            [
                "*IDN?",
                "*OPT?",
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

    def test_anritsu_live_temporarily_enables_and_restores_continuous_sweep(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "INST?": "SPECT",
                "FORM?": "ASC,0",
                "FREQ:STAR?": "1000000",
                "FREQ:STOP?": "2000000",
                "DISP:WIND:TRAC:Y:RLEV?": "0",
                "SWE:POIN?": "101",
                "INIT:CONT?": "0",
            }
        )
        adapter = AnritsuAdapter(
            simulation_settings(anritsu_enabled=False),
            session_factory=FakeVisaSessionFactory(session),
        )
        adapter.connect()

        adapter.start_live(ensure_continuous=True)
        adapter.stop_live()

        self.assertNotIn("TRAC:TYPE?", session.writes)
        self.assertIn("INIT:CONT ON", session.writes)
        self.assertIn("INIT:CONT OFF", session.writes)

    def test_anritsu_live_does_not_depend_on_trace_type_query(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "INST?": "SPECT",
                "FORM?": "ASC,0",
                "FREQ:STAR?": "1000000",
                "FREQ:STOP?": "2000000",
                "DISP:WIND:TRAC:Y:RLEV?": "0",
                "SWE:POIN?": "101",
                "INIT:CONT?": "1",
            }
        )
        adapter = AnritsuAdapter(
            simulation_settings(anritsu_enabled=False),
            session_factory=FakeVisaSessionFactory(session),
        )
        adapter.connect()

        adapter.start_live(ensure_continuous=True)

        self.assertTrue(adapter.live)
        self.assertNotIn("TRAC:TYPE?", session.writes)
        self.assertNotIn("INIT:CONT ON", session.writes)

    def test_anritsu_signal_generator_requires_qualified_limits_and_readback(self) -> None:
        from app.devices.simulators import SimulatedVisaFactory

        raw = simulation_settings(approved=True).model_dump(mode="python")
        raw["devices"]["anritsu"]["signal_generator"] = {
            "control_protocol": "basic_scpi",
            "frequency": {"min": "250 kHz", "max": "3.6 GHz"},
            "power": {"min": "-100 dBm", "max": "0 dBm"},
        }
        raw["devices"]["anritsu"]["safety"]["signal_generator_output_allowed"] = True
        settings = StationSettings.model_validate(raw)
        adapter = AnritsuAdapter(
            settings, session_factory=SimulatedVisaFactory("anritsu")
        )

        adapter.connect()
        self.assertTrue(adapter.capabilities.supports("signal_generator"))
        with self.assertRaisesRegex(DeviceError, "explicit SG mode"):
            adapter.read_signal_generator_configuration()
        snapshot = adapter.configure_signal_generator(SignalGeneratorConfig(1e9, -20.0))
        self.assertFalse(snapshot.output_enabled)
        self.assertTrue(adapter.set_signal_generator_output(True))
        self.assertEqual(adapter.state, DeviceState.OUTPUT_ON)
        updated = adapter.update_signal_generator(
            SignalGeneratorConfig(1.1e9, -15.0)
        )
        self.assertEqual(updated.frequency_hz, 1.1e9)
        self.assertEqual(updated.power_dbm, -15.0)
        self.assertTrue(updated.output_enabled)
        self.assertEqual(adapter.state, DeviceState.OUTPUT_ON)
        self.assertFalse(adapter.set_signal_generator_output(False))
        self.assertEqual(adapter.state, DeviceState.OUTPUT_OFF)

    def test_anritsu_connect_and_disconnect_prove_optional_sg_output_off(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "*OPT?": "020",
                "OUTP?": "0",
            }
        )
        adapter = AnritsuAdapter(
            simulation_settings(anritsu_enabled=False),
            session_factory=FakeVisaSessionFactory(session),
        )

        adapter.connect()
        self.assertEqual(
            session.writes[:7],
            ["*IDN?", "*OPT?", "INST SG", "OUTP 0", "OUTP?", "INST SPECT",],
        )
        session.writes.clear()

        adapter.disconnect()
        self.assertEqual(
            session.writes,
            ["INST SG", "OUTP 0", "OUTP?", "INST SPECT", "ABORT"],
        )
        self.assertEqual(adapter.state, DeviceState.DISCONNECTED)

    def test_anritsu_connect_fails_closed_when_optional_sg_will_not_turn_off(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "*OPT?": "020",
                "OUTP?": "1",
            }
        )
        adapter = AnritsuAdapter(
            simulation_settings(anritsu_enabled=False),
            session_factory=FakeVisaSessionFactory(session),
        )

        with self.assertRaisesRegex(DeviceError, "did not confirm RF OUTPUT OFF"):
            adapter.connect()

        self.assertEqual(adapter.state, DeviceState.DISCONNECTED)
        self.assertIsNone(adapter.identity)

    def test_anritsu_connect_enforces_profile_required_hardware_options(self) -> None:
        raw = simulation_settings(anritsu_enabled=False).model_dump(mode="python")
        raw["devices"]["anritsu"]["identity"]["required_options"] = ["008"]
        session = FakeVisaSession(
            responses={
                "*IDN?": "ANRITSU,MS2830A,123456,1.0",
                "*OPT?": "041",
            }
        )
        adapter = AnritsuAdapter(
            StationSettings.model_validate(raw),
            session_factory=FakeVisaSessionFactory(session),
        )

        with self.assertRaisesRegex(DeviceError, "missing profile-required.*008"):
            adapter.connect()

        self.assertEqual(adapter.state, DeviceState.DISCONNECTED)

    def test_anritsu_signal_generator_unverified_protocol_never_writes_configuration(self) -> None:
        from app.devices.simulators import SimulatedVisaFactory

        adapter = AnritsuAdapter(
            simulation_settings(approved=True),
            session_factory=SimulatedVisaFactory("anritsu"),
        )
        adapter.connect()

        with self.assertRaisesRegex(SafetyViolation, "unverified"):
            adapter.configure_signal_generator(SignalGeneratorConfig(1e9, -30.0))
        self.assertEqual(adapter.state, DeviceState.VERIFIED)

    def test_anritsu_advanced_spectrum_is_read_only_until_firmware_is_qualified(self) -> None:
        from app.devices.simulators import SimulatedVisaFactory

        adapter = AnritsuAdapter(
            simulation_settings(approved=True),
            session_factory=SimulatedVisaFactory("anritsu"),
        )
        adapter.connect()

        snapshot = adapter.read_advanced_spectrum_configuration()
        self.assertTrue(snapshot.rbw_auto)
        self.assertEqual(snapshot.detector, "NORM")
        self.assertFalse(snapshot.preamplifier_enabled)
        with self.assertRaisesRegex(SafetyViolation, "unverified"):
            adapter.configure_advanced_spectrum(AdvancedSpectrumConfig())

    def test_anritsu_advanced_spectrum_configures_and_verifies_documented_controls(self) -> None:
        from app.devices.simulators import SimulatedVisaFactory

        raw = simulation_settings(approved=True).model_dump(mode="python")
        raw["devices"]["anritsu"]["advanced_spectrum"] = {
            "control_protocol": "standard_scpi",
            "qualified_firmware": ["sim-1.0"],
        }
        rf_input = raw["devices"]["anritsu"]["safety"]["rf_input"]
        rf_input["minimum_internal_attenuation"] = "20 dB"
        rf_input["preamplifier_allowed"] = True
        settings = StationSettings.model_validate(raw)
        adapter = AnritsuAdapter(
            settings,
            session_factory=SimulatedVisaFactory("anritsu"),
        )
        adapter.connect()

        actual = adapter.configure_advanced_spectrum(
            AdvancedSpectrumConfig(
                rbw_auto=False,
                rbw_hz=3e3,
                vbw_mode="off",
                detector="POS",
                attenuation_auto=False,
                attenuation_db=20,
                preamplifier_enabled=True,
                sweep_time_auto=False,
                sweep_time_s=0.2,
            )
        )

        self.assertFalse(actual.rbw_auto)
        self.assertEqual(actual.rbw_hz, 3e3)
        self.assertEqual(actual.vbw_mode, "off")
        self.assertEqual(actual.detector, "POS")
        self.assertEqual(actual.attenuation_db, 20)
        self.assertTrue(actual.preamplifier_enabled)
        self.assertFalse(actual.sweep_time_auto)
        self.assertEqual(actual.sweep_time_s, 0.2)

    def test_anritsu_advanced_spectrum_rejects_auto_attenuation_with_profile_minimum(self) -> None:
        from app.devices.simulators import SimulatedVisaFactory

        raw = simulation_settings(approved=True).model_dump(mode="python")
        raw["devices"]["anritsu"]["advanced_spectrum"] = {
            "control_protocol": "standard_scpi",
            "qualified_firmware": ["sim-1.0"],
        }
        raw["devices"]["anritsu"]["safety"]["rf_input"][
            "minimum_internal_attenuation"
        ] = "20 dB"
        adapter = AnritsuAdapter(
            StationSettings.model_validate(raw),
            session_factory=SimulatedVisaFactory("anritsu"),
        )
        adapter.connect()

        with self.assertRaisesRegex(SafetyViolation, "Automatic attenuation is forbidden"):
            adapter.configure_advanced_spectrum(AdvancedSpectrumConfig())

    def test_anritsu_advanced_recipe_executes_verified_configuration(self) -> None:
        from app.devices.simulators import SimulatedVisaFactory

        raw = simulation_settings(approved=True).model_dump(mode="python")
        raw["devices"]["anritsu"]["advanced_spectrum"] = {
            "control_protocol": "standard_scpi",
            "qualified_firmware": ["sim-1.0"],
        }
        raw["devices"]["anritsu"]["identity"]["required_options"] = ["008"]
        raw["devices"]["anritsu"]["safety"]["rf_input"][
            "minimum_internal_attenuation"
        ] = "20 dB"
        settings = StationSettings.model_validate(raw)
        recipe = parse_recipe_text(
            """
schema_version: 1
name: advanced spectrum recipe
root:
  id: root
  type: sequence
  children:
    - id: advanced
      type: configure_anritsu_advanced
      rbw_mode: manual
      rbw: 3 kHz
      vbw_mode: manual
      vbw: 1 kHz
      detector: RMS
      attenuation_mode: manual
      attenuation: 20 dB
      sweep_time_mode: manual
      sweep_time: 200 ms
    - {id: point, type: checkpoint, label: configured}
"""
        )
        plan = RecipeCompiler(settings).compile(recipe)
        anritsu = AnritsuAdapter(
            settings, session_factory=SimulatedVisaFactory("anritsu")
        )
        anritsu.connect()
        writer = MemoryWriter()
        result = RecipeRunner(
            rigol=RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol")),
            keithley=KeithleyAdapter(
                settings, session_factory=SimulatedVisaFactory("keithley")
            ),
            anritsu=anritsu,
            writer=writer,  # type: ignore[arg-type]
        ).run(plan)

        self.assertEqual(result.state, ApplicationState.SAFE)
        self.assertEqual(result.stored_points, 1)
        point, _trace = writer.points[0]
        advanced_context = point.metadata["safety_context"]["anritsu.advanced"]
        self.assertEqual(advanced_context["rbw_hz"], 3e3)
        self.assertEqual(advanced_context["vbw_hz"], 1e3)
        self.assertEqual(advanced_context["detector"], "RMS")
        self.assertEqual(advanced_context["attenuation_db"], 20)
        self.assertEqual(advanced_context["sweep_time_s"], 0.2)
        configured = [
            event for event in writer.events if event[0] == "action_finished"
        ]
        self.assertTrue(configured)

    def test_anritsu_signal_generator_recipe_uses_output_and_finally_off(self) -> None:
        from app.devices.simulators import SimulatedVisaFactory

        raw = simulation_settings(approved=True).model_dump(mode="python")
        raw["devices"]["anritsu"]["signal_generator"] = {
            "control_protocol": "basic_scpi",
            "frequency": {"min": "250 kHz", "max": "3.6 GHz"},
            "power": {"min": "-100 dBm", "max": "0 dBm"},
        }
        raw["devices"]["anritsu"]["safety"]["signal_generator_output_allowed"] = True
        settings = StationSettings.model_validate(raw)
        recipe = parse_recipe_text(
            """
schema_version: 1
name: SG qualification
dut_limits:
  anritsu:
    max_signal_generator_output: -10 dBm
root:
  id: root
  type: sequence
  children:
    - {id: config, type: configure_anritsu_sg, frequency: 1 GHz, power: -20 dBm}
    - {id: on, type: set_anritsu_sg_output, enabled: true}
    - {id: point, type: checkpoint, label: sg-on}
finally:
  - {id: off, type: set_anritsu_sg_output, enabled: false}
"""
        )
        plan = RecipeCompiler(settings).compile(recipe)
        anritsu = AnritsuAdapter(
            settings, session_factory=SimulatedVisaFactory("anritsu")
        )
        anritsu.connect()
        writer = MemoryWriter()
        result = RecipeRunner(
            rigol=RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol")),
            keithley=KeithleyAdapter(
                settings, session_factory=SimulatedVisaFactory("keithley")
            ),
            anritsu=anritsu,
            writer=writer,  # type: ignore[arg-type]
        ).run(plan)

        self.assertEqual(result.state, ApplicationState.SAFE)
        self.assertEqual(result.stored_points, 1)
        self.assertEqual(writer.status, "completed")
        self.assertEqual(anritsu.state, DeviceState.VERIFIED)
        self.assertIn("anritsu.rf_off_and_abort", plan.safe_shutdown_actions)

    def test_anritsu_signal_generator_output_requires_prior_configuration(self) -> None:
        raw = simulation_settings(approved=True).model_dump(mode="python")
        raw["devices"]["anritsu"]["signal_generator"] = {
            "control_protocol": "basic_scpi",
            "frequency": {"min": "250 kHz", "max": "3.6 GHz"},
            "power": {"min": "-100 dBm", "max": "0 dBm"},
        }
        raw["devices"]["anritsu"]["safety"]["signal_generator_output_allowed"] = True
        settings = StationSettings.model_validate(raw)
        recipe = parse_recipe_text(
            """
schema_version: 1
name: unsafe SG
root:
  id: root
  type: sequence
  children:
    - {id: on, type: set_anritsu_sg_output, enabled: true}
"""
        )
        with self.assertRaisesRegex(ConfigurationError, "requires an earlier configuration"):
            RecipeCompiler(settings).compile(recipe)

    def test_anritsu_rejects_frequency_outside_configured_limits(self) -> None:
        session = FakeVisaSession(responses={"*IDN?": "ANRITSU,MS2830A,123456,1.0"})
        adapter = AnritsuAdapter(self.settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        writes_before = len(session.writes)
        with self.assertRaises(SafetyViolation):
            adapter.configure_spectrum(SpectrumConfig(1e6, 101e9, 0, 101))
        self.assertEqual(len(session.writes), writes_before)

    def test_anritsu_sg_rejects_sweep_point_above_limit_before_scpi(self) -> None:
        raw = simulation_settings(approved=True).model_dump(mode="python")
        raw["devices"]["anritsu"]["signal_generator"] = {
            "control_protocol": "basic_scpi",
            "frequency": {"min": "250 kHz", "max": "3.6 GHz"},
            "power": {"min": "-100 dBm", "max": "0 dBm"},
        }
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession(
            responses={"*IDN?": "ANRITSU,MS2830A,123456,1.0"}
        )
        adapter = AnritsuAdapter(
            settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        writes_before = len(session.writes)

        with self.assertRaises(SafetyViolation):
            adapter.configure_signal_generator(
                SignalGeneratorConfig(3.600_000_001e9, -20.0)
            )

        self.assertEqual(len(session.writes), writes_before)

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

    def test_rigol_advanced_modes_force_output_off_and_reject_implicit_switching(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SYST:ERR?": "0,No error",
                ":OUTP1?": "OFF",
                ":SOUR1:FUNC?": "SIN",
                ":SOUR1:FREQ?": "1000",
                ":SOUR1:VOLT:HIGH?": "0.001",
                ":SOUR1:VOLT:LOW?": "-0.001",
                ":SOUR1:MOD?": lambda _command: next(
                    (write.rsplit(" ", 1)[-1] for write in reversed(session.writes) if write.startswith(":SOUR1:MOD ")),
                    "OFF",
                ),
                ":SOUR1:SWE:STAT?": lambda _command: next(
                    (write.rsplit(" ", 1)[-1] for write in reversed(session.writes) if write.startswith(":SOUR1:SWE:STAT ")),
                    "OFF",
                ),
                ":SOUR1:BURS:STAT?": lambda _command: next(
                    (write.rsplit(" ", 1)[-1] for write in reversed(session.writes) if write.startswith(":SOUR1:BURS ")),
                    "OFF",
                ),
                ":SOUR1:PHAS?": "0",
                ":SOUR1:HARM?": "OFF",
                ":COUP?": "FREQ:OFF,PHASE:OFF,AMPL:OFF",
                ":SOUR1:TRACK?": "OFF",
            }
        )
        adapter = RigolAdapter(self.settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_channel(
            RigolChannelConfig(
                1, "SIN", 1000, 0.001, -0.001
            )
        )
        adapter.configure_modulation(RigolModulationConfig(1, True, "AM", rate_hz=1000, parameter=50))
        with self.assertRaisesRegex(SafetyViolation, "modulation is active"):
            adapter.configure_frequency_sweep(
                RigolFrequencySweepConfig(1, True, 100, 1000, 1.0, steps=10)
            )
        adapter.configure_modulation(
            RigolModulationConfig(1, False, "AM", rate_hz=1000, parameter=50)
        )
        adapter.configure_frequency_sweep(
            RigolFrequencySweepConfig(1, True, 100, 1000, 1.0, steps=10)
        )
        with self.assertRaisesRegex(SafetyViolation, "frequency sweep is active"):
            adapter.configure_burst(
                RigolBurstConfig(1, True, cycles=3, period_s=0.01)
            )
        adapter.configure_frequency_sweep(
            RigolFrequencySweepConfig(1, False, 100, 1000, 1.0, steps=10)
        )
        adapter.configure_burst(RigolBurstConfig(1, True, cycles=3, period_s=0.01))
        self.assertIn(":OUTP1 OFF", session.writes)
        self.assertIn(":SOUR1:MOD:TYPE AM", session.writes)
        self.assertIn(":SOUR1:SWE:STAT ON", session.writes)
        self.assertIn(":SOUR1:BURS ON", session.writes)

    def test_rigol_external_modulation_does_not_write_internal_source_fields(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SYST:ERR?": "0,No error",
                ":OUTP1?": "OFF",
                ":SOUR1:FUNC?": "SIN",
                ":SOUR1:FREQ?": "1000",
                ":SOUR1:VOLT:HIGH?": "0.001",
                ":SOUR1:VOLT:LOW?": "-0.001",
                ":SOUR1:MOD?": lambda _command: next(
                    (write.rsplit(" ", 1)[-1] for write in reversed(session.writes) if write.startswith(":SOUR1:MOD ")),
                    "OFF",
                ),
                ":SOUR1:SWE:STAT?": "OFF",
                ":SOUR1:BURS:STAT?": "OFF",
                ":SOUR1:PHAS?": "0",
                ":SOUR1:HARM?": "OFF",
                ":COUP?": "FREQ:OFF,PHASE:OFF,AMPL:OFF",
                ":SOUR1:TRACK?": "OFF",
            }
        )
        adapter = RigolAdapter(self.settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_channel(
            RigolChannelConfig(
                1, "SIN", 1000, 0.001, -0.001
            )
        )

        adapter.configure_modulation(
            RigolModulationConfig(
                1,
                True,
                "AM",
                source="EXT",
                rate_hz=float("nan"),
                parameter=25,
                internal_shape="ARB",
            )
        )

        self.assertIn(":SOUR1:AM:SOUR EXT", session.writes)
        self.assertNotIn(":SOUR1:AM:INT:FREQ nan", session.writes)
        self.assertFalse(any(write.startswith(":SOUR1:AM:INT:FUNC") for write in session.writes))

    def test_rigol_advanced_modes_can_always_be_disabled_without_valid_form_values(self) -> None:
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

        adapter.configure_modulation(
            RigolModulationConfig(
                1, False, "AM", rate_hz=float("nan"), parameter=float("nan")
            )
        )
        adapter.configure_frequency_sweep(
            RigolFrequencySweepConfig(
                1, False, float("nan"), float("nan"), float("nan"), steps=-1
            )
        )
        adapter.configure_burst(
            RigolBurstConfig(
                1, False, cycles=-1, period_s=float("nan"), delay_s=-1
            )
        )

        self.assertIn(":SOUR1:MOD OFF", session.writes)
        self.assertIn(":SOUR1:SWE:STAT OFF", session.writes)
        self.assertIn(":SOUR1:BURS OFF", session.writes)

    def test_rigol_rejects_independent_channel_edit_when_coupling_or_track_is_active(self) -> None:
        for coupling, tracking, expected in (
            ("FREQ:OFF,PHASE:OFF,AMPL:ON", "OFF", "coupling is active"),
            ("FREQ:OFF,PHASE:OFF,AMPL:OFF", "INVERTED", "tracking is active"),
        ):
            with self.subTest(coupling=coupling, tracking=tracking):
                session = FakeVisaSession(
                    responses={
                        "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                        ":SOUR1:MOD?": "OFF",
                        ":SOUR1:SWE:STAT?": "OFF",
                        ":SOUR1:BURS:STAT?": "OFF",
                        ":SOUR1:PHAS?": "0",
                        ":SOUR1:HARM?": "OFF",
                        ":COUP?": coupling,
                        ":SOUR1:TRACK?": tracking,
                    }
                )
                adapter = RigolAdapter(
                    self.settings, session_factory=FakeVisaSessionFactory(session)
                )
                adapter.connect()
                writes_before = len(session.writes)

                with self.assertRaisesRegex(SafetyViolation, expected):
                    adapter.configure_channel(
                        RigolChannelConfig(
                            1,
                            "SIN",
                            1000,
                            0.001,
                            -0.001,
                        )
                    )

                self.assertNotIn(":SOUR1:FUNC SIN", session.writes[writes_before:])

    def test_rigol_failed_output_on_transaction_forces_and_confirms_both_outputs_off(self) -> None:
        raw = deepcopy(self.settings.model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession()

        def output_one(_command: str) -> str:
            # Simulate a failed ON readback. After the adapter sends the
            # emergency OFF command, the same query confirms OFF.
            last = next(
                (write for write in reversed(session.writes[:-1]) if write.startswith(":OUTP1 ")),
                ":OUTP1 OFF",
            )
            return last.rsplit(" ", 1)[-1]

        session.responses.update(
            {
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SYST:ERR?": "-100,Injected output failure",
                ":OUTP1?": output_one,
                ":OUTP2?": "OFF",
                ":SOUR1:FUNC?": "SIN",
                ":SOUR1:FREQ?": "1000",
                ":SOUR1:VOLT:HIGH?": "0.001",
                ":SOUR1:VOLT:LOW?": "-0.001",
                ":SOUR1:MOD?": "OFF",
                ":SOUR1:SWE:STAT?": "OFF",
                ":SOUR1:BURS:STAT?": "OFF",
                ":SOUR1:PHAS?": "0",
                ":SOUR1:HARM?": "OFF",
                ":COUP?": "FREQ:OFF,PHASE:OFF,AMPL:OFF",
                ":SOUR1:TRACK?": "OFF",
            }
        )
        adapter = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        # Configuration must see an empty error queue; inject the failure only
        # for the following OUTPUT ON transaction.
        session.responses[":SYST:ERR?"] = "0,No error"
        adapter.configure_channel(
            RigolChannelConfig(
                1, "SIN", 1000, 0.001, -0.001
            )
        )
        session.responses[":SYST:ERR?"] = "-100,Injected output failure"

        with self.assertRaisesRegex(DeviceError, "Rigol reported an error"):
            adapter.set_output(1, True)

        self.assertEqual(session.writes[-4:-2], [":OUTP1 OFF", ":OUTP2 OFF"])
        self.assertEqual(adapter.state, DeviceState.OUTPUT_OFF)

    def test_rigol_failed_live_frequency_readback_forces_outputs_off(self) -> None:
        raw = deepcopy(self.settings.model_dump(mode="python"))
        raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        session = FakeVisaSession()
        inject_mismatch = {"enabled": False}

        def output_one(_command: str) -> str:
            last = next(
                (write for write in reversed(session.writes[:-1]) if write.startswith(":OUTP1 ")),
                ":OUTP1 OFF",
            )
            return last.rsplit(" ", 1)[-1]

        session.responses.update(
            {
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SYST:ERR?": "0,No error",
                ":OUTP1?": output_one,
                ":OUTP2?": "OFF",
                ":SOUR1:FUNC?": "SIN",
                ":SOUR1:FREQ?": lambda _command: (
                    "1999" if inject_mismatch["enabled"] else "1000"
                ),
                ":SOUR1:VOLT:HIGH?": "0.001",
                ":SOUR1:VOLT:LOW?": "-0.001",
                ":SOUR1:MOD?": "OFF",
                ":SOUR1:SWE:STAT?": "OFF",
                ":SOUR1:BURS:STAT?": "OFF",
                ":SOUR1:PHAS?": "0",
                ":SOUR1:HARM?": "OFF",
                ":COUP?": "FREQ:OFF,PHASE:OFF,AMPL:OFF",
                ":SOUR1:TRACK?": "OFF",
            }
        )
        adapter = RigolAdapter(settings, session_factory=FakeVisaSessionFactory(session))
        adapter.connect()
        adapter.configure_channel(
            RigolChannelConfig(
                1, "SIN", 1000, 0.001, -0.001
            )
        )
        adapter.set_output(1, True)
        inject_mismatch["enabled"] = True

        with self.assertRaisesRegex(DeviceError, "frequency readback"):
            adapter.update_frequency(1, 2000)

        self.assertEqual(session.writes[-4:-2], [":OUTP1 OFF", ":OUTP2 OFF"])
        self.assertEqual(adapter.state, DeviceState.OUTPUT_OFF)

    def test_rigol_modulation_rejects_parameter_readback_mismatch(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08",
                ":SYST:ERR?": "0,No error",
                ":OUTP1?": "OFF",
                ":SOUR1:FUNC?": "SIN",
                ":SOUR1:FREQ?": "1000",
                ":SOUR1:VOLT:HIGH?": "0.001",
                ":SOUR1:VOLT:LOW?": "-0.001",
                ":SOUR1:MOD?": lambda _command: (
                    "ON" if ":SOUR1:MOD ON" in session.writes else "OFF"
                ),
                ":SOUR1:SWE:STAT?": "OFF",
                ":SOUR1:BURS:STAT?": "OFF",
                ":SOUR1:PHAS?": "0",
                ":SOUR1:HARM?": "OFF",
                ":COUP?": "FREQ:OFF,PHASE:OFF,AMPL:OFF",
                ":SOUR1:TRACK?": "OFF",
                ":SOUR1:AM:DEPT?": "49",
            }
        )
        adapter = RigolAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()
        adapter.configure_channel(
            RigolChannelConfig(
                1, "SIN", 1000, 0.001, -0.001
            )
        )

        with self.assertRaisesRegex(DeviceError, "modulation readback failed"):
            adapter.configure_modulation(
                RigolModulationConfig(
                    1, True, "AM", rate_hz=1000, parameter=50
                )
            )

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

    def test_runner_enforces_plan_owned_output_continuity_during_live_update(self) -> None:
        from app.devices.simulators import SimulatedVisaFactory

        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
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
        plan = ExecutionPlan(
            recipe_name="continuous-output",
            actions=(
                PlanAction(
                    "configure",
                    "configure_keithley",
                    {
                        "request": KeithleySourceRequest(
                            "B", "current", 0.0005, 0.067
                        )
                    },
                    {},
                ),
                PlanAction(
                    "on",
                    "set_keithley_output",
                    {"channel": "B", "enabled": True},
                    {},
                ),
                PlanAction(
                    "continuity",
                    "assert_output_on",
                    {
                        "device": "keithley",
                        "channel": "B",
                        "expected_state": {
                            "mode": "current",
                            "source_level_si": 0.0005,
                            "compliance_si": 0.067,
                        },
                    },
                    {},
                ),
                PlanAction(
                    "live-update",
                    "update_keithley_level",
                    {"channel": "B", "mode": "current", "level_si": 0.00075},
                    {},
                ),
            ),
            total_points=0,
            sha256="continuous-output",
            recipe_source="schema_version: 1\n",
            safe_shutdown_actions=("keithley.outputs_off",),
        )
        writer = MemoryWriter()

        result = RecipeRunner(
            rigol=rigol,
            keithley=keithley,
            anritsu=anritsu,
            writer=writer,
        ).run(plan)  # type: ignore[arg-type]

        self.assertIsNone(result.error)
        self.assertEqual(writer.status, "completed")
        self.assertFalse(keithley._output_states["B"])

    def test_runner_faults_safe_when_continuity_configuration_does_not_match(self) -> None:
        from app.devices.simulators import SimulatedVisaFactory

        raw = deepcopy(simulation_settings(approved=True).model_dump(mode="python"))
        raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
        settings = StationSettings.model_validate(raw)
        rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
        keithley = KeithleyAdapter(
            settings, session_factory=SimulatedVisaFactory("keithley")
        )
        anritsu = AnritsuAdapter(
            settings, session_factory=SimulatedVisaFactory("anritsu")
        )
        for device in (rigol, keithley, anritsu):
            device.connect()
        plan = ExecutionPlan(
            recipe_name="continuity-mismatch",
            actions=(
                PlanAction(
                    "configure",
                    "configure_keithley",
                    {
                        "request": KeithleySourceRequest(
                            "B", "current", 0.0005, 0.067
                        )
                    },
                    {},
                ),
                PlanAction(
                    "on",
                    "set_keithley_output",
                    {"channel": "B", "enabled": True},
                    {},
                ),
                PlanAction(
                    "continuity",
                    "assert_output_on",
                    {
                        "device": "keithley",
                        "channel": "B",
                        "expected_state": {
                            "mode": "current",
                            "source_level_si": 0.0006,
                            "compliance_si": 0.067,
                        },
                    },
                    {},
                ),
            ),
            total_points=0,
            sha256="continuity-mismatch",
            recipe_source="schema_version: 1\n",
            safe_shutdown_actions=("keithley.outputs_off",),
        )
        writer = MemoryWriter()

        result = RecipeRunner(
            rigol=rigol,
            keithley=keithley,
            anritsu=anritsu,
            writer=writer,
        ).run(plan)  # type: ignore[arg-type]

        self.assertIsNotNone(result.error)
        self.assertIn("different configuration", str(result.error))
        self.assertEqual(writer.status, "faulted")
        self.assertFalse(keithley._output_states["B"])

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
        self.assertIn("smub.sense = smub.SENSE_REMOTE", session.writes)
        self.assertNotIn("smub.sense = smub.SENSE_4WIRE", session.writes)

    def test_keithley_two_wire_uses_documented_2602a_local_sense_constant(self) -> None:
        session = FakeVisaSession(
            responses={
                "*IDN?": "KEITHLEY INSTRUMENTS,2602A,123456,1.0",
                "print(errorqueue.count)": "0",
            }
        )
        adapter = KeithleyAdapter(
            self.settings, session_factory=FakeVisaSessionFactory(session)
        )
        adapter.connect()

        adapter.configure_source(
            KeithleySourceRequest("B", "current", 0.001, 0.067)
        )

        self.assertIn("smub.sense = smub.SENSE_LOCAL", session.writes)
        self.assertNotIn("smub.sense = smub.SENSE_2WIRE", session.writes)

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
        self.assertIn("smub.sense = smub.SENSE_REMOTE", session.writes)
        self.assertNotIn("smub.sense = smub.SENSE_4WIRE", session.writes)
        self.assertNotIn("smub.source.rangev =", "\n".join(session.writes))
        with self.assertRaises(SafetyViolation):
            adapter.configure_source(
                KeithleySourceRequest("B", "measure_only", 0, 0, source_autorange=False, source_range_si=0.01)
            )

    def test_rigol_enables_output_after_configuration_and_readback(self) -> None:
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
        adapter.configure_channel(RigolChannelConfig(1, "SQU", 1000, .001, -.001))
        self.assertTrue(adapter.set_output(1, True))
        output_on_index = session.writes.index(":OUTP1 ON")
        for command in (
            ":OUTP1:LOAD INF",
            ":OUTP1:POL NORM",
            ":OUTP1:MODE NORM",
            ":OUTP1:SYNC OFF",
        ):
            self.assertIn(command, session.writes)
            self.assertLess(session.writes.index(command), output_on_index)

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
                PlanAction("rigol", "configure_rigol", {"config": RigolChannelConfig(1, "SQU", 1000, .001, -.001)}, {"rigol.1.high_level": .001}),
                PlanAction("measure", "measure_keithley", {"channel": "B"}, {}),
                PlanAction(
                    "trace",
                    "acquire_spectrum",
                    {"trace": "TRAC1", "average_count": 3},
                    {},
                ),
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
        stored_point, _stored_trace = writer.points[0]
        self.assertEqual(stored_point.metadata["spectrum_average_count"], 3)
        self.assertEqual(writer.events[-1][0], "run_completed")
        self.assertEqual(anritsu_session.writes.count("INIT:IMM"), 3)

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

    def test_pause_waits_for_checkpoint_and_shutdown_skips_unused_devices(self) -> None:
        from app.devices.simulators import SimulatedVisaFactory, simulated_station_settings

        settings = simulated_station_settings(loaded_settings())
        rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
        keithley = KeithleyAdapter(
            settings, session_factory=SimulatedVisaFactory("keithley")
        )
        anritsu = AnritsuAdapter(
            settings, session_factory=SimulatedVisaFactory("anritsu")
        )
        anritsu.connect()
        plan = ExecutionPlan(
            recipe_name="anritsu-only-pause",
            actions=(
                PlanAction(
                    "configure",
                    "configure_anritsu",
                    {"config": SpectrumConfig(1e6, 2e6, 0, 101)},
                    {},
                ),
                PlanAction("spectrum", "acquire_spectrum", {"trace": "TRAC1"}, {}),
            ),
            total_points=1,
            sha256="anritsu-only-pause",
            recipe_source="schema_version: 1\n",
            required_devices=frozenset({"anritsu"}),
        )
        writer = MemoryWriter()
        runner = RecipeRunner(
            rigol=rigol,
            keithley=keithley,
            anritsu=anritsu,
            writer=writer,
        )  # type: ignore[arg-type]
        results: list[object] = []
        runner.pause_after_point()
        thread = threading.Thread(target=lambda: results.append(runner.run(plan)), daemon=True)
        thread.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any(event[0] == "pause_pending" for event in writer.events):
                break
            time.sleep(0.01)

        self.assertEqual(len(writer.points), 1)
        self.assertTrue(thread.is_alive())
        self.assertEqual(rigol.state, DeviceState.DISCONNECTED)
        self.assertEqual(keithley.state, DeviceState.DISCONNECTED)

        runner.resume()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.state, ApplicationState.SAFE)
        self.assertEqual(writer.status, "completed")
        self.assertNotIn("shutdown_error", tuple(event[0] for event in writer.events))


if __name__ == "__main__":
    unittest.main()

