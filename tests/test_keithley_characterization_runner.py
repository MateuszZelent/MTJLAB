"""Tests for Keithley characterization runner and preflight safety verification."""

from __future__ import annotations

import math
import threading
import pytest

from app.devices.keithley_2600.adapter import KeithleyMeasurement
from app.devices.keithley_2600.characterization.models import (
    CharacterizationSweepConfig,
)
from app.devices.keithley_2600.characterization.runner import (
    KeithleyCharacterizationRunner,
)
from app.domain.errors import SafetyViolation
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

    def compliance_policy(self, channel: str) -> str:
        return self.compliance_policy_state

    def set_compliance_policy(self, channel: str, stop_on_compliance: bool) -> None:
        self.compliance_policy_state = "stop" if stop_on_compliance else "warn_clamp"
        self.calls.append(f"set_compliance_policy:{self.compliance_policy_state}")

    def configure_source(self, request) -> None:
        self.mode = request.mode
        if self.mode == "current":
            self.v_comp = request.compliance_si
        else:
            self.i_comp = request.compliance_si
        self.calls.append(f"configure_source:{request.channel}:{request.mode}:{request.compliance_si}")

    def set_output(self, channel: str, enabled: bool) -> None:
        self.output_enabled = enabled
        self.calls.append(f"set_output:{channel}:{enabled}")

    def update_source_level(self, channel: str, level_si: float) -> None:
        self.current_level = level_si
        self.calls.append(f"update_source_level:{channel}:{level_si:.6e}")

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
    with pytest.raises(SafetyViolation, match="co najmniej 2"):
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
    with pytest.raises(SafetyViolation, match="maksymalny dopuszczalny limit"):
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
    with pytest.raises(SafetyViolation, match="ściśle dodatnia"):
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
    with pytest.raises(SafetyViolation, match="identyczne"):
        KeithleyCharacterizationRunner.validate_preflight(zero_span, station_settings)


def test_runner_execution_and_shutdown():
    """Verify nominal sweep execution order and guaranteed shutdown."""
    device = _MockKeithleyDevice()
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

    assert len(dataset.points) == 5
    # Output must be confirmed OFF after sweep
    assert device.output_enabled is False
    assert "ramp_to_zero:A" in device.calls
    assert "set_output:A:False" in device.calls
    # Original compliance policy must be restored
    assert device.compliance_policy_state == "stop"


def test_runner_early_cancellation():
    """Verify that cancellation stops the sweep and still executes safe shutdown."""
    device = _MockKeithleyDevice()
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
    # Safe shutdown must still have executed
    assert device.output_enabled is False
    assert "ramp_to_zero:A" in device.calls
    assert "set_output:A:False" in device.calls


def test_runner_voltage_mode_execution():
    """Verify runner handles voltage sweep mode and computes apparent resistance accurately."""
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
    assert len(dataset.points) == 5
    assert device.output_enabled is False

    # Check point 4 (at demanded V=1.0 V):
    # Sample has R=50 Ohm, current clamped at I=0.010 A.
    # Measured voltage = 0.010 * 50 = 0.5 V.
    # True R = V_meas / I_meas = 0.5 / 0.010 = 50 Ohm.
    # Apparent R = V_demanded / I_meas = 1.0 / 0.010 = 100 Ohm.
    pt_last = dataset.points[-1]
    assert pt_last.compliance_active is True
    assert math.isclose(pt_last.true_resistance_ohm, 50.0, rel_tol=0.01)
    assert math.isclose(pt_last.apparent_resistance_ohm, 100.0, rel_tol=0.01)
