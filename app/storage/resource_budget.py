"""Bound eager public-file imports before PyThat allocates complete arrays."""

import os
from numbers import Integral

from app.domain.errors import ExecutionError


DEFAULT_IMPORT_MEMORY_BUDGET_BYTES = 512 * 1024 * 1024
IMPORT_WORKING_COPY_FACTOR = 8
IMPORT_BASE_BYTES = 64 * 1024 * 1024


def persisted_validation_memory_budget(file) -> int:
    budget = file["run"].attrs.get("validation_memory_budget_bytes", DEFAULT_IMPORT_MEMORY_BUDGET_BYTES) if "run" in file else DEFAULT_IMPORT_MEMORY_BUDGET_BYTES
    if isinstance(budget, bool) or not isinstance(budget, Integral) or budget <= 0:
        raise ExecutionError("The persisted validation memory budget must be a positive integer.")
    return int(budget)


def available_physical_memory_bytes() -> int | None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in
                ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.available
        return None
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None


def resolve_import_memory_budget(budget_bytes: int | None = None) -> int:
    """Use half of currently available RAM unless an operator sets a lower cap.

    The legacy 512 MiB fallback applies only when the OS cannot report RAM.
    Persist the resolved number for reproducible recovery and later imports.
    """
    if budget_bytes is not None and (type(budget_bytes) is not int or budget_bytes <= 0):
        raise ExecutionError("Public import memory budget must be a positive exact byte count or null for automatic.")
    available = available_physical_memory_bytes()
    automatic = max(1, available // 2) if available is not None else DEFAULT_IMPORT_MEMORY_BUDGET_BYTES
    return min(budget_bytes, automatic) if budget_bytes is not None else automatic


def require_public_import_capacity(required_bytes: int, *, budget_bytes: int | None = None) -> None:
    if type(required_bytes) is not int or required_bytes < 0:
        raise ExecutionError("Public import memory estimates must be non-negative exact byte counts.")
    effective = resolve_import_memory_budget(budget_bytes)
    if required_bytes > effective:
        raise ExecutionError(
            f"PyThat eager import requires an estimated {required_bytes} bytes; "
            f"the configured/available memory budget is {effective} bytes. "
            "Use bounded checkpoint readers or qualify a larger import budget before running."
        )


def archive_public_import_bytes(file) -> int:
    """Estimate working arrays from public dataset shapes, without loading them."""
    import h5py

    root = file.get("measurement")
    total = 0
    if root is not None:
        def count(_name, value):
            nonlocal total
            if isinstance(value, h5py.Dataset) and value.dtype.kind in "biuf":
                total += value.size * max(8, value.dtype.itemsize)
        root.visititems(count)
    return IMPORT_BASE_BYTES + IMPORT_WORKING_COPY_FACTOR * total
