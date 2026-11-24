"""Worker-owned spectral sources for a result archive."""

from collections import OrderedDict

from app.storage import Hdf5RunReader, ThatecRunReader


class HeatmapSourceCache:
    """Keep full grids: crop and DSP are derived views, never cached sources.

    A preloaded archive shares compact matrices across all coordinate planes.
    Only the serial result worker accesses mutable cache entries.
    """

    def __init__(self, spectral_rows=None):
        self._values = {}
        self._baselines = OrderedDict()
        self._spectral_rows = spectral_rows or {}

    def _read(self, method, path, *args, **kwargs):
        key = (method, str(path), args, tuple(sorted(kwargs.items())))
        values = self._baselines if method in {"reference", "reference_sweep"} else self._values
        if key not in values:
            reader = ThatecRunReader if method == "spectrum_slice" else Hdf5RunReader
            values[key] = getattr(reader, method)(path, *args, **kwargs)
        if values is self._baselines:
            values.move_to_end(key)
            while len(values) > 8:
                values.popitem(last=False)
        return values[key]

    def spectrum_slice(self, path, row_id, checkpoint):
        row = self._spectral_rows.get(row_id)
        if row is not None:
            return row.spectrum(checkpoint)
        return self._read("spectrum_slice", path, row_id, checkpoint)

    def preloaded_row(self, row_id):
        """Return the immutable bulk-loaded matrix, if this archive has one."""
        return self._spectral_rows.get(row_id)

    def spectrum(self, path, checkpoint):
        return self._read("spectrum", path, checkpoint)

    def references(self, path, *, metadata_only=False):
        return self._read("references", path, metadata_only=metadata_only)

    def reference(self, path, index):
        return self._read("reference", path, index)

    def reference_sweep(self, path, index, sweep):
        return self._read("reference_sweep", path, index, sweep)
