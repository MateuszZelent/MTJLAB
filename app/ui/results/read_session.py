"""Bounded, read-only working set for one selected result archive.

Used by the spectrum worker only. Changing/refreshing a file creates a new
session, so a cancelled job can never publish cached data into a newer run.
No HDF5 handle is retained between reads.
"""

from collections import OrderedDict
from dataclasses import fields, is_dataclass
from pathlib import Path
from sys import getsizeof

from app.storage import Hdf5RunReader, ThatecRunReader


def retained_size(value, seen=None):
    """Count tuple/metadata overhead as well as numeric buffers."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = getsizeof(value)
    if is_dataclass(value) and not isinstance(value, type):
        size += sum(retained_size(getattr(value, f.name), seen) for f in fields(value))
    elif isinstance(value, dict):
        size += sum(retained_size(k, seen) + retained_size(v, seen) for k, v in value.items())
    elif isinstance(value, (tuple, list)):
        size += sum(retained_size(v, seen) for v in value)
    return size


class ResultReadSession:
    def __init__(self, path, *, references=None, budget_bytes=64 * 1024 * 1024):
        self.path = Path(path)
        self.catalogue = None if references is None else tuple(references)
        self.budget_bytes = budget_bytes
        self.retained_bytes = 0
        self._cache = OrderedDict()

    def cached(self, key, operation):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key][0]
        result = operation()
        size = retained_size(result) + retained_size(key)
        if size > self.budget_bytes:
            return result
        while self._cache and self.retained_bytes + size > self.budget_bytes:
            _, (_, evicted) = self._cache.popitem(last=False)
            self.retained_bytes -= evicted
        self._cache[key] = result, size
        self.retained_bytes += size
        return result

    def point(self, index):
        return self.cached(("point", index), lambda: Hdf5RunReader.point(self.path, index))

    def spectrum(self, path, index):
        return self.cached(("raw", index), lambda: Hdf5RunReader.spectrum(self.path, index))

    def reference(self, path, index):
        return self.cached(("reference", index), lambda: Hdf5RunReader.reference(self.path, index))

    def reference_sweep(self, path, index, sweep):
        return self.cached(("reference_sweep", index, sweep),
                           lambda: Hdf5RunReader.reference_sweep(self.path, index, sweep))

    def references(self, path, *, metadata_only=True):
        if self.catalogue is None:
            self.catalogue = Hdf5RunReader.references(self.path, metadata_only=True)
        return self.catalogue

    def checkpoint(self, path, point):
        resolved = self.point(point.index)
        trace = self.spectrum(path, point.index) if resolved.has_spectrum else None
        return resolved, trace

    def public_spectrum(self, path, row_id, checkpoint):
        return self.cached(
            ("public", row_id, checkpoint),
            lambda: ThatecRunReader.spectrum_slice(self.path, row_id, checkpoint),
        )

    def processed_private(self, path, point, state):
        from .processing import read_processed_private

        return self.cached(
            ("processed", point.index, state),
            lambda: read_processed_private(path, point, state, reader=self),
        )

    def processed_public(self, path, spectrum, trace_index, state, points):
        from .processing import read_processed_public

        return self.cached(
            ("processed_public", spectrum.row_id, spectrum.checkpoint, trace_index, state),
            lambda: read_processed_public(path, spectrum, trace_index, state, points, reader=self),
        )
