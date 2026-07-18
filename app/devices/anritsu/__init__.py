"""Anritsu MS2830A adapter."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
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
    """Resolve public symbols lazily so hardware constants do not import the adapter."""

    if name not in __all__:
        raise AttributeError(name)
    module_name = (
        "app.devices.anritsu.hardware"
        if name in _HARDWARE_EXPORTS
        else "app.devices.anritsu.adapter"
    )
    return getattr(import_module(module_name), name)
