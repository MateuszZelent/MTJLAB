"""The upper time model includes full operation deadlines for each retry."""

import pytest

from app.engine.compiler import ExecutionPlan, PlanAction
from app.engine.estimation import PlanEstimator
from app.engine.policy import ExecutionPolicy
from tests.helpers import simulation_settings


@pytest.mark.parametrize("kind,payload,retryable", [
    ("acquire_spectrum", {"average_count": 4, "inter_sweep_delay_s": 3.}, True),
    ("acquire_reference", {"average_count": 30, "minimum_duration_s": 30.}, True),
    ("set_keithley_output", {"channel": "A", "enabled": False}, True),
    ("set_keithley_output", {"channel": "A", "enabled": True}, False),
    ("update_moke_voltage", {"ramp_timeout_s": 20.}, False),
    ("wait", {"duration_s": 3.}, False),
])
def test_retry_time_delta_matches_runner_operation_budget(kind, payload, retryable):
    settings = simulation_settings()
    action = PlanAction("operation", kind, payload, {})
    plan = ExecutionPlan("retry estimate", (action,), 0, "a" * 64, "name: retry")
    settings.execution["retry_count"] = 0
    base = PlanEstimator(settings).estimate(plan)
    settings.execution["retry_count"] = 3
    policy = ExecutionPolicy.from_settings(settings)
    estimate = PlanEstimator(settings).estimate(plan)
    expected = 3 * (policy.deadline_for(action) + policy.retry_backoff_s) if retryable else 0.
    assert estimate.retry_upper_duration_s - base.retry_upper_duration_s == pytest.approx(expected)
    assert estimate.nominal_duration_s == base.nominal_duration_s


@pytest.mark.parametrize("kind,timed", [
    ("acquire_spectrum", False),
    ("acquire_reference", False),
    ("acquire_reference", True),
])
def test_retry_storage_retains_all_raw_attempts_without_duplicating_public_mean(kind, timed):
    from app.domain.recipe_spectrum import MAX_RECIPE_SWEEP_JSON_BYTES

    settings = simulation_settings()
    payload = {"average_count": 4, "store_processed": True}
    if timed:
        payload["minimum_duration_s"] = 30.
    plan = ExecutionPlan("raw retry", (PlanAction("raw", kind, payload, {}),),
                         1, "b" * 64, "name: raw", total_spectra=int(kind == "acquire_spectrum"))
    settings.execution["retry_count"] = 0
    base = PlanEstimator(settings).estimate(plan)
    settings.execution["retry_count"] = 2
    retried = PlanEstimator(settings).estimate(plan)
    from app.safety.anritsu import ANRITSU_SWEEP_POINT_COUNTS

    points = max(ANRITSU_SWEEP_POINT_COUNTS)
    extra_sources = 2 * (9999 if timed else 4)
    assert retried.spectrum_values - base.spectrum_values == extra_sources * points
    assert retried.uncompressed_hdf5_bytes - base.uncompressed_hdf5_bytes >= (
        extra_sources * (points * 16 + MAX_RECIPE_SWEEP_JSON_BYTES + 8192)
    )
    assert retried.public_import_upper_bytes == base.public_import_upper_bytes
    assert retried.nominal_duration_s == base.nominal_duration_s
    if timed:
        assert "29997 raw sweeps across 3 attempts" in " ".join(retried.warnings)


def test_imported_reference_has_no_raw_retry_budget():
    settings = simulation_settings()
    plan = ExecutionPlan("import", (PlanAction("ref", "acquire_reference",
                         {"source_file": "reference.h5"}, {}),), 0, "c" * 64, "name: import")
    settings.execution["retry_count"] = 5
    assert PlanEstimator(settings).estimate(plan).spectrum_values == 0
