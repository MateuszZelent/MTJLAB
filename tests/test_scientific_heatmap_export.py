"""Publication exports exercise saved pixels and vectors, without showing a window."""

import importlib
import json
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFontDatabase, QImage
from PySide6.QtWidgets import QApplication, QWidget


@pytest.fixture(scope="session")
def figure_app():
    app = QApplication.instance() or QApplication([])
    # The Windows offscreen plugin starts with an empty font database.
    font = Path("C:/Windows/Fonts/arial.ttf")
    if "Arial" not in QFontDatabase.families() and font.is_file():
        assert QFontDatabase.addApplicationFont(str(font)) >= 0
    return app


@pytest.fixture
def renderer(figure_app):
    return importlib.import_module("app.ui.widgets.figure_export")


def heatmap_arguments():
    return {
        "values": np.array([[-4e-12, np.nan, 0], [2e-12, 4e-12, -2e-12]]),
        "x_edges": np.array([-5.0, -2.0, 1.0, 9.0]),
        "y_edges": np.array([0.001, 0.002, 0.008]),
        "view_ranges": [[-4.0, 7.0], [0.0015, 0.007]],
        "x_label": "Field",
        "x_unit": "T",
        "y_label": "Current",
        "y_unit": "A",
        "z_label": "Residual power",
        "z_unit": "W",
        "levels": (-4e-12, 4e-12),
        "colormap": "CET-D1",
        "title": "Reference-subtracted power",
    }


def pixel_at(image, info, x, y):
    left, top, width, height = info["plot_rect_px"]
    (xmin, xmax), (ymin, ymax) = info["view_ranges"]
    x_fraction = (x - xmin) / (xmax - xmin)
    y_fraction = (ymax - y) / (ymax - ymin)
    inverted_x, inverted_y = info.get("inverted_axes", (False, False))
    px = round(left + (1 - x_fraction if inverted_x else x_fraction) * width)
    py = round(top + (1 - y_fraction if inverted_y else y_fraction) * height)
    return image.pixelColor(px, py).getRgb()[:3]


def assert_rgb(actual, expected, tolerance=3):
    assert max(abs(int(a) - int(b)) for a, b in zip(actual, expected)) <= tolerance


def figure_image(path):
    if path.suffix == ".png":
        return QImage(str(path))
    from PySide6.QtGui import QPainter
    from PySide6.QtSvg import QSvgRenderer

    image = QImage(1500, 1500, QImage.Format.Format_ARGB32)
    image.fill("white")
    painter = QPainter(image)
    QSvgRenderer(str(path)).render(painter)
    painter.end()
    return image


def png_chunks(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    while offset < len(data):
        count = struct.unpack(">I", data[offset : offset + 4])[0]
        yield data[offset + 4 : offset + 8], data[offset + 8 : offset + 8 + count]
        offset += count + 12


def test_png_physical_cells_tiny_signed_levels_nan_and_dpi(renderer, tmp_path):
    arguments = heatmap_arguments()
    path = tmp_path / "heatmap.png"
    info = renderer.export_heatmap_figure(path, **arguments)
    image = QImage(str(path))
    assert (image.width(), image.height()) == (1500, 1500)
    assert image.pixelColor(0, 0).getRgb() == (255, 255, 255, 255)
    assert abs(image.dotsPerMeterX() * 0.0254 - 300) < 0.01
    assert struct.unpack(">IIB", dict(png_chunks(path))[b"pHYs"]) == (11811, 11811, 1)
    assert (info["width_px"], info["height_px"], info["dpi"]) == (1500, 1500, 300)
    assert info["profile"] == "mmpp-paper"
    assert info["resolved_font_family"] == "Arial"
    assert info["font_sizes_pt"] == {"tick": 11, "label": 11, "title": 11, "legend": 11}
    np.testing.assert_array_equal(info["view_ranges"], arguments["view_ranges"])
    np.testing.assert_array_equal(info["color_levels"], arguments["levels"])
    assert info["axes"]["z"]["display_unit"] == "pW"
    assert info["axes"]["y"]["display_unit"] == "mA"
    json.dumps(info, allow_nan=False)
    lut = pg.colormap.get(arguments["colormap"]).getLookupTable(nPts=256, mode="byte")
    # Each side of every internal edge, including the nonuniform Y edge.
    for x, y, value in [
        (-3, 0.00175, -4e-12),
        (-2.05, 0.00175, -4e-12),
        (1.05, 0.00175, 0),
        (6, 0.00175, 0),
        (-3, 0.00195, -4e-12),
        (-3, 0.00205, 2e-12),
        (-3, 0.006, 2e-12),
        (-1.95, 0.006, 4e-12),
        (0.95, 0.006, 4e-12),
        (1.05, 0.006, -2e-12),
        (6, 0.006, -2e-12),
    ]:
        index = int(np.clip((value + 4e-12) / 8e-12 * 255, 0, 255))
        assert_rgb(pixel_at(image, info, x, y), lut[index][:3])
    assert pixel_at(image, info, -1, 0.00175) == (255, 255, 255)
    assert pixel_at(image, info, -1.95, 0.00175) == (255, 255, 255)


def test_svg_native_text_cells_and_publication_style(renderer, tmp_path, monkeypatch):
    from app.ui.widgets import plot_style

    calls = []
    original = plot_style.apply_scientific_style

    def style(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(plot_style, "apply_scientific_style", style)
    path = tmp_path / "heatmap.svg"
    info = renderer.export_heatmap_figure(path, **heatmap_arguments())
    root = ET.parse(path).getroot()
    ns = {"s": "http://www.w3.org/2000/svg"}
    texts = root.findall(".//s:text", ns)
    visible = " ".join("".join(node.itertext()) for node in texts)
    for word in ("Field", "Current", "Residual power", "pW", "mA", "Reference-subtracted"):
        assert word in visible
    assert len(texts) > 12  # Includes numeric axis ticks, not just labels.
    assert root.findall(".//s:polygon", ns) or root.findall(".//s:path", ns)
    assert not root.findall(".//s:image", ns)
    assert not root.findall(".//s:script", ns)
    assert not any(key.lower().startswith("on") for node in root.iter() for key in node.attrib)
    assert "crosshair" not in visible.lower() and "Plot Options" not in visible
    assert calls and all(call["publication"] is True for call in calls)
    assert info["format"] == "svg" and info["colormap"] == "CET-D1"
    # Native SVG remains readable when rasterized at the same publication size.
    from PySide6.QtGui import QPainter
    from PySide6.QtSvg import QSvgRenderer

    image = QImage(1500, 1500, QImage.Format.Format_ARGB32)
    image.fill("white")
    painter = QPainter(image)
    QSvgRenderer(str(path)).render(painter)
    painter.end()
    assert_rgb(pixel_at(image, info, -1, 0.00175), (255, 255, 255))
    lut = pg.colormap.get("CET-D1").getLookupTable(nPts=256, mode="byte")
    assert_rgb(pixel_at(image, info, -3, 0.006), lut[191][:3])
    left, top, width, _ = info["plot_rect_px"]
    assert_rgb(image.pixelColor(round(left - 10), round(top + 24)).getRgb()[:3], (255, 255, 255))
    assert_rgb(
        image.pixelColor(round(left + width * 0.1), round(top - 12)).getRgb()[:3], (255, 255, 255)
    )


@pytest.mark.parametrize(
    "change",
    [
        {"x_edges": [-5, -2, -2, 9]},
        {"x_edges": [-5, 1, 9]},
        {"x_edges": [-5, -2, np.inf, 9]},
        {"y_edges": [0.008, 0.002, 0.001]},
        {"values": np.ones((3, 3))},
        {"values": np.array([[np.inf] * 3] * 2)},
        {"levels": [0, 0]},
        {"levels": [1e-12, -1e-12]},
        {"levels": [0, np.nan]},
        {"view_ranges": [[0, 0], [0, 1]]},
        {"view_ranges": [[0, 1], [np.nan, 1]]},
        {"colormap": "nonexistent-publication-palette"},
    ],
)
def test_bad_inputs_fail_before_creating_output(renderer, tmp_path, change):
    arguments = heatmap_arguments() | change
    path = tmp_path / "bad.png"
    with pytest.raises(ValueError):
        renderer.export_heatmap_figure(path, **arguments)
    assert not path.exists()


@pytest.mark.parametrize("all_nan", [False, True])
def test_full_edges_and_all_nan_cells(renderer, tmp_path, all_nan):
    arguments = heatmap_arguments()
    arguments["view_ranges"] = [[-5, 9], [0.001, 0.008]]
    if all_nan:
        arguments["values"][:] = np.nan
    path = tmp_path / "gaps.png"
    info = renderer.export_heatmap_figure(path, **arguments)
    image = QImage(str(path))
    lut = pg.colormap.get("CET-D1").getLookupTable(nPts=256, mode="byte")
    for x, y, index in [
        (-4.95, 0.00105, 0),
        (8.95, 0.00105, 127),
        (-4.95, 0.00795, 191),
        (8.95, 0.00795, 63),
    ]:
        expected = (255, 255, 255) if all_nan else lut[index][:3]
        assert_rgb(pixel_at(image, info, x, y), expected)


def test_encoder_failure_propagates_before_creation(renderer, tmp_path, monkeypatch):
    path = tmp_path / "failed.png"
    widgets = set(QApplication.topLevelWidgets())
    monkeypatch.setattr(QImage, "save", lambda *args, **kwargs: False)
    with pytest.raises(OSError, match="PNG"):
        renderer.export_heatmap_figure(path, **heatmap_arguments())
    assert not path.exists()
    assert set(QApplication.topLevelWidgets()) == widgets


def test_failed_write_preserves_existing_destination(renderer, tmp_path, monkeypatch):
    from PySide6.QtCore import QSaveFile

    class FullDisk(QSaveFile):
        def write(self, data):
            super().write(data[:10])
            self.cancelWriting()
            return -1

    path = tmp_path / "existing.png"
    original = b"previous publication figure"
    path.write_bytes(original)
    monkeypatch.setattr(renderer, "QSaveFile", FullDisk, raising=False)
    with pytest.raises(OSError):
        renderer.export_heatmap_figure(path, **heatmap_arguments())
    assert path.read_bytes() == original


def test_detached_export_preserves_arrays_state_and_queued_events(renderer, figure_app, tmp_path):
    live = pg.PlotWidget()
    live.resize(1000, 600)
    live.setRange(xRange=(2, 9), yRange=(-20, -10), padding=0)
    live.setBackground("#101010")
    live.plot([2, 3, 8], [-14, -13, -12], pen="y")
    arguments = heatmap_arguments()
    arrays = {key: arguments[key].copy() for key in ("values", "x_edges", "y_edges")}
    for array in (arguments[key] for key in arrays):
        array.flags.writeable = False
    ranges = json.dumps(arguments["view_ranges"])
    config = pg.CONFIG_OPTIONS.copy()
    before = live.saveState()
    widgets = set(QApplication.topLevelWidgets())
    events = []
    QTimer.singleShot(0, lambda: events.append("reentered"))
    try:
        for extension in ("png", "svg", "png"):
            renderer.export_heatmap_figure(tmp_path / f"detached.{extension}", **arguments)
            assert set(QApplication.topLevelWidgets()) == widgets
        assert not events
        assert live.saveState() == before
        assert live.backgroundBrush().color().name() == "#101010"
        assert pg.CONFIG_OPTIONS == config
        assert json.dumps(arguments["view_ranges"]) == ranges
        for key, array in arrays.items():
            np.testing.assert_array_equal(arguments[key], array)
    finally:
        from shiboken6 import delete

        delete(live)


@pytest.mark.parametrize("extension", ["png", "svg"])
def test_save_errors_propagate_without_leaking_widgets(renderer, tmp_path, extension):
    widgets = set(QApplication.topLevelWidgets())
    with pytest.raises(OSError):
        renderer.export_heatmap_figure(
            tmp_path / "absent" / f"heatmap.{extension}", **heatmap_arguments()
        )
    assert set(QApplication.topLevelWidgets()) == widgets


def test_no_widget_is_ever_shown(renderer, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Export showed a widget or reentered the Qt event loop")

    monkeypatch.setattr(QWidget, "show", forbidden)
    monkeypatch.setattr(QApplication, "processEvents", forbidden)
    renderer.export_heatmap_figure(tmp_path / "hidden.png", **heatmap_arguments())


def test_main_axes_have_bounded_publication_major_ticks(renderer, tmp_path):
    arguments = heatmap_arguments() | {
        "x_edges": [0.2e9, 1e9, 3e9, 6e9],
        "y_edges": [0.0019, 0.008, 0.0141],
        "view_ranges": [[0.2e9, 6e9], [0.0019, 0.0141]],
        "x_label": "Frequency",
        "x_unit": "Hz",
    }
    path = tmp_path / "density.svg"
    info = renderer.export_heatmap_figure(path, **arguments)
    texts = [
        "".join(node.itertext()).strip()
        for node in ET.parse(path).getroot().iter()
        if node.tag.endswith("}text")
    ]
    for expected in ("1", "2", "3", "4", "5", "6", "8", "10", "12", "14"):
        assert expected in texts
    assert info["axes"]["x"]["major_tick_spacing"] == 1e9
    assert info["axes"]["y"]["major_tick_spacing"] == 0.002
    assert len([text for text in texts if text.replace("-", "").replace(".", "").isdigit()]) <= 22


def test_svg_merges_flat_rows_with_nonuniform_edges_and_nan_holes(renderer, tmp_path):
    from PySide6.QtGui import QPainter
    from PySide6.QtSvg import QSvgRenderer

    values = np.full((3, 200), 1e-12)
    values[1, 70:80] = np.nan
    x_edges = np.cumsum(np.r_[0.0, np.tile([1.0, 2.0, 4.0, 3.0], 50)])
    y_edges = [0.0, 1.0, 3.0, 8.0]
    arguments = heatmap_arguments() | {
        "values": values,
        "x_edges": x_edges,
        "y_edges": y_edges,
        "view_ranges": [[x_edges[0], x_edges[-1]], [0.0, 8.0]],
        "levels": [-1e-12, 1e-12],
    }
    path = tmp_path / "merged.svg"
    info = renderer.export_heatmap_figure(path, **arguments)
    root = ET.parse(path).getroot()
    polygons = root.findall(".//{http://www.w3.org/2000/svg}polygon")
    # One run per outer row, two around the middle-row NaN hole.
    assert len(polygons) == 4
    assert path.stat().st_size < 100_000
    image = QImage(1500, 1500, QImage.Format.Format_ARGB32)
    image.fill("white")
    painter = QPainter(image)
    QSvgRenderer(str(path)).render(painter)
    painter.end()
    rgb = pg.colormap.get("CET-D1").getLookupTable(nPts=256, mode="byte")[-1][:3]
    for x, y, expected in [
        (x_edges[70] - 2, 2.2, rgb),
        (x_edges[70] + 2, 2.2, (255, 255, 255)),
        (x_edges[80] - 2, 2.2, (255, 255, 255)),
        (x_edges[80] + 2, 2.2, rgb),
        ((x_edges[70] + x_edges[80]) / 2, 0.5, rgb),
        ((x_edges[70] + x_edges[80]) / 2, 5.0, rgb),
    ]:
        assert_rgb(pixel_at(image, info, x, y), expected)


@pytest.mark.parametrize("extension", ["png", "svg"])
@pytest.mark.parametrize("inverted", [(True, False), (False, True), (True, True)])
def test_heatmap_preserves_axis_inversion(renderer, tmp_path, inverted, extension):
    path = tmp_path / f"inverted.{extension}"
    info = renderer.export_heatmap_figure(path, inverted_axes=inverted, **heatmap_arguments())
    assert info["inverted_axes"] == list(inverted)
    lut = pg.colormap.get("CET-D1").getLookupTable(nPts=256, mode="byte")
    image = figure_image(path)
    assert_rgb(pixel_at(image, info, -3, 0.006), lut[191][:3])
    assert_rgb(pixel_at(image, info, -1, 0.00175), (255, 255, 255))
