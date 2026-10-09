"""Read-only post-acquisition processing shared by spectra and coordinate planes."""

from dataclasses import dataclass, field
from pathlib import Path

from app.spectrum.analysis import SpectrumAnalysisParameters
from app.spectrum.display_processing import clean_display_spectrum
from app.spectrum.preview_processing import SpectrumPreviewProcessor
from app.spectrum.processing import apply_reference_operation, frequency_grids_match
from app.storage import Hdf5RunReader
from app.storage.hdf5_reader import iter_recipe_spectrum_sweeps
from app.recipes.spectrum_processing import REFERENCE_UNITS


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

    @classmethod
    def from_reference(cls, reference):
        return cls(reference.index, reference.purpose, reference.acquired_at_utc,
            reference.average_count, reference.configuration_fingerprint, reference.selected_sweep,
            reference.source_sweep_indices, reference.collection_average_count)


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
            trace = self.reader.spectrum(self.path, checkpoint)
            linked = trace.reference_index if trace else None
            candidates = [r for r in self._reference_catalogue if r.purpose == purpose]
            if any(r.index == linked for r in candidates):
                index = linked
            elif len(candidates) == 1:
                index = candidates[0].index
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
        evidence = (point.metadata.get("spectrum_processing_v1") or {}) if point else {}
        fingerprint = evidence.get("configuration_fingerprint")
        if fingerprint and reference.configuration_fingerprint:
            if fingerprint != reference.configuration_fingerprint:
                raise ValueError("Reference/background analyzer settings differ from this checkpoint.")
        else:
            note = "Analyzer settings compatibility is unavailable in this archive; frequency grids match."
            notes.append(note)
        return reference, tuple(notes)

    def process(self, checkpoint, frequencies_hz, values, unit="dBm"):
        state = self.state
        operation = state.math_operation
        params = state.parameters
        point = self.points.get(checkpoint)
        history = ()
        times = ()
        if "emi_reject" in state.modes or params.temporal_average_frames > 1:
            indices = set(point.metadata.get("raw_recipe_sweep_indices", ())) if point else set()
            if indices:
                records = []
                # Keep only the bounded tail needed by the shared live algorithms.
                limit = max(params.emi_min_frames, params.temporal_average_frames)
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
        filter_history = history
        if history and state.operation != "none":
            filter_history = tuple(
                apply_reference_operation(row, reference_values, operation)[0]
                for row in history
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
        if state.operation != "none":
            sample = (f"repeat {reference.selected_sweep + 1}/{reference.collection_average_count}"
                      if reference.selected_sweep is not None else f"stored mean of {reference.average_count} sweep(s)")
            method = f"Recorded {reference.purpose} {reference.index} · {sample} → {method}"
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
    )


def read_processed_public(path, spectrum, trace_index, state, points=(), *, reader=None):
    # Public processed rows are never corrected a second time implicitly.
    trace = spectrum.traces[trace_index]
    return ResultSpectrumProcessor(path, state, points, reader=reader).process(
        spectrum.checkpoint,
        spectrum.x_values,
        trace.values,
        spectrum.y_unit,
    )
