"""Spectral stripe correction using bands from an independent background."""

import math
import re
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

from app.domain.quantities import DIMENSION_FREQUENCY, format_quantity_auto, parse_quantity
from app.domain.spectrum_correction import FloatVector

from .narrow_spikes import filter_narrow_spikes
from .processing import apply_reference_operation
from .streaming_statistics import finite_vector

FrequencyBand = tuple[float, float]

_MIN_BACKGROUND_PEAK_RATIO = 10 ** (1.0 / 10)  # 1 dB of local background contrast.
_MIN_LIVE_FLOOR_RATIO = 10 ** (-1.0 / 10)       # Allow about 1 dB of local variation.


def parse_frequency_bands(text: str) -> tuple[FrequencyBand, ...]:
    bands = []
    for entry in filter(str.strip, re.split(r"[;\n]", text)):
        bounds = entry.split("..")
        if len(bounds) != 2:
            raise ValueError("Use '797 MHz .. 824 MHz' for each band.")
        low, high = (parse_quantity(value.strip(), DIMENSION_FREQUENCY).si_value for value in bounds)
        if not 0 <= low < high:
            raise ValueError(f"Frequency band must have increasing nonnegative bounds ({entry.strip()}).")
        bands.append((low, high))
    return tuple(bands)


@dataclass(frozen=True, slots=True)
class StripeBands:
    bands_hz: tuple[FrequencyBand, ...]
    protected_hz: tuple[FrequencyBand, ...] = ()
    shoulder_hz: float = 5e6

    def __post_init__(self) -> None:
        if not self.bands_hz:
            raise ValueError("Enter at least one stripe band.")
        if not math.isfinite(self.shoulder_hz) or self.shoulder_hz <= 0:
            raise ValueError("Shoulder width must be a positive frequency.")
        for low, high in (*self.bands_hz, *self.protected_hz):
            if not math.isfinite(high) or not 0 <= low < high:
                raise ValueError("Frequency bands must have finite, increasing nonnegative bounds.")


def build_stripe_offsets(
    frequencies_hz: ArrayLike, background_w: ArrayLike, settings: StripeBands,
) -> FloatVector:
    """Estimate only the signed departure from the local baseline, in W."""
    frequencies = finite_vector(frequencies_hz)
    background = finite_vector(background_w, size=frequencies.size)
    if np.any(np.diff(frequencies) <= 0) or np.any(background < 0):
        raise ValueError("Stripe correction requires an increasing grid and nonnegative background power.")

    merged: list[FrequencyBand] = []
    for low, high in sorted(settings.bands_hz):
        if merged and low <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(high, merged[-1][1])
        else:
            merged.append((low, high))

    protected = np.zeros(frequencies.size, dtype=bool)
    for low, high in settings.protected_hz:
        protected |= (frequencies >= low) & (frequencies <= high)
    excluded = protected.copy()
    for low, high in merged:
        excluded |= (frequencies >= low) & (frequencies <= high)

    offsets = np.zeros_like(background)
    supported = False
    for low, high in merged:
        core = (frequencies >= low) & (frequencies <= high) & ~protected
        if not core.any():
            continue
        left = (frequencies >= low - settings.shoulder_hz) & (frequencies < low) & ~excluded
        right = (frequencies > high) & (frequencies <= high + settings.shoulder_hz) & ~excluded
        if min(np.count_nonzero(left), np.count_nonzero(right)) < 3:
            start = format_quantity_auto(low, DIMENSION_FREQUENCY)
            stop = format_quantity_auto(high, DIMENSION_FREQUENCY)
            raise ValueError(f"{start} .. {stop} needs at least three bins on each shoulder. Increase the shoulder width.")
        anchors_hz = (np.median(frequencies[left]), np.median(frequencies[right]))
        anchors_w = (np.median(background[left]), np.median(background[right]))
        offsets[core] = background[core] - np.interp(frequencies[core], anchors_hz, anchors_w)
        supported = True
    if not supported:
        raise ValueError("No stripe bins are available on this grid outside the protected bands.")
    offsets.setflags(write=False)
    return offsets


def stripe_band_mask(
    frequencies_hz: ArrayLike, bands_hz: tuple[FrequencyBand, ...],
    protected_hz: tuple[FrequencyBand, ...] = (),
) -> NDArray[np.bool_]:
    frequencies = finite_vector(frequencies_hz)
    masked = np.zeros(frequencies.size, dtype=bool)
    for low, high in bands_hz:
        masked |= (frequencies >= low) & (frequencies <= high)
    for low, high in protected_hz:
        masked &= (frequencies < low) | (frequencies > high)
    return masked


@dataclass(frozen=True, slots=True)
class StripeReconstruction:
    values: FloatVector
    estimated_bands_hz: tuple[FrequencyBand, ...]
    estimated_count: int
    skipped_count: int


def reconstruct_stripe_bands(
    frequencies_hz: ArrayLike, values: ArrayLike, bands_hz: tuple[FrequencyBand, ...],
    protected_hz: tuple[FrequencyBand, ...] = (),
) -> StripeReconstruction:
    """Estimate broad structure between measured bins in the input amplitude units."""
    frequencies = finite_vector(frequencies_hz)
    observed = np.asarray(values, dtype=float)
    if (observed.shape != frequencies.shape or frequencies.size < 2
            or np.any(np.diff(frequencies) <= 0) or np.any(np.isinf(observed))):
        raise ValueError("Stripe reconstruction needs an increasing frequency grid and matching spectrum.")

    contaminated = stripe_band_mask(frequencies, bands_hz)
    protected = stripe_band_mask(frequencies, protected_hz)
    finite = np.isfinite(observed)
    requested = contaminated & ~protected
    usable = finite & ~contaminated & ~protected
    anchors = frequencies[usable]
    if anchors.size < 2:
        return StripeReconstruction(observed.copy(), (), 0, int(requested.sum()))

    # Use only bracketed samples. Undefined input bins and protected peaks
    # remain in place, and excluded peaks cannot serve as interpolation anchors.
    estimated = requested & finite & (frequencies > anchors[0]) & (frequencies < anchors[-1])
    reconstructed = observed.copy()
    reconstructed[estimated] = np.interp(frequencies[estimated], anchors, observed[usable])
    edges = np.flatnonzero(np.diff(np.r_[False, estimated, False]))
    intervals = tuple(
        (float((frequencies[start - 1] + frequencies[start]) / 2),
         float((frequencies[stop - 1] + frequencies[stop]) / 2))
        for start, stop in zip(edges[::2], edges[1::2], strict=True)
    )
    count = int(estimated.sum())
    return StripeReconstruction(reconstructed, intervals, count, int(requested.sum()) - count)


def apply_stripe_reference(
    signal_w: FloatVector, reference_w: FloatVector, operation: str,
) -> tuple[tuple[float, ...], str, int]:
    """Compare de-striped powers, leaving gaps where the logarithm is undefined."""
    positive = np.isfinite(signal_w) & np.isfinite(reference_w) & (signal_w > 0) & (reference_w > 0)
    # The reference operation takes finite dBm. Mask undefined bins after
    # evaluating it so they never appear as a made-up level on the plot.
    signal_dbm = 10 * np.log10(np.where(positive, signal_w, 1.0)) + 30
    reference_dbm = 10 * np.log10(np.where(positive, reference_w, 1.0)) + 30
    values, unit = apply_reference_operation(signal_dbm, reference_dbm, operation)
    missing = int(positive.size - np.count_nonzero(positive))
    if missing:
        values = tuple(np.where(positive, values, np.nan).tolist())
    return values, unit, missing


@dataclass(frozen=True, slots=True, eq=False)
class _AutomaticStripeBand:
    bounds_hz: FrequencyBand
    bins: slice
    peak: int
    left: NDArray[np.intp]
    right: NDArray[np.intp]
    interpolation: FloatVector

    def local_floor(self, power_w: FloatVector) -> FloatVector:
        left_w = np.median(power_w[self.left])
        right_w = np.median(power_w[self.right])
        return (1.0 - self.interpolation) * left_w + self.interpolation * right_w


def _offset_supported(measured_w, floor_w, offset_w, peak: int) -> bool:
    if offset_w[peak] == 0:
        return True
    remaining = measured_w - offset_w
    core = offset_w >= 0.25 * offset_w[peak]
    return bool(
        measured_w[peak] - floor_w[peak] >= 0.5 * offset_w[peak]
        and np.all(remaining > 0)
        and np.all(remaining[core] >= _MIN_LIVE_FLOOR_RATIO * floor_w[core])
    )


def _wing_gain(measured_w, floor_w, stripe_w, left, right) -> float | None:
    excess = measured_w - floor_w
    detectable = excess - (_MIN_BACKGROUND_PEAK_RATIO - 1) * floor_w
    if max(np.median(detectable[left]), np.median(detectable[right])) <= 0:
        return 0.0
    ratios = (excess[left] / stripe_w[left], excess[right] / stripe_w[right])
    medians = tuple(np.median(side) for side in ratios)
    low, high = sorted(medians)
    if low <= 0 or high > 1.25 * low:
        return None
    if any(np.median(np.abs(side - center)) > .20 * low for side, center in zip(ratios, medians, strict=True)):
        return None
    return float(np.median(np.concatenate(ratios)))


def _single_trace_gain(measured_w, floor_w, stripe_w, peak: int) -> float | None:
    if _offset_supported(measured_w, floor_w, stripe_w, peak):
        return 1.0
    if measured_w[peak] <= _MIN_BACKGROUND_PEAK_RATIO * floor_w[peak]:
        return 0.0
    return None


@dataclass(frozen=True, slots=True, eq=False)
class StripeReferenceBalance:
    signal_w: FloatVector
    reference_w: FloatVector
    unresolved_bands_hz: tuple[FrequencyBand, ...]


@dataclass(frozen=True, slots=True, eq=False)
class AutomaticStripeTemplate:
    offsets_w: FloatVector
    _bands: tuple[_AutomaticStripeBand, ...]
    notes: tuple[str, ...]
    reconstruction_bands_hz: tuple[FrequencyBand, ...] = ()

    @property
    def bands_hz(self) -> tuple[FrequencyBand, ...]:
        return tuple(band.bounds_hz for band in self._bands)

    def offsets_for(self, input_w: ArrayLike) -> tuple[FloatVector, tuple[FrequencyBand, ...]]:
        """Use the fixed estimate only where the current line still supports it."""
        power = finite_vector(input_w, size=self.offsets_w.size)
        offsets = self.offsets_w.copy()
        skipped = []
        for band in self._bands:
            correction = offsets[band.bins]
            measured = power[band.bins]
            floor = band.local_floor(power)
            if not _offset_supported(measured, floor, correction, band.peak):
                correction.fill(0)
                skipped.append(band.bounds_hz)
        return offsets, tuple(skipped)

    def balance_reference(self, signal_w: ArrayLike, reference_w: ArrayLike) -> StripeReferenceBalance:
        """Match the variable part of a resolved line before a relative dB comparison."""
        signal = finite_vector(signal_w, size=self.offsets_w.size)
        reference = finite_vector(reference_w, size=self.offsets_w.size)
        matched_signal, matched_reference = signal.copy(), reference.copy()
        unresolved = []
        for band in self._bands:
            stripe = self.offsets_w[band.bins]
            peak = band.peak
            measured, baseline = signal[band.bins], reference[band.bins]
            measured_floor, baseline_floor = band.local_floor(signal), band.local_floor(reference)
            wing = (stripe >= .20 * stripe[peak]) & (stripe <= .85 * stripe[peak])
            left, right = np.flatnonzero(wing[:peak]), np.flatnonzero(wing[peak + 1:]) + peak + 1
            # Fit the wings outside the center so a narrow new feature at
            # the same frequency cannot set the scale of the broader line.
            resolved = min(left.size, right.size) >= 2
            if resolved:
                signal_gain = _wing_gain(measured, measured_floor, stripe, left, right)
                reference_gain = _wing_gain(baseline, baseline_floor, stripe, left, right)
            else:
                signal_gain = _single_trace_gain(measured, measured_floor, stripe, peak)
                reference_gain = _single_trace_gain(baseline, baseline_floor, stripe, peak)
            if signal_gain is None or reference_gain is None:
                unresolved.append(band.bounds_hz)
                continue
            # Keep a shared carrier in both operands of the ratio. Subtracting
            # two nearly equal absolute powers amplifies small shape errors.
            shared = min(signal_gain, reference_gain) if resolved else 0.0
            signal_offset = (signal_gain - shared) * stripe
            reference_offset = (reference_gain - shared) * stripe
            if not (_offset_supported(measured, measured_floor, signal_offset, peak)
                    and _offset_supported(baseline, baseline_floor, reference_offset, peak)):
                unresolved.append(band.bounds_hz)
                continue
            matched_signal[band.bins] -= signal_offset
            matched_reference[band.bins] -= reference_offset
        return StripeReferenceBalance(matched_signal, matched_reference, tuple(unresolved))


@dataclass(frozen=True, slots=True)
class AutomaticStripeSettings:
    protected_hz: tuple[FrequencyBand, ...] = ()


def build_automatic_stripe_template(
    frequencies_hz: ArrayLike, background_w: ArrayLike, sample_variance_w2: ArrayLike,
    *, rbw_hz: float, protected_hz: tuple[FrequencyBand, ...] = (),
) -> AutomaticStripeTemplate:
    """Detect narrow positive excess using a separately collected background.

    The background's sweep scatter screens bands for amplitude subtraction.
    Reconstruction can also use bands with variable amplitudes.
    """
    frequencies = finite_vector(frequencies_hz)
    power = finite_vector(background_w, size=frequencies.size)
    variance = finite_vector(sample_variance_w2, size=frequencies.size)
    if not math.isfinite(rbw_hz) or rbw_hz <= 0:
        raise ValueError("Automatic Stripes needs a verified positive resolution bandwidth.")
    steps_hz = np.diff(frequencies)
    if frequencies[0] < 0 or np.any(steps_hz <= 0) or np.any(power < 0) or np.any(variance < 0):
        raise ValueError("Automatic Stripes needs increasing frequencies and nonnegative background power and variance.")
    step_hz = float(np.median(steps_hz))
    # Keep the rank filter's neighborhoods bounded even in a highly
    # oversampled sweep. Six RBWs cover several bins in wideband sweeps.
    maximum_width_hz = min(max(6 * rbw_hz, 2 * step_hz), 30 * step_hz)
    result = filter_narrow_spikes(
        frequencies, power, maximum_width_hz=maximum_width_hz, protected_regions_hz=protected_hz,
    )
    detected = np.zeros(power.size, dtype=bool)
    detected[list(result.modified_indices)] = True
    flank_ok = ~detected
    for low, high in protected_hz:
        flank_ok &= (frequencies < low) | (frequencies > high)
    width = max(5, math.ceil(maximum_width_hz / step_hz))
    edges = np.diff(np.r_[False, detected, False].astype(np.int8))
    offsets = np.zeros_like(power)
    bands = []
    reconstruction_bands = []
    peaks = np.asarray(result.peak_indices, dtype=np.intp)
    estimated_excess = power - np.asarray(result.values)
    for first, stop in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True):
        left_first, right_stop = max(0, first - width), min(power.size, stop + width)
        left = np.flatnonzero(flank_ok[left_first:first]) + left_first
        right = np.flatnonzero(flank_ok[stop:right_stop]) + stop
        if min(left.size, right.size) < 3:
            continue
        candidates = peaks[(peaks >= first) & (peaks < stop)]
        if candidates.size == 0:
            continue
        anchor_low, anchor_high = np.median(frequencies[left]), np.median(frequencies[right])
        bins = slice(first, stop)
        fraction = (frequencies[bins] - anchor_low) / (anchor_high - anchor_low)
        peak = int(candidates[np.argmax(estimated_excess[candidates])] - first)
        bounds_hz = float(frequencies[first]), float(frequencies[stop - 1])
        band = _AutomaticStripeBand(bounds_hz, bins, peak, left, right, fraction)
        floor = band.local_floor(power)
        excess = np.maximum(power[bins] - floor, 0.0)
        peak_excess = excess[peak]
        if (estimated_excess[first + peak] <= 0
                or peak_excess <= floor[peak] * (_MIN_BACKGROUND_PEAK_RATIO - 1)):
            continue
        reconstruction_bands.append(bounds_hz)
        if peak_excess <= 2 * math.sqrt(variance[first + peak]):
            continue
        for array in (left, right, fraction):
            array.setflags(write=False)
        offsets[bins] = excess
        bands.append(band)
    offsets.setflags(write=False)
    return AutomaticStripeTemplate(offsets, tuple(bands), result.notes, tuple(reconstruction_bands))
