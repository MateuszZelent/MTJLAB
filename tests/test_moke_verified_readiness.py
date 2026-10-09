"""Confirmed DAC control must not masquerade as a failed connection."""
import pytest

from app.domain.models import DeviceState
from app.domain.readiness import ReadinessLevel, evaluate_station_readiness
from app.engine.compiler import ExecutionPlan
from app.settings.models import StationSettings
from tests.helpers import loaded_settings
from tests.test_moke_voltage_control import controlled_adapter, plan_for


@pytest.mark.parametrize("zero", [False, True])
def test_confirmed_control_preserves_verified_session_and_preflight(zero):
    adapter, transport, profile = controlled_adapter(minimum_settling_s=0)
    plan = plan_for(profile, targets=(.1,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    adapter.ramp_vout(2, .1)
    if zero:
        assert adapter.stop_vout(2).safe_target_confirmed
    assert adapter.state is DeviceState.VERIFIED
    assert adapter.connected and adapter.identity is not None
    assert adapter.state is not DeviceState.OUTPUT_OFF
    before = tuple(transport.sent)
    assert adapter.connect() is adapter.identity  # Reuses the session, no reset/write.
    assert tuple(transport.sent) == before
    settings = loaded_settings()
    settings = replace_moke_settings(settings)
    recipe_plan = ExecutionPlan("moke", (), 0, "a" * 64, "name: moke", frozenset({"moke_box"}), 0)
    readiness = evaluate_station_readiness(settings, device_states={"moke_box": adapter.state.value},
        verified_resources={"moke_box": settings.moke_box.endpoint}, audit_healthy=True,
        plan=recipe_plan, storage_probe_result=(True, "test"))
    assert next(item.level for item in readiness.items if item.key == "device.moke_box") is ReadinessLevel.PASS


def replace_moke_settings(settings):
    raw = settings.model_dump(mode="python")
    raw["devices"]["moke_box"].update(enabled=True, endpoint="SIM::MOKE::INSTR")
    return StationSettings.model_validate(raw)


def test_emergency_zero_does_not_claim_verified_recovery_or_power_off():
    adapter, _, profile = controlled_adapter(minimum_settling_s=0)
    plan = plan_for(profile, targets=(.1,))
    adapter.configure_voltage_plan(plan)
    adapter.arm_voltage_plan(plan)
    adapter.ramp_vout(2, .1)
    adapter.emergency_off()
    assert adapter.safe_target_confirmed
    assert adapter.state is DeviceState.UNKNOWN
    assert adapter.state is not DeviceState.OUTPUT_OFF
