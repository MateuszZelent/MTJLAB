"""Qualified boundary for opening thaTEC HDF5 files through PyThat."""

from __future__ import annotations

from contextlib import redirect_stdout
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Any

from app.domain.errors import ExecutionError


_PYTHAT_IMPORT_LOCK = RLock()


def open_measurement_tree(path: str | Path) -> Any:
    """Open and fully load a PyThat tree without selecting the netCDF4 backend."""

    return _read_measurement_tree(path, metadata_only=False)


def inspect_measurement_tree(path: str | Path) -> tuple[tuple[tuple[str, int], ...], tuple[str, ...]]:
    """Perform the complete PyThat conversion without retaining all samples."""

    return _read_measurement_tree(path, metadata_only=True)


def _read_measurement_tree(path: str | Path, *, metadata_only: bool) -> Any:

    target = Path(path)
    _require_importable_checkpoints(target)
    import h5py
    from .resource_budget import archive_public_import_bytes, persisted_validation_memory_budget, require_public_import_capacity

    with h5py.File(target, "r") as file:
        budget = persisted_validation_memory_budget(file)
        if not metadata_only:
            require_public_import_capacity(archive_public_import_bytes(file), budget_bytes=budget)

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
    import dask
    import dask.array as da

    # PyThat 0.2.14 derives its netCDF destination from self.path during
    # save_netcdf. Redirect only that method, without copying the HDF5 source
    # or touching a user's adjacent .nc. Independent processes get separate
    # temporary directories; the lock protects process-global xarray options.
    with _PYTHAT_IMPORT_LOCK, TemporaryDirectory(prefix="mtjlab-pythat-") as directory:
        conversion_path = Path(directory) / "conversion.h5"

        class StreamingSource:
            def __init__(self, handle):
                self.handle = handle

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __getitem__(self, key):
                data = self.handle[key]
                if isinstance(data, h5py.Dataset) and data.name.startswith("/measurement/") and data.name.endswith("/data") and data.ndim > 1:
                    # A spectrum is read one recorded point at a time. NumPy
                    # reshape dispatches to Dask, so PyThat keeps a lazy array
                    # through construction and streams it to the temporary NC.
                    return da.from_array(data, chunks=(1, *data.shape[1:]), lock=True)
                return data

        class IsolatedMeasurementTree(MeasurementTree):
            def construct_tree(self):
                if metadata_only:
                    # Group.get_data accesses f directly, bypassing the tree's
                    # get_data method. Intercept that shared source boundary.
                    self.f = StreamingSource(self.f)
                return super().construct_tree()

            def get_scales(self, row, index):
                import numpy as np

                scale = self.f.get(f"measurement/{row}/scale")
                if scale is None:
                    return None
                width = (int(self.definition[row]["dimensions"]) + 1) * 2
                count = self.data.shape[1:][index]
                if scale.ndim != 1:
                    raise ExecutionError(f"Public scale for {row} must be a flat dataset.")
                if scale.size != self.data.shape[0] * width:
                    raise ExecutionError(f"Public scale for {row} does not match the recorded data dimensions.")
                # PyThat represents this dimension by one shared coordinate.
                # Check every recorded scale in bounded batches rather than
                # silently assigning the first trace's axis to later traces.
                timestamps = self.f.get(f"measurement/{row}/timestamp")
                public_data = self.f.get(f"measurement/{row}/data")
                application_spectrum = self.definition[row].get("lab control role") in {
                    "spectrum", "spectrum_processed",
                }
                chosen = None
                for start in range(0, self.data.shape[0], 4096):
                    end = min(start + 4096, self.data.shape[0])
                    pairs = scale[start * width:end * width].reshape(-1, width)[:, index * 2:index * 2 + 2]
                    if application_spectrum and timestamps is not None:
                        # Missing checkpoints have a NaN timestamp and an
                        # all-NaN trace. Their placeholder scale is not an axis.
                        missing = np.isnan(timestamps[start:end])
                        for local in np.flatnonzero(missing):
                            if not np.isnan(public_data[start + local]).all():
                                raise ExecutionError(f"Public spectrum {row} has data without a timestamp.")
                        pairs = pairs[~missing]
                    if not pairs.size:
                        continue
                    if not np.isfinite(pairs).all():
                        raise ExecutionError(f"Public scale for {row} contains non-finite coordinates.")
                    if chosen is None:
                        chosen = pairs[0].copy()
                    if not np.all(pairs == chosen):
                        raise ExecutionError(
                            f"Public scale for {row} changes between records; a shared PyThat axis "
                            "cannot represent these spectra. Read the per-point HDF5 frequency axes."
                        )
                if chosen is None:
                    raise ExecutionError(f"Public scale for {row} has no recorded coordinates.")
                offset, increment = chosen
                with np.errstate(over="ignore", invalid="ignore"):
                    coordinates = np.arange(count) * increment + offset
                if not np.isfinite(coordinates).all():
                    raise ExecutionError(f"Public scale for {row} produces non-finite coordinates.")
                return coordinates

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
            with open(os.devnull, "w", encoding="utf-8") as quiet_output, xr.set_options(
                netcdf_engine_order=["h5netcdf", "scipy", "netcdf4"]
            ), dask.config.set(scheduler="single-threaded"), redirect_stdout(quiet_output):
                tree.__init__(target, index=True, override=True)
            if metadata_only:
                return (
                    tuple((str(name), int(size)) for name, size in tree.dataset.sizes.items()),
                    tuple(str(name) for name in tree.dataset.data_vars),
                )
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
