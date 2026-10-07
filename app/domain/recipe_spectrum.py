"""Immutable raw source sweeps from recipe acquisitions, without qualification claims."""

from dataclasses import dataclass
import math

import numpy as np

from .spectrum_correction import FloatVector, SpectrumFrameRole, SweepEvidence, immutable_vector


MAX_RECIPE_SWEEP_JSON_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class RecipeSpectrumSweep:
    frequencies_hz: FloatVector
    powers_dbm: FloatVector
    recipe_node_id: str
    execution_id: str
    role: SpectrumFrameRole
    average_index: int
    average_count: int | None
    acquired_at_s: float
    configuration_generation: int
    evidence: SweepEvidence
    sweep_id: str | None = None
    started_at_s: float | None = None
    completed_at_s: float | None = None
    setpoints_si: tuple[tuple[str, float], ...] = ()
    # Additive v2 provenance; legacy setpoints_si remains the nominal vector.
    setpoint_metadata_version: int = 1
    requested_setpoints_si: tuple[tuple[str, float], ...] = ()
    applied_setpoints_si: tuple[tuple[str, float], ...] = ()
    readback_setpoints_si: tuple[tuple[str, float], ...] = ()
    inter_sweep_delay_s: float = 0.0
    safety_measurements_si: tuple[tuple[str, float], ...] = ()
    safety_sampled_at_s: float | None = None
    # Timed blocks have no final count until collection finishes. Each raw
    # source is committed immediately, retaining an explicit unknown count.
    minimum_duration_s: float = 0.0

    def __post_init__(self):
        if type(self.inter_sweep_delay_s) not in (float, int) or not math.isfinite(self.inter_sweep_delay_s) or not 0 <= self.inter_sweep_delay_s <= 3600:
            raise ValueError("Recipe sweep delay must be finite and in 0..3600 s.")
        frequencies = immutable_vector(self.frequencies_hz, name="recipe frequencies_hz", nonnegative=True)
        powers = immutable_vector(self.powers_dbm, name="recipe powers_dbm")
        if powers.shape != frequencies.shape or powers.size > 1048576 or np.any(np.diff(frequencies) <= 0):
            raise ValueError("Recipe sweep requires equally sized bounded arrays on an increasing Hz grid.")
        for value in (self.recipe_node_id, self.execution_id):
            if type(value) is not str or not value.strip() or len(value) > 1024:
                raise ValueError("Recipe sweep requires bounded node and execution identities.")
        if not isinstance(self.role, SpectrumFrameRole) or not isinstance(self.evidence, SweepEvidence):
            raise ValueError("Recipe sweep requires explicit role and completion evidence.")
        if (type(self.minimum_duration_s) not in (float, int) or not math.isfinite(self.minimum_duration_s)
                or not 0 <= self.minimum_duration_s <= 3600):
            raise ValueError("Minimum collection duration must be in 0..3600 s.")
        count_valid = (self.average_count is None and self.minimum_duration_s > 0
                       and self.role == SpectrumFrameRole.REFERENCE)
        if type(self.average_count) is int:
            count_valid = self.minimum_duration_s == 0 and 1 <= self.average_count <= 9999
        if (type(self.average_index) is not int or not count_valid
                or not 0 <= self.average_index < (self.average_count if self.average_count is not None else 9999)
                or type(self.configuration_generation) is not int or self.configuration_generation < 0):
            raise ValueError("Recipe sweep block indices and generation must be nonnegative integers.")
        for value in (self.acquired_at_s, self.started_at_s, self.completed_at_s, self.safety_sampled_at_s):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                raise ValueError("Recipe sweep timestamps must be finite positive UTC epoch seconds.")
        if self.acquired_at_s is None or (self.started_at_s is not None and self.completed_at_s is not None
                                         and self.started_at_s > self.completed_at_s):
            raise ValueError("Recipe sweep timestamps are missing or reversed.")
        if self.sweep_id is not None and (type(self.sweep_id) is not str or not self.sweep_id or len(self.sweep_id) > 1024):
            raise ValueError("Recipe sweep counter must be a bounded explicit identity.")
        if type(self.setpoint_metadata_version) is not int or self.setpoint_metadata_version not in (1, 2):
            raise ValueError("Unsupported recipe sweep setpoint metadata version.")
        for vector in (self.setpoints_si, self.requested_setpoints_si, self.applied_setpoints_si, self.readback_setpoints_si, self.safety_measurements_si):
            if type(vector) is not tuple or len(vector) > 1024:
                raise ValueError("Recipe sweep setpoints must be bounded immutable SI pairs.")
            names = set()
            for pair in vector:
                if (type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str
                        or not pair[0] or len(pair[0]) > 1024 or pair[0] in names
                        or type(pair[1]) not in (int, float) or not math.isfinite(pair[1])):
                    raise ValueError("Recipe sweep setpoints require unique names and finite SI values.")
                names.add(pair[0])
        if self.setpoint_metadata_version == 1 and any((self.requested_setpoints_si, self.applied_setpoints_si, self.readback_setpoints_si)):
            raise ValueError("Explicit setpoint provenance requires metadata version 2.")
        object.__setattr__(self, "frequencies_hz", frequencies)
        object.__setattr__(self, "powers_dbm", powers)
