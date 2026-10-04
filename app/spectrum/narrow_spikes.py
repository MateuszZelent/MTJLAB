"""Bounded, width-gated spectral outlier replacement for display only.

Inspired by spectral Hampel filtering, with separate geometry checks on the
original trace. Width is half local prominence in the input ordinate domain;
it is not a physical power FWHM when that domain is dB. No temporal history,
training, FFT, hardware access or claim of identifying interference is used.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import median_filter

from app.domain.quantities import DIMENSION_FREQUENCY, format_quantity_auto

_MAX_WINDOW_BINS = 257


@dataclass(frozen=True, slots=True)
class NarrowSpikeResult:
    values: tuple[float, ...]
    modified_indices: tuple[int, ...]
    peak_indices: tuple[int, ...]
    noise_scale: float
    notes: tuple[str, ...] = ()


def _median(values, window):
    """Native sliding rank filter; preserve the centered odd-window median.

    Nearest-edge extension matches the former padded NumPy windows exactly.
    SciPy's 1-D implementation maintains the moving rank instead of copying
    and partitioning every overlapping window (three passes per spectrum).
    """
    return median_filter(values, size=window, mode="nearest")


def _crossing(x, y, center, boundary, level, direction):
    index = center
    while index != boundary:
        following = index + direction
        if y[following] <= level:
            fraction = (level - y[index]) / (y[following] - y[index])
            return float(x[index] + fraction * (x[following] - x[index]))
        index = following
    return None


def filter_narrow_spikes(frequencies_hz: Sequence[float], values: Sequence[float], *,
                         maximum_width_hz: float = 6e6, threshold_sigma: float = 6,
                         protected_regions_hz: tuple[tuple[float, float], ...] = ()) -> NarrowSpikeResult:
    """Replace only isolated, significant, two-sided narrow excursions.

    Reject broad cores, broad tails, protected intervals, edge features and
    crowded spectra. Values outside the replacement mask are bit-for-bit
    unchanged. Geometry uses actual Hz, including on a descending grid.
    Window bound is explicit; unsupported fine grids fail rather than silently
    changing the requested width. MAD is a heuristic scale, not an uncertainty.
    """
    if not math.isfinite(maximum_width_hz) or maximum_width_hz <= 0:
        raise ValueError("Maximum narrow-peak width must be a positive finite frequency.")
    if not math.isfinite(threshold_sigma) or not 3 <= threshold_sigma <= 30:
        raise ValueError("Narrow-peak threshold must be between 3 and 30 noise scales.")
    if len(protected_regions_hz) > 32:
        raise ValueError("At most 32 protected frequency regions are supported.")
    for lower, upper in protected_regions_hz:
        if not all(math.isfinite(v) for v in (lower, upper)) or not 0 <= lower < upper:
            raise ValueError("Protected frequency intervals must be finite, nonnegative and ordered.")
    x, y = np.asarray(frequencies_hz, dtype=float), np.asarray(values, dtype=float)
    if x.ndim != 1 or y.shape != x.shape or x.size < 5:
        raise ValueError("Narrow-peak filtering requires at least five matching frequency/value points.")
    if x.size > 100_001:
        raise ValueError("Narrow-peak filtering exceeds the 100001-point buffer budget.")
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise ValueError("Narrow-peak filtering requires finite frequency and amplitude values.")
    delta = np.diff(x)
    descending = bool(np.all(delta < 0))
    if not descending and not np.all(delta > 0):
        raise ValueError("Narrow-peak frequency grid must be strictly monotonic.")
    if descending:
        x, y = x[::-1], y[::-1]
        delta = -delta[::-1]
    step_hz = float(np.median(delta))
    if not np.allclose(delta, step_hz, rtol=1e-6, atol=step_hz * 1e-9):
        raise ValueError("Narrow-peak filtering requires a uniform frequency grid.")
    width_bins = maximum_width_hz / step_hz
    if width_bins < 1:
        return NarrowSpikeResult(tuple(map(float, y[::-1] if descending else y)), (), (), 0,
                                 ("Width below one frequency-grid step; no bins replaced.",))
    window = max(9, 2 * math.ceil(4 * width_bins) + 1)
    if window > _MAX_WINDOW_BINS:
        supported_width_hz = ((_MAX_WINDOW_BINS - 1) // 8) * step_hz
        maximum = format_quantity_auto(supported_width_hz, DIMENSION_FREQUENCY)
        requested = format_quantity_auto(maximum_width_hz, DIMENSION_FREQUENCY)
        raise ValueError(
            f"Maximum peak width {requested} exceeds the supported {maximum} "
            "for this frequency grid. Reduce Maximum peak width in Parameters."
        )
    if window > x.size:
        return NarrowSpikeResult(tuple(map(float, y[::-1] if descending else y)), (), (), 0,
                                 ("Trace too short for two-sided narrow-peak filtering.",))
    baseline = _median(y, window)
    residual = y - baseline
    if not np.all(np.isfinite(residual)):
        raise ValueError("Narrow-peak residual overflow; trace left unfiltered.")
    local_center = _median(residual, window)
    scale = 1.4826 * _median(np.abs(residual - local_center), window)
    numerical_floor = max(float(np.max(np.abs(y))) * np.finfo(float).eps * 64,
                          np.finfo(float).tiny)
    scale = np.maximum(scale, numerical_floor)
    above = np.abs(residual - local_center) > threshold_sigma * scale
    positive_max = np.r_[False, (y[1:-1] >= y[:-2]) & (y[1:-1] >= y[2:])
                        & ((y[1:-1] > y[:-2]) | (y[1:-1] > y[2:])), False]
    negative_max = np.r_[False, (y[1:-1] <= y[:-2]) & (y[1:-1] <= y[2:])
                        & ((y[1:-1] < y[:-2]) | (y[1:-1] < y[2:])), False]
    candidates = np.flatnonzero(above & (((residual > 0) & positive_max) | ((residual < 0) & negative_max)))
    if candidates.size > 512:
        return NarrowSpikeResult(tuple(map(float, y[::-1] if descending else y)), (), (), float(np.median(scale)),
                                 ("Too many candidate extrema; crowded trace left unchanged.",))
    radius = window // 2
    intervals = []
    for center in candidates:
        if center < radius or center + radius >= y.size:
            continue  # Neither fabricate edge anchors nor extrapolate.
        sign = 1 if residual[center] > 0 else -1
        # Local original-trace prominence bounded by nearest higher point.
        left, right = center - radius, center + radius
        z = sign * y[left:right + 1]
        offset = radius
        higher_left = np.flatnonzero(z[:offset] > z[offset])
        higher_right = np.flatnonzero(z[offset + 1:] > z[offset])
        lo = int(higher_left[-1] + 1) if higher_left.size else 0
        hi = int(offset + 1 + higher_right[0] - 1) if higher_right.size else z.size - 1
        prominence = z[offset] - max(float(z[lo:offset + 1].min()), float(z[offset:hi + 1].min()))
        if prominence <= threshold_sigma * scale[center]:
            continue
        xx = x[left:right + 1]
        half_left = _crossing(xx, z, offset, lo, z[offset] - .5 * prominence, -1)
        half_right = _crossing(xx, z, offset, hi, z[offset] - .5 * prominence, 1)
        if half_left is None or half_right is None or half_right - half_left > maximum_width_hz:
            continue
        shoulder_left = _crossing(xx, z, offset, lo, z[offset] - .8 * prominence, -1)
        shoulder_right = _crossing(xx, z, offset, hi, z[offset] - .8 * prominence, 1)
        if (shoulder_left is None or shoulder_right is None
                or shoulder_right - shoulder_left > 3 * maximum_width_hz):
            continue  # A narrow core with wide shoulders remains unchanged.
        zz = sign * residual[left:right + 1]
        # Residual is referenced to its robust baseline, not a neighboring
        # opposite-sign dip. Such a dip must not inflate this spike's extent.
        tail_left = _crossing(xx, zz, offset, lo, .05 * zz[offset], -1)
        tail_right = _crossing(xx, zz, offset, hi, .05 * zz[offset], 1)
        if tail_left is None or tail_right is None or tail_right - tail_left > 3 * maximum_width_hz:
            continue
        first = max(1, int(np.searchsorted(x, tail_left)) - 1)
        last = min(y.size - 2, int(np.searchsorted(x, tail_right)))
        # Whole feature including interpolation anchors must avoid protected band.
        if any(x[first - 1] <= upper and x[last + 1] >= lower for lower, upper in protected_regions_hz):
            continue
        intervals.append((first, last, int(center)))
    mask = np.zeros(y.size, dtype=bool)
    for first, last, _center in intervals:
        mask[first:last + 1] = True
    # Merge overlapping signed spikes before selecting clean endpoint anchors.
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    runs = list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))
    for first, stop in runs:
        if (x[stop] - x[first - 1] > 3 * maximum_width_hz + 4 * step_hz
                or any(x[first - 1] <= upper and x[stop] >= lower for lower, upper in protected_regions_hz)):
            mask[first:stop] = False
    if np.count_nonzero(mask) > .1 * y.size:
        mask[:] = False
        notes = ("Replacement would exceed 10% of the trace; trace left unchanged.",)
    else:
        notes = ()
    cleaned = y.copy()
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    for first, stop in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
        # Interpolate only the residual above the robust baseline. This retains
        # broad baseline curvature better than a straight bridge of the raw y.
        cleaned[first:stop] = baseline[first:stop] + np.interp(
            x[first:stop], [x[first - 1], x[stop]], [residual[first - 1], residual[stop]])
    centers = [center for _first, _last, center in intervals if mask[center]]
    if descending:
        cleaned, mask = cleaned[::-1], mask[::-1]
        centers = [y.size - 1 - center for center in centers]
    return NarrowSpikeResult(tuple(map(float, cleaned)), tuple(map(int, np.flatnonzero(mask))),
                             tuple(sorted(set(centers))), float(np.median(scale)), notes)
