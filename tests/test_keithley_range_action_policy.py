"""Selected range policies through real compilation and simulated hardware only."""

import json
from dataclasses import replace

import pytest

from app.bootstrap import StationComposition
from app.devices.simulators import simulated_station_settings
from app.domain.errors import ConfigurationError, SafetyViolation
from app.engine.compiler import RecipeCompiler
from app.recipes.models import parse_recipe_text
from app.settings.models import StationSettings
from tests.helpers import loaded_settings


def settings_for(channel, source_auto=False):
    raw = simulated_station_settings(loaded_settings()).model_dump(mode="python")
    raw["devices"]["keithley"]["safety"]["channels"][channel]["enabled"] = True
    raw["devices"]["keithley"]["safety"]["channels"][channel]["defaults"]["source_autorange"] = (
        source_auto
    )
    return StationSettings.model_validate(raw)


def compile_range(settings, channel, parameter, value, *, initial_auto=False, mode="set"):
    source_mode = "voltage" if parameter == "measurement.current_range" else "current"
    source_auto = settings.keithley.safety.channels[channel].defaults["source_autorange"]
    baseline = {
        "id": "baseline",
        "type": "configure_keithley",
        "channel": channel,
        "mode": source_mode,
        "level": "20 mV" if source_mode == "voltage" else "1 mA",
        "compliance": "200 uA" if source_mode == "voltage" else "20 mV",
        "source_autorange": source_auto,
        "source_range": "AUTO"
        if source_auto
        else "100 mV"
        if source_mode == "voltage"
        else "10 mA",
        "nplc": 4,
        "settle_time": "250 ms",
        "sense_mode": "2wire",
        "measure_voltage_autorange": False,
        "measure_voltage_range": "100 mV",
        "measure_current_autorange": False,
        "measure_current_range": "10 mA",
    }
    if parameter.startswith("measurement."):
        quantity = parameter.split(".")[1].removesuffix("_range")
        baseline[f"measure_{quantity}_autorange"] = initial_auto
        if initial_auto:
            baseline[f"measure_{quantity}_range"] = "AUTO"
    action = {"parameter_id": parameter, "mode": mode, "value": value}
    if mode == "sweep":
        action["segments"] = [{"start": "1 mV", "stop": "2 mV", "points": 2}]
    selected = {
        "id": "selected",
        "type": "sequence",
        "device_module": "keithley",
        "operation": "configure_selected_parameters",
        "channel": channel,
        "source_mode": source_mode,
        "output_policy": "unchanged",
        # Deliberately stale UI draft: only parameter_actions may change policy.
        "configuration": {
            "channel": channel,
            "source_mode": source_mode,
            "source_autorange": not source_auto,
            "source_range": "1 A",
            "measure_voltage_autorange": not initial_auto,
            "measure_current_autorange": not initial_auto,
            "nplc": 1,
            "settling_time": "0 s",
        },
        "parameter_actions": [action],
    }
    recipe = parse_recipe_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "range policy",
                "root": {"id": "root", "type": "sequence", "children": [baseline, selected]},
            }
        )
    )
    plan = RecipeCompiler(settings).compile(recipe)
    return tuple(a.payload["request"] for a in plan.actions if a.kind == "configure_keithley")


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize(
    "quantity,suffix,fixed,si",
    [
        ("voltage", "v", "1 V", 1.0),
        ("current", "i", "100 mA", 0.1),
    ],
)
@pytest.mark.parametrize("initial_auto", [False, True])
@pytest.mark.parametrize("selected_auto", [False, True])
def test_measurement_range_policy_compiles_and_executes_only_selected_fields(
    channel,
    quantity,
    suffix,
    fixed,
    si,
    initial_auto,
    selected_auto,
):
    settings = settings_for(channel)
    baseline, selected = compile_range(
        settings,
        channel,
        f"measurement.{quantity}_range",
        "AUTO" if selected_auto else fixed,
        initial_auto=initial_auto,
    )
    flag, field = f"measure_{quantity}_autorange", f"measure_{quantity}_range_si"
    changes = {flag: selected_auto, field: None if selected_auto else si}
    assert selected.changed_fields == (flag, field)
    assert selected == replace(baseline, **changes, changed_fields=(flag, field))
    adapter = StationComposition(settings, simulation=True).create_adapter("keithley")
    adapter.connect()
    try:
        before = adapter.configure_source(baseline)
        session = adapter._require_session()
        session.commands.clear()
        applied = adapter.configure_source(selected)
        assert applied == replace(before, **changes)
        smu = f"smu{channel.lower()}"
        expected = []
        if initial_auto != selected_auto:
            expected.append(
                f"{smu}.measure.autorange{suffix} = {smu}.AUTORANGE_{'ON' if selected_auto else 'OFF'}"
            )
        if not selected_auto:
            expected.append(f"{smu}.measure.range{suffix} = {si:.12g}")
        assert [cmd for cmd in session.commands if " = " in cmd] == expected
        readback = next(c for c in adapter.read_configuration().channels if c.channel == channel)
        assert getattr(readback, flag) is selected_auto
        assert not readback.output_enabled
        if not selected_auto:
            assert (
                getattr(
                    readback, f"measure_{quantity}_range_{'v' if quantity == 'voltage' else 'a'}"
                )
                == si
            )
    finally:
        adapter.disconnect()


@pytest.mark.parametrize(
    "source_auto,value,expected", [(False, "100 mA", 0.1), (True, "AUTO", None)]
)
def test_source_range_matching_station_policy_preserves_other_fields(source_auto, value, expected):
    settings = settings_for("B", source_auto)
    baseline, selected = compile_range(settings, "B", "source.range", value)
    fields = ("source_autorange", "source_range_si")
    assert selected.changed_fields == fields
    assert selected == replace(
        baseline, source_autorange=source_auto, source_range_si=expected, changed_fields=fields
    )
    adapter = StationComposition(settings, simulation=True).create_adapter("keithley")
    adapter.connect()
    try:
        adapter.configure_source(baseline)
        session = adapter._require_session()
        session.commands.clear()
        applied = adapter.configure_source(selected)
        assert applied.source_autorange is source_auto
        assert applied.source_range_si == expected
        assert [cmd for cmd in session.commands if " = " in cmd] == (
            [] if source_auto else ["smub.source.rangei = 0.1"]
        )
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("station_auto,value", [(False, "AUTO"), (True, "100 mA")])
def test_source_range_actions_cannot_override_station_autorange_policy(station_auto, value):
    settings = settings_for("B", station_auto)
    with pytest.raises(
        SafetyViolation, match="source_autorange.*conflicts|source autorange must match"
    ):
        compile_range(settings, "B", "source.range", value)


@pytest.mark.parametrize(
    "parameter", ["source.range", "measurement.voltage_range", "measurement.current_range"]
)
def test_selected_range_sweeps_remain_unsupported(parameter):
    settings = settings_for("B")
    with pytest.raises(ConfigurationError, match="fixed-only|unsupported.*sweep"):
        compile_range(settings, "B", parameter, "AUTO", mode="sweep")
