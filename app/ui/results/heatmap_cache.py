"""Worker-owned source cache for one immutable archive coordinate plane."""

from collections import OrderedDict

from app.storage import Hdf5RunReader, ThatecRunReader


class HeatmapSourceCache:
    """Keep full grids: crop and DSP are derived views, never cached sources.

    The tab replaces this object when the file identity, row or plane changes.
    Only the serial result worker accesses its dictionaries.
    """

    def __init__(self):
        self._values = {}
        self._baselines = OrderedDict()

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
        return self._read("spectrum_slice", path, row_id, checkpoint)

    def spectrum(self, path, checkpoint):
        return self._read("spectrum", path, checkpoint)

    def references(self, path, *, metadata_only=False):
        return self._read("references", path, metadata_only=metadata_only)

    def reference(self, path, index):
        return self._read("reference", path, index)

    def reference_sweep(self, path, index, sweep):
        return self._read("reference_sweep", path, index, sweep)
