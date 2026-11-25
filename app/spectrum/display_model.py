"""Immutable state for the spectrum traces currently shown to the user.

The page used to build the chart and the analysis request independently.  As a
result, analysis could continue to consume the raw trace after the chart had
switched to a reference or cleaned trace.  This module makes the displayed
trace the source of truth: one state is derived for a frame, and both rendering
and analysis can use the same immutable snapshot.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import overload

import numpy as np

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace

from .processing import _apply_reference_operation_array, frequency_grids_match


class ImmutableFloatVector(Sequence[float]):
    """Read-only 1-D float64 vector compatible with both NumPy and Python sequences."""

    __slots__ = ("_array",)

    def __init__(self, values: Sequence[float] | np.ndarray, *, _trusted_readonly: bool = False) -> None:
        if _trusted_readonly and isinstance(values, np.ndarray):
            self._array = values
            return
        if isinstance(values, ImmutableFloatVector):
            self._array = values._array
            return
        if isinstance(values, np.ndarray) and values.dtype == np.float64 and values.ndim == 1:
            if not values.flags.writeable:
                self._array = values
                return
            arr = values.copy()
        else:
            arr = np.array(values, dtype=np.float64, copy=True)
            if arr.ndim != 1:
                raise ValueError("Spectrum display vector must be one-dimensional.")
        arr.setflags(write=False)
        self._array = arr

    @classmethod
    def _from_owned(cls, array: np.ndarray) -> ImmutableFloatVector:
        arr = np.asarray(array, dtype=np.float64)
        if arr.ndim != 1:
            raise ValueError("Spectrum display vector must be one-dimensional.")
        arr.setflags(write=False)
        return cls(arr, _trusted_readonly=True)

    def __array__(self, dtype: object = None, copy: bool | None = None) -> np.ndarray:
        if copy is True:
            return np.array(self._array, dtype=dtype, copy=True)
        if dtype is None or np.dtype(dtype) == self._array.dtype:
            return self._array
        return self._array.astype(dtype)

    def __len__(self) -> int:
        return int(self._array.size)

    def __bool__(self) -> bool:
        return bool(self._array.size)

    @overload
    def __getitem__(self, index: int) -> float: ...

    @overload
    def __getitem__(self, index: slice) -> ImmutableFloatVector: ...

    def __getitem__(self, index: int | slice) -> float | ImmutableFloatVector:
        if isinstance(index, slice):
            return ImmutableFloatVector(self._array[index], _trusted_readonly=True)
        return float(self._array[index])

    def __iter__(self) -> Iterator[float]:
        return iter(self._array.tolist())

    def __eq__(self, other: object) -> bool:
        if other is self:
            return True
        if isinstance(other, ImmutableFloatVector):
            if self._array is other._array:
                return True
            return bool(np.array_equal(self._array, other._array))
        if isinstance(other, (tuple, list)):
            if len(other) != self._array.size:
                return False
            try:
                other_arr = np.asarray(other, dtype=np.float64)
            except (TypeError, ValueError):
                return False
            return other_arr.ndim == 1 and bool(np.array_equal(self._array, other_arr))
        return NotImplemented

    def __hash__(self) -> int:
        return hash(tuple(self._array.tolist()))

    def __repr__(self) -> str:
        return f"ImmutableFloatVector(len={self._array.size})"


def _as_readonly_vector(values: Sequence[float] | np.ndarray) -> ImmutableFloatVector:
    if isinstance(values, ImmutableFloatVector):
        return values
    return ImmutableFloatVector(values)


@dataclass(frozen=True, slots=True)
class SpectrumDisplayTrace:
    """One immutable trace in the current display frame."""

    key: str
    label: str
    frequencies_hz: Sequence[float]
    values: Sequence[float]
    unit: str
    frame_id: int
    provenance: tuple[str, ...] = ()
    estimated_bands_hz: tuple[tuple[float, float], ...] = ()

    @property
    def is_signed_difference(self) -> bool:
        return self.unit == "dB" or (
            self.unit == "W"
            and any(source in self.provenance for source in (
                "background", "subtract_power_signed", "signed power subtraction",
            ))
        )


@dataclass(frozen=True, slots=True)
class SpectrumDisplayState:
    """Visible traces and the trace selected for analysis in one frame."""

    traces: tuple[SpectrumDisplayTrace, ...]
    selected_key: str | None
    primary_key: str | None
    frame_id: int
    by_key: Mapping[str, SpectrumDisplayTrace]

    @property
    def available_keys(self) -> tuple[str, ...]:
        return tuple(trace.key for trace in self.traces)

    @property
    def selected(self) -> SpectrumDisplayTrace | None:
        if self.selected_key is None:
            return None
        return self.by_key[self.selected_key]

    def select(self, key: str) -> SpectrumDisplayState:
        if key not in self.by_key:
            raise KeyError(key)
        return SpectrumDisplayState(
            traces=self.traces,
            selected_key=key,
            primary_key=self.primary_key,
            frame_id=self.frame_id,
            by_key=self.by_key,
        )

    def absolute_differences(self) -> SpectrumDisplayState:
        """Fold signed residuals at zero while preserving their physical units."""
        traces = tuple(
            replace(
                trace,
                label=f"Abs · {trace.label}",
                values=ImmutableFloatVector._from_owned(np.abs(np.asarray(trace.values, dtype=np.float64))),
                provenance=(*trace.provenance, "absolute_difference"),
            )
            if trace.is_signed_difference else trace
            for trace in self.traces
        )
        return replace(self, traces=traces, by_key=MappingProxyType({trace.key: trace for trace in traces}))

    def with_overlay(self, trace: SpectrumDisplayTrace) -> SpectrumDisplayState:
        return replace(
            self, traces=(*self.traces, trace), by_key=MappingProxyType({**self.by_key, trace.key: trace}),
        )


def _trace(
    *,
    key: str,
    label: str,
    source: SpectrumTrace,
    frame_id: int,
    provenance: tuple[str, ...] = (),
    shared_frequencies: tuple[object, ImmutableFloatVector] | None = None,
) -> SpectrumDisplayTrace:
    if shared_frequencies is not None and source.frequencies_hz is shared_frequencies[0]:
        frequencies = shared_frequencies[1]
    else:
        frequencies = _as_readonly_vector(source.frequencies_hz)
    values = _as_readonly_vector(source.powers_dbm)
    if len(frequencies) != len(values) or len(frequencies) < 2:
        raise ValueError(f"Trace {key!r} must contain matching frequency and value vectors.")
    return SpectrumDisplayTrace(
        key=key,
        label=label,
        frequencies_hz=frequencies,
        values=values,
        unit="dBm",
        frame_id=frame_id,
        provenance=provenance,
    )


def _with_reference(
    source: SpectrumDisplayTrace,
    reference: SpectrumTrace,
    operation: str,
    *,
    key: str,
    label: str,
    reference_view: SpectrumDisplayTrace | None = None,
) -> SpectrumDisplayTrace | None:
    ref_freqs = reference_view.frequencies_hz if reference_view is not None else reference.frequencies_hz
    ref_vals = reference_view.values if reference_view is not None else reference.powers_dbm
    if not frequency_grids_match(source.frequencies_hz, ref_freqs):
        return None
    try:
        values_array, unit = _apply_reference_operation_array(source.values, ref_vals, operation)
    except ValueError:
        return None
    return replace(
        source,
        key=key,
        label=label,
        values=ImmutableFloatVector._from_owned(values_array),
        unit=unit,
        provenance=(*source.provenance, "reference", operation),
    )


def build_display_state(
    *,
    raw: SpectrumTrace | None,
    averaged: SpectrumTrace | None,
    reference: SpectrumTrace | None,
    reference_operation: str,
    visible: Mapping[str, bool],
    frame_id: int,
    preferred_key: str | None = None,
    use_average_for_reference: bool = False,
    live_average_label: str = "Live average",
    show_live_frame: bool = False,
    analysis_values: Sequence[float] | None = None,
    analysis_unit: str = "dBm",
    analysis_source_key: str | None = None,
    analysis_method: str | None = None,
    analysis_input_values: Sequence[float] | None = None,
    analysis_source_snapshot: SpectrumDisplayTrace | None = None,
    analysis_input_provenance: tuple[str, ...] = (),
    analysis_modes: tuple[str, ...] | None = None,
) -> SpectrumDisplayState:
    """Derive all visible traces and a stable analysis selection.

    ``reference_operation`` requires compatible grids. Use the acquired raw
    trace by default, or the integrated average when explicitly selected.
    An invalid/incompatible processed view is omitted rather than
    silently analysing a different source.  Visibility is applied after the
    derived views are built so a hidden raw trace can still be used to derive a
    visible processed trace.
    """

    raw_view = (_trace(key="raw", label="Raw", source=raw, frame_id=frame_id, provenance=("raw",))
                if raw is not None else None)
    shared_freq = (
        (raw.frequencies_hz, raw_view.frequencies_hz)
        if raw is not None and raw_view is not None and isinstance(raw_view.frequencies_hz, ImmutableFloatVector)
        else None
    )
    average_view = (_trace(key="averaged", label=live_average_label if use_average_for_reference else "Averaged",
                           source=averaged, frame_id=frame_id, provenance=("averaged",),
                           shared_frequencies=shared_freq)
                    if averaged is not None else None)
    if shared_freq is None and averaged is not None and average_view is not None and isinstance(average_view.frequencies_hz, ImmutableFloatVector):
        shared_freq = (averaged.frequencies_hz, average_view.frequencies_hz)
    candidates = [view for view in (raw_view, average_view) if view is not None]
    reference_view = None
    if reference is not None:
        reference_view = _trace(
            key="reference", label="Reference", source=reference, frame_id=frame_id,
            provenance=("reference",), shared_frequencies=shared_freq,
        )
        candidates.append(reference_view)

    operation = reference_operation.strip().lower()
    reference_input = average_view if use_average_for_reference else raw_view
    processed = None
    if reference_input is not None and reference is not None and operation not in {"", "none"}:
        processed = _with_reference(
            reference_input, reference, operation, key="processed",
            label=f"{live_average_label} · Processed" if use_average_for_reference else "Processed",
            reference_view=reference_view,
        )
        if processed is not None:
            candidates.append(processed)

    if (show_live_frame and use_average_for_reference and raw_view is not None and average_view is not None
            and raw.configuration_generation == averaged.configuration_generation
            and (raw_view.frequencies_hz is average_view.frequencies_hz
                 or frequency_grids_match(raw_view.frequencies_hz, average_view.frequencies_hz))):
        if processed is not None and raw is not reference:
            subtraction = operation in {"difference_db", "subtract_power", "subtract_power_signed"}
            overlay = _with_reference(
                raw_view, reference, operation, key="live_frame",
                label="Live − reference" if subtraction else "Live frame · Processed",
                reference_view=reference_view,
            )
            if overlay is not None:
                candidates.append(overlay)
        elif operation in {"", "none"}:
            candidates.append(replace(raw_view, key="live_frame", label="Live frame"))

    if analysis_values is not None and analysis_source_key:
        if analysis_source_snapshot is not None and analysis_source_snapshot.key == analysis_source_key:
            candidates = [analysis_source_snapshot if trace.key == analysis_source_key else trace for trace in candidates]
        source = next((trace for trace in candidates if trace.key == analysis_source_key), None)
        if source is not None and len(analysis_values) == len(source.frequencies_hz):
            background = "background" in analysis_input_provenance
            digital_filters = analysis_modes is None or any(mode not in {"background", "power_average"} for mode in analysis_modes)
            input_label = "Raw − background" if background else source.label
            if "power_average" in analysis_input_provenance:
                input_label = "Power average − background" if background else "Power average" if source.key == "raw" else f"Power average · {source.label}"
            if (analysis_input_values is not None and analysis_unit != source.unit
                    and len(analysis_input_values) == len(source.frequencies_hz) and digital_filters):
                candidates.append(SpectrumDisplayTrace(
                    key=f"correction:{analysis_source_key}", label=input_label,
                    frequencies_hz=source.frequencies_hz, values=_as_readonly_vector(analysis_input_values),
                    unit=analysis_unit, frame_id=source.frame_id,
                    provenance=(*source.provenance, *analysis_input_provenance, "before DSP"),
                ))
            candidates.append(
                SpectrumDisplayTrace(
                    key=f"analysis:{analysis_source_key}",
                    label=f"Filtered · {input_label}" if digital_filters else input_label,
                    frequencies_hz=source.frequencies_hz,
                    values=_as_readonly_vector(analysis_values),
                    unit=analysis_unit,
                    frame_id=source.frame_id,
                    provenance=(*source.provenance, *analysis_input_provenance, "analysis", analysis_method or ""),
                )
            )

    traces = tuple(trace for trace in candidates if visible.get(trace.key, True))
    by_key = MappingProxyType({trace.key: trace for trace in traces})
    priority = (*[trace.key for trace in traces if trace.key.startswith("analysis:")],
                "processed", "averaged", "reference", "raw")
    primary_key = next((key for key in priority if key in by_key), None)
    selected_key = preferred_key if preferred_key in by_key else primary_key
    return SpectrumDisplayState(
        traces=traces,
        selected_key=selected_key,
        primary_key=primary_key,
        frame_id=frame_id,
        by_key=by_key,
    )
