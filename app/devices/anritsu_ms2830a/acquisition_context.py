"""Canonical identity of analyzer settings shared by archives and display filters."""

import json
from dataclasses import asdict

from .adapter import AdvancedSpectrumSnapshot, AnritsuFullConfigurationReadback


def spectrum_configuration_fingerprint(
    full: AnritsuFullConfigurationReadback,
    advanced: AdvancedSpectrumSnapshot,
    device_idn: str,
) -> str:
    values = asdict(full)
    # Canonical preamplifier evidence remains in advanced settings.
    values.pop("preamplifier_enabled", None)
    # Continuous versus single acquisition does not change the RF input path;
    # sweep-completion evidence remains a separate archive contract.
    values.pop("continuous_sweep", None)
    advanced_values = asdict(advanced)
    # Already represented by full.vbw_mode; keep existing fingerprint identity.
    advanced_values.pop("vbw_filter_mode", None)
    return json.dumps({
        "device_idn": device_idn, "full": values, "advanced": advanced_values,
        "sweep_method": "qualified_single", "trace": "TRAC1",
    }, sort_keys=True, allow_nan=False)
