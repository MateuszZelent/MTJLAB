"""Focused regression tests for backend, VISA safety, engine, and simulator audit fixes."""

from __future__ import annotations

import json
import math
import struct
from copy import deepcopy

import h5py
import pytest

from app.bootstrap import StationComposition
from app.devices.anritsu_ms2830a.adapter import (
    AnritsuAdapter,
    AnritsuConfigurationSnapshot,
    SpectrumConfig,
)
from app.devices.anritsu_ms2830a.averaging import instrument_average_count_for_duration
from app.devices.anritsu_ms2830a.module import MODULE as ANRITSU_MODULE
from app.devices.base import DeviceIdentity, InstrumentSession
from app.devices.simulators import AnritsuSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import DeviceError, ExecutionError, SafetyViolation
from app.domain.models import ApplicationState, DeviceState
from app.engine.compiler import RecipeCompiler
from app.engine.policy import ExecutionPolicy
from app.engine.recovery import RunRecoveryManager
from app.engine.runner import RecipeRunner
from app.recipes import parse_recipe_text
from app.safety.anritsu import validate_anritsu_advanced_spectrum
from app.settings.models import StationSettings
from app.storage.hdf5_writer import Hdf5RunWriter
from tests.helpers import simulation_settings


class _FakeVisaSession(InstrumentSession):
    """Minimal strict fake session that fails on unexpected SCPI queries."""

    def __init__(
        self,
        responses: dict[str, str | list[str]],
        *,
        fail_on_write: str | None = None,
    ) -> None:
        self._responses = {
            key: list(value) if isinstance(value, list) else value
            for key, value in responses.items()
        }
        self._fail_on_write = fail_on_write
        self.writes: list[str] = []
        self.queries: list[str] = []
        self.timeout: int = 5000
        self.closed = False

    def write(self, command: str) -> None:
        self.writes.append(command)
        if self._fail_on_write is not None and command.startswith(self._fail_on_write):
            raise DeviceError(f"Injected write failure on {command!r}")

    def query(self, command: str) -> str:
        self.queries.append(command)
        if command == "SYST:HARD:OPT:CAT?" and command not in self._responses:
            raise DeviceError("Hardware option catalogue unsupported on test fixture.")
        if command not in self._responses:
            raise AssertionError(f"Unexpected SCPI query: {command!r}")
        value = self._responses[command]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"Response queue exhausted for {command!r}")
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    def close(self) -> None:
        self.closed = True


def _qualified_settings(
    *,
    anritsu_enabled: bool = True,
    sg_enabled: bool = False,
    resource: str = "TCPIP0::192.168.1.5::INSTR",
) -> StationSettings:
    raw = deepcopy(simulation_settings(anritsu_enabled=anritsu_enabled).model_dump(mode="python"))
    raw["devices"]["anritsu"]["connection"]["resource"] = resource
    raw["devices"]["anritsu"]["advanced_spectrum"]["control_protocol"] = "standard_scpi"
    raw["devices"]["anritsu"]["advanced_spectrum"]["qualified_firmware"] = ["7.03.00", "sim-1.0"]
    if sg_enabled:
        raw["devices"]["anritsu"]["safety"]["signal_generator_output_allowed"] = True
        raw["devices"]["anritsu"]["signal_generator"].update(
            control_protocol="basic_scpi",
            frequency={"min": "100 MHz", "max": "6 GHz"},
            power={"min": "-100 dBm", "max": "0 dBm"},
        )
    return StationSettings.model_validate(raw)


# ---------------------------------------------------------------------------
# BUG-C2: VISA/SCPI regressions & Trace 2..6 handling
# ---------------------------------------------------------------------------


def test_instrument_average_allows_preexisting_count_one_and_restores_it() -> None:
    settings = _qualified_settings()
    sim = AnritsuSimulator()
    sim.average_count = 1
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(sim))
    adapter.connect()
    try:
        with pytest.raises(SafetyViolation):
            adapter.acquire_instrument_average(sweep_count=1, acquisition_timeout_s=30.0)
        trace, count, _sweep_time = adapter.acquire_instrument_average(
            sweep_count=4, acquisition_timeout_s=30.0
        )
        assert count == 4
        assert trace.instrument_average is not None
        assert trace.instrument_average.requested_sweep_count == 4
        assert trace.instrument_average.sweep_count == 4
        assert sim.average_count == 1
    finally:
        adapter.disconnect()


def test_instrument_average_temporarily_disables_and_restores_other_traces() -> None:
    settings = _qualified_settings()
    sim = AnritsuSimulator()
    initial_modes = ["OFF", "MAXH", "AVER", "OFF", "MINH", "OFF"]
    sim.trace_storage_modes = list(initial_modes)
    observed_during_sweep: list[list[str]] = []
    original_write = sim._write

    def spy_write(command: str) -> None:
        if command.upper() == "INIT:MODE:SING":
            observed_during_sweep.append(list(sim.trace_storage_modes))
        original_write(command)

    sim._write = spy_write
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(sim))
    adapter.connect()
    try:
        trace, count, _sweep_time = adapter.acquire_instrument_average(
            sweep_count=3, acquisition_timeout_s=30.0
        )
        assert count == 3
        assert trace.instrument_average is not None
        assert observed_during_sweep == [["LAV", "OFF", "OFF", "OFF", "OFF", "OFF"]]
        assert sim.trace_storage_modes == initial_modes
    finally:
        adapter.disconnect()


def test_unqualified_firmware_gates_trace_protocol_queries() -> None:
    settings = simulation_settings(anritsu_enabled=True)
    fake = _FakeVisaSession(
        {
            "*IDN?": "ANRITSU,MS2830A,SN1,1.0",
            "SYST:LANG?": "SCPI",
            "INST?": "SPECT",
            "SYST:ERR?": '0,"No error"',
            "FORM?": "ASC,0",
            "FREQ:STAR?": "1000000",
            "FREQ:STOP?": "2000000",
            "SWE:POIN?": "3",
            "TRAC? TRAC1": "-80,-75,-70",
        }
    )
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(fake))
    adapter.connect()
    try:
        with pytest.raises(SafetyViolation, match="unverified"):
            adapter.acquire_instrument_average(sweep_count=2, acquisition_timeout_s=30.0)
        assert not any("STOR:MODE" in query or "AVER:COUN" in query for query in fake.queries)
        trace = adapter.fetch_trace()
        assert len(trace.powers_dbm) == 3
        assert "DISP:WIND:TRAC:Y:SPAC?" not in fake.queries
    finally:
        adapter.disconnect()


def test_passive_live_preview_with_acquisition_disabled_is_readonly() -> None:
    settings = simulation_settings(anritsu_enabled=False)
    fake = _FakeVisaSession(
        {
            "*IDN?": "ANRITSU,MS2830A,SN1,1.0",
            "SYST:LANG?": "SCPI",
            "INST?": "SPECT",
            "SYST:ERR?": '0,"No error"',
            "FORM?": "ASC,0",
            "DISP:WIND:TRAC:Y:RLEV?": "0",
            "FREQ:STAR?": "1000000",
            "FREQ:STOP?": "2000000",
            "SWE:POIN?": "101",
            "TRAC? TRAC1": ",".join(["-80"] * 101),
        }
    )
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(fake))
    adapter.connect()
    try:
        adapter.start_live(ensure_continuous=False)
        trace = adapter.fetch_current_trace()
        assert len(trace.powers_dbm) == 101
        assert fake.writes == []
        adapter.stop_live()
        # BUG-H8: outside passive Live preview, direct fetch_current_trace must fail closed.
        with pytest.raises(SafetyViolation, match="acquisition"):
            adapter.fetch_current_trace()
    finally:
        adapter.disconnect()


# ---------------------------------------------------------------------------
# BUG-C3: configure_spectrum bandwidth validation, shift ordering, UNKNOWN state
# ---------------------------------------------------------------------------


def test_configure_spectrum_validates_bandwidth_before_writing() -> None:
    # 1. Unqualified firmware must reject bandwidth writes before any SCPI write.
    unqualified = simulation_settings(anritsu_enabled=True)
    fake = _FakeVisaSession(
        {
            "*IDN?": "ANRITSU,MS2830A,SN1,1.0",
            "SYST:LANG?": "SCPI",
            "SYST:ERR?": '0,"No error"',
        }
    )
    adapter = AnritsuAdapter(unqualified, session_factory=FakeVisaSessionFactory(fake))
    adapter.connect()
    try:
        with pytest.raises(SafetyViolation, match="unverified|qualified"):
            adapter.configure_spectrum(
                SpectrumConfig(
                    start_hz=1e6,
                    stop_hz=10e6,
                    reference_level_dbm=0.0,
                    points=101,
                    rbw_auto=False,
                    rbw_hz=1e3,
                )
            )
        assert fake.writes == []
    finally:
        adapter.disconnect()

    # 2. Qualified firmware validates NaN/negative RBW before any SCPI write, and verifies readback.
    qualified = _qualified_settings()
    sim = AnritsuSimulator()
    sim_adapter = AnritsuAdapter(qualified, session_factory=FakeVisaSessionFactory(sim))
    sim_adapter.connect()
    try:
        sim.commands.clear()
        with pytest.raises(SafetyViolation):
            sim_adapter.configure_spectrum(
                SpectrumConfig(
                    start_hz=1e6,
                    stop_hz=10e6,
                    reference_level_dbm=0.0,
                    points=101,
                    rbw_auto=False,
                    rbw_hz=float("nan"),
                )
            )
        assert sim.commands == []
        applied = sim_adapter.configure_spectrum(
            SpectrumConfig(
                start_hz=1e6,
                stop_hz=10e6,
                reference_level_dbm=0.0,
                points=101,
                rbw_auto=False,
                rbw_hz=30e3,
                vbw_auto=False,
                vbw_hz=10e3,
            )
        )
        assert applied.points == 101
        assert "BAND:AUTO?" in sim.commands
        assert "BAND?" in sim.commands
        assert "BAND:VID:AUTO?" in sim.commands
        assert "BAND:VID?" in sim.commands
    finally:
        sim_adapter.disconnect()


def test_configure_spectrum_full_upward_shift_orders_stop_before_start() -> None:
    settings = _qualified_settings()
    sim = AnritsuSimulator()
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(sim))
    adapter.connect()
    try:
        # Simulator starts at [1 MHz, 10 MHz]; shifting to [20 MHz, 30 MHz] with changed_fields=None
        # must write FREQ:STOP before FREQ:STAR.
        sim.commands.clear()
        applied = adapter.configure_spectrum(
            SpectrumConfig(
                start_hz=20e6,
                stop_hz=30e6,
                reference_level_dbm=-5.0,
                points=101,
                changed_fields=None,
            )
        )
        assert applied.start_hz == 20e6
        assert applied.stop_hz == 30e6
        stop_idx = sim.commands.index("FREQ:STOP 30000000HZ")
        start_idx = sim.commands.index("FREQ:STAR 20000000HZ")
        assert stop_idx < start_idx
    finally:
        adapter.disconnect()


def test_configure_spectrum_mid_write_failure_marks_device_unknown() -> None:
    settings = simulation_settings(anritsu_enabled=True)
    fake = _FakeVisaSession(
        {
            "*IDN?": "ANRITSU,MS2830A,SN1,7.03.00",
            "SYST:LANG?": "SCPI",
            "INST?": "SPECT",
            "FREQ:STOP?": "10000000",
            "SYST:ERR?": '0,"No error"',
        },
        fail_on_write="SWE:POIN",
    )
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(fake))
    adapter.connect()
    try:
        assert adapter.state == DeviceState.VERIFIED
        with pytest.raises(DeviceError, match="Injected write failure"):
            adapter.configure_spectrum(
                SpectrumConfig(start_hz=1e6, stop_hz=5e6, reference_level_dbm=0.0, points=101)
            )
        assert adapter.state == DeviceState.UNKNOWN
    finally:
        adapter.disconnect()


# ---------------------------------------------------------------------------
# BUG-H8: ASCII trace generation tracking & non-positive start rejection
# ---------------------------------------------------------------------------


def test_ascii_trace_carries_configuration_generation_and_rejects_non_positive_start() -> None:
    settings = simulation_settings(anritsu_enabled=True)
    sim = AnritsuSimulator()
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(sim))
    adapter.connect()
    try:
        adapter.configure_spectrum(
            SpectrumConfig(start_hz=2e6, stop_hz=8e6, reference_level_dbm=0.0, points=101)
        )
        expected_gen = adapter._configuration_generation
        trace = adapter.fetch_trace()
        assert trace.configuration_generation == expected_gen
    finally:
        adapter.disconnect()

    bad = _FakeVisaSession(
        {
            "*IDN?": "ANRITSU,MS2830A,SN1,1.0",
            "SYST:LANG?": "SCPI",
            "INST?": "SPECT",
            "SYST:ERR?": '0,"No error"',
            "FORM?": "ASC,0",
            "FREQ:STAR?": "0",
            "FREQ:STOP?": "2000000",
            "SWE:POIN?": "3",
            "TRAC? TRAC1": "-80,-75,-70",
        }
    )
    bad_adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(bad))
    bad_adapter.connect()
    try:
        with pytest.raises(DeviceError, match="frequency axis"):
            bad_adapter.fetch_trace()
    finally:
        bad_adapter.disconnect()


# ---------------------------------------------------------------------------
# BUG-H9: Atomic SG -> Spectrum RF-OFF transition proof (compiler + adapter + runner)
# ---------------------------------------------------------------------------


def test_sg_to_spectrum_recipe_physically_confirms_rf_off_and_records_audit(tmp_path) -> None:
    settings = _qualified_settings(anritsu_enabled=True, sg_enabled=True)
    recipe_yaml = """schema_version: 1
name: sg_to_spectrum_rf_off
root:
  id: root
  type: sequence
  children:
    - {id: sg_cfg, type: configure_anritsu_sg, frequency: '1 GHz', power: '-30 dBm'}
    - {id: sg_on, type: set_anritsu_sg_output, enabled: true}
    - {id: sg_off, type: set_anritsu_sg_output, enabled: false}
    - {id: sa_cfg, type: configure_anritsu, start_frequency: '1 MHz', stop_frequency: '10 MHz', reference_level: '0 dBm', points: 101}
    - {id: sweep, type: acquire_spectrum}
"""
    recipe = parse_recipe_text(recipe_yaml)
    plan = RecipeCompiler(settings).compile(recipe)
    sa_action = next(action for action in plan.actions if action.kind == "configure_anritsu")
    # Compiler cleared changed_fields for post-SG transition.
    assert sa_action.payload["config"].changed_fields is None

    composition = StationComposition(settings, simulation=True)
    anritsu = composition.create_adapter("anritsu")
    rigol = composition.create_adapter("rigol")
    keithley = composition.create_adapter("keithley")
    anritsu.connect()
    try:
        session = anritsu._require_session()
        events: list[tuple[str, dict[str, object]]] = []
        writer = Hdf5RunWriter(
            tmp_path / "sg_to_sa.h5",
            recipe_source=recipe_yaml,
            settings_source="test",
            plan_hash=plan.sha256,
            device_idn={"anritsu": anritsu.identity.idn},
            expected_points=plan.total_points,
        )
        runner = RecipeRunner(
            rigol=rigol,
            keithley=keithley,
            anritsu=anritsu,
            writer=writer,
            on_event=lambda name, payload: events.append((name, payload)),
            policy=ExecutionPolicy.from_settings(settings),
        )
        result = runner.run(plan)
        assert result.error is None
        assert result.state == ApplicationState.SAFE
        assert result.stored_points == 1
        # Verify physical RF-OFF commands occurred before entering SPECT mode.
        assert "INST SG" in session.commands
        assert "OUTP 0" in session.commands
        assert "OUTP?" in session.commands
        assert "INST SPECT" in session.commands
        sa_finished = [
            payload
            for name, payload in events
            if name == "action_finished" and payload.get("kind") == "configure_anritsu"
        ]
        assert sa_finished
        snapshot = sa_finished[0]["state_snapshot"]
        assert snapshot["output_status"]["anritsu.sg"] == "off"
    finally:
        anritsu.disconnect()


def test_sg_to_spectrum_refuses_rf_off_confirmation_when_outp_stays_on(tmp_path) -> None:
    settings = _qualified_settings(anritsu_enabled=True, sg_enabled=True)
    recipe_yaml = """schema_version: 1
name: sg_stuck_on
root:
  id: root
  type: sequence
  children:
    - {id: sg_cfg, type: configure_anritsu_sg, frequency: '1 GHz', power: '-30 dBm'}
    - {id: sa_cfg, type: configure_anritsu, start_frequency: '1 MHz', stop_frequency: '10 MHz', reference_level: '0 dBm', points: 101}
"""
    recipe = parse_recipe_text(recipe_yaml)
    plan = RecipeCompiler(settings).compile(recipe)

    composition = StationComposition(settings, simulation=True)
    anritsu = composition.create_adapter("anritsu")
    rigol = composition.create_adapter("rigol")
    keithley = composition.create_adapter("keithley")
    anritsu.connect()
    try:
        session = anritsu._require_session()
        original_query = session._query
        in_sa_transition = False

        def stuck_on_query(command: str) -> str:
            nonlocal in_sa_transition
            if command == "OUTP?" and in_sa_transition:
                return "1"
            return original_query(command)

        original_configure = anritsu.configure_spectrum

        def wrapped_configure(config, **kwargs):
            nonlocal in_sa_transition
            in_sa_transition = True
            return original_configure(config, **kwargs)

        session._query = stuck_on_query
        anritsu.configure_spectrum = wrapped_configure  # type: ignore[method-assign]
        events: list[tuple[str, dict[str, object]]] = []
        writer = Hdf5RunWriter(
            tmp_path / "sg_stuck_on.h5",
            recipe_source=recipe_yaml,
            settings_source="test",
            plan_hash=plan.sha256,
            device_idn={"anritsu": anritsu.identity.idn},
            expected_points=plan.total_points,
        )
        runner = RecipeRunner(
            rigol=rigol,
            keithley=keithley,
            anritsu=anritsu,
            writer=writer,
            on_event=lambda name, payload: events.append((name, payload)),
            policy=ExecutionPolicy.from_settings(settings),
        )
        result = runner.run(plan)
        assert result.state == ApplicationState.FAULT
        assert result.error is not None
        assert "RF OUTPUT OFF" in result.error or "RF OFF" in result.error
        assert not any(
            name == "action_finished" and payload.get("kind") == "configure_anritsu"
            for name, payload in events
        )
    finally:
        session._query = original_query
        anritsu.disconnect()


def test_partial_configure_anritsu_never_falsely_confirms_rf_off() -> None:
    settings = _qualified_settings(anritsu_enabled=True, sg_enabled=True)
    sim = AnritsuSimulator()
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(sim))
    adapter.connect()
    try:
        partial = SpectrumConfig(
            start_hz=1e6,
            stop_hz=10e6,
            reference_level_dbm=-5.0,
            points=1001,
            changed_fields=("reference_level_dbm",),
        )
        with pytest.raises(SafetyViolation, match="complete Spectrum Analyzer configuration"):
            adapter.configure_spectrum(partial, require_rf_off_proof=True)
        applied = adapter.configure_spectrum(partial)
        assert isinstance(applied, AnritsuConfigurationSnapshot)
        assert applied.rf_off_confirmed is False
    finally:
        adapter.disconnect()


# ---------------------------------------------------------------------------
# BUG-H10: Recovery event-log compatibility & partial-corruption fail-closed
# ---------------------------------------------------------------------------


def test_recovery_rejects_half_corrupted_event_log_and_accepts_minimal_legacy(tmp_path) -> None:
    settings = simulation_settings()
    recipe_yaml = """schema_version: 1
name: checkpoint_recovery
root:
  id: root
  type: sequence
  children:
    - {id: cp, type: checkpoint}
"""
    plan = RecipeCompiler(settings).compile(parse_recipe_text(recipe_yaml))

    # 1. Minimal 2-column legacy archive with trailing partial row still drops tail safely.
    minimal = tmp_path / "minimal.h5"
    with h5py.File(minimal, "w") as h5:
        run = h5.create_group("run")
        run.attrs.update(plan_sha256=plan.sha256, status="aborted")
        h5.create_group("points/0").attrs["complete"] = True
        events = h5.create_group("events")
        events.create_dataset(
            "name",
            data=["safe_resume_boundary", "safe_resume_boundary"],
            dtype=h5py.string_dtype(),
        )
        events.create_dataset(
            "message",
            data=[
                json.dumps({"stored_points": 1, "next_action_index": 1, "plan_sha256": plan.sha256}),
                "{truncated",
            ],
            dtype=h5py.string_dtype(),
        )
    assert RunRecoveryManager().inspect(minimal, plan).stored_points == 1

    # 2. Half-corrupted 4-column archive (has timestamp, missing severity) must fail closed.
    corrupt = tmp_path / "corrupt.h5"
    with h5py.File(corrupt, "w") as h5:
        run = h5.create_group("run")
        run.attrs.update(plan_sha256=plan.sha256, status="aborted")
        h5.create_group("points/0").attrs["complete"] = True
        events = h5.create_group("events")
        events.create_dataset("timestamp", data=["2026-01-01T00:00:00Z"], dtype=h5py.string_dtype())
        events.create_dataset("name", data=["safe_resume_boundary"], dtype=h5py.string_dtype())
        events.create_dataset(
            "message",
            data=[json.dumps({"stored_points": 1, "next_action_index": 1, "plan_sha256": plan.sha256})],
            dtype=h5py.string_dtype(),
        )
    with pytest.raises(ExecutionError, match="Invalid recovery event log"):
        RunRecoveryManager().inspect(corrupt, plan)


# ---------------------------------------------------------------------------
# BUG-M4: Physical quantity / finiteness / step-tolerance validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("duration_s", "sweep_time_s"),
    [
        (0.0, 0.1),
        (-1.0, 0.1),
        (float("nan"), 0.1),
        (float("inf"), 0.1),
        (1.0, 0.0),
        (1.0, -0.1),
        (1.0, float("nan")),
        (1.0, float("inf")),
        (True, 0.1),
        (1.0, True),
    ],
)
def test_averaging_duration_rejects_non_positive_non_finite_and_bool(
    duration_s: float, sweep_time_s: float
) -> None:
    with pytest.raises(SafetyViolation):
        instrument_average_count_for_duration(duration_s, sweep_time_s)


def test_anritsu_attenuation_float_tolerance_and_non_finite_auto_rejection() -> None:
    settings = _qualified_settings().anritsu
    # 10.0 - 1e-12 is within 1e-9 of a valid 2 dB step and must be accepted.
    validate_anritsu_advanced_spectrum(
        settings,
        rbw_auto=None,
        rbw_hz=None,
        vbw_mode=None,
        vbw_hz=None,
        detector=None,
        attenuation_auto=False,
        attenuation_db=10.0 - 1e-12,
        preamplifier_enabled=False,
        sweep_time_auto=None,
        sweep_time_s=None,
        hardware_options=(),
    )
    # Non-finite attenuation must be rejected even when attenuation_auto=True.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(SafetyViolation, match="finite"):
            validate_anritsu_advanced_spectrum(
                settings,
                rbw_auto=None,
                rbw_hz=None,
                vbw_mode=None,
                vbw_hz=None,
                detector=None,
                attenuation_auto=True,
                attenuation_db=bad,
                preamplifier_enabled=False,
                sweep_time_auto=None,
                sweep_time_s=None,
                hardware_options=(),
            )


@pytest.mark.parametrize(
    ("bad_query", "bad_value"),
    [
        ("BAND?", "0"),
        ("BAND?", "nan"),
        ("BAND:VID?", "-100"),
        ("SWE:TIME?", "0"),
        ("SWE:TIME?", "inf"),
        ("POW:ATT?", "nan"),
        ("POW:ATT?", "-2"),
    ],
)
def test_read_full_configuration_rejects_non_positive_or_non_finite_values(
    bad_query: str, bad_value: str
) -> None:
    settings = simulation_settings(anritsu_enabled=True)
    responses = {
        "*IDN?": "ANRITSU,MS2830A,SN1,7.03.00",
        "SYST:LANG?": "SCPI",
        "INST?": "SPECT",
        "FREQ:STAR?": "1000000",
        "FREQ:STOP?": "10000000",
        "DISP:WIND:TRAC:Y:RLEV?": "0",
        "SWE:POIN?": "101",
        "BAND:AUTO?": "1",
        "BAND?": "1000",
        "BAND:VID:AUTO?": "1",
        "BAND:VID?": "1000",
        "BAND:VID:MODE?": "VID",
        "DET?": "NORM",
        "POW:ATT:AUTO?": "1",
        "POW:ATT?": "10",
        "SWE:TIME:AUTO?": "1",
        "SWE:TIME?": "0.1",
        "INIT:CONT?": "1",
        "AVER:COUN?": "10",
    }
    responses[bad_query] = bad_value
    fake = _FakeVisaSession(responses)
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(fake))
    adapter.connect()
    try:
        with pytest.raises(DeviceError):
            adapter.read_full_configuration()
    finally:
        adapter.disconnect()


# ---------------------------------------------------------------------------
# BUG-M5: Preserve primary error on cleanup failure & explicit restore_continuous
# ---------------------------------------------------------------------------


def test_acquire_unaveraged_sweep_preserves_primary_error_when_restore_fails(monkeypatch) -> None:
    settings = _qualified_settings()
    sim = AnritsuSimulator()
    sim.trace_storage_modes[0] = "AVER"
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(sim))
    adapter.connect()
    try:
        monkeypatch.setattr(
            adapter,
            "acquire_single_sweep",
            lambda *_a, **_kw: (_ for _ in ()).throw(DeviceError("primary sweep timeout")),
        )
        monkeypatch.setattr(
            adapter,
            "_restore_trace_storage_modes",
            lambda *_a, **_kw: (_ for _ in ()).throw(DeviceError("restore storage bus error")),
        )
        with pytest.raises(DeviceError, match="primary sweep timeout") as exc_info:
            adapter.acquire_unaveraged_sweep("TRAC1")
        assert "restore storage bus error" in str(exc_info.value)
        assert exc_info.value.__cause__ is not None
        assert "primary sweep timeout" in str(exc_info.value.__cause__)
        notes = getattr(exc_info.value.__cause__, "__notes__", [])
        assert any("restore storage bus error" in note for note in notes)
    finally:
        adapter.disconnect()


def test_module_single_sweep_supports_explicit_restore_continuous() -> None:
    settings = _qualified_settings()
    sim = AnritsuSimulator()
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(sim))
    adapter.connect()
    try:
        # With instrument initially in Continuous mode, explicit restore_continuous=True restores it.
        sim.continuous_sweep = True
        sim.commands.clear()
        ANRITSU_MODULE.dispatch(adapter, "single_sweep", ("TRAC1", True))
        assert "INIT:MODE:CONT" in sim.commands
        assert sim.continuous_sweep is True

        # Default string payload keeps restore_continuous=False (does not restart Continuous).
        sim.continuous_sweep = True
        sim.commands.clear()
        ANRITSU_MODULE.dispatch(adapter, "single_sweep", "TRAC1")
        assert "INIT:MODE:CONT" not in sim.commands
    finally:
        adapter.disconnect()


# ---------------------------------------------------------------------------
# BUG-L4: Simulator validation tightening
# ---------------------------------------------------------------------------


def test_anritsu_simulator_rejects_invalid_frequency_bandwidth_and_points() -> None:
    sim = AnritsuSimulator()
    # Default range is [1 MHz, 10 MHz].
    with pytest.raises(DeviceError, match="start frequency"):
        sim.write("FREQ:STAR 10000000HZ")
    with pytest.raises(DeviceError, match="stop frequency"):
        sim.write("FREQ:STOP 1000000HZ")
    with pytest.raises(DeviceError, match="invalid frequency"):
        sim.write("FREQ:STAR 0HZ")
    with pytest.raises(DeviceError, match="invalid RBW"):
        sim.write("BAND 0HZ")
    with pytest.raises(DeviceError, match="invalid VBW"):
        sim.write("BAND:VID -100HZ")
    with pytest.raises(DeviceError, match="sweep points"):
        sim.write("SWE:POIN 1")
    # Valid small point counts like 5 (used by simulation context tests) remain accepted.
    sim.write("SWE:POIN 5")
    assert sim.points == 5


# ---------------------------------------------------------------------------
# PERF-H2 & PERF-L1: Binary decoding via numpy, grouped queries, opt-in binary sweep
# ---------------------------------------------------------------------------


def test_decode_binary_trace_handles_raw_bytes_via_numpy() -> None:
    settings = _qualified_settings()
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(AnritsuSimulator()))
    adapter._configuration_generation = 7
    payload = struct.pack("<3f", -80.5, -70.25, -60.0)
    trace = adapter._decode_binary_trace("TRAC1", memoryview(payload), (3, 1e6, 3e6))
    assert trace.configuration_generation == 7
    assert trace.frequencies_hz == (1e6, 2e6, 3e6)
    assert math.isclose(trace.powers_dbm[0], -80.5, abs_tol=1e-5)
    assert math.isclose(trace.powers_dbm[1], -70.25, abs_tol=1e-5)
    assert math.isclose(trace.powers_dbm[2], -60.0, abs_tol=1e-5)


@pytest.mark.parametrize(
    ("resource", "firmware", "expected"),
    [
        ("GPIB0::18::INSTR", "7.03.00", True),
        ("TCPIP0::192.168.1.5::INSTR", "7.03.00", True),
        ("USB0::0x0B5B::0x0006::SN::INSTR", "7.03.00", True),
        ("TCPIP0::192.168.1.5::INSTR", "1.0", False),
        ("ASRL1::INSTR", "7.03.00", False),
    ],
)
def test_grouped_native_queries_enabled_on_tcpip_and_usb_for_70300(
    resource: str, firmware: str, expected: bool
) -> None:
    settings = _qualified_settings(resource=resource)
    sim = AnritsuSimulator()
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(sim))
    adapter._identity = DeviceIdentity(
        manufacturer="ANRITSU",
        model="MS2830A",
        serial="SN1",
        firmware=firmware,
        resource=resource,
        idn=f"ANRITSU,MS2830A,SN1,{firmware}",
    )
    assert adapter._supports_grouped_native_queries() is expected


def test_acquire_unaveraged_sweep_opt_in_binary_transfer() -> None:
    settings = _qualified_settings()
    sim = AnritsuSimulator()
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(sim))
    adapter.connect()
    try:
        trace = adapter.acquire_unaveraged_sweep("TRAC1", binary=True)
        assert len(trace.powers_dbm) == 1001
        assert trace.trace_name == "TRAC1"
    finally:
        adapter.disconnect()

    unqualified = simulation_settings(anritsu_enabled=True)
    fake = _FakeVisaSession(
        {
            "*IDN?": "ANRITSU,MS2830A,SN1,1.0",
            "SYST:LANG?": "SCPI",
            "INST?": "SPECT",
            "SYST:ERR?": '0,"No error"',
        }
    )
    bad_adapter = AnritsuAdapter(unqualified, session_factory=FakeVisaSessionFactory(fake))
    bad_adapter.connect()
    try:
        with pytest.raises(SafetyViolation, match="unverified"):
            bad_adapter.acquire_unaveraged_sweep("TRAC1", binary=True)
    finally:
        bad_adapter.disconnect()
