"""Hardware settings cannot be inferred from application defaults."""

from app.devices.anritsu_ms2830a.configuration import SpectrumConfig
from app.engine.compiler import ExecutionPlan, PlanAction
from app.engine.estimation import PlanEstimator
from app.safety.anritsu import ANRITSU_SWEEP_POINT_COUNTS
from tests.helpers import simulation_settings


def estimate(actions):
    settings = simulation_settings()
    settings.execution["retry_count"] = 0
    plan = ExecutionPlan("baseline", tuple(actions), 1, "d" * 64, "name: baseline",
                         total_spectra=1)
    return PlanEstimator(settings).estimate(plan)


def spectrum():
    return PlanAction("spectrum", "acquire_spectrum", {"average_count": 1}, {})


def configuration(points):
    return PlanAction("configure", "configure_anritsu", {
        "config": SpectrumConfig(1e6, 2e6, 0., points),
    }, {})


def test_unknown_hardware_uses_supported_maximum_including_public_import():
    unknown = estimate([spectrum()])
    explicit = estimate([configuration(max(ANRITSU_SWEEP_POINT_COUNTS)), spectrum()])
    assert unknown.spectrum_values == 2 * max(ANRITSU_SWEEP_POINT_COUNTS)
    assert unknown.public_import_upper_bytes == explicit.public_import_upper_bytes
    assert "not established by the plan" in " ".join(unknown.warnings)


def test_explicit_configuration_is_used_only_after_its_action():
    result = estimate([spectrum(), configuration(101), spectrum()])
    assert result.spectrum_values == 2 * (max(ANRITSU_SWEEP_POINT_COUNTS) + 101)
    assert sum("not established" in message for message in result.warnings) == 1


def test_explicit_small_configuration_does_not_reserve_unknown_size():
    result = estimate([configuration(101), spectrum()])
    assert result.spectrum_values == 202
    assert not any("not established" in message for message in result.warnings)
