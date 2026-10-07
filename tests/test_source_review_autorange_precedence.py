"""Explicit recipe/form values cannot silently change with Settings."""

from types import SimpleNamespace

import pytest

from app.devices.keithley_2600.ui.page import KeithleyConfigurationSnapshot, KeithleyPage
from app.domain.errors import SafetyViolation
from app.engine.compiler import RecipeCompiler
from tests.test_keithley_coupled_ranges import settings_for


@pytest.mark.parametrize("settings_auto", [False, True])
@pytest.mark.parametrize("recipe_auto", [False, True, None])
def test_explicit_autorange_must_match_policy_and_omitted_inherits(settings_auto, recipe_auto):
    data = dict(channel="A", mode="current", level="100 uA", compliance="50 mV", source_range="10 mA")
    if recipe_auto is not None:
        data["source_autorange"] = recipe_auto
    compiler = RecipeCompiler(settings_for(auto=settings_auto))
    if recipe_auto is not None and recipe_auto != settings_auto:
        with pytest.raises(SafetyViolation, match="conflicts"):
            compiler._compile_keithley(data, "source")
    else:
        request = compiler._compile_keithley(data, "source")["request"]
        assert request.source_autorange is settings_auto
        assert request.source_range_si == (None if settings_auto else .01)


@pytest.mark.parametrize("settings_auto", [False, True])
def test_measure_only_never_inherits_or_accepts_source_auto(settings_auto):
    compiler = RecipeCompiler(settings_for(auto=settings_auto))
    data = dict(channel="A", mode="measure_only")
    assert not compiler._compile_keithley(data, "measurement")["request"].source_autorange
    with pytest.raises(SafetyViolation, match="measure_only"):
        compiler._compile_keithley(dict(data, source_autorange=True), "measurement")


@pytest.mark.parametrize("settings_auto", [False, True])
def test_stale_manual_snapshot_is_rejected_instead_of_overwritten(settings_auto):
    owner = SimpleNamespace(_station_settings=settings_for(auto=settings_auto), _manual_range=KeithleyPage._manual_range)
    snapshot = KeithleyConfigurationSnapshot(channel="A", source_mode="current", source_level="100 uA",
        compliance="50 mV", source_range="10 mA", source_autorange=not settings_auto)
    with pytest.raises(SafetyViolation, match="must match Settings"):
        KeithleyPage._source_request_from_snapshot(owner, snapshot)
