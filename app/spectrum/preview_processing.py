"""Shared, bounded power averaging and correction for all passive previews.

Received frames are not assumed to be independent sweeps. Scatter is descriptive;
neither a confidence interval nor an improvement in instrumental resolution is
inferred. All arrays, model fitting and filter work belong on a CPU worker.
"""

import json
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace

import numpy as np

from .analysis import SpectrumAnalysisParameters, clean_spectrum_pipeline
from .interference_model import calibrated_interference_model
from .processing import apply_reference_operation
from .streaming_statistics import dbm_to_w


@dataclass(frozen=True, slots=True)
class PreviewStatistics:
    frame_count: int
    target_frames: int
    duration_s: float
    median_temporal_scatter_w: float | None
    reference_sweeps: int = 0
    reference_mean_uncertainty_w: float | None = None
    model_id: str | None = None
    # No finite-frame noise heuristic is promoted to statistical qualification.
    uncertainty_qualified: bool = False


class SpectrumPreviewProcessor:
    """One implementation for Spectrum, floating mirrors and Spectrogram rows."""

    CACHE_BYTES = 32 * 1024 * 1024

    def __init__(self):
        self._power_cache = OrderedDict()
        self._cache_bytes = 0
        self._calibration = None
        self._model = None

    def _watts(self, row):
        # Hold the immutable input alongside its conversion to prevent id reuse.
        # Public callers may also pass mutable arrays/lists. Recompute those
        # so in-place changes can never reuse a stale power conversion.
        if not (isinstance(row, tuple) or isinstance(row, np.ndarray) and not row.flags.writeable):
            return dbm_to_w(row)
        key = id(row)
        cached = self._power_cache.get(key)
        if cached is not None and cached[0] is row:
            self._power_cache.move_to_end(key)
            return cached[1]
        power = dbm_to_w(row)
        retained_bytes = power.nbytes + (row.nbytes if isinstance(row, np.ndarray) else len(row) * 32) + 128
        while self._power_cache and self._cache_bytes + retained_bytes > self.CACHE_BYTES:
            _key, (_source, _power, size) = self._power_cache.popitem(last=False)
            self._cache_bytes -= size
        if retained_bytes <= self.CACHE_BYTES:
            self._power_cache[key] = (row, power, retained_bytes)
            self._cache_bytes += retained_bytes
        return power

    def _background_model(self, calibration, profile, context, parameters):
        if calibration is None:
            self._calibration = self._model = None
            return None
        if profile is None or context is None:
            raise ValueError("Load a matching background before selecting a drift model.")
        if (calibration.context.context_id != context.context_id
                or calibration.source_profiles != ((profile.profile_id, profile.content_hash),)
                or not np.array_equal(calibration.baseline_w, profile.mean_w)):
            raise ValueError("Drift model does not match the active background and analyzer settings.")
        if not calibration.signal_control_regions_qualified:
            raise ValueError("Drift model needs independently qualified signal-free control bands.")
        controls = calibration.control_mask & ~calibration.protected_mask
        for low, high in parameters.narrow_protected_regions_hz:
            if np.any(controls & (context.frequencies_hz >= low) & (context.frequencies_hz <= high)):
                raise ValueError("Protected signal bands overlap model control bands. Retrain with matching protection.")
        if self._calibration is not calibration:
            estimate = 4 * calibration.basis_w.nbytes + 64 * len(calibration.baseline_w)
            if estimate > self.CACHE_BYTES:
                raise ValueError("Drift model exceeds the preview working-memory budget.")
            model = calibrated_interference_model(calibration)
            self._calibration, self._model = calibration, model
        return self._model

    def process(self, values, *, frequencies_hz, unit="dBm", modes=(),
                parameters=None, power_rows=(), timestamps_s=(), history=(),
                reference_values_dbm=None, reference_operation="none",
                background_profile=None, background_context=None,
                interference_calibration=None, cleaner=clean_spectrum_pipeline,
                allow_gaps=False):
        params = parameters or SpectrumAnalysisParameters()
        averaging = params.temporal_average_frames > 1
        reference = reference_operation != "none"
        if reference and (reference_values_dbm is None or "background" in modes):
            raise ValueError("Choose one configured correction: Background or Reference.")
        if interference_calibration is not None and "background" not in modes:
            raise ValueError("Enable Background before applying its drift model.")
        count, duration, scatter = 1, 0.0, None
        provenance = []
        if averaging:
            if unit != "dBm":
                raise ValueError("Power averaging requires absolute input before reference mathematics.")
            rows = tuple(power_rows) if len(power_rows) else (values,)
            times = np.asarray(timestamps_s, dtype=float)
            if times.size and (times.shape != (len(rows),) or not np.all(np.isfinite(times))
                               or np.any(np.diff(times) <= 0)):
                raise ValueError("Preview history needs matching, strictly increasing timestamps.")
            start = max(0, len(rows) - params.temporal_average_frames)
            if times.size:
                gaps = np.flatnonzero(np.diff(times) > params.temporal_max_gap_s)
                if gaps.size:
                    start = max(start, int(gaps[-1]) + 1)
                duration = float(times[-1] - times[start])
            selected = rows[start:]
            if any(len(row) != len(frequencies_hz) for row in selected):
                raise ValueError("Averaging history has incompatible frequency-point counts.")
            # Native array arithmetic; count the actual supplied frames, including
            # those skipped by a coalescing display worker, exactly once.
            matrix = np.stack([self._watts(row) for row in selected])
            watts = np.mean(matrix, axis=0)
            count = len(selected)
            if count > 1:
                scatter = float(np.median(np.std(matrix, axis=0, ddof=1)))
            values = tuple(10 * np.log10(watts) + 30)
            provenance.extend(("power_average", f"frames={count}", f"duration_s={duration:.9g}"))
        if reference:
            values, unit = apply_reference_operation(values, reference_values_dbm, reference_operation)
            provenance.extend(("reference", reference_operation))
        model = self._background_model(interference_calibration, background_profile, background_context, params)
        if model is not None:
            # The model's protected signal support also protects subsequent
            # display filters, even if the user has not repeated those bands.
            axis = np.asarray(frequencies_hz)
            protected = interference_calibration.protected_mask.copy()
            for low, high in params.narrow_protected_regions_hz:
                protected |= (axis >= low) & (axis <= high)
            edges = np.flatnonzero(np.diff(np.r_[False, protected, False].astype(np.int8)))
            regions = tuple((float(axis[start]), float(axis[stop-1]) if stop-start > 1
                             else float(np.nextafter(axis[start], np.inf)))
                            for start, stop in zip(edges[::2], edges[1::2], strict=True))
            params = replace(params, narrow_protected_regions_hz=regions)
        options = {"unit": unit, "modes": modes, "history": history, "parameters": params,
                   "frequencies_hz": frequencies_hz, "background_profile": background_profile,
                   "background_context": background_context, "background_model": model}
        if allow_gaps:
            options["allow_gaps"] = True
        result = cleaner(values, **options)
        method = result.method
        notes = list(result.notes)
        if averaging:
            method = f"Power average {count}/{params.temporal_average_frames} frames → {method}"
            notes.append("Received-frame averaging; independence and standard uncertainty are unqualified.")
        if reference:
            method = f"Reference {reference_operation} → {method}"
        if model is not None:
            method = f"Background drift model {interference_calibration.model_id} → {method}"
            provenance.extend(("drift_model", interference_calibration.model_id, interference_calibration.content_hash))
            notes.append("Reference-trained drift fit on qualified control bands; preview uncertainty unqualified.")
        profile = background_profile if "background" in modes else None
        reference_uncertainty = None
        if profile is not None and profile.mean_variance_w2 is not None:
            # A held reference contribution is shared by all signal frames. It
            # must never be divided by the number of averaged signal frames.
            reference_uncertainty = float(np.median(np.sqrt(profile.mean_variance_w2)))
        stats = PreviewStatistics(count, params.temporal_average_frames, duration, scatter,
                                  profile.sweep_count if profile is not None else 0,
                                  reference_uncertainty,
                                  interference_calibration.model_id if model is not None else None)
        provenance.extend(result.input_provenance)
        provenance.extend(("preview-processing-v1", json.dumps(asdict(params), sort_keys=True, allow_nan=False)))
        result = replace(result, method=method, notes=tuple(notes), input_provenance=tuple(provenance),
                         applied_modes=(("power_average",) if averaging else ()) + result.applied_modes)
        return result, stats
