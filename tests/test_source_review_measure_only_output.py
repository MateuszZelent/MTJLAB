"""A measurement configuration cannot authorize retained source registers."""

from dataclasses import replace

import pytest
import yaml

from app.devices.keithley_2600.adapter import KeithleyAdapter
from app.devices.simulators import KeithleySimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import ConfigurationError, SafetyViolation
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from tests.test_keithley_coupled_ranges import request_for, settings_for


@pytest.fixture
def hardware():
    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings_for(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    yield adapter, session
    adapter.disconnect()


def measurement_request(channel, mode):
    return replace(request_for(channel, mode), mode="measure_only", level_si=0,
                   compliance_si=0, source_range_si=None)


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("mode", ["current", "voltage"])
@pytest.mark.parametrize("group", [False, True])
def test_retained_source_cannot_be_energized_by_measure_only(hardware, channel, mode, group):
    adapter, session = hardware
    for selected in ("A", "B"):
        adapter.configure_source(request_for(selected, mode))
    smu = "smua" if channel == "A" else "smub"
    suffix = "i" if mode == "current" else "v"
    retained = session.query(f"print({smu}.source.level{suffix})")
    adapter.configure_source(measurement_request(channel, mode))
    assert session.query(f"print({smu}.source.level{suffix})") == retained
    before = len(session.commands)
    with pytest.raises(SafetyViolation, match="measure_only cannot enable"):
        if group:
            adapter.set_output_group(("A", "B"), True)
        else:
            adapter.set_output(channel, True)
    assert not any("OUTPUT_ON" in command for command in session.commands[before:])
    assert adapter.set_output(channel, False) is False
    # A deliberate source configuration restores the normal enable path.
    adapter.configure_source(request_for(channel, mode))
    assert adapter.set_output(channel, True) is True


@pytest.mark.parametrize("field", ["level_si", "compliance_si", "source_autorange", "source_range_si"])
def test_selected_measure_only_fields_do_not_mutate_retained_source(hardware, field):
    adapter, session = hardware
    adapter.configure_source(request_for())
    request = replace(measurement_request("A", "current"), changed_fields=("mode", field))
    before = len(session.commands)
    with pytest.raises(SafetyViolation, match="measure_only cannot select"):
        adapter.configure_source(request)
    assert session.commands[before:] == []


@pytest.mark.parametrize("reconfigure", [False, True])
def test_compiler_revokes_old_source_authorization_after_measure_only(reconfigure):
    source = {"type": "configure_keithley", "channel": "A", "mode": "current",
              "level": "100 uA", "compliance": "50 mV", "source_range": "10 mA"}
    children = [dict(source, id="source"),
                {"id": "measure", "type": "configure_keithley", "channel": "A", "mode": "measure_only"}]
    if reconfigure:
        children.append(dict(source, id="new-source"))
    children.append({"id": "on", "type": "set_keithley_output", "channel": "A", "enabled": True})
    recipe = parse_recipe_text(yaml.safe_dump({"schema_version": 1, "name": "measure only guard",
        "root": {"id": "sequence", "type": "sequence", "children": children}}))
    compiler = RecipeCompiler(settings_for())
    if reconfigure:
        compiler.compile(recipe)
    else:
        with pytest.raises(ConfigurationError, match="earlier configuration"):
            compiler.compile(recipe)
