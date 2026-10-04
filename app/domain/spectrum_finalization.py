"""Immutable operator selections for offline spectrum finalization."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SpectrumFinalizationRequest:
    signal_path: Path
    before_path: Path
    after_path: Path
    destination: Path
    point_indices: tuple[int, ...] | None = None
    before_profile_id: str | None = None
    after_profile_id: str | None = None
    signal_profile_id: str | None = None

    def __post_init__(self):
        if self.point_indices is not None and (type(self.point_indices) is not tuple or
                not self.point_indices or any(type(index) is not int or index < 0 for index in self.point_indices) or
                any(first >= second for first, second in zip(self.point_indices, self.point_indices[1:]))):
            raise ValueError("Finalization checkpoints require an immutable strictly ordered integer tuple.")
        for profile_id in (self.before_profile_id, self.after_profile_id, self.signal_profile_id):
            if profile_id is not None and (type(profile_id) is not str or not profile_id):
                raise ValueError("Finalization profile IDs must be explicit nonempty strings.")


@dataclass(frozen=True, slots=True)
class SpectrumFinalizationBatchRequest:
    blocks: tuple[SpectrumFinalizationRequest, ...]
    journal_path: Path

    def __post_init__(self):
        if type(self.blocks) is not tuple or not 1 <= len(self.blocks) <= 256 or any(
            type(block) is not SpectrumFinalizationRequest for block in self.blocks
        ):
            raise ValueError("Choose an immutable tuple of 1..256 explicit finalization blocks.")


@dataclass(frozen=True, slots=True)
class SpectrumFinalizationResumeRequest:
    previous_journal: Path
    journal_path: Path
    previous_processing_stopped: bool
    replacement_destinations: tuple[tuple[int, Path], ...] = ()
    recover_torn_tail: bool = False
    expected_parent_hash: str | None = None
    adopt_closed_output: bool = False
    expected_closed_output_hash: str | None = None

    def __post_init__(self):
        if self.previous_processing_stopped is not True:
            raise ValueError("Confirm that previous offline processing has stopped before resuming.")
        if type(self.recover_torn_tail) is not bool or type(self.adopt_closed_output) is not bool:
            raise ValueError("Torn-tail recovery must be an explicit boolean.")
        for identity in (self.expected_parent_hash, self.expected_closed_output_hash):
            if identity is not None and (type(identity) is not str or len(identity) != 64 or any(
                character not in "0123456789abcdef" for character in identity
            )):
                raise ValueError("Expected identities must be SHA-256 hex digests.")
        if self.expected_closed_output_hash is not None and not self.adopt_closed_output:
            raise ValueError("A closed-output identity requires explicit adoption.")
        if type(self.replacement_destinations) is not tuple or len(self.replacement_destinations) > 256 or any(
            type(item) is not tuple or len(item) != 2 or type(item[0]) is not int or item[0] < 0
            or not isinstance(item[1], Path) for item in self.replacement_destinations
        ) or len({item[0] for item in self.replacement_destinations}) != len(self.replacement_destinations):
            raise ValueError("Replacement outputs require unique integer block indices and immutable Path pairs.")


@dataclass(frozen=True, slots=True)
class SpectrumResumeInspectionRequest:
    previous_journal: Path
    recover_torn_tail: bool = False

    def __post_init__(self):
        if type(self.recover_torn_tail) is not bool:
            raise ValueError("Torn-tail recovery must be an explicit boolean.")


@dataclass(frozen=True, slots=True)
class SpectrumResumeBlockSummary:
    index: int
    destination: Path
    completed: bool
    output_exists: bool
    signal_path: Path
    adoptable: bool = False
    closed_output_hash: str | None = None
    adoption_error: str | None = None


@dataclass(frozen=True, slots=True)
class SpectrumResumeInspection:
    previous_journal: Path
    parent_hash: str
    completed_blocks: int
    status: str
    blocks: tuple[SpectrumResumeBlockSummary, ...]
    source_paths: tuple[Path, ...]
    torn_tail_recovered: bool
