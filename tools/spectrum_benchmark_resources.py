"""Read-only current-process telemetry and conservative raw-archive disk budget."""

import ctypes
import os
from pathlib import Path
import shutil


def process_resources():
    if os.name != "nt":
        return {"os_handles": None, "os_threads": None, "resource_source": "unavailable"}
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("thread", wintypes.DWORD), ("owner", wintypes.DWORD),
                    ("priority", wintypes.LONG), ("delta", wintypes.LONG), ("flags", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetCurrentProcessId.restype = wintypes.DWORD
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessHandleCount.restype = wintypes.BOOL
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Thread32First", "Thread32Next"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        function.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    count = wintypes.DWORD()
    if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count)):
        raise OSError(ctypes.get_last_error(), "GetProcessHandleCount failed")
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "Thread snapshot failed")
    threads, owner = 0, kernel.GetCurrentProcessId()
    entry = ThreadEntry()
    entry.size = ctypes.sizeof(entry)
    try:
        if not kernel.Thread32First(snapshot, ctypes.byref(entry)):
            raise OSError(ctypes.get_last_error(), "Thread32First failed")
        while True:
            threads += int(entry.owner == owner)
            entry.size = ctypes.sizeof(entry)
            if not kernel.Thread32Next(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error != 18:  # ERROR_NO_MORE_FILES
                    raise OSError(error, "Thread32Next failed")
                break
    finally:
        if not kernel.CloseHandle(snapshot):
            raise OSError(ctypes.get_last_error(), "CloseHandle failed")
    return {"os_handles": count.value, "os_threads": threads,
            "resource_source": "Windows GetProcessHandleCount and Toolhelp32 thread snapshot"}


def archive_disk_budget(directory, *, points, frames, reserve_bytes=1024**3):
    if any(type(value) is not int or value < 1 for value in (points, frames, reserve_bytes)):
        raise ValueError("Archive budget requires positive integer points, frames and reserve bytes.")
    directory = Path(directory).resolve()
    while not directory.exists():
        directory = directory.parent
    # Raw, corrected/private and public duplicated f8 vectors plus per-point metadata.
    estimated = frames * (64 * points + 16384)
    # Qualified PyThat close converts the full public tree to temporary netCDF.
    # Budget this separately; source-archive size alone understates peak disk use.
    conversion = frames * 32 * points
    free = shutil.disk_usage(directory).free
    required = estimated + conversion + reserve_bytes
    if free < required:
        raise OSError(f"Insufficient archive disk budget: {required} bytes required, {free} available.")
    return {"estimated_archive_bytes": estimated, "reserve_bytes": reserve_bytes,
            "estimated_conversion_bytes": conversion, "required_free_bytes": required,
            "free_bytes_before": free,
            "method": "archive 64 bytes/bin/frame + 16 KiB/frame; temporary PyThat 32 bytes/bin/frame; estimates"}
