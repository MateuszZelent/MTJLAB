"""Explicit physical frequency regions on an acquired grid."""

import numpy as np

from app.domain.quantities import DIMENSION_FREQUENCY, parse_quantity


def frequency_region_mask(frequencies_hz, regions):
    if not isinstance(regions, list) or len(regions) > 32:
        raise ValueError("Specify at most 32 explicit frequency intervals.")
    mask = np.zeros(len(frequencies_hz), dtype=bool)
    for pair in regions:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("Frequency regions require [lower quantity, upper quantity].")
        low, high = (parse_quantity(value, DIMENSION_FREQUENCY).si_value for value in pair)
        if low >= high or low < frequencies_hz[0] or high > frequencies_hz[-1]:
            raise ValueError("Frequency intervals must be ordered within the reference grid.")
        selected = (frequencies_hz >= low) & (frequencies_hz <= high)
        if not selected.any():
            raise ValueError("Frequency region selects no acquired bin.")
        mask |= selected
    return mask
