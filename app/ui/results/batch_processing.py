"""Batch post-processing for recorded heatmap planes and measurement spectra."""

from __future__ import annotations

import csv
import json
import tempfile
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from textwrap import wrap

import numpy as np

from app.domain.errors import ExecutionError
from app.domain.quantities import Quantity, QuantityError, format_quantity_auto, parse_quantity
from app.recipes.parameter_registry import PARAMETERS_BY_TARGET, persisted_quantity_unit
from app.spectrum.display_model import SpectrumDisplayTrace
from app.storage import Hdf5RunReader, StoredPoint, ThatecRow, ThatecRunReader
from app.storage.filesystem_paths import native_path
from app.storage.thatec_reader import ThatecSpectralRow

from .analysis_export import (
    analysis_file_stem,
    postprocessing_directory,
    processing_manifest,
    write_analysis_manifest,
)
from .heatmap_cache import HeatmapSourceCache
from .heatmap_coordinates import HeatmapCoordinates, HeatmapRequest, _selected_checkpoints, _selection_range
from .map_processing import MapProcessing
from .processing import (
    ResultProcessing,
    baseline_sample_label,
    recorded_instrument_average,
    recorded_spectrum_label,
    recorded_spectrum_provenance,
)
from .read_session import ResultReadSession
from .spectrum_views import SpectrumView, read_private_view, read_public_view

_POWERPOINT_NAME = "Plots.pptx"
_PLOT_TITLE_WIDTH = 108
_PLOT_SIZE_INCHES = (10.8, 5.6)
_PLOT_DPI = 125


@dataclass(frozen=True, slots=True)
class BatchItem:
    title: str
    checkpoints: tuple[int, ...]
    request: HeatmapRequest | None = None
    parameters: tuple = ()


def _parameter_label(label):
    if label.startswith("VOUT ") and label.endswith(" programming voltage"):
        return "MOKE Box " + label.removesuffix(" programming voltage")
    if label.startswith("Keithley "):
        return label.removesuffix(" current").removesuffix(" voltage").removesuffix(" level")
    return label


def _formatted_range(low, high, unit):
    try:
        scale = parse_quantity(f"1 {unit}") if unit else None
    except QuantityError:
        scale = None
    if scale is not None:
        low, high = low * scale.si_value, high * scale.si_value
        dimension = scale.dimension
        if low == high:
            return format_quantity_auto(low, dimension, preferred_unit=unit)
        engineering = format_quantity_auto(max(abs(low), abs(high)), dimension).split(" ", 1)[-1]
        low, high = (Quantity(value, dimension).as_unit(engineering) for value in (low, high))
        unit = engineering
    values = f"{low:.9g}" if low == high else f"{low:.9g} to {high:+.9g}" if low < 0 < high else f"{low:.9g} to {high:.9g}"
    return f"{values} {unit}".strip()


def measurement_parameters(coordinates, checkpoints, points_by_index=None):
    if coordinates is None or not checkpoints:
        return ()
    parameters = []
    for axis in coordinates.dimensions:
        if axis.is_frequency:
            continue
        values = [axis.values[index] for index in checkpoints]
        parameters.append((axis.id, _parameter_label(axis.label), axis.unit, min(values), max(values)))
    if points_by_index and all(index in points_by_index for index in checkpoints):
        setpoints = tuple(points_by_index[index].setpoints for index in checkpoints)
        shown = {axis.id for axis in coordinates.dimensions}
        for key in setpoints[0]:
            descriptor = PARAMETERS_BY_TARGET.get(key)
            if key in shown or descriptor is None:
                continue
            values = [point.get(key) for point in setpoints]
            if all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and np.isfinite(value) for value in values):
                label = _parameter_label(descriptor.control_name)
                parameters.append((key, label, descriptor.unit, min(values), max(values)))
    return tuple(parameters)


def measurement_title(parameters):
    return " | ".join(f"{label} {_formatted_range(low, high, unit)}"
                      for _, label, unit, low, high in parameters)


def heatmap_title(coordinates, request, parameters):
    axes = {request.x_id, request.y_id}
    fixed = [parameter for parameter in parameters
             if parameter[0] not in axes and parameter[3] == parameter[4]]
    frequency = request.filters.get("frequency")
    if "frequency" not in axes and frequency is not None:
        bounds = _selection_range(frequency)
        if bounds.minimum == bounds.maximum:
            fixed.append(("frequency", "Frequency", "Hz", bounds.minimum, bounds.maximum))
    if fixed:
        return measurement_title(fixed)
    x = coordinates.dimension(request.x_id).label
    y = coordinates.dimension(request.y_id).label
    return f"{y} × {x}"


def plan_heatmaps(coordinates, request, points=()):
    """Group the archive in recorded order, including repeated visits."""
    axes = {request.x_id, request.y_id}
    carry = {key: value for key, value in request.filters.items()
             if key in axes or coordinates.dimension(key).is_frequency}
    fixed = [axis for axis in coordinates.dimensions if not axis.is_frequency and axis.id not in axes]
    groups = defaultdict(list)
    for index in _selected_checkpoints(coordinates, carry):
        groups[tuple(axis.values[index] for axis in fixed)].append(index)
    points_by_index = {point.index: point for point in points}
    items = []
    for values, checkpoints in groups.items():
        parameters = measurement_parameters(coordinates, checkpoints, points_by_index)
        filters = {**carry, **dict(zip((axis.id for axis in fixed), values, strict=True))}
        plane = HeatmapRequest(request.x_id, request.y_id, filters)
        items.append(BatchItem(heatmap_title(coordinates, plane, parameters),
                               tuple(checkpoints), plane, parameters))
    return tuple(items)


def plan_spectra(checkpoints):
    items = []
    for index, setpoints in checkpoints:
        parameters = []
        for key, value in (setpoints or {}).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
                continue
            descriptor = PARAMETERS_BY_TARGET.get(key)
            label = _parameter_label(descriptor.control_name) if descriptor else key
            unit = persisted_quantity_unit(key)
            parameters.append((key, label, unit, value, value))
        title = " | ".join(filter(None, (measurement_title(parameters), f"Checkpoint {index}")))
        items.append(BatchItem(title, (index,), parameters=tuple(parameters)))
    return tuple(items)


@dataclass(frozen=True, slots=True)
class HeatmapBatch:
    row: ThatecRow
    coordinates: HeatmapCoordinates
    points: tuple[StoredPoint, ...]
    processing: ResultProcessing
    map_state: MapProcessing
    absolute: bool
    palette: str
    colours: tuple[tuple[float, ...], ...]
    manual_levels: tuple[float, float] | None = None
    cached_row: ThatecSpectralRow | None = None


@dataclass(frozen=True, slots=True)
class SpectrumBatch:
    points: tuple[StoredPoint, ...]
    processing: ResultProcessing
    view: SpectrumView
    absolute: bool
    variant: str = "raw"
    row: ThatecRow | None = None
    trace_index: int = 0


def _spectrum_traces(session, state, checkpoint, point, cancelled):
    if state.row is not None:
        spectrum = session.public_spectrum(session.path, state.row.id, checkpoint)
        if state.processing.active or state.view.active:
            result = read_public_view(session, spectrum, state.trace_index, (point,) if point else (),
                                      state.processing, state.view, cancelled=cancelled)
            return result.traces, result.baselines, spectrum.x_label, spectrum.x_unit
        trace = spectrum.traces[state.trace_index]
        if spectrum.role == "spectrum_processed":
            label, provenance = trace.name, ("subtract_power_signed",)
        else:
            label = recorded_spectrum_label(point, trace.name)
            provenance = recorded_spectrum_provenance(point)
        return (SpectrumDisplayTrace("recorded", label, spectrum.x_values, trace.values,
                                     spectrum.y_unit, checkpoint, provenance),), (), spectrum.x_label, spectrum.x_unit
    if state.processing.active or state.view.active:
        result = read_private_view(session, point, state.processing, state.view, cancelled=cancelled)
        return result.traces, result.baselines, "Frequency", "Hz"
    raw = session.spectrum(session.path, checkpoint)
    if raw is None:
        raise ValueError("No raw spectrum at this checkpoint.")
    if state.variant == "saved_view":
        if raw.saved_display is None:
            raise ValueError("This checkpoint has no saved Anritsu view.")
        return raw.saved_display.traces, (), "Frequency", "Hz"
    traces, baselines = [], ()
    if state.variant in {"raw", "raw_processed", "raw_reference"}:
        label = recorded_spectrum_label(point, "Stored spectrum", trace_name=raw.trace_name)
        provenance = recorded_spectrum_provenance(point, trace_name=raw.trace_name)
        traces.append(SpectrumDisplayTrace("raw", label, raw.frequencies_hz,
                                           raw.powers_dbm, "dBm", checkpoint, provenance))
    if state.variant in {"processed", "raw_processed"}:
        if raw.processed_values is None or (traces and raw.processed_unit != "dBm"):
            raise ValueError("Stored processed spectrum is missing or uses incompatible overlay units.")
        traces.append(SpectrumDisplayTrace("processed", "Processed spectrum", raw.frequencies_hz,
                                           raw.processed_values, raw.processed_unit, checkpoint,
                                           ("subtract_power_signed",) if raw.processing_operation in
                                           {"subtract_power_signed", "subtract_reference_signed"} else ()))
    if state.variant in {"reference", "raw_reference"}:
        from .processing import RecordedBaselineEvidence

        reference = session.reference(session.path, raw.reference_index if raw.reference_index is not None else 0)
        if reference is None:
            raise ValueError("Linked reference is unavailable.")
        label = (f"Reference · {baseline_sample_label(reference)}" if reference.averaging_source == "instrument"
                 else "Reference spectrum")
        traces.append(SpectrumDisplayTrace("reference", label, reference.frequencies_hz,
                                           reference.powers_dbm, "dBm", checkpoint))
        baselines = (RecordedBaselineEvidence.from_reference(reference),)
    return tuple(traces), baselines, "Frequency", "Hz"


def _figure(title):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=_PLOT_SIZE_INCHES, dpi=_PLOT_DPI, layout="constrained")
    figure.suptitle("\n".join(wrap(title, width=_PLOT_TITLE_WIDTH)), fontsize=11)
    return FigureCanvasAgg(figure), figure.add_subplot()


def _axis_label(label, unit):
    return f"{label} [{unit}]" if unit else label


def _set_axis_label(axis, label, unit):
    from matplotlib.ticker import EngFormatter

    if unit in {"Hz", "A", "V", "W", "T", "s", "Ohm", "Ω"}:
        axis.set_label_text(label)
        axis.set_major_formatter(EngFormatter(unit=unit))
    else:
        axis.set_label_text(_axis_label(label, unit))


def _export_heatmap(path, item, state, source, cache, metadata, cancelled):
    from matplotlib.colors import ListedColormap

    from .heatmap_tab import (
        HeatmapPlotWidget,
        _heatmap_display,
        _process_map_payload,
        _read_heatmap_payload,
        _write_heatmap_companions,
    )

    payload = _read_heatmap_payload(source, state.row, state.coordinates, item.request, cancelled=cancelled,
                                    processing=state.processing, points=state.points, source_cache=cache,
                                    checkpoint_indices=item.checkpoints)
    frequency_axis = 1 if item.request.x_id == "frequency" else 0 if item.request.y_id == "frequency" else None
    payload = _process_map_payload(payload, state.map_state, frequency_axis, cancelled=cancelled)
    matrix, levels, z_label, signed, absolute = _heatmap_display(
        payload, state.row, state.processing, state.map_state, state.absolute)
    levels = state.manual_levels or levels
    x_edges = HeatmapPlotWidget._cell_edges(payload.x_values)
    y_edges = HeatmapPlotWidget._cell_edges(payload.y_values)
    metadata.update(spectrum_row=state.row.id, coordinate_selection=processing_manifest(item.request),
                    recorded_baselines=[processing_manifest(r) for r in payload.baselines],
                    axes={"x": {"label": payload.x_label, "unit": payload.x_unit},
                          "y": {"label": payload.y_label, "unit": payload.y_unit}},
                    value_unit=payload.z_unit, notes=list(payload.processing_notes),
                    corrected_frequencies_hz=list(payload.corrected_frequencies_hz),
                    palette=state.palette, colour_levels=levels,
                    missing_checkpoints=payload.missing_checkpoints,
                    signed_difference=signed, display_transform="absolute_difference" if absolute else "none",
                    line_classification_source="Recorded dBm, same selected coordinate plane"
                    if state.map_state.mask_lines else None)
    if payload.instrument_averages:
        metadata["instrument_averages_by_checkpoint"] = payload.instrument_averages
    canvas, ax = _figure(item.title)
    image = ax.pcolormesh(x_edges, y_edges, matrix, cmap=ListedColormap(state.colours),
                          vmin=levels[0], vmax=levels[1], shading="flat", rasterized=True)
    canvas.figure.colorbar(image, ax=ax, label=_axis_label(z_label, payload.z_unit))
    _set_axis_label(ax.xaxis, payload.x_label, payload.x_unit)
    _set_axis_label(ax.yaxis, payload.y_label, payload.y_unit)
    canvas.print_png(path)
    canvas.figure.clear()
    _write_heatmap_companions(path, matrix, payload.x_values, payload.y_values,
                             payload.cell_checkpoints, metadata, x_edges, y_edges)


def _export_spectrum(path, item, state, session, points, metadata, cancelled):
    checkpoint = item.checkpoints[0]
    point = points.get(checkpoint)
    traces, baselines, x_label, x_unit = _spectrum_traces(session, state, checkpoint, point, cancelled)
    if not traces:
        raise ValueError("No visible spectrum trace is available.")
    canvas, ax = _figure(item.title)
    columns = []
    absolute_traces = []
    for trace in traces:
        absolute = state.absolute and state.variant != "saved_view" and trace.is_signed_difference
        label = f"Abs · {trace.label}" if absolute else trace.label
        values = np.abs(trace.values) if absolute else trace.values
        if absolute:
            absolute_traces.append(trace.label)
        columns.append((label, trace.unit, trace.frequencies_hz, values))
        ax.plot(trace.frequencies_hz, values, label=label, linewidth=0.9)
    ax.set_ylabel(_axis_label("Amplitude", traces[0].unit))
    _set_axis_label(ax.xaxis, x_label, x_unit)
    if len(columns) > 1:
        ax.legend()
    ax.grid(alpha=0.2)
    canvas.print_png(path)
    canvas.figure.clear()
    with path.with_suffix(".csv").open("x", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("trace", "frequency_hz" if x_unit == "Hz" else "coordinate", "amplitude", "unit"))
        for label, unit, frequencies, values in columns:
            writer.writerows((label, frequency, value, unit)
                             for frequency, value in zip(frequencies, values, strict=True))
    metadata.update(absolute_traces=absolute_traces, value_unit=traces[0].unit,
                    axes={"x": {"label": x_label, "unit": x_unit}},
                    recorded_baselines=[processing_manifest(ref) for ref in baselines])
    average = recorded_instrument_average(point)
    if average is not None:
        metadata["instrument_average"] = average
    write_analysis_manifest(path, metadata)


def export_batch(source, items, state, *, power_point, expected_identity, cancelled, progress, preview):
    from .heatmap_tab import _archive_identity

    source = Path(source)
    if expected_identity != _archive_identity(source):
        raise ValueError("The archive changed. Reopen the result before exporting.")
    heatmaps = isinstance(state, HeatmapBatch)
    base = postprocessing_directory(source) / ("Heatmaps" if heatmaps else "Spectra")
    native_path(base).mkdir(parents=True, exist_ok=True)
    batch = tempfile.mkdtemp(prefix=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-"),
                            dir=native_path(base, reserved=32))
    folder = base / Path(batch).name
    settings = {"spectrum_processing": processing_manifest(state.processing), "absolute": state.absolute}
    if heatmaps:
        settings["map_processing"] = processing_manifest(state.map_state)
        settings["palette"] = state.palette
        settings["manual_colour_levels"] = state.manual_levels
    else:
        settings.update(view=processing_manifest(state.view), spectrum_variant=state.variant,
                        spectrum_row=state.row.id if state.row else None, trace_index=state.trace_index)
    index = {"schema": "pylab.batch-postprocessing.v1", "source_file": str(source),
             "source_mtime_ns": expected_identity[1], "source_bytes": expected_identity[2],
             "source_data_modified": False, "started_at_utc": datetime.now(UTC).isoformat(),
             "settings": settings, "planned": len(items), "items": []}
    count = 0
    index_path = native_path(folder / "index.json")
    readers = ExitStack()
    try:
        progress(0, len(items))
        cache = None
        if heatmaps and state.cached_row is not None:
            cache = HeatmapSourceCache({state.row.id: state.cached_row})
            index["read_mode"] = "memory"
        elif state.row is not None and len(state.row.shape) == 2 and not cancelled():
            try:
                cache = HeatmapSourceCache(ThatecRunReader.read_spectral_rows(
                    source, (state.row,), cancelled=cancelled))
            except (ExecutionError, OSError, TypeError, ValueError) as exc:
                if cancelled():
                    raise InterruptedError from exc
                # Archives over the in-memory budget and generic 2D public rows
                # may still be readable one plane or checkpoint at a time.
                index["read_mode"] = "checkpoint"
            else:
                index["read_mode"] = "memory"
        points = {point.index: point for point in state.points} if not heatmaps else None
        private_reader = None
        if not heatmaps and state.row is None and not cancelled():
            private_reader = readers.enter_context(Hdf5RunReader.committed_spectrum_reader(source))
        session = ResultReadSession(source, public_reader=cache.spectrum_slice if cache else None,
                                    spectrum_reader=private_reader, points=points)
        for number, item in enumerate(items, 1):
            if cancelled():
                break
            if expected_identity != _archive_identity(source):
                raise ValueError("The archive changed during export.")
            stem = analysis_file_stem(item.title)
            if native_path(folder / f"{stem}.png").exists():
                stem = analysis_file_stem(f"{number:04d} - {stem}")
            path = native_path(folder / f"{stem}.png")
            parameters = [{"id": key, "label": label, "unit": unit, "minimum": low, "maximum": high}
                          for key, label, unit, low, high in item.parameters]
            entry = {"title": item.title, "checkpoints": item.checkpoints, "parameters": parameters}
            metadata = {"source_file": str(source), "title": item.title,
                        "checkpoints": item.checkpoints, "parameters": parameters, **settings}
            completed = False
            try:
                if heatmaps:
                    _export_heatmap(path, item, state, source, cache, metadata, cancelled)
                else:
                    _export_spectrum(path, item, state, session, points, metadata, cancelled)
                if expected_identity != _archive_identity(source):
                    raise ValueError("The archive changed during export.")
                entry["image"] = path.name
                entry["data"] = path.with_suffix(".h5" if heatmaps else ".csv").name
                completed = True
            except InterruptedError:
                if not cancelled():
                    raise
                break
            except Exception as exc:  # noqa: BLE001 - isolate a failed item from the remaining sweep
                if cancelled():
                    break
                entry["error"] = str(exc)
            finally:
                if not completed:
                    for output in (path, path.with_suffix(".h5"), path.with_suffix(".csv"),
                                   path.with_suffix(".png.analysis.json")):
                        native_path(output).unlink(missing_ok=True)
            index["items"].append(entry)
            if completed:
                count += 1
                preview(item.title, folder / path.name)
            progress(number, len(items))
        if power_point and count and not cancelled():
            preview("Preparing PowerPoint…", None)
            if _write_powerpoint(folder, index["items"], cancelled):
                index["powerpoint"] = _POWERPOINT_NAME
    except InterruptedError:
        if not cancelled():
            raise
    except Exception as exc:  # noqa: BLE001 - retain the batch report if export cannot continue
        index["error"] = str(exc)
    finally:
        readers.close()
        index.update(cancelled=cancelled(), exported=count, finished_at_utc=datetime.now(UTC).isoformat())
        staging_index = native_path(folder / ".index.json")
        staging_index.write_text(
            json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        staging_index.replace(index_path)
    return folder, index


def _write_powerpoint(folder, entries, cancelled):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    for entry in entries:
        if cancelled():
            return False
        if "image" not in entry:
            continue
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor.from_string("FFFFFF")
        title = slide.shapes.add_textbox(Inches(.45), Inches(.2), Inches(12.4), Inches(1)).text_frame
        title.word_wrap = True
        paragraph = title.paragraphs[0]
        paragraph.text = entry["title"]
        paragraph.font.size = Pt(20)
        paragraph.font.bold = True
        with native_path(folder / entry["image"]).open("rb") as image:
            picture = slide.shapes.add_picture(image, Inches(.85), Inches(1.22), width=Inches(11.6))
        # Reserve the slide header for editable text and trim the same heading from the plot.
        lines = len(wrap(entry["title"], width=_PLOT_TITLE_WIDTH))
        heading_crop = (5 + 21 * lines) / (_PLOT_SIZE_INCHES[1] * _PLOT_DPI)
        picture.crop_top = heading_crop
        picture.height = round(picture.height * (1 - heading_crop))
    deck.core_properties.title = "Sweep post-processing"
    target = native_path(folder / _POWERPOINT_NAME)
    staging = native_path(folder / f".{_POWERPOINT_NAME}")
    try:
        deck.save(staging)
        if cancelled():
            return False
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)
    return True
