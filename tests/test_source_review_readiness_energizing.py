"""MOKE DAC updates and RF ON have the same preflight review classification."""

import pytest

from app.domain.readiness import ReadinessLevel, evaluate_station_readiness
from app.engine.compiler import ExecutionPlan, PlanAction, action_can_energize
from app.engine.estimation import PlanEstimator
from tests.helpers import simulation_settings


@pytest.mark.parametrize("kind,payload,energizing", [
    ("update_moke_voltage", {"voltage_v": .1, "ramp_timeout_s": 1.}, True),
    ("update_moke_voltage", {"voltage_v": 0., "ramp_timeout_s": 1.}, True),
    ("set_anritsu_sg_output", {"enabled": True}, True),
    ("set_anritsu_sg_output", {"enabled": False}, False),
    ("set_rigol_output", {"channel": 1, "enabled": True}, True),
    ("set_keithley_output", {"channel": "A", "enabled": False}, False),
    ("stop_moke_voltage", {"ramp_timeout_s": 1.}, False),
])
def test_readiness_and_estimation_agree_about_output_actions(kind, payload, energizing):
    settings = simulation_settings()
    action = PlanAction("action", kind, payload, {})
    plan = ExecutionPlan("review", (action,), 0, "a" * 64, "name: review",
                         frozenset(), 0, recipe_dut_limits={"legacy": "declaration"})
    assert action_can_energize(action) == energizing
    estimate = PlanEstimator(settings).estimate(plan)
    assert any("DUT and cabling review" in warning for warning in estimate.warnings) == energizing
    result = evaluate_station_readiness(settings, device_states={}, verified_resources={},
        audit_healthy=True, plan=plan, estimate=estimate, storage_probe_result=(True, "test directory"))
    item = next(item for item in result.items if item.key == "dut")
    assert item.level == (ReadinessLevel.WARNING if energizing else ReadinessLevel.PASS)
    if energizing:
        assert "metadata only" in item.detail and "not enforced" in item.detail
        assert "Plan contains no" not in item.detail
