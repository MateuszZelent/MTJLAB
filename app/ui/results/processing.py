"""Read-only post-acquisition processing shared by spectra and coordinate planes."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.domain.spectrum_averaging import (
    ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS,
    ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS,
    InstrumentAverageProvenance,
    instrument_average_metadata,
    legacy_instrument_average_count,
)
from app.recipes.spectrum_processing import REFERENCE_UNITS
from app.spectrum.analysis import SpectrumAnalysisParameters
from app.spectrum.display_processing import clean_display_spectrum
from app.spectrum.preview_processing import SpectrumPreviewProcessor
from app.spectrum.processing import apply_reference_operation, frequency_grids_match
from app.storage import Hdf5RunReader
from app.storage.hdf5_reader import iter_recipe_spectrum_sweeps


ANRITSU_MEAN_HISTORY_MESSAGE = (
    "Anritsu average stores one completed mean. Use recorded individual PyLab sweeps for same-point power averaging or EMI lines."
)


def recorded_instrument_average(point, *, trace_name: str = "") -> dict | None:
    metadata = point.metadata if point is not None else {}
    evidence = instrument_average_metadata(metadata)
    if evidence is not None:
        try:
            return InstrumentAverageProvenance.from_metadata(evidence).metadata()
        except ValueError:
            pass
        # Keep an explicit older count visible even if timing is unavailable.
        if isinstance(evidence, Mapping) and set(evidence) == {"sweep_count"}:
            count = evidence["sweep_count"]
            if type(count) is int and ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS <= count <= ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS:
                return {"sweep_count": count}
    count = metadata.get("spectrum_average_count", metadata.get("average_count"))
    if ("artifact_schema" in metadata and isinstance(metadata.get("acquisition_metadata"), Mapping)):
        source = metadata["acquisition_metadata"].get("averaging_source")
    else:
        source = metadata.get("averaging_source")
    if source == "instrument" and type(count) is int and ANRITSU_INSTRUMENT_AVERAGE_MIN_SWEEPS <= count <= ANRITSU_INSTRUMENT_AVERAGE_MAX_SWEEPS:
        return {"sweep_count": count}
    recoverable = legacy_instrument_average_count(trace_name or getattr(point, "recorded_trace_name", None) or "")
    if recoverable is not None and (count is None or recoverable == count):
        return {"sweep_count": recoverable}
    return None


def recorded_spectrum_label(point, default: str, *, trace_name: str = "") -> str:
    average = recorded_instrument_average(point, trace_name=trace_name)
    if average is not None:
        return f"Anritsu average ({average['sweep_count']} sweeps)"
    metadata = point.metadata if point is not None else {}
    count = metadata.get("spectrum_average_count", metadata.get("average_count"))
    return f"Stored mean ({count} sweeps)" if type(count) is int and count > 1 else default


def recorded_spectrum_provenance(point, *, trace_name: str = "") -> tuple[str, ...]:
    average = recorded_instrument_average(point, trace_name=trace_name)
    if average:
        provenance = ("Anritsu Lin Average", f"sweeps={average['sweep_count']}")
        nominal = average.get("nominal_duration_s")
        return provenance if nominal is None else (*provenance, f"nominal_duration_s={nominal:.9g}")
    return ("recorded spectrum",)


def baseline_sample_label(reference) -> str:
    selected = getattr(reference, "selected_sweep", None)
    if selected is not None:
        return f"repeat {selected + 1}/{reference.collection_average_count}"
    if getattr(reference, "averaging_source", None) == "instrument":
        return f"Anritsu mean · {reference.average_count} sweeps"
    return f"stored mean · {reference.average_count} sweep(s)"


@dataclass(frozen=True, slots=True)
class ResultProcessing:
    operation: str = "none"
    reference_index: int | None = None
    modes: tuple[str, ...] = ()
    parameters: SpectrumAnalysisParameters = field(default_factory=SpectrumAnalysisParameters)
    reference_sweep: int | None = None

    def __post_init__(self):
        if self.reference_sweep is not None and (type(self.reference_sweep) is not int or self.reference_sweep < 0):
            raise ValueError("Reference repeat must be a nonnegative integer, or None for the stored mean.")

    @property
    def math_operation(self):
        # Results distinguishes baseline purpose without introducing a new DSP formula.
        return (
            "subtract_power_signed"
            if self.operation == "subtract_reference_signed"
            else self.operation
        )

    @property
    def baseline_purpose(self):
        return "background" if self.operation == "subtract_power_signed" else "reference"

    @property
    def output_unit(self):
        return REFERENCE_UNITS[self.math_operation]

    @property
    def active(self):
        return (
            self.operation != "none"
            or bool(self.modes)
            or self.parameters.temporal_average_frames > 1
        )


@dataclass(frozen=True, slots=True)
class RecordedBaselineEvidence:
    index: int
    purpose: str
    acquired_at_utc: str | None
    average_count: int
    configuration_fingerprint: str | None
    selected_sweep: int | None = None
    source_sweep_indices: tuple[int, ...] = ()
    collection_average_count: int | None = None
    averaging_source: str | None = None
    instrument_average: dict | None = None

    @classmethod
    def from_reference(cls, reference):
        average = getattr(reference, "instrument_average", None)
        return cls(reference.index, reference.purpose, reference.acquired_at_utc,
            reference.average_count, reference.configuration_fingerprint, reference.selected_sweep,
            reference.source_sweep_indices, reference.collection_average_count,
            getattr(reference, "averaging_source", None),
            average.metadata() if average is not None else None)


@dataclass(frozen=True, slots=True)
class ProcessedResultSpectrum:
    frequencies_hz: tuple[float, ...]
    values: tuple[float, ...]
    unit: str
    method: str
    notes: tuple[str, ...]
    baselines: tuple[RecordedBaselineEvidence, ...] = ()


class ResultSpectrumProcessor:
    """Process full grids; never infer temporal history from adjacent sweep points."""

    def __init__(self, path, state, points=(), *, cancelled=None, reader=None):
        self.path = Path(path)
        self.state = state
        self.points = {point.index: point for point in points}
        self.cancelled = cancelled
        self.reader = reader or Hdf5RunReader
        self._references = {}
        self._reference_catalogue = None
        self.notes = []

    @property
    def baselines(self):
        return tuple(RecordedBaselineEvidence.from_reference(r)
                     for r in self._references.values() if r is not None)

    def resolve_reference(self, checkpoint, frequencies_hz, unit="dBm"):
        """One authoritative selection and compatibility check for plots and DSP."""
        state = self.state
        if unit != "dBm":
            raise ValueError("Reference/background correction requires raw absolute power in dBm.")
        index = state.reference_index
        if index is None:
            if self._reference_catalogue is None:
                self._reference_catalogue = self.reader.references(self.path, metadata_only=True)
            purpose = state.baseline_purpose
            candidates = [r for r in self._reference_catalogue if r.purpose == purpose]
            if len(candidates) == 1:
                index = candidates[0].index
            elif candidates:
                # Read checkpoint linkage only when there is a real choice;
                # one collection never requires reloading every raw spectrum.
                trace = self.reader.spectrum(self.path, checkpoint)
                linked = trace.reference_index if trace else None
                if any(r.index == linked for r in candidates):
                    index = linked
            if index is None:
                raise ValueError(f"No unique recorded {purpose} is linked to this raw checkpoint. "
                                 "Select the recorded reference/background explicitly.")
        key = (index, state.reference_sweep)
        if key not in self._references:
            self._references[key] = (self.reader.reference(self.path, index) if state.reference_sweep is None
                else self.reader.reference_sweep(self.path, index, state.reference_sweep))
        reference = self._references[key]
        if reference is None:
            raise ValueError("The selected reference/background is unavailable.")
        if state.operation == "subtract_reference_signed" and reference.purpose != "reference":
            raise ValueError("Raw minus reference requires a recorded reference, not a background.")
        if state.operation == "subtract_power_signed" and reference.purpose != "background":
            raise ValueError("Raw minus background requires a recorded background, not a reference.")
        if not frequency_grids_match(frequencies_hz, reference.frequencies_hz):
            raise ValueError("Reference/background frequency grid differs from the raw spectrum.")
        point = self.points.get(checkpoint)
        notes = []
        evidence = point.metadata.get("spectrum_processing_v1") if point else None
        fingerprint = evidence.get("configuration_fingerprint") if isinstance(evidence, Mapping) else None
        if fingerprint and reference.configuration_fingerprint:
            if fingerprint != reference.configuration_fingerprint:
                raise ValueError("Reference/background analyzer settings differ from this checkpoint.")
        else:
            note = "Analyzer settings compatibility is unavailable in this archive; frequency grids match."
            notes.append(note)
        return reference, tuple(notes)

    def process(self, checkpoint, frequencies_hz, values, unit="dBm", *, trace_name=""):
        state = self.state
        operation = state.math_operation
        params = state.parameters
        point = self.points.get(checkpoint)
        average = recorded_instrument_average(point, trace_name=trace_name)
        history = ()
        times = ()
        if "emi_reject" in state.modes or params.temporal_average_frames > 1:
            if average is not None or (point is not None and instrument_average_metadata(point.metadata) is not None):
                raise ValueError(ANRITSU_MEAN_HISTORY_MESSAGE)
            indices = set(point.metadata.get("raw_recipe_sweep_indices", ())) if point else set()
            if indices:
                records = []
                # Keep only the bounded tail needed by the shared live algorithms.
                limit = max(24 if "emi_reject" in state.modes else 1,
                            params.temporal_average_frames)
                for ordinal, _boundary, record in iter_recipe_spectrum_sweeps(
                    self.path, selected_indices=set(sorted(indices)[-limit:])
                ):
                    if self.cancelled is not None and self.cancelled():
                        raise RuntimeError("Spectrum processing cancelled.")
                    if ordinal in indices:
                        if not frequency_grids_match(frequencies_hz, record.frequencies_hz):
                            raise ValueError(
                                "Recorded source sweeps have a different frequency grid."
                            )
                        records.append(record)
                        records = records[-limit:]
                if len(records) != min(len(indices), limit):
                    raise ValueError("The checkpoint is missing recorded source sweeps.")
                if (
                    len(
                        {
                            (
                                r.execution_id,
                                r.recipe_node_id,
                                r.configuration_generation,
                                r.setpoints_si,
                            )
                            for r in records
                        }
                    )
                    != 1
                ):
                    raise ValueError("Source sweeps do not belong to one acquisition block.")
                history = tuple(r.powers_dbm for r in records)
                times = tuple(r.acquired_at_s for r in records)
            if params.temporal_average_frames > 1 and not history:
                raise ValueError(
                    "Power averaging needs recorded individual sweeps from this checkpoint."
                )

        compatibility_notes = []
        reference_values = None
        if state.operation != "none":
            reference, compatibility_notes = self.resolve_reference(checkpoint, frequencies_hz, unit)
            reference_values = reference.powers_dbm

        # EMI classifies the same domain as the displayed trace, as in recipe execution.
        # Live EMI sees at most 24 corrected frames, even when power averaging
        # requires a longer raw history from this same checkpoint.
        filter_history = history[-24:]
        if filter_history and state.operation != "none":
            filter_history = tuple(
                apply_reference_operation(row, reference_values, operation)[0]
                for row in filter_history
            )
        result, _stats = SpectrumPreviewProcessor().process(
            values,
            frequencies_hz=frequencies_hz,
            unit=unit,
            modes=state.modes,
            parameters=params,
            history=filter_history,
            power_rows=history,
            timestamps_s=times,
            reference_values_dbm=reference_values,
            reference_operation=operation,
            cleaner=clean_display_spectrum,
            allow_gaps=operation == "subtract_power",
        )
        notes = tuple(compatibility_notes) + result.notes
        self.notes.extend(note for note in notes if note not in self.notes)
        method = result.method
        if average is not None:
            method = f"Anritsu average ({average['sweep_count']} sweeps) → {method}"
        if state.operation != "none":
            method = f"Recorded {reference.purpose} {reference.index} · {baseline_sample_label(reference)} → {method}"
        return ProcessedResultSpectrum(
            tuple(frequencies_hz), result.values, result.unit, method, notes, self.baselines
        )


def read_processed_private(path, point, state, *, reader=None):
    reader = reader or Hdf5RunReader
    trace = reader.spectrum(path, point.index)
    if trace is None:
        raise ValueError("The selected checkpoint has no complete raw spectrum.")
    return ResultSpectrumProcessor(path, state, (point,), reader=reader).process(
        point.index,
        trace.frequencies_hz,
        trace.powers_dbm,
        trace_name=trace.trace_name,
    )


def read_processed_public(path, spectrum, trace_index, state, points=(), *, reader=None):
    # Public processed rows are never corrected a second time implicitly.
    if state.active and spectrum.role == "spectrum_processed":
        raise ValueError("Choose the recorded spectrum row for post-processing. This row already contains derived values.")
    trace = spectrum.traces[trace_index]
    return ResultSpectrumProcessor(path, state, points, reader=reader).process(
        spectrum.checkpoint,
        spectrum.x_values,
        trace.values,
        spectrum.y_unit,
    )
