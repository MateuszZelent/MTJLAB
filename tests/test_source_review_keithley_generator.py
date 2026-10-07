"""Plain level generators must preserve the authored source configuration."""
from itertools import count
from types import MethodType, SimpleNamespace

import pytest
import yaml

from app.domain.errors import ConfigurationError
from app.ui.recipes.page import RecipePage
from tests.test_sweep_audit_contracts import audit_settings, compile_source


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("fixed", [False, True])
def test_level_generator_preserves_baseline_and_output(tmp_path, channel, fixed):
    settings = audit_settings(tmp_path)
    ids = count()
    page = SimpleNamespace(_settings=settings, _new_node_id=lambda prefix: f"{prefix}-{next(ids)}")
    page._sweep_node_from_generator = MethodType(RecipePage._sweep_node_from_generator, page)
    definition = {"target": f"keithley.{channel}.current"}
    if fixed:
        node = RecipePage._fixed_node_from_dialog(page, definition, SimpleNamespace(value=SimpleNamespace(text=lambda: "0.2 mA")))
    else:
        node = page._sweep_node_from_generator(definition, [{"start": "0.2 mA", "stop": "0.4 mA", "points": 2}])
    assert node["children"] == []
    generated = "\n".join("    " + line for line in yaml.safe_dump([node], sort_keys=False).splitlines()) + "\n"
    baseline = f"""    - {{id: baseline, type: configure_keithley, channel: {channel}, mode: current, level: '0.1 mA', compliance: '20 mV', source_range: '10 mA', nplc: 2, settle_time: '250 ms', sense_mode: 2wire}}
    - {{id: output, type: set_keithley_output, channel: {channel}, enabled: true}}
"""
    plan = compile_source(settings, baseline + generated)
    assert [action.kind for action in plan.actions] == [
        "configure_keithley", "set_keithley_output",
        *(["update_keithley_level"] * (1 if fixed else 2)),
    ]
    for action, expected in zip(plan.actions[2:], [0.0002, 0.0004], strict=False):
        assert action.payload["channel"] == channel
        assert action.payload["mode"] == "current"
        assert action.payload["level_si"] == pytest.approx(expected)
        assert not {"compliance", "nplc", "settle_time", "config"}.intersection(action.payload)
    with pytest.raises(ConfigurationError, match="requires explicit device configuration"):
        compile_source(settings, generated)


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("settle", ["0 s", "250 ms"])
def test_explicit_configuration_occurs_once_before_axis(tmp_path, channel, settle):
    settings = audit_settings(tmp_path)
    ids = count()
    page = SimpleNamespace(_settings=settings, _new_node_id=lambda prefix: f"{prefix}-{next(ids)}")
    options = {"compliance": "20 mV", "nplc": 2, "settle_time": settle, "sense_mode": "2wire", "source_range": "10 mA"}
    node = RecipePage._sweep_node_from_generator(
        page, {"target": f"keithley.{channel}.current"},
        [{"start": "0.2 mA", "stop": "0.4 mA", "points": 2}], keithley_options=options,
    )
    assert node["type"] == "sequence"
    baseline, axis = node["children"]
    assert baseline["type"] == "configure_keithley"
    assert all(baseline[key] == value for key, value in options.items())
    assert axis["type"] == "sweep"
    # User-authored enable belongs between baseline and the entire loop.
    node["children"].insert(1, {"id": "output", "type": "set_keithley_output", "channel": channel, "enabled": True})
    generated = "\n".join("    " + line for line in yaml.safe_dump([node], sort_keys=False).splitlines()) + "\n"
    plan = compile_source(settings, generated)
    point_kinds = ["update_keithley_level"] + (["wait"] if settle != "0 s" else [])
    assert [action.kind for action in plan.actions] == ["configure_keithley", "set_keithley_output", *point_kinds, *point_kinds]
    assert [action.payload["level_si"] for action in plan.actions if action.kind == "update_keithley_level"] == pytest.approx([0.0002, 0.0004])


@pytest.mark.parametrize("options", [
    {},
    {"compliance": "20 mV", "nplc": 2, "settle_time": "-1 s", "sense_mode": "2wire", "source_range": "10 mA"},
    {"compliance": "20 mV", "nplc": 2, "settle_time": "invalid", "sense_mode": "2wire", "source_range": "10 mA"},
    {"compliance": "20 mV", "nplc": 2, "settle_time": "0 s", "sense_mode": "4wire", "source_range": "10 mA"},
])
def test_explicit_generator_rejects_missing_or_invalid_options(tmp_path, options):
    ids = count()
    page = SimpleNamespace(_settings=audit_settings(tmp_path), _new_node_id=lambda prefix: f"{prefix}-{next(ids)}")
    with pytest.raises((ConfigurationError, ValueError)):
        RecipePage._sweep_node_from_generator(page, {"target": "keithley.A.current"}, [{"value": "0.2 mA"}], keithley_options=options)
