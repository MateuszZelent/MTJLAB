"""Qualified boundary for opening thaTEC HDF5 files through PyThat."""

from __future__ import annotations

from contextlib import redirect_stdout
from importlib.metadata import PackageNotFoundError, version
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Any

from app.domain.errors import ExecutionError


_PYTHAT_IMPORT_LOCK = RLock()


def open_measurement_tree(path: str | Path) -> Any:
    """Open and fully load a PyThat tree without selecting the netCDF4 backend."""

    target = Path(path)
    _require_importable_checkpoints(target)

    try:
        installed_version = version("PyThat")
    except PackageNotFoundError as exc:
        raise ExecutionError(
            "Opening result data requires the qualified PyThat 0.2.14 dependency."
        ) from exc
    if installed_version != "0.2.14":
        raise ExecutionError(
            f"PyThat {installed_version} is not qualified; expected 0.2.14."
        )

    import xarray as xr

    if "h5netcdf" not in xr.backends.list_engines():
        raise ExecutionError("The qualified PyThat bridge requires h5netcdf.")

    from PyThat import MeasurementTree

    # PyThat 0.2.14 derives its netCDF destination from self.path during
    # save_netcdf. Redirect only that method, without copying the HDF5 source
    # or touching a user's adjacent .nc. Independent processes get separate
    # temporary directories; the lock protects process-global xarray options.
    with _PYTHAT_IMPORT_LOCK, TemporaryDirectory(prefix="mtjlab-pythat-") as directory:
        conversion_path = Path(directory) / "conversion.h5"

        class IsolatedMeasurementTree(MeasurementTree):
            def save_netcdf(self):
                source_path = self.path
                self.path = conversion_path
                try:
                    return super().save_netcdf()
                finally:
                    self.path = source_path

        # Keep a reference even if __init__ fails after opening the source.
        tree = IsolatedMeasurementTree.__new__(IsolatedMeasurementTree)
        try:
            with xr.set_options(
                netcdf_engine_order=["h5netcdf", "scipy", "netcdf4"]
            ), redirect_stdout(StringIO()):
                tree.__init__(target, index=True, override=True)
            tree.dataset.load()
            tree.dataset.close()
            return tree
        except ExecutionError:
            raise
        except Exception as exc:
            raise ExecutionError(
                f"PyThat cannot open this result file through h5netcdf: {exc}"
            ) from exc
        finally:
            dataset = getattr(tree, "dataset", None)
            if dataset is not None:
                dataset.close()
            handle = getattr(tree, "f", None)
            if handle is not None:
                handle.close()


def _require_importable_checkpoints(path: Path) -> None:
    """A full public import requires a closed, coherent application archive.

    Interrupted archives remain inspectable through the checkpoint readers.
    Recovery on a copy must reconcile public/private rows before full import.
    External thaTEC files do not carry this application-specific contract.
    """
    import h5py
    from .hdf5_reader import Hdf5RunReader

    try:
        with h5py.File(path, "r") as file:
            if "run" not in file or "points" not in file:
                return
            status = Hdf5RunReader._attribute_text(file["run"].attrs.get("status"))
            names = Hdf5RunReader._committed_point_names(file)
            if status not in {"completed", "aborted", "faulted"} or (
                len(names) != len(file["points"]) or len(file.get("_pending", {}))
            ):
                raise ExecutionError(
                    "Full PyThat import requires a closed archive with committed checkpoints; "
                    "inspect committed data or recover a copy of the interrupted archive first."
                )
    except OSError as exc:
        raise ExecutionError(f"Cannot inspect HDF5 checkpoints before PyThat import: {path}") from exc
