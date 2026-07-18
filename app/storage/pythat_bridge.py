"""Qualified boundary for opening thaTEC HDF5 files through PyThat."""

from __future__ import annotations

from contextlib import redirect_stdout
from importlib.metadata import PackageNotFoundError, version
from io import StringIO
from pathlib import Path
from typing import Any

from app.domain.errors import ExecutionError


def open_measurement_tree(path: str | Path) -> Any:
    """Open and fully load a PyThat tree without selecting the netCDF4 backend."""

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

    target = Path(path)
    sidecar = target.with_suffix(".nc")
    tree: Any | None = None
    try:
        with xr.set_options(
            netcdf_engine_order=["h5netcdf", "scipy", "netcdf4"]
        ), redirect_stdout(StringIO()):
            tree = MeasurementTree(target, index=True, override=True)
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
        handle = getattr(tree, "f", None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        try:
            sidecar.unlink(missing_ok=True)
        except OSError as exc:
            raise ExecutionError(f"Could not remove PyThat sidecar {sidecar}: {exc}") from exc
