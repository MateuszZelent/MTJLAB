"""Classify result data types for adaptive visualization."""

from __future__ import annotations

from enum import Enum, auto

from app.storage.thatec_reader import ThatecRow, ThatecRun


class ResultDataKind(Enum):
    """Describes the dominant data type stored in a THATEC result."""

    SPECTRUM_SWEEP = auto()
    SCALAR_SWEEP = auto()
    MIXED = auto()
    UNKNOWN = auto()


def classify_result(run: ThatecRun) -> ResultDataKind:
    """Inspect row shapes and return the dominant data kind.

    * ``SPECTRUM_SWEEP`` — at least one row has 2-D data (e.g. Anritsu spectra).
    * ``SCALAR_SWEEP``  — all measured rows are 1-D scalars.
    * ``MIXED``         — both 1-D and 2-D rows are present.
    * ``UNKNOWN``       — no measured rows at all.
    """
    has_spectrum = False
    has_scalar = False
    for row in run.rows.values():
        if not row.shape:
            continue
        if len(row.shape) >= 2:
            has_spectrum = True
        elif len(row.shape) == 1:
            has_scalar = True
    if has_spectrum and has_scalar:
        return ResultDataKind.MIXED
    if has_spectrum:
        return ResultDataKind.SPECTRUM_SWEEP
    if has_scalar:
        return ResultDataKind.SCALAR_SWEEP
    return ResultDataKind.UNKNOWN


def find_spectrum_rows(run: ThatecRun) -> list[ThatecRow]:
    """Return rows with 2-D data (spectral arrays per checkpoint)."""
    return [row for row in run.rows.values() if len(row.shape) >= 2]


def find_scalar_rows(run: ThatecRun) -> list[ThatecRow]:
    """Return rows with 1-D scalar data."""
    return [row for row in run.rows.values() if len(row.shape) == 1]


def find_sweep_axes(run: ThatecRun) -> list[ThatecRow]:
    """Return rows that look like sweep control axes.

    A sweep axis is typically a 1-D row whose ``function`` field indicates it
    drives a device parameter rather than recording a measurement.  Common
    THATEC functions for control axes include ``set``, ``sweep``, and
    ``ramp``.  Rows without any measurement data are also included as they
    often represent internal control nodes.
    """
    control_functions = {"set", "sweep", "ramp", "move", "goto"}
    axes: list[ThatecRow] = []
    for row in run.rows.values():
        if not row.shape:
            # Internal control node with no recorded array.
            axes.append(row)
            continue
        if len(row.shape) == 1 and row.function.lower().strip() in control_functions:
            axes.append(row)
    return axes
