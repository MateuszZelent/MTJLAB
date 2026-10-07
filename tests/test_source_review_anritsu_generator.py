"""Anritsu generator must not reset fields unrelated to its selected axis."""
from itertools import count
from types import MethodType, SimpleNamespace

import pytest
import yaml

from app.domain.errors import ConfigurationError
from app.settings.models import StationSettings
from app.ui.recipes.page import RecipePage
from tests.test_sweep_audit_contracts import audit_settings, compile_source


@pytest.mark.parametrize("parameter,start,stop,changed", [
    ("start_frequency", "2 MHz", "3 MHz", "start_hz"),
    ("stop_frequency", "9 MHz", "8 MHz", "stop_hz"),
    ("reference_level", "-10 dBm", "-20 dBm", "reference_level_dbm"),
])
@pytest.mark.parametrize("fixed", [False, True])
def test_spectrum_generator_preserves_other_config_fields(tmp_path, parameter, start, stop, changed, fixed):
    settings = audit_settings(tmp_path)
    ids = count()
    page = SimpleNamespace(_settings=settings, _new_node_id=lambda prefix: f"{prefix}-{next(ids)}")
    page._sweep_node_from_generator = MethodType(RecipePage._sweep_node_from_generator, page)
    definition = {"target": f"anritsu.spectrum.{parameter}"}
    if fixed:
        node = RecipePage._fixed_node_from_dialog(page, definition, SimpleNamespace(value=SimpleNamespace(text=lambda: start)))
    else:
        node = page._sweep_node_from_generator(definition, [{"start": start, "stop": stop, "points": 2}])
    generated = "\n".join("    " + line for line in yaml.safe_dump([node], sort_keys=False).splitlines()) + "\n"
    baseline = "    - {id: baseline, type: configure_anritsu, start_frequency: '1 MHz', stop_frequency: '10 MHz', reference_level: '-5 dBm', points: 101}\n"
    plan = compile_source(settings, baseline + generated)
    initial = plan.actions[0].payload["config"]
    assert len(plan.actions) == (2 if fixed else 3)
    for action in plan.actions[1:]:
        assert action.kind == "configure_anritsu"
        config = action.payload["config"]
        assert config.changed_fields == (changed,)
        assert getattr(config, changed) == pytest.approx(action.payload["requested_si"])
        for field in ("start_hz", "stop_hz", "reference_level_dbm", "points"):
            if field != changed:
                assert getattr(config, field) == getattr(initial, field)
    with pytest.raises(ConfigurationError, match="requires explicit device configuration"):
        compile_source(settings, generated)


@pytest.mark.parametrize("parameter,start,stop,changed", [
    ("frequency", "2 GHz", "3 GHz", "frequency_hz"),
    ("power", "-40 dBm", "-50 dBm", "power_dbm"),
])
@pytest.mark.parametrize("fixed", [False, True])
def test_sg_generator_preserves_other_field_without_enabling_rf(tmp_path, parameter, start, stop, changed, fixed):
    raw = audit_settings(tmp_path).model_dump(mode="python")
    # Synthetic compiler qualification only; never enables station RF control.
    raw["devices"]["anritsu"]["signal_generator"].update({
        "control_protocol": "basic_scpi",
        "frequency": {"min": "100 MHz", "max": "6 GHz"},
        "power": {"min": "-100 dBm", "max": "0 dBm"},
    })
    settings = StationSettings.model_validate(raw)
    ids = count()
    page = SimpleNamespace(_settings=settings, _new_node_id=lambda prefix: f"{prefix}-{next(ids)}")
    page._sweep_node_from_generator = MethodType(RecipePage._sweep_node_from_generator, page)
    definition = {"target": f"anritsu.sg.{parameter}"}
    if fixed:
        node = RecipePage._fixed_node_from_dialog(page, definition, SimpleNamespace(value=SimpleNamespace(text=lambda: start)))
    else:
        node = page._sweep_node_from_generator(definition, [{"start": start, "stop": stop, "points": 2}])
    generated = "\n".join("    " + line for line in yaml.safe_dump([node], sort_keys=False).splitlines()) + "\n"
    baseline = "    - {id: baseline, type: configure_anritsu_sg, frequency: '1 GHz', power: '-30 dBm'}\n"
    plan = compile_source(settings, baseline + generated)
    initial = plan.actions[0].payload["config"]
    assert len(plan.actions) == (2 if fixed else 3)
    for action in plan.actions[1:]:
        assert action.kind == "update_anritsu_sg"
        config = action.payload["config"]
        assert config.changed_fields == (changed,)
        assert getattr(config, changed) == pytest.approx(action.payload["requested_si"])
        other = "power_dbm" if changed == "frequency_hz" else "frequency_hz"
        assert getattr(config, other) == getattr(initial, other)
    with pytest.raises(ConfigurationError, match="requires explicit device configuration"):
        compile_source(settings, generated)
