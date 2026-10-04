"""Coalescing CPU worker for display-only spectrum analysis."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

import numpy as np
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from app.domain.spectrum_correction import BackgroundProfile, SpectrumAcquisitionContext
from app.spectrum import (
    SpectrumAnalysisParameters,
    SpectrumCleanupResult,
    SpectrumPeak,
    apply_reference_operation,
    clean_spectrum_dbm,
    clean_spectrum_pipeline,
    clean_spectrum_values,
    detect_spectrum_peaks,
)
from app.spectrum.preview_processing import PreviewStatistics, SpectrumPreviewProcessor


@dataclass(frozen=True, slots=True)
class SpectrumAnalysisRequest:
    generation: int
    frequencies_hz: tuple[float, ...]
    powers_dbm: tuple[float, ...]
    mode: str | tuple[str, ...]
    history_dbm: tuple[tuple[float, ...] | np.ndarray, ...]
    detect_peaks: bool
    source_key: str = "raw"
    frame_id: int = 0
    source_unit: str = "dBm"
    provenance: tuple[str, ...] = ()
    parameters: SpectrumAnalysisParameters = field(
        default_factory=SpectrumAnalysisParameters
    )
    source_snapshot: object = None
    background_profile: BackgroundProfile | None = None
    background_context: SpectrumAcquisitionContext | None = None
    allow_gaps: bool = False
    raw_snapshot: object = None
    power_rows: tuple = ()
    timestamps_s: tuple[float, ...] = ()
    reference_values_dbm: tuple[float, ...] | None = None
    reference_operation: str = "none"
    interference_calibration: object = None


@dataclass(frozen=True, slots=True)
class SpectrogramAnalysisRequest:
    generation: int
    frequencies_hz: tuple[float, ...]
    timestamps_s: tuple[float, ...]
    rows: tuple[np.ndarray, ...]
    source_unit: str
    modes: tuple[str, ...]
    parameters: SpectrumAnalysisParameters
    cache_key: tuple
    background_profile: BackgroundProfile | None = None
    background_context: SpectrumAcquisitionContext | None = None
    reference_values_dbm: tuple[float, ...] | None = None
    reference_operation: str = "none"
    interference_calibration: object = None
    display_start_s: float | None = None


@dataclass(frozen=True, slots=True)
class SpectrogramAnalysisOutcome:
    generation: int
    frequencies_hz: tuple[float, ...]
    timestamps_s: tuple[float, ...]
    matrix: np.ndarray
    unit: str
    method: str
    cache_key: tuple
    color_levels: tuple[float, float]
    statistics: PreviewStatistics | None = None


def spectrogram_color_levels(matrix: np.ndarray, unit: str) -> tuple[float, float] | None:
    """Estimate contrast from a bounded sample rather than sorting full histories."""
    flat = matrix.reshape(-1)
    sample = flat[::max(1, flat.size // 25_000)]
    finite = sample[np.isfinite(sample)]
    if finite.size == 0:
        finite = flat[np.isfinite(flat)]
    if finite.size == 0:
        return None
    low, high = (float(value) for value in np.percentile(finite, (2., 98.)))
    if low == high:
        padding = max(abs(low) * .05, 1e-15 if unit == "W" else 1.)
        low, high = low - padding, high + padding
    if unit == "W":
        bound = max(abs(low), abs(high))
        low, high = -bound, bound
    return low, high


@dataclass(frozen=True, slots=True)
class SpectrumAnalysisOutcome:
    generation: int
    cleanup: SpectrumCleanupResult
    peaks: tuple[SpectrumPeak, ...] | None
    source_key: str = "raw"
    frame_id: int = 0
    source_unit: str = "dBm"
    provenance: tuple[str, ...] = ()
    frequencies_hz: tuple[float, ...] = ()
    source_snapshot: object = None
    processing_duration_s: float = 0.0
    raw_snapshot: object = None
    statistics: PreviewStatistics | None = None


def _finite_runs(values):
    valid = np.isfinite(values)
    edges = np.flatnonzero(np.diff(np.r_[False, valid, False].astype(np.int8)))
    return tuple(zip(edges[::2], edges[1::2], strict=True))


def _clean_display(values, *, allow_gaps=False, **options):
    """Preserve invalid linear-subtraction bins without filtering across gaps."""
    array = np.asarray(values, dtype=float)
    if np.all(np.isfinite(array)) or not allow_gaps:
        return clean_spectrum_pipeline(values, **options)
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
        result = clean_spectrum_pipeline(array[start:stop], **{
            **options, "frequencies_hz": frequencies[start:stop],
            "history": tuple(row[start:stop] for row in history),
        })
        cleaned[start:stop] = result.values
        noise.append(result.noise_sigma_db)
        methods.append(result.method)
        applied_modes.extend(result.applied_modes)
        notes.extend(result.notes)
        modified.extend(start + index for index in result.modified_bin_indices)
        removed.extend(start + index for index in result.removed_peak_indices)
        interference.extend(start + index for index in result.stationary_interference_indices)
    notes.insert(0, f"{np.count_nonzero(~np.isfinite(array))} non-positive power residual bins remain undefined in dBm.")
    return SpectrumCleanupResult(tuple(cleaned), max(noise, default=0.), tuple(interference),
        "; ".join(dict.fromkeys(methods)) or "No finite residual", options["unit"],
        tuple(modified), tuple(removed), tuple(dict.fromkeys(notes)),
        input_values=tuple(array), applied_modes=tuple(dict.fromkeys(applied_modes)))


def _display_peaks(frequencies, values, *, allow_gaps=False, **options):
    if not allow_gaps or np.all(np.isfinite(values)):
        return detect_spectrum_peaks(frequencies, values, **options)
    peaks = []
    for start, stop in _finite_runs(values):
        if stop - start >= 5:
            peaks.extend(replace(peak, index=peak.index + start) for peak in
                detect_spectrum_peaks(frequencies[start:stop], values[start:stop], **options))
    peaks.sort(key=lambda peak: peak.snr_db, reverse=True)
    parameters = options["parameters"]
    accepted = []
    for peak in peaks:
        if any(abs(peak.frequency_hz - other.frequency_hz) < parameters.peak_min_distance_hz for other in accepted):
            continue
        accepted.append(peak)
        if len(accepted) >= parameters.peak_max_count:
            break
    return tuple(accepted)


class _SpectrumAnalysisWorker(QObject):
    completed = Signal(object)
    failed = Signal(int, str)

    def __init__(self):
        super().__init__()
        self._spectrogram_cache_key = None
        self._spectrogram_rows = {}
        self._reference_rows = {}
        self._preview_processor = SpectrumPreviewProcessor()

    def _analyze_spectrogram(self, request):
        if self._spectrogram_cache_key != request.cache_key:
            self._spectrogram_rows.clear()
            self._spectrogram_cache_key = request.cache_key
            self._reference_rows.clear()
        active = set(request.timestamps_s)
        self._spectrogram_rows = {stamp: row for stamp, row in self._spectrogram_rows.items() if stamp in active}
        self._reference_rows = {stamp: row for stamp, row in self._reference_rows.items() if stamp in active}
        reference_enabled = request.reference_operation != "none"
        rows, input_unit = request.rows, request.source_unit
        if reference_enabled:
            if request.reference_values_dbm is None or "background" in request.modes:
                raise ValueError("Choose one configured correction: Background or Reference.")
            converted = []
            for stamp, row in zip(request.timestamps_s, request.rows, strict=True):
                if stamp not in self._reference_rows:
                    values, input_unit = apply_reference_operation(row, request.reference_values_dbm, request.reference_operation)
                    self._reference_rows[stamp] = (values, input_unit)
                values, input_unit = self._reference_rows[stamp]
                converted.append(values)
            rows = tuple(converted)
        outputs = []
        output_times = []
        statistics = None
        unit, method = request.source_unit, "Raw"
        # A completed row's EMI history remains unchanged when later frames
        # arrive. Only invalidate rows whose preceding history was truncated.
        temporal = "emi_reject" in request.modes and "background" not in request.modes
        history_length = max(24 if temporal else 1, request.parameters.temporal_average_frames)
        for index, (stamp, row) in enumerate(zip(request.timestamps_s, rows, strict=True)):
            if QThread.currentThread().isInterruptionRequested():
                raise RuntimeError("Spectrogram processing cancelled during shutdown.")
            if request.display_start_s is not None and stamp < request.display_start_s:
                continue
            start = max(0, index - history_length + 1)
            history_key = (request.timestamps_s[start], index - start + 1) if history_length > 1 else None
            cached = self._spectrogram_rows.get(stamp)
            if cached is not None and cached[3] != history_key:
                cached = None
            if cached is None:
                result, statistics = self._preview_processor.process(
                    request.rows[index], unit=request.source_unit, modes=request.modes, allow_gaps=reference_enabled,
                    parameters=request.parameters, frequencies_hz=request.frequencies_hz,
                    history=rows[max(0, index - 23):index + 1] if temporal else (),
                    power_rows=request.rows[start:index + 1], timestamps_s=request.timestamps_s[start:index + 1],
                    reference_values_dbm=request.reference_values_dbm, reference_operation=request.reference_operation,
                    background_profile=request.background_profile, background_context=request.background_context,
                    interference_calibration=request.interference_calibration, cleaner=_clean_display,
                )
                cached = (np.asarray(result.values, dtype=np.float32), result.unit, result.method, history_key, statistics)
                self._spectrogram_rows[stamp] = cached
            values, unit, method, _history_key, statistics = cached
            outputs.append(values)
            output_times.append(stamp)
        matrix = np.stack(outputs)
        matrix = np.frombuffer(matrix.tobytes(), dtype=matrix.dtype).reshape(matrix.shape)
        levels = spectrogram_color_levels(matrix, unit)
        if levels is None:
            raise ValueError("No finite spectrum values are available for the spectrogram.")
        self.completed.emit(SpectrogramAnalysisOutcome(
            request.generation, request.frequencies_hz, tuple(output_times),
            matrix, unit, method, request.cache_key, levels, statistics))

    @Slot(object)
    def analyze(self, request: object) -> None:
        if isinstance(request, SpectrogramAnalysisRequest):
            try:
                self._analyze_spectrogram(request)
            except Exception as exc:  # noqa: BLE001 - report worker failures through Qt
                self.failed.emit(request.generation, str(exc))
            return
        if not isinstance(request, SpectrumAnalysisRequest):
            self.failed.emit(-1, "Invalid spectrum-analysis request.")
            return
        try:
            started = time.perf_counter()
            statistics = None
            if isinstance(request.mode, tuple):
                cleanup, statistics = self._preview_processor.process(
                    request.powers_dbm, unit="dBm" if request.reference_operation != "none" else request.source_unit, modes=request.mode,
                    history=request.history_dbm, parameters=request.parameters,
                    frequencies_hz=request.frequencies_hz,
                    background_profile=request.background_profile,
                    background_context=request.background_context,
                    allow_gaps=request.allow_gaps,
                    power_rows=request.power_rows, timestamps_s=request.timestamps_s,
                    reference_values_dbm=request.reference_values_dbm, reference_operation=request.reference_operation,
                    interference_calibration=request.interference_calibration, cleaner=_clean_display,
                )
            elif request.source_unit == "dBm":
                cleanup = clean_spectrum_dbm(
                    request.powers_dbm,
                    mode=request.mode,
                    history_dbm=request.history_dbm,
                    parameters=request.parameters,
                    frequencies_hz=request.frequencies_hz,
                )
            else:
                cleanup = clean_spectrum_values(
                    request.powers_dbm,
                    unit=request.source_unit,
                    mode=request.mode,
                    history_dbm=request.history_dbm,
                    parameters=request.parameters,
                    frequencies_hz=request.frequencies_hz,
                )
            peaks: tuple[SpectrumPeak, ...] | None = (
                _display_peaks(
                    request.frequencies_hz,
                    (cleanup.values if request.parameters.peak_measure_filtered or cleanup.input_values is None
                     else cleanup.input_values),
                    fit=cleanup.unit == "dBm",
                    unit=cleanup.unit,
                    parameters=request.parameters,
                    allow_gaps=request.allow_gaps,
                )
                if request.detect_peaks
                else None
            )
            self.completed.emit(
                SpectrumAnalysisOutcome(
                    request.generation,
                    cleanup,
                    peaks,
                    request.source_key,
                    request.frame_id,
                    request.source_unit,
                    request.provenance,
                    request.frequencies_hz,
                    request.source_snapshot,
                    time.perf_counter() - started,
                    request.raw_snapshot,
                    statistics,
                )
            )
        except Exception as exc:  # noqa: BLE001 - report failures through Qt
            self.failed.emit(request.generation, str(exc))


class SpectrumAnalysisController(QObject):
    """Run at most one analysis and retain only the newest pending frame."""

    _request = Signal(object)
    result = Signal(object)
    error = Signal(int, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._thread.setObjectName("anritsu-spectrum-analysis")
        self._worker = _SpectrumAnalysisWorker()
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)
        self._request.connect(
            self._worker.analyze, Qt.ConnectionType.QueuedConnection
        )
        self._worker.completed.connect(self._completed)
        self._worker.failed.connect(self._failed)
        self._busy = False
        self._closed = False
        self._pending: SpectrumAnalysisRequest | SpectrogramAnalysisRequest | None = None
        self._thread.start()

    @property
    def busy(self) -> bool:
        return self._busy

    def submit(self, request: SpectrumAnalysisRequest | SpectrogramAnalysisRequest) -> None:
        if self._closed:
            return
        if self._busy:
            self._pending = request
            return
        self._busy = True
        self._request.emit(request)

    @Slot(object)
    def _completed(self, outcome: object) -> None:
        self._busy = False
        if self._closed:
            return
        self.result.emit(outcome)
        self._start_pending()

    @Slot(int, str)
    def _failed(self, generation: int, message: str) -> None:
        self._busy = False
        if self._closed:
            return
        self.error.emit(generation, message)
        self._start_pending()

    def _start_pending(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            self.submit(pending)

    def close(self) -> None:
        self._closed = True
        self._pending = None
        if not self._thread.isRunning():
            return
        self._thread.requestInterruption()
        self._thread.quit()
        if not self._thread.wait(3_000):
            self._thread.requestInterruption()
            self._thread.wait()
