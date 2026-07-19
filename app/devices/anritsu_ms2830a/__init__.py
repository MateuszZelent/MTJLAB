"""Complete vertical module for the Anritsu MS2830A."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "MODULE",
    "ANRITSU_FREQUENCY_OPTIONS",
    "ANRITSU_PREAMPLIFIER_OPTIONS",
    "ANRITSU_SIGNAL_GENERATOR_OPTIONS",
    "AdvancedSpectrumConfig",
    "AdvancedSpectrumSnapshot",
    "AnritsuAdapter",
    "AnritsuConfigurationSnapshot",
    "AnritsuFrequencyOption",
    "ReferenceSpectrum",
    "SignalGeneratorConfig",
    "SignalGeneratorSnapshot",
    "SpectrumConfig",
    "SpectrumTrace",
    "frequency_option_for",
    "parse_anritsu_option_response",
]

_HARDWARE_EXPORTS = frozenset(
    {
        "ANRITSU_FREQUENCY_OPTIONS",
        "ANRITSU_PREAMPLIFIER_OPTIONS",
        "ANRITSU_SIGNAL_GENERATOR_OPTIONS",
        "AnritsuFrequencyOption",
        "frequency_option_for",
        "parse_anritsu_option_response",
    }
)


def __getattr__(name: str) -> Any:
    """Resolve domain symbols lazily and keep the Qt manifest isolated."""

    if name == "MODULE":
        return getattr(
            import_module("app.devices.anritsu_ms2830a.module"),
            name,
        )
    if name not in __all__:
        raise AttributeError(name)
    module_name = (
        "app.devices.anritsu_ms2830a.hardware"
        if name in _HARDWARE_EXPORTS
        else "app.devices.anritsu_ms2830a.adapter"
    )
    return getattr(import_module(module_name), name)
