"""Read the public THATEC-compatible result view through PyThat."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from app.domain.errors import ExecutionError


@dataclass(frozen=True, slots=True)
class PyThatRunData:
    dimensions: dict[str, int]
    variables: tuple[str, ...]


def read_pythat_run_data(path: str | Path) -> PyThatRunData:
    """Open the public measurement tree, without using private HDF5 groups."""

    try:
        from PyThat import MeasurementTree
    except ImportError as exc:
        raise ExecutionError("Opening result data requires the bundled PyThat dependency.") from exc
    try:
        with redirect_stdout(StringIO()):
            tree = MeasurementTree(Path(path), index=True, override=True)
        return PyThatRunData(
            dimensions={name: int(size) for name, size in tree.dataset.sizes.items()},
            variables=tuple(sorted(str(name) for name in tree.dataset.data_vars)),
        )
    except Exception as exc:
        raise ExecutionError(f"PyThat cannot open this result file: {exc}") from exc
