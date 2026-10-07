"""Legacy Add paths must preserve the explicit carrier and OUTPUT state."""
from itertools import count
from types import SimpleNamespace, MethodType

import pytest
import yaml

from app.domain.errors import ConfigurationError
from app.ui.recipes.page import RecipePage
from tests.test_sweep_audit_contracts import audit_settings, compile_source


@pytest.mark.parametrize("parameter,start,stop", [
    ("frequency", "100 kHz", "200 kHz"),
    ("high_level", "10 mV", "20 mV"),
    ("low_level", "-10 mV", "-20 mV"),
    ("amplitude", "20 mV", "30 mV"),
    ("offset", "1 mV", "2 mV"),
])
@pytest.mark.parametrize("fixed", [False, True])
def test_generated_rigol_axis_changes_only_selected_parameter(tmp_path, parameter, start, stop, fixed):
    settings = audit_settings(tmp_path)
    raw = settings.model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    settings = type(settings).model_validate(raw)
    ids = count()
    page = SimpleNamespace(_settings=settings, _new_node_id=lambda prefix: f"{prefix}-{next(ids)}")
    page._sweep_node_from_generator = MethodType(RecipePage._sweep_node_from_generator, page)
    definition = {"target": f"rigol.1.{parameter}"}
    if fixed:
        node = RecipePage._fixed_node_from_dialog(page, definition, SimpleNamespace(value=SimpleNamespace(text=lambda: start)))
    else:
        node = page._sweep_node_from_generator(definition, [{"start": start, "stop": stop, "points": 2}])
    generated = "\n".join("    " + line for line in yaml.safe_dump([node], sort_keys=False).splitlines()) + "\n"
    baseline = """    - {id: baseline, type: configure_rigol, channel: 1, waveform: SIN, frequency: '1 kHz', high_level: '10 mV', low_level: '-10 mV', phase_deg: 37}
    - {id: output, type: set_rigol_output, channel: 1, enabled: true}
"""
    plan = compile_source(settings, baseline + generated)
    updates = plan.actions[2:]
    assert len(updates) == (1 if fixed else 2)
    assert all(action.kind == ("update_rigol_frequency" if parameter == "frequency" else "update_rigol_levels") for action in updates)
    assert all(action.payload["target"] == definition["target"] for action in updates)
    assert node["children"] == []
    values = [action.payload["requested_si"] for action in updates]
    from app.domain.quantities import parse_quantity
    expected = [parse_quantity(value, "frequency" if parameter == "frequency" else "voltage").si_value for value in ([start] if fixed else [start, stop])]
    assert values == pytest.approx(expected)
    for action in updates:
        if parameter == "amplitude":
            assert action.payload["high_level_v"] - action.payload["low_level_v"] == pytest.approx(action.payload["requested_si"])
        elif parameter == "offset":
            assert (action.payload["high_level_v"] + action.payload["low_level_v"]) / 2 == pytest.approx(action.payload["requested_si"])
    with pytest.raises(ConfigurationError, match="requires explicit device configuration"):
        compile_source(settings, generated)
