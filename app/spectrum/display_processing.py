"""Shared display filtering that preserves missing spectrum bins."""

import numpy as np

from app.spectrum.analysis import SpectrumCleanupResult, clean_spectrum_pipeline


def _finite_runs(values):
    valid = np.isfinite(values)
    edges = np.flatnonzero(np.diff(np.r_[False, valid, False].astype(np.int8)))
    return tuple(zip(edges[::2], edges[1::2], strict=True))


def clean_display_spectrum(
    values, *, allow_gaps=False, pipeline=clean_spectrum_pipeline, **options
):
    """Filter each finite segment independently, preserving all gaps."""
    array = np.asarray(values, dtype=float)
    if np.all(np.isfinite(array)) or not allow_gaps:
        return pipeline(values, **options)
    if np.any(np.isinf(array)):
        raise ValueError("Reference processing overflowed; infinite powers cannot be displayed.")
    frequencies = options["frequencies_hz"]
    history = options.pop("history", ())
    cleaned = array.copy()
    modified, removed, interference = [], [], []
    methods, notes, noise = [], [], []
    applied_modes = []
    for start, stop in _finite_runs(array):
        if stop - start < 3:
            continue
        result = pipeline(
            array[start:stop],
            **{
                **options,
                "frequencies_hz": frequencies[start:stop],
                "history": tuple(row[start:stop] for row in history),
            },
        )
        cleaned[start:stop] = result.values
        noise.append(result.noise_sigma_db)
        methods.append(result.method)
        applied_modes.extend(result.applied_modes)
        notes.extend(result.notes)
        modified.extend(start + index for index in result.modified_bin_indices)
        removed.extend(start + index for index in result.removed_peak_indices)
        interference.extend(start + index for index in result.stationary_interference_indices)
    gap_note = f"{np.count_nonzero(~np.isfinite(array))} missing spectrum bins retained as gaps."
    if options["unit"] == "dBm":
        gap_note += " Non-positive power is undefined in dBm."
    notes.insert(0, gap_note)
    return SpectrumCleanupResult(
        tuple(cleaned),
        max(noise, default=0.0),
        tuple(interference),
        "; ".join(dict.fromkeys(methods)) or "No finite residual",
        options["unit"],
        tuple(modified),
        tuple(removed),
        tuple(dict.fromkeys(notes)),
        input_values=tuple(array),
        applied_modes=tuple(dict.fromkeys(applied_modes)),
    )
