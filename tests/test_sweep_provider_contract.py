from __future__ import annotations

import pytest

from app.contracts import DeviceModuleRegistry
from app.devices.registry import built_in_device_registry
from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.quantities import DIMENSION_CURRENT, DIMENSION_VOLTAGE, parse_quantity
from app.recipes.models import RecipeNode
from tests.helpers import simulation_settings


def test_registered_recipe_module_owns_its_sweep_provider() -> None:
    registry = built_in_device_registry()
    for module_key in ("keithley", "rigol", "anritsu"):
        extension = registry.get(module_key).recipe_extension
        assert extension is not None
        assert extension.sweep_provider is not None
        assert extension.sweep_provider.module_key == module_key


def test_registry_exposes_only_enabled_provider_extensions() -> None:
    registry = built_in_device_registry()
    providers = registry.sweep_providers()
    assert set(providers) >= {"keithley", "rigol", "anritsu"}
    assert all(provider.module_key == key for key, provider in providers.items())


def test_keithley_provider_compiles_current_and_compliance_independently() -> None:
    provider = built_in_device_registry().sweep_providers()["keithley"]
    node = RecipeNode("keithley-b", "sequence", {"configuration": {"channel": "B", "source_mode": "current"}})
    current = provider.compile_point(
        node,
        provider.binding_for_target(node, "keithley.B.current"),
        parse_quantity("2 mA", DIMENSION_CURRENT),
        {},
        simulation_settings(),
    )
    compliance = provider.compile_point(
        node,
        provider.binding_for_target(node, "keithley.B.compliance_voltage"),
        parse_quantity("10 mV", DIMENSION_VOLTAGE),
        {},
        simulation_settings(),
    )
    assert current.action_kind == "update_keithley_level"
    assert compliance.action_kind == "update_keithley_compliance"
    assert current.requested_si == pytest.approx(0.002)
    assert compliance.requested_si == pytest.approx(0.01)


def test_provider_rejects_wrong_dimension() -> None:
    provider = built_in_device_registry().sweep_providers()["keithley"]
    node = RecipeNode("keithley-b", "sequence", {"configuration": {"channel": "B", "source_mode": "current"}})
    binding = provider.binding_for_target(node, "keithley.B.current")
    with pytest.raises((ConfigurationError, SafetyViolation, ValueError), match="dimension"):
        provider.compile_point(node, binding, parse_quantity("1 V"), {}, simulation_settings())


def test_registry_rejects_provider_module_key_mismatch() -> None:
    from app.contracts.device_module import DeviceModule, RecipeExtension

    class FakeProvider:
        module_key = "wrong"

    base = built_in_device_registry().get("keithley")
    with pytest.raises(ValueError, match="provider key"):
        DeviceModuleRegistry(
            (
                DeviceModule(
                    key=base.key,
                    implementation_key="fake_keithley",
                    display_name=base.display_name,
                    settings_key=base.settings_key,
                    adapter_factory=base.adapter_factory,
                    dispatch=base.dispatch,
                    recipe_extension=RecipeExtension(
                        module_key=base.key,
                        sweep_provider=FakeProvider(),  # type: ignore[arg-type]
                    ),
                ),
            )
        )


def test_rigol_level_provider_preserves_the_other_level() -> None:
    provider = built_in_device_registry().sweep_providers()["rigol"]
    node = RecipeNode(
        "rigol-1",
        "sequence",
        {"configuration": {"channel": 1, "high_level": "0.5 mV", "low_level": "-0.2 mV"}},
    )
    binding = provider.binding_for_target(node, "rigol.1.high_level")
    result = provider.compile_point(
        node,
        binding,
        parse_quantity("0.3 mV", DIMENSION_VOLTAGE),
        {},
        simulation_settings(),
    )
    assert result.payload["high_level_v"] == pytest.approx(0.0003)
    assert result.payload["low_level_v"] == pytest.approx(-0.0002)


def test_anritsu_provider_does_not_import_adapter_or_visa_modules() -> None:
    from pathlib import Path

    for path in Path("app/devices").glob("*/sweep_provider.py"):
        source = path.read_text(encoding="utf-8")
        assert "adapter import" not in source
        assert "visa" not in source.lower()
