"""Read-only power comparisons and explicit display-unit conversion."""

from dataclasses import replace
from types import MappingProxyType

import numpy as np

from .display_model import SpectrumDisplayState, SpectrumDisplayTrace
from .processing import frequency_grids_match
from .streaming_statistics import dbm_to_w


def display_state(traces, frame_id):
    traces = tuple(traces)
    key = traces[-1].key if traces else None
    return SpectrumDisplayState(traces, key, key, frame_id,
                                MappingProxyType({trace.key: trace for trace in traces}))


def compare_power(raw, *, frame_id, show_raw=False, background_w=None, reference=None,
                  background_provenance=()):
    """Subtract in watts from the same raw frame; caller validates context/age."""
    if raw is None:
        return display_state((), frame_id)
    frequencies = tuple(raw.frequencies_hz)
    power_w = dbm_to_w(raw.powers_dbm)
    traces = []

    def add(key, label, values, provenance):
        if len(values) != len(frequencies):
            raise ValueError("Comparison frequency and power vector lengths differ.")
        traces.append(SpectrumDisplayTrace(key, label, frequencies, tuple(values), "W",
                                           frame_id, provenance))

    if show_raw:
        add("raw", "Raw", power_w, ("raw",))
    if background_w is not None:
        baseline = np.asarray(background_w, dtype=float)
        if baseline.shape != power_w.shape or not np.all(np.isfinite(baseline)):
            raise ValueError("Background power vector is invalid.")
        add("background_difference", "Raw − background", power_w - baseline,
            ("raw", "background", *background_provenance))
    if reference is not None:
        if not frequency_grids_match(reference.frequencies_hz, frequencies):
            raise ValueError("Reference frequency grid differs from the current spectrum.")
        add("reference_difference", "Raw − reference", power_w - dbm_to_w(reference.powers_dbm),
            ("raw", "reference", reference.acquired_at_utc.isoformat(), "signed power subtraction"))
    return display_state(traces, frame_id)


def convert_power_view(state, unit):
    """Return a new state; dBm never takes abs() of negative residuals."""
    if unit == "auto" or not state.traces:
        return state, ""
    if unit not in {"W", "dBm"}:
        raise ValueError("Unsupported power display unit.")
    if any(trace.unit not in {"W", "dBm"} for trace in state.traces):
        return state, "Power units are unavailable for a ratio or other non-power quantity."
    converted = []
    omitted = 0
    for trace in state.traces:
        values = np.asarray(trace.values)
        if trace.unit != unit:
            if unit == "W":
                # Display traces can contain undefined (NaN) residual bins.
                # dBm is referenced to 1 mW: P[W] = 10 ** ((dBm - 30) / 10).
                with np.errstate(over="ignore", invalid="ignore"):
                    values = np.power(10.0, (values - 30.0) / 10.0)
                values[~np.isfinite(values)] = np.nan
            else:
                positive = values > 0
                omitted += int(np.count_nonzero(np.isfinite(values) & ~positive))
                result = np.full(values.shape, np.nan)
                # W / 1 mW gives the dimensionless power ratio for dBm.
                result[positive] = 10 * np.log10(values[positive] / 1e-3)
                values = result
        provenance = trace.provenance
        if trace.unit != unit:
            provenance += (f"display units {unit}",)
            if unit == "dBm":
                provenance += ("non-positive power undefined",)
        converted.append(replace(trace, values=tuple(values), unit=unit, provenance=provenance))
    result = replace(state, traces=tuple(converted),
                     by_key=MappingProxyType({trace.key: trace for trace in converted}))
    note = f"dBm: {omitted} non-positive points omitted; use W to see signed residuals." if omitted else ""
    return result, note
