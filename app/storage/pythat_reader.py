"""Read the public THATEC-compatible result view through PyThat."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.storage.pythat_bridge import open_measurement_tree


@dataclass(frozen=True, slots=True)
class PyThatRunData:
    dimensions: dict[str, int]
    variables: tuple[str, ...]


def read_pythat_run_data(path: str | Path) -> PyThatRunData:
    """Open the public measurement tree, without using private HDF5 groups."""

    tree = open_measurement_tree(path)
    return PyThatRunData(
        dimensions={name: int(size) for name, size in tree.dataset.sizes.items()},
        variables=tuple(sorted(str(name) for name in tree.dataset.data_vars)),
    )
