"""Explicit, portable processing policy for each recipe acquisition block."""

import math
from dataclasses import asdict, replace
from pathlib import Path

from app.domain.quantities import DIMENSION_FREQUENCY, format_quantity_auto, parse_quantity
from app.spectrum.analysis import SpectrumAnalysisParameters

REFERENCE_OPERATIONS = (
    ("None — raw spectrum", "none"),
    ("Remove background — signed W", "subtract_power_signed"),
    ("Difference in dB", "difference_db"),
    ("Linear ratio", "ratio_linear"),
    ("Add power", "add_power"),
    ("Subtract power — positive residual only (dBm)", "subtract_power"),
    ("Multiply linear", "multiply_linear"),
)
FILTERS = ("narrow_reject", "emi_reject", "denoise")
REFERENCE_UNITS = {
    "none": "dBm",
    "difference_db": "dB",
    "ratio_linear": "ratio",
    "add_power": "dBm",
    "subtract_power": "dBm",
    "multiply_linear": "mW²",
    "subtract_power_signed": "W",
}
_SCALARS = {
    "narrow_threshold_sigma",
    "denoise_window",
    "emi_min_frames",
    "emi_threshold_db",
    "emi_max_std_db",
}


def acquisition_summary(fields, *, reference_only=False):
    """One operator-facing description shared by Builder and Execution."""
    if reference_only and fields.get("source_file"):
        return (
            f"Load {fields.get('file_kind', 'reference')} · {Path(str(fields['source_file'])).name}"
        )
    stages = [f"{fields.get('average_count', 1)} complete sweeps"]
    if not reference_only:
        operation = str(fields.get("reference_operation", "none"))
        if operation != "none":
            stages.append(
                {value: label for label, value in REFERENCE_OPERATIONS}.get(
                    operation, str(operation)
                )
            )
        labels = {"narrow_reject": "Narrow peaks", "emi_reject": "EMI lines", "denoise": "Denoise"}
        processing = fields.get("processing") or {}
        if isinstance(processing, dict):
            modes = processing.get("filters", [])
            if not isinstance(modes, (tuple, list)):
                return "Invalid filters — validate recipe"
            stages.extend(
                labels.get(mode, str(mode))
                for mode in modes
                if isinstance(mode, str)
            )
    return " → ".join(stages)


def parse_processing(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {"filters", "parameters"}:
        raise ValueError("Spectrum processing supports only filters and parameters.")
    filters = value.get("filters", [])
    if not isinstance(filters, list) or any(
        not isinstance(item, str) or item not in FILTERS for item in filters
    ):
        raise ValueError("Choose Narrow peaks, EMI lines or Denoise as spectrum filters.")
    if len(filters) != len(set(filters)):
        raise ValueError("A spectrum filter can appear only once.")
    raw = value.get("parameters", {})
    if not isinstance(raw, dict) or set(raw) - (_SCALARS | {"narrow_max_width", "protected_bands"}):
        raise ValueError("Unknown recipe spectrum processing parameter.")
    changes = {}
    for name in _SCALARS & raw.keys():
        number = raw[name]
        integer = name in {"denoise_window", "emi_min_frames"}
        if type(number) not in ({int} if integer else {int, float}):
            raise ValueError(f"{name} must be {'an integer' if integer else 'a number'}.")
        changes[name] = number
    if "narrow_max_width" in raw:
        changes["narrow_max_width_hz"] = parse_quantity(
            raw["narrow_max_width"], DIMENSION_FREQUENCY
        ).si_value
    bands = raw.get("protected_bands", [])
    if not isinstance(bands, list) or any(
        not isinstance(band, (list, tuple)) or len(band) != 2 for band in bands
    ):
        raise ValueError("Protected bands must be pairs of explicit frequencies.")
    changes["narrow_protected_regions_hz"] = tuple(
        tuple(parse_quantity(v, DIMENSION_FREQUENCY).si_value for v in band) for band in bands
    )
    parameters = replace(SpectrumAnalysisParameters(), **changes)
    if not 3 <= parameters.denoise_window <= 51 or parameters.denoise_window % 2 != 1:
        raise ValueError("Denoise window must be an odd number from 3 to 51 bins.")
    if not 3 <= parameters.emi_min_frames <= 24:
        raise ValueError("EMI requires 3 to 24 sweeps within one point.")
    for name in (
        "narrow_threshold_sigma",
        "narrow_max_width_hz",
        "emi_threshold_db",
        "emi_max_std_db",
    ):
        value = getattr(parameters, name)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive.")
    return tuple(mode for mode in FILTERS if mode in filters), parameters


def processing_mapping(filters, parameters):
    values = asdict(parameters)
    result = {
        "filters": list(filters),
        "parameters": {
            **{key: values[key] for key in sorted(_SCALARS)},
            "narrow_max_width": format_quantity_auto(
                parameters.narrow_max_width_hz, DIMENSION_FREQUENCY, precision=17
            ),
            "protected_bands": [
                [format_quantity_auto(v, DIMENSION_FREQUENCY, precision=17) for v in band]
                for band in parameters.narrow_protected_regions_hz
            ],
        },
    }
    parse_processing(result)
    return result
