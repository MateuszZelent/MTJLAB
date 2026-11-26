"""Presentation settings saved beside a sweep's derived results."""

import json
import math
from dataclasses import asdict, dataclass

from PySide6.QtCore import QIODevice, QLockFile, QSaveFile

from app.spectrum.analysis import SpectrumAnalysisParameters
from app.storage.filesystem_paths import native_path

from .analysis_export import postprocessing_directory
from .heatmap_coordinates import SERIES_DIMENSION_ID, HeatmapRequest, _selection_range
from .map_processing import MapProcessing
from .processing import ResultProcessing

_SCHEMA = "pylab.series-heatmap-views.v1"


@dataclass(frozen=True, slots=True)
class SeriesHeatmapView:
    row_id: str
    request: HeatmapRequest
    palette: str
    colour_levels: tuple[float, float]
    view_ranges: tuple[tuple[float, float], tuple[float, float]]
    inverted_axes: tuple[bool, bool]
    absolute: bool
    processing: ResultProcessing
    map_state: MapProcessing

    @property
    def key(self):
        series = _selection_range(self.request.filters[SERIES_DIMENSION_ID])
        if series.minimum != series.maximum or series.minimum <= 0 or not series.minimum.is_integer():
            raise ValueError("A saved heatmap view must select one recorded series.")
        return self.row_id, self.request.x_id, self.request.y_id, int(series.minimum)

    def __post_init__(self):
        if (not all(isinstance(value, str) and value for value in
                    (self.row_id, self.request.x_id, self.request.y_id, self.palette))
                or self.request.x_id == self.request.y_id):
            raise ValueError("Invalid saved heatmap axes, row, or palette.")
        _ = self.key
        for limits in (self.colour_levels, *self.view_ranges):
            if len(limits) != 2 or not all(math.isfinite(value) for value in limits):
                raise ValueError("Saved heatmap limits must be finite pairs.")
        if (len(self.view_ranges) != 2 or any(lo >= hi for lo, hi in self.view_ranges)
                or self.colour_levels[0] > self.colour_levels[1]):
            raise ValueError("Saved heatmap limits must be ordered.")
        if (type(self.absolute) is not bool or len(self.inverted_axes) != 2
                or any(type(value) is not bool for value in self.inverted_axes)):
            raise ValueError("Invalid saved heatmap display flags.")

    @classmethod
    def from_record(cls, record):
        fields = dict(record)
        request = dict(fields["request"])
        filters = {key: tuple(value) if isinstance(value, list) else value
                   for key, value in dict(request["filters"]).items()}
        for value in filters.values():
            _selection_range(value)
        fields["request"] = HeatmapRequest(request["x_id"], request["y_id"], filters)
        processing = dict(fields["processing"])
        parameters = dict(processing["parameters"])
        parameters["narrow_protected_regions_hz"] = tuple(map(tuple, parameters["narrow_protected_regions_hz"]))
        processing["parameters"] = SpectrumAnalysisParameters(**parameters)
        processing["modes"] = tuple(processing["modes"])
        fields["processing"] = ResultProcessing(**processing)
        _ = fields["processing"].output_unit
        map_state = dict(fields["map_state"])
        for key in ("protected_bands_hz", "stripe_bands_hz"):
            map_state[key] = tuple(map(tuple, map_state[key]))
        fields["map_state"] = MapProcessing(**map_state)
        fields["colour_levels"] = tuple(fields["colour_levels"])
        fields["view_ranges"] = tuple(map(tuple, fields["view_ranges"]))
        fields["inverted_axes"] = tuple(fields["inverted_axes"])
        return cls(**fields)


def _views_path(source):
    return postprocessing_directory(source) / f"{source.stem}.series-heatmap-views.json"


def read_series_heatmap_views(identity):
    source, mtime_ns, size = identity
    try:
        document = json.loads(native_path(_views_path(source)).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(document, dict) or document.get("schema") != _SCHEMA:
        raise ValueError("Unknown series heatmap settings format.")
    if document.get("source_identity") != [mtime_ns, size]:
        return {}
    try:
        views = (SeriesHeatmapView.from_record(record) for record in document["views"])
        return {view.key: view for view in views}
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"Invalid saved series heatmap view: {exc}") from exc


def save_series_heatmap_view(identity, view):
    source, mtime_ns, size = identity
    path = _views_path(source)
    native_path(path.parent).mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(native_path(path.with_suffix(".lock"))))
    if not lock.tryLock(0):
        raise OSError(f"Cannot lock saved heatmap views in {path.parent} ({lock.error().name}).")
    try:
        stat = native_path(source).stat()
        if (stat.st_mtime_ns, stat.st_size) != (mtime_ns, size):
            raise ValueError("The archive changed. Reopen the result before saving a view.")
        views = read_series_heatmap_views(identity)
        views[view.key] = view
        document = {"schema": _SCHEMA, "source_identity": [mtime_ns, size],
                    "views": [asdict(saved) for saved in views.values()]}
        data = (json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
        output = QSaveFile(str(path))
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            raise OSError(output.errorString())
        if output.write(data) != len(data):
            output.cancelWriting()
            raise OSError(output.errorString())
        if not output.commit():
            raise OSError(output.errorString())
        return views
    finally:
        lock.unlock()
