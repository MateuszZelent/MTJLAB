"""The station must never authorize or program remote (4-wire) sense."""

from dataclasses import replace

import pytest

from app.bootstrap import StationComposition
from app.domain.errors import SafetyViolation
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.safety.keithley import KeithleySourceRequest, validate_keithley_source
from app.settings.models import StationSettings
from tests.helpers import simulation_settings


@pytest.mark.parametrize("field", ["channel", "defaults", "conflict"])
def test_profile_rejects_remote_sense_in_either_location(field):
    raw = simulation_settings().model_dump(mode="python")
    channel = raw["devices"]["keithley"]["safety"]["channels"]["B"]
    if field in {"channel", "conflict"}:
        channel["sense_mode"] = "4wire"
    if field == "defaults":
        channel["defaults"]["sense_mode"] = "4wire"
    with pytest.raises(ValueError, match="prohibited"):
        StationSettings.model_validate(raw)


@pytest.mark.parametrize("mode", ["current", "voltage", "measure_only"])
@pytest.mark.parametrize("selected", [None, ("sense_mode",), ("level_si",)])
def test_adapter_rejects_before_any_instrument_traffic(mode, selected):
    settings = simulation_settings()
    adapter = StationComposition(settings, simulation=True).create_adapter("keithley")
    adapter.connect()
    try:
        session = adapter._require_session()
        session.commands.clear()
        request = KeithleySourceRequest("B", mode, .001, .02, sense_mode="4wire",
                                        source_range_si=.1, changed_fields=selected)
        with pytest.raises(SafetyViolation, match="prohibited"):
            adapter.configure_source(request)
        assert session.commands == []
        with pytest.raises(SafetyViolation, match="prohibited"):
            adapter._measurement_range_and_sense_commands("smub", request)
    finally:
        adapter.disconnect()


def test_compiler_rejects_remote_sense_recipe():
    recipe = parse_recipe_text("""schema_version: 1
name: rejected remote sense
root:
  id: configure
  type: configure_keithley
  channel: B
  mode: current
  level: 1 mA
  compliance: 20 mV
  source_range: 100 mA
  sense_mode: 4wire
""")
    with pytest.raises(SafetyViolation, match="prohibited"):
        RecipeCompiler(simulation_settings()).compile(recipe)


def test_source_validation_also_rejects_invalid_runtime_profile():
    settings = simulation_settings()
    channel = settings.keithley.safety.channels["B"].model_copy(update={"sense_mode": "4wire"})
    request = KeithleySourceRequest("B", "current", .001, .02, source_range_si=.1)
    with pytest.raises(SafetyViolation, match="prohibited"):
        validate_keithley_source(channel, request)
    validate_keithley_source(settings.keithley.safety.channels["B"], replace(request, sense_mode="2wire"))
