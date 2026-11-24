"""Private checkpoint data for replaying the curves displayed at manual save."""

import json
from dataclasses import dataclass

import numpy as np

from app.domain.errors import ExecutionError
from app.spectrum.display_model import SpectrumDisplayTrace


@dataclass(frozen=True, slots=True)
class SavedSpectrumView:
    traces: tuple[SpectrumDisplayTrace, ...]
    primary_key: str | None = None


def _trace_arrays(trace):
    frequencies = np.asarray(trace.frequencies_hz)
    values = np.asarray(trace.values)
    if (frequencies.ndim != 1 or frequencies.size < 2 or frequencies.shape != values.shape
            or frequencies.dtype.kind not in "iuf" or values.dtype.kind not in "iuf"
            or not np.all(np.isfinite(frequencies)) or not np.all(np.diff(frequencies) > 0)
            or np.any(np.isinf(values))):
        raise ExecutionError("Saved spectrum view contains an invalid frequency grid or trace.")
    # Undefined display conversions can contain gaps. Raw and reference
    # datasets continue to require all-finite measurements.
    if not all(isinstance(text, str) and text.strip() for text in (trace.key, trace.label, trace.unit)):
        raise ExecutionError("Saved spectrum curves need a key, label and unit.")
    bands_hz = np.asarray(trace.estimated_bands_hz)
    if bands_hz.size and (bands_hz.ndim != 2 or bands_hz.shape[1] != 2
            or bands_hz.dtype.kind not in "iuf" or not np.all(np.isfinite(bands_hz))
            or np.any(bands_hz[:, 0] >= bands_hz[:, 1]) or np.any(bands_hz[:-1, 1] > bands_hz[1:, 0])
            or bands_hz[0, 0] < frequencies[0] or bands_hz[-1, 1] > frequencies[-1]):
        raise ExecutionError("Saved spectrum view has invalid estimated frequency bands.")
    return frequencies.astype("f8", copy=False), values.astype("f8", copy=False), bands_hz.astype("f8", copy=False)


def _validate_selection(view):
    keys = {trace.key for trace in view.traces}
    if (not keys or len(view.traces) != len(keys)
            or view.primary_key is not None and view.primary_key not in keys):
        raise ExecutionError("Saved spectrum view has an invalid trace selection.")


def write_saved_spectrum_view(group, view, compression):
    _validate_selection(view)
    group.attrs["primary_key"] = view.primary_key or ""
    for index, trace in enumerate(view.traces):
        frequencies, values, bands_hz = _trace_arrays(trace)
        entry = group.create_group(str(index))
        entry.attrs.update(key=trace.key, label=trace.label, unit=trace.unit,
                           provenance_json=json.dumps(trace.provenance, ensure_ascii=False))
        entry.create_dataset("frequency_hz", data=frequencies, **compression(len(frequencies)))
        entry.create_dataset("values", data=values, **compression(len(values)))
        if bands_hz.size:
            entry.create_dataset("estimated_bands_hz", data=bands_hz)


def read_saved_spectrum_view(group, checkpoint):
    try:
        traces = []
        for index in range(len(group)):
            entry = group[str(index)]
            provenance = json.loads(entry.attrs["provenance_json"])
            if not isinstance(provenance, list) or not all(isinstance(item, str) for item in provenance):
                raise ValueError("Invalid saved trace provenance.")
            bands_hz = tuple(map(tuple, entry["estimated_bands_hz"][:])) if "estimated_bands_hz" in entry else ()
            trace = SpectrumDisplayTrace(
                entry.attrs["key"], entry.attrs["label"],
                tuple(entry["frequency_hz"][:]), tuple(entry["values"][:]),
                entry.attrs["unit"], checkpoint, tuple(provenance), bands_hz)
            _trace_arrays(trace)
            traces.append(trace)
        view = SavedSpectrumView(tuple(traces), group.attrs["primary_key"] or None)
        _validate_selection(view)
        return view
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionError("The saved spectrum view is invalid.") from exc
