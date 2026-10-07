"""MOKE point dispatch stays bounded and uses qualified readback precision."""
from dataclasses import replace

import pytest

from app.domain.errors import DeviceError, SafetyViolation
from app.safety.moke_box import MokeVoltagePlan
from tests.test_moke_voltage_control import controlled_adapter, mutations, plan_for


def test_large_trajectory_is_not_rescanned_for_each_point(monkeypatch):
    adapter, transport, profile = controlled_adapter()
    plan = plan_for(profile, targets=(.01,) * 10000)
    validations = []
    original = MokeVoltagePlan.validate

    def validate(self, active_profile):
        validations.append(len(self.targets_v))
        return original(self, active_profile)

    monkeypatch.setattr(MokeVoltagePlan, "validate", validate)
    try:
        adapter.configure_voltage_plan(plan)
        adapter.arm_voltage_plan(plan)
        for _ in range(100):
            adapter.ramp_vout(2, .01)
        assert validations == [10000, 10000]
        assert len(mutations(transport)) == 100
        adapter._active_profile = replace(profile, maximum_v=.005)
        with pytest.raises(SafetyViolation):
            adapter.ramp_vout(2, .01)
        assert len(mutations(transport)) == 100
    finally:
        adapter._active_profile = profile
        adapter.disconnect()


@pytest.mark.parametrize("error_v", [-.0004, .0004])
def test_final_readback_inside_protocol_tolerance_finishes_without_reissuing_target(monkeypatch, error_v):
    adapter, transport, profile = controlled_adapter()
    plan = plan_for(profile, targets=(.01,))
    original = adapter._read_vouts_from_transport

    def read():
        values = original()
        if values[2] != 0:
            values[2] += error_v
        return values

    monkeypatch.setattr(adapter, "_read_vouts_from_transport", read)
    try:
        adapter.configure_voltage_plan(plan)
        adapter.arm_voltage_plan(plan)
        result = adapter.ramp_vout(2, .01)
        assert result.actual_v == pytest.approx(result.applied_v + error_v)
        assert len(mutations(transport)) == 1
        assert not result.safe_target_confirmed
        stopped = adapter.stop_vout()
        assert stopped.safe_target_confirmed and stopped.actual_v == 0
    finally:
        adapter.disconnect()


def test_protocol_tolerance_does_not_authorize_readback_outside_station_limits(monkeypatch):
    adapter, transport, profile = controlled_adapter(minimum=-.01, maximum=.01)
    plan = plan_for(profile, targets=(.01,), minimum=-.01, maximum=.01)
    original = adapter._read_vouts_from_transport

    def read():
        values = original()
        if values[2] > 0:
            values[2] += .0004
        return values

    monkeypatch.setattr(adapter, "_read_vouts_from_transport", read)
    try:
        adapter.configure_voltage_plan(plan)
        adapter.arm_voltage_plan(plan)
        with pytest.raises(DeviceError):
            adapter.ramp_vout(2, .01)
        assert not adapter.safe_target_confirmed
    finally:
        adapter.disconnect()
