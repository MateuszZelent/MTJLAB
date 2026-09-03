"""Immutable runtime state for semantic measurement-tree operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.recipes.semantic_tree import AxisPointContext


@dataclass(frozen=True, slots=True)
class SemanticOperationState:
    """Latest confirmed/pending state rendered by Builder and Execution."""

    semantic_id: str
    phase: Literal["waiting", "running", "applied", "failed", "skipped"]
    requested_si: float | None
    applied_si: float | None
    readback_si: float | None
    verification: Literal["readback", "simulated_ack", "configured_unchanged"] | None
    action_index: int
    total_actions: int
    axis_context: AxisPointContext | None
    # Display metadata is part of the immutable event projection.  Keeping it
    # with the coalesced state prevents the UI cadence buffer from turning a
    # WAIT/acquisition/configuration into a generic "set point" row.
    kind: str | None = None
    device: str | None = None
    channel: str | int | None = None
    duration_s: float | None = None
    trace: str | None = None
    reference_operation: str | None = None


__all__ = ["SemanticOperationState"]
