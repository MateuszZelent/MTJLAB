"""Tests for Keithley characterization runner and preflight safety verification."""

from __future__ import annotations

import math
import threading
from typing import Any
import pytest

from app.devices.keithley_2600 import KeithleySourceRequest
from app.devices.keithley_2600.adapter import KeithleyMeasurement
from app.devices.keithley_2600.characterization.models import (
    CharacterizationSweepConfig,
)
from app.devices.keithley_2600.characterization.runner import (
    KeithleyCharacterizationRunner,
)
from app.domain.errors import DeviceError, SafetyViolation
from tests.helpers import loaded_settings
from app.devices.simulators import simulated_station_settings


class _MockKeithleyDevice:
    """Mock device tracking call sequences and state."""

    def __init__(
        self,
        r_sample: float = 450.0,
        v_comp: float = 0.670,
        i_comp: float = 0.010,
        mode: str = "current",
    ) -> None:
        self.r_sample = r_sample
        self.v_comp = v_comp
        self.i_comp = i_comp
        self.mode = mode
        self.output_enabled = False
        self.current_level = 0.0
        self.calls: list[str] = []
        self.compliance_policy_state = "stop"
        self._last_request = KeithleySourceRequest(
            channel="A",
            mode=mode,
            level_si=0.0,
            compliance_si=v_comp if mode == "current" else i_comp,
            nplc=1.0,
            settle_time_s=0.001,
            sense_mode="2wire",
        )

    def compliance_policy(self, channel: str) -> str:
        return self.compliance_policy_state

    def set_compliance_policy(self, channel: str, stop_on_compliance: Any) -> None:
        if isinstance(stop_on_compliance, bool):
            self.compliance_policy_state = "stop" if stop_on_compliance else "warn_clamp"
        else:
            self.compliance_policy_state = str(stop_on_compliance)
        self.calls.append(f"set_compliance_policy:{self.compliance_policy_state}")

    def configure_source(self, request):
        self.mode = request.mode
        if self.mode == "current":
            self.v_comp = request.compliance_si
        else:
            self.i_comp = request.compliance_si
        self._last_request = request
        self.calls.append(f"configure_source:{request.channel}:{request.mode}:{request.compliance_si}")
        return request

    def last_source_request(self, channel: str):
        return self._last_request

    def set_output(self, channel: str, enabled: bool) -> None:
        self.output_enabled = enabled
        self.calls.append(f"set_output:{channel}:{enabled}")

    def update_source_level(self, channel: str, level_si: float | None = None, *, mode: str = "current", **kwargs) -> None:
        lvl = kwargs.get("level_si", level_si)
        if lvl is None:
            lvl = 0.0
        self.current_level = float(lvl)
        self.calls.append(f"update_source_level:{channel}:{self.current_level:.6e}")

    def measure(self, channel: str) -> KeithleyMeasurement:
        if self.mode == "current":
            v = self.current_level * self.r_sample
            comp = abs(v) >= self.v_comp
            if comp:
                v_meas = self.v_comp if v > 0 else -self.v_comp
                i_meas = v_meas / self.r_sample
            else:
                v_meas = v
                i_meas = self.current_level
        else:
            i = self.current_level / self.r_sample
            comp = abs(i) >= self.i_comp
            if comp:
                i_meas = self.i_comp if i > 0 else -self.i_comp
                v_meas = i_meas * self.r_sample
            else:
                v_meas = self.current_level
                i_meas = i

        return KeithleyMeasurement(
            channel=channel,
            voltage_v=v_meas,
            current_a=i_meas,
            power_w=abs(v_meas * i_meas),
            output_enabled=self.output_enabled,
            compliance_detected=comp,
            source_level_si=self.current_level,
            source_mode=self.mode,
        )

    def assert_output_state(self, channel: str, *, expected_enabled: bool) -> bool:
        self.calls.append(f"assert_output_state:{channel}:{expected_enabled}")
        if self.output_enabled != expected_enabled:
            raise RuntimeError("output state mismatch")
        return self.output_enabled

    def ramp_to_zero(self, channel: str) -> None:
        self.current_level = 0.0
        self.calls.append(f"ramp_to_zero:{channel}")


@pytest.fixture
def station_settings():
    return simulated_station_settings(loaded_settings())


def test_runner_preflight_limits(station_settings):
    """Verify preflight rejects setpoints that exceed lab limits."""
    # Valid config on enabled channel B (which has limit 0 mA to 10 mA, 10 mV to 67 mV compliance): 0 mA to 5 mA, 50 mV compliance
    valid_cfg = CharacterizationSweepConfig(
        channel="B",
        mode="current",
        start_level_si=0.000,
        stop_level_si=0.005,
        points_count=21,
        compliance_si=0.050,
    )
    # Should pass without exception
    KeithleyCharacterizationRunner.validate_preflight(valid_cfg, station_settings)

    # Negative current on Channel B (which has min 0 mA) must be rejected
    neg_cfg = CharacterizationSweepConfig(
        channel="B",
        mode="current",
        start_level_si=-0.005,
        stop_level_si=0.005,
        points_count=21,
        compliance_si=0.670,
    )
    with pytest.raises(SafetyViolation):
        KeithleyCharacterizationRunner.validate_preflight(neg_cfg, station_settings)

    # Exceeds max current (e.g. 500 mA while lab limit is 10 mA)
    excess_cfg = CharacterizationSweepConfig(
        channel="B",
        mode="current",
        start_level_si=0.001,
        stop_level_si=0.500,
        points_count=21,
        compliance_si=0.670,
    )
    with pytest.raises(SafetyViolation):
        KeithleyCharacterizationRunner.validate_preflight(excess_cfg, station_settings)

    # Exceeds compliance voltage (e.g. 50 V when limit is lower)
    excess_v_cfg = CharacterizationSweepConfig(
        channel="B",
        mode="current",
        start_level_si=0.001,
        stop_level_si=0.005,
        points_count=21,
        compliance_si=50.0,
    )
    with pytest.raises(SafetyViolation):
        KeithleyCharacterizationRunner.validate_preflight(excess_v_cfg, station_settings)

    # Points count < 2
    too_few_pts = CharacterizationSweepConfig(
        channel="B",
        mode="current",
        start_level_si=0.001,
        stop_level_si=0.005,
        points_count=1,
        compliance_si=0.050,
    )
    with pytest.raises(SafetyViolation, match="at least 2"):
        KeithleyCharacterizationRunner.validate_preflight(too_few_pts, station_settings)

    # Points count > sweep_points_max (e.g. 1500 > 1000)
    too_many_pts = CharacterizationSweepConfig(
        channel="B",
        mode="current",
        start_level_si=0.001,
        stop_level_si=0.005,
        points_count=1500,
        compliance_si=0.050,
    )
    with pytest.raises(SafetyViolation, match="exceeds station lab limit"):
        KeithleyCharacterizationRunner.validate_preflight(too_many_pts, station_settings)

    # Non-positive compliance
    zero_comp = CharacterizationSweepConfig(
        channel="B",
        mode="current",
        start_level_si=0.001,
        stop_level_si=0.005,
        points_count=21,
        compliance_si=0.0,
    )
    with pytest.raises(SafetyViolation, match="strictly positive"):
        KeithleyCharacterizationRunner.validate_preflight(zero_comp, station_settings)

    # Zero span (start == stop)
    zero_span = CharacterizationSweepConfig(
        channel="B",
        mode="current",
        start_level_si=0.001,
        stop_level_si=0.001,
        points_count=21,
        compliance_si=0.050,
    )
    with pytest.raises(SafetyViolation, match="cannot be identical"):
        KeithleyCharacterizationRunner.validate_preflight(zero_span, station_settings)


def test_runner_execution_and_shutdown():
    """Verify nominal sweep execution order and guaranteed shutdown."""
    device = _MockKeithleyDevice(r_sample=100.0)
    config = CharacterizationSweepConfig(
        channel="A",
        mode="current",
        start_level_si=-0.002,
        stop_level_si=0.002,
        points_count=5,
        compliance_si=0.670,
        dwell_time_s=0.001,
    )

    dataset = KeithleyCharacterizationRunner.run_sweep(device, config)

    assert len(dataset.points) == 4
    assert dataset.completion_status == "completed"
    # Output must be confirmed OFF after sweep
    assert device.output_enabled is False
    assert "ramp_to_zero:A" in device.calls
    assert "set_output:A:False" in device.calls
    assert device.calls.count("assert_output_state:A:True") == 4
    # Original compliance policy must be restored
    assert device.compliance_policy_state == "stop"


def test_runner_early_cancellation():
    """Verify that cancellation stops the sweep and still executes safe shutdown."""
    device = _MockKeithleyDevice(r_sample=50.0)
    config = CharacterizationSweepConfig(
        channel="A",
        mode="current",
        start_level_si=-0.010,
        stop_level_si=0.010,
        points_count=100,
        compliance_si=0.670,
        dwell_time_s=0.001,
    )
    cancel = threading.Event()

    def _cancel_after_first_point(pt):
        if pt.index >= 2:
            cancel.set()

    dataset = KeithleyCharacterizationRunner.run_sweep(
        device,
        config,
        cancel_event=cancel,
        on_point=_cancel_after_first_point,
    )

    # Should have stopped early (around 3 points)
    assert len(dataset.points) < 10
    assert dataset.completion_status == "cancelled"
    # Safe shutdown must still have executed
    assert device.output_enabled is False
    assert "ramp_to_zero:A" in device.calls
    assert "set_output:A:False" in device.calls


def test_runner_omits_zero_from_symmetric_current_sweep():
    """Zero current is neither applied nor recorded as a characterization point."""
    device = _MockKeithleyDevice(r_sample=100.0)
    config = CharacterizationSweepConfig(
        channel="A",
        mode="current",
        start_level_si=-10e-6,
        stop_level_si=10e-6,
        points_count=101,
        compliance_si=0.670,
        dwell_time_s=0.001,
    )

    dataset = KeithleyCharacterizationRunner.run_sweep(device, config)

    assert len(dataset.points) == 100
    assert dataset.zero_setpoint_omitted is True
    assert dataset.points[0].demanded_si == pytest.approx(-10e-6)
    assert dataset.points[-1].demanded_si == pytest.approx(10e-6)
    assert all(point.demanded_si != 0.0 for point in dataset.points)
    assert "update_source_level:A:0.000000e+00" not in device.calls
    assert "ramp_to_zero:A" in device.calls


def test_runner_voltage_mode_execution():
    """Verify voltage sweep records the compliance point and stops immediately."""
    device = _MockKeithleyDevice(r_sample=50.0, i_comp=0.010, mode="voltage")
    config = CharacterizationSweepConfig(
        channel="A",
        mode="voltage",
        start_level_si=-1.0,
        stop_level_si=1.0,
        points_count=5,
        compliance_si=0.010,  # 10 mA compliance
        dwell_time_s=0.001,
    )
    dataset = KeithleyCharacterizationRunner.run_sweep(device, config)
    assert len(dataset.points) == 1
    assert dataset.completion_status == "stopped_on_compliance"
    assert "point 1/4" in dataset.termination_detail
    assert device.output_enabled is False

    # The first demanded point is -1.0 V:
    # Sample has R=50 Ohm, current clamped at I=0.010 A.
    # Measured voltage = 0.010 * 50 = 0.5 V.
    # True R = V_meas / I_meas = 0.5 / 0.010 = 50 Ohm.
    # Apparent R = V_demanded / I_meas = 1.0 / 0.010 = 100 Ohm.
    pt_last = dataset.points[-1]
    assert pt_last.compliance_active is True
    assert math.isclose(pt_last.true_resistance_ohm, 50.0, rel_tol=0.01)
    assert math.isclose(pt_last.apparent_resistance_ohm, 100.0, rel_tol=0.01)


def test_runner_compliance_stops_before_next_setpoint_without_policy_change():
    """Compliance stores its point and stops without changing the shared policy."""
    device = _MockKeithleyDevice(mode="current")
    device.compliance_policy_state = "stop"
    config = CharacterizationSweepConfig(
        channel="A",
        mode="current",
        start_level_si=0.001,
        stop_level_si=0.003,
        points_count=3,
        compliance_si=0.670,
        dwell_time_s=0.001,
    )
    dataset = KeithleyCharacterizationRunner.run_sweep(device, config)
    assert len(dataset.points) == 2
    assert dataset.points[-1].compliance_active is True
    assert dataset.completion_status == "stopped_on_compliance"
    assert not any(call.startswith("set_compliance_policy:") for call in device.calls)
    assert "update_source_level:A:3.000000e-03" not in device.calls
    assert device.output_enabled is False
    assert device.compliance_policy_state == "stop"


def test_runner_reports_unconfirmed_output_off_as_failure():
    """The runner must not return a successful dataset when shutdown is unconfirmed."""

    class _OffFailureDevice(_MockKeithleyDevice):
        def set_output(self, channel: str, enabled: bool) -> None:
            if not enabled:
                raise RuntimeError("readback unavailable")
            super().set_output(channel, enabled)

    device = _OffFailureDevice(r_sample=100.0)
    config = CharacterizationSweepConfig(
        channel="A",
        mode="current",
        start_level_si=-0.001,
        stop_level_si=0.001,
        points_count=3,
        compliance_si=0.670,
        dwell_time_s=0.001,
    )

    with pytest.raises(DeviceError, match="OUTPUT OFF could not be confirmed"):
        KeithleyCharacterizationRunner.run_sweep(device, config)


def test_runner_rejects_adapter_policy_different_from_shared_stop(station_settings):
    """Characterization must not silently change a different normal-card policy."""
    from copy import deepcopy

    from app.devices.keithley_2600.adapter import KeithleyAdapter
    from app.devices.simulators import SimulatedVisaFactory
    from app.settings.models import StationSettings

    raw = deepcopy(station_settings.model_dump(mode="python"))
    raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
    raw["devices"]["keithley"]["safety"]["compliance_policy"] = "warn_clamp"
    raw["devices"]["keithley"]["safety"]["stop_on_compliance"] = False
    settings = StationSettings.model_validate(raw)
    device = KeithleyAdapter(
        settings,
        session_factory=SimulatedVisaFactory(
            "keithley", keithley_resistance_ohm=67.0
        ),
    )
    device.connect()
    config = CharacterizationSweepConfig(
        channel="B",
        mode="current",
        start_level_si=0.0005,
        stop_level_si=0.0015,
        points_count=3,
        compliance_si=0.067,
        dwell_time_s=0.0,
    )

    with pytest.raises(SafetyViolation, match="No characterization output was enabled"):
        KeithleyCharacterizationRunner.run_sweep(device, config)

    assert device._output_states["B"] is False
    assert device.compliance_policy("B") == "warn_clamp"


def test_characterization_initial_tsp_matches_normal_configuration(station_settings):
    """The complete pre-output TSP configuration must match the normal card path."""
    from copy import deepcopy

    from app.devices.keithley_2600 import KeithleySourceRequest
    from app.devices.keithley_2600.adapter import KeithleyAdapter
    from app.devices.simulators import KeithleySimulator
    from app.settings.models import StationSettings

    class _CapturingFactory:
        def __init__(self) -> None:
            self.session = KeithleySimulator()

        def open(self, resource: str, backend: str, timeout_ms: int):
            del resource, backend
            self.session.timeout = timeout_ms
            return self.session

    raw = deepcopy(station_settings.model_dump(mode="python"))
    raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
    raw["devices"]["keithley"]["safety"]["stop_on_compliance"] = True
    raw["devices"]["keithley"]["safety"]["compliance_policy"] = "stop"
    settings = StationSettings.model_validate(raw)
    normal_request = KeithleySourceRequest(
        channel="B",
        mode="current",
        level_si=0.0005,
        compliance_si=0.050,
        nplc=2.5,
        settle_time_s=0.0,
        sense_mode="2wire",
        source_autorange=False,
        source_range_si=0.010,
        measure_voltage_autorange=False,
        measure_voltage_range_si=0.100,
        measure_current_autorange=False,
        measure_current_range_si=0.010,
    )

    normal_factory = _CapturingFactory()
    normal_adapter = KeithleyAdapter(settings, session_factory=normal_factory)
    normal_adapter.connect()
    normal_start = len(normal_factory.session.commands)
    normal_adapter.configure_source(normal_request)
    normal_adapter.set_output("B", True)
    normal_traffic = normal_factory.session.commands[normal_start:]
    normal_output_on_index = normal_traffic.index(
        "smub.source.output = smub.OUTPUT_ON"
    )
    normal_commands = [
        command
        for command in normal_traffic[:normal_output_on_index]
        if " = " in command
        and ".source.output" not in command
        and ".source.offmode" not in command
    ]
    normal_adapter.set_output("B", False)

    config = CharacterizationSweepConfig(
        channel="B",
        mode="current",
        start_level_si=0.0005,
        stop_level_si=0.0006,
        points_count=2,
        compliance_si=0.050,
        dwell_time_s=0.0,
        nplc=2.5,
        sense_mode="2wire",
        source_autorange=False,
        source_range_si=0.010,
        measure_voltage_autorange=False,
        measure_voltage_range_si=0.100,
        measure_current_autorange=False,
        measure_current_range_si=0.010,
    )
    characterization_start = len(normal_factory.session.commands)
    dataset = KeithleyCharacterizationRunner.run_sweep(
        normal_adapter,
        config,
    )
    characterization_commands = normal_factory.session.commands[
        characterization_start:
    ]
    output_on_index = characterization_commands.index(
        "smub.source.output = smub.OUTPUT_ON"
    )
    characterization_programming_commands = [
        command
        for command in characterization_commands[:output_on_index]
        if " = " in command
        and ".source.output" not in command
        and ".source.offmode" not in command
    ]

    assert dataset.completion_status == "completed"
    assert characterization_programming_commands == normal_commands


def test_runner_positive_only_limits(station_settings):
    """Verify that a channel with positive min (e.g. 1 mA to 10 mA) does not falsely fail on 0.0."""
    from copy import deepcopy
    from app.settings.models import StationSettings
    raw = deepcopy(station_settings.model_dump(mode="python"))
    raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]["source_current"]["min"] = "1 mA"
    raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]["source_current"]["max"] = "10 mA"
    mod_settings = StationSettings.model_validate(raw)

    valid_pos_cfg = CharacterizationSweepConfig(
        channel="B",
        mode="current",
        start_level_si=0.002,
        stop_level_si=0.008,
        points_count=11,
        compliance_si=0.050,
    )
    # Must not raise SafetyViolation even though 0.0 < 1 mA
    KeithleyCharacterizationRunner.validate_preflight(valid_pos_cfg, mod_settings)


def test_characterization_config_defaults_are_safe_for_mtj():
    """Default config must use 2-wire sense and microampere sweep range to protect MTJ samples."""
    cfg = CharacterizationSweepConfig()
    assert cfg.sense_mode == "2wire"
    assert cfg.start_level_si == -100e-6
    assert cfg.stop_level_si == 100e-6
    assert cfg.compliance_si == 0.500
