"""Immutable state for the spectrum traces currently shown to the user.

The page used to build the chart and the analysis request independently.  As a
result, analysis could continue to consume the raw trace after the chart had
switched to a reference or cleaned trace.  This module makes the displayed
trace the source of truth: one state is derived for a frame, and both rendering
and analysis can use the same immutable snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace

from .processing import apply_reference_operation, frequency_grids_match


@dataclass(frozen=True, slots=True)
class SpectrumDisplayTrace:
    """One immutable trace in the current display frame."""

    key: str
    label: str
    frequencies_hz: tuple[float, ...]
    values: tuple[float, ...]
    unit: str
    frame_id: int
    provenance: tuple[str, ...] = ()


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

    def select(self, key: str) -> "SpectrumDisplayState":
        if key not in self.by_key:
            raise KeyError(key)
        return SpectrumDisplayState(
            traces=self.traces,
            selected_key=key,
            primary_key=self.primary_key,
            frame_id=self.frame_id,
            by_key=self.by_key,
        )


def _trace(
    *,
    key: str,
    label: str,
    source: SpectrumTrace,
    frame_id: int,
    provenance: tuple[str, ...] = (),
) -> SpectrumDisplayTrace:
    frequencies = tuple(float(value) for value in source.frequencies_hz)
    values = tuple(float(value) for value in source.powers_dbm)
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


def build_display_state(
    *,
    raw: SpectrumTrace | None,
    averaged: SpectrumTrace | None,
    reference: SpectrumTrace | None,
    reference_operation: str,
    visible: Mapping[str, bool],
    frame_id: int,
    preferred_key: str | None = None,
    analysis_values: tuple[float, ...] | None = None,
    analysis_unit: str = "dBm",
    analysis_source_key: str | None = None,
    analysis_method: str | None = None,
    analysis_input_values: tuple[float, ...] | None = None,
    analysis_source_snapshot: SpectrumDisplayTrace | None = None,
    analysis_input_provenance: tuple[str, ...] = (),
    analysis_modes: tuple[str, ...] | None = None,
) -> SpectrumDisplayState:
    """Derive all visible traces and a stable analysis selection.

    ``reference_operation`` is evaluated only when raw and reference grids
    match.  An invalid/incompatible processed view is omitted rather than
    silently analysing a different source.  Visibility is applied after the
    derived views are built so a hidden raw trace can still be used to derive a
    visible processed trace.
    """

    candidates: list[SpectrumDisplayTrace] = []
    if raw is not None:
        candidates.append(_trace(key="raw", label="Raw", source=raw, frame_id=frame_id, provenance=("raw",)))
    if averaged is not None:
        candidates.append(
            _trace(key="averaged", label="Averaged", source=averaged, frame_id=frame_id, provenance=("averaged",))
        )
    if reference is not None:
        candidates.append(
            _trace(key="reference", label="Reference", source=reference, frame_id=frame_id, provenance=("reference",))
        )

    operation = reference_operation.strip().lower()
    if raw is not None and reference is not None and operation not in {"", "none"}:
        if frequency_grids_match(raw.frequencies_hz, reference.frequencies_hz):
            try:
                values, unit = apply_reference_operation(raw.powers_dbm, reference.powers_dbm, operation)
            except ValueError:
                pass
            else:
                candidates.append(
                    SpectrumDisplayTrace(
                        key="processed",
                        label="Processed",
                        frequencies_hz=tuple(float(value) for value in raw.frequencies_hz),
                        values=tuple(float(value) for value in values),
                        unit=unit,
                        frame_id=frame_id,
                        provenance=("raw", "reference", operation),
                    )
                )

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
                    frequencies_hz=source.frequencies_hz, values=analysis_input_values,
                    unit=analysis_unit, frame_id=source.frame_id,
                    provenance=(*source.provenance, *analysis_input_provenance, "before DSP"),
                ))
            candidates.append(
                SpectrumDisplayTrace(
                    key=f"analysis:{analysis_source_key}",
                    label=f"Filtered · {input_label}" if digital_filters else input_label,
                    frequencies_hz=source.frequencies_hz,
                    values=tuple(float(value) for value in analysis_values),
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
