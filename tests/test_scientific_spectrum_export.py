"""Detached spectrum figures preserve gaps, overlays, SI units and print contrast."""

import copy
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from tests.test_scientific_heatmap_export import assert_rgb, figure_image, pixel_at
from tests.test_scientific_heatmap_export import figure_app as figure_app  # noqa: PLC0414
from tests.test_scientific_heatmap_export import renderer as renderer  # noqa: PLC0414


def spectrum_arguments():
    return {
        "traces": [
            {
                "name": "Measured",
                "x": np.array([1e6, 2e6, 3e6, 4e6, 5e6]),
                "y": np.array([-4e-12, -4e-12, np.nan, -4e-12, -4e-12]),
                "color": "#0000ff",
                "width": 2,
            },
            {
                "name": "Maximum hold",
                "x": np.array([1e6, 5e6]),
                "y": np.array([4e-12, 4e-12]),
                "color": "#ffff00",
                "width": 2,
            },
        ],
        "view_ranges": [[1e6, 5e6], [-8e-12, 8e-12]],
        "x_label": "Frequency",
        "x_unit": "Hz",
        "y_label": "Residual power",
        "y_unit": "W",
        "title": "Residual spectrum",
        "markers": [
            {"position": 1.5e6, "label": "M1", "color": "#ff0000"},
            {"position": 4.5e6, "label": "Δf", "color": "#008000"},
        ],
        "band": (3.5e6, 4e6),
    }


def test_spectrum_pixels_gaps_units_markers_band_and_contrast(renderer, tmp_path):
    arguments = spectrum_arguments()
    original = copy.deepcopy(arguments)
    path = tmp_path / "spectrum.png"
    widgets = set(QApplication.topLevelWidgets())
    info = renderer.export_spectrum_figure(path, **arguments)
    assert set(QApplication.topLevelWidgets()) == widgets
    image = QImage(str(path))
    assert (image.width(), image.height()) == (1500, 1500)
    assert abs(image.dotsPerMeterY() * 0.0254 - 300) < 0.01
    assert_rgb(pixel_at(image, info, 1.8e6, -4e-12), (0, 0, 255), tolerance=20)
    # The missing-data gap remains empty between the paper style's grid lines.
    assert_rgb(pixel_at(image, info, 3.1e6, -3.7e-12), (255, 255, 255))
    assert pixel_at(image, info, 3.75e6, 0) != (255, 255, 255)
    assert_rgb(pixel_at(image, info, 1.5e6, 0), (255, 0, 0), tolerance=20)
    assert info["axes"]["x"]["display_unit"] == "MHz"
    assert info["axes"]["y"]["display_unit"] == "pW"
    assert info["traces"][1]["color"] != "#ffff00"
    for trace, saved in zip(arguments["traces"], original["traces"]):
        for key in ("x", "y"):
            np.testing.assert_array_equal(trace[key], saved[key])
        assert trace["color"] == saved["color"] and trace["width"] == saved["width"]
    assert arguments["markers"] == original["markers"]


def test_spectrum_svg_text_legend_explicit_markers_and_log_units(renderer, tmp_path):
    arguments = spectrum_arguments() | {
        "y_unit": "dBm",
        "y_label": "Power",
        "view_ranges": [[1e6, 5e6], [-80, -20]],
    }
    for trace in arguments["traces"]:
        trace["y"] = np.full_like(trace["y"], -50)
    path = tmp_path / "spectrum.svg"
    info = renderer.export_spectrum_figure(path, **arguments)
    root = ET.parse(path).getroot()
    ns = {"s": "http://www.w3.org/2000/svg"}
    # SVG may split a Greek symbol and the following Latin glyph into text runs.
    text = "".join("".join(node.itertext()) for node in root.findall(".//s:text", ns))
    for expected in ("Measured", "Maximum hold", "Frequency", "MHz", "dBm", "M1", "Δf"):
        assert expected in text
    assert "mdBm" not in text and "crosshair" not in text.lower()
    assert not root.findall(".//s:image", ns)
    assert info["axes"]["y"]["display_unit"] == "dBm"

    def text_styles(node, inherited=None):
        style = dict(inherited or {}) | node.attrib
        if node.tag.endswith("}text"):
            yield "".join(node.itertext()), style
        for child in node:
            yield from text_styles(child, style)

    legend = [style for text, style in text_styles(root) if "Maximum hold" in text]
    assert legend and all(style["fill"] == "#636363" for style in legend)
    assert all(float(style["font-size"]) >= 14 for style in legend)


@pytest.mark.parametrize(
    "change",
    [
        {"traces": []},
        {"traces": [{"x": [1, 2], "y": [1], "name": "Bad", "color": "b", "width": 1}]},
        {"traces": [{"x": [1, np.inf], "y": [1, 2], "name": "Bad", "color": "b", "width": 1}]},
        {"traces": [{"x": [1, 2], "y": [1, np.inf], "name": "Bad", "color": "b", "width": 1}]},
        {"traces": [{"x": [1, 2], "y": [1, 2], "name": "Bad", "color": "b", "width": 0}]},
        {"markers": [{"position": np.nan, "label": "M1", "color": "r"}]},
        {"band": (4, 2)},
    ],
)
def test_spectrum_validation_precedes_file_creation(renderer, tmp_path, change):
    path = tmp_path / "invalid.svg"
    with pytest.raises(ValueError):
        renderer.export_spectrum_figure(path, **(spectrum_arguments() | change))
    assert not path.exists()


@pytest.mark.parametrize("color", ["#89b4fa", "#cba6f7", "#ffffff"])
def test_light_trace_and_annotation_colors_keep_hue_and_print_contrast(renderer, tmp_path, color):
    from PySide6.QtGui import QColor

    arguments = spectrum_arguments()
    arguments["traces"][0]["color"] = color
    arguments["markers"][0]["color"] = color
    info = renderer.export_spectrum_figure(tmp_path / "contrast.png", **arguments)
    for output in (info["traces"][0]["color"], info["markers"][0]["color"]):
        actual, original = QColor(output), QColor(color)
        rgb = np.array(actual.getRgbF()[:3])
        linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
        assert 1.05 / (float(linear @ [0.2126, 0.7152, 0.0722]) + 0.05) >= 3
        if original.hsvHueF() >= 0:
            assert actual.hsvHueF() == pytest.approx(original.hsvHueF(), abs=0.005)


@pytest.mark.parametrize("extension", ["png", "svg"])
@pytest.mark.parametrize("symbol", ["o", "s", "t"])
def test_scientific_scatter_points_are_colored_vectors(renderer, tmp_path, extension, symbol):
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtSvg import QSvgRenderer

    arguments = spectrum_arguments()
    points = [
        {
            "x": np.array([2.6e6]),
            "y": np.array([-1e-12]),
            "color": "#ffcccc",
            "symbol": symbol,
            "size": 9,
        }
    ]
    original = copy.deepcopy(points)
    path = tmp_path / f"points.{extension}"
    info = renderer.export_spectrum_figure(path, points=points, **arguments)
    if extension == "png":
        image = QImage(str(path))
    else:
        root = ET.parse(path).getroot()
        assert not root.findall(".//{http://www.w3.org/2000/svg}image")
        image = QImage(1500, 1500, QImage.Format.Format_ARGB32)
        image.fill("white")
        painter = QPainter(image)
        QSvgRenderer(str(path)).render(painter)
        painter.end()
    assert_rgb(
        pixel_at(image, info, 2.6e6, -1e-12), QColor(info["points"][0]["color"]).getRgb()[:3]
    )
    for key in ("x", "y"):
        np.testing.assert_array_equal(points[0][key], original[0][key])


@pytest.mark.parametrize("change", [{"x": [1, 2]}, {"size": 0}, {"symbol": "invalid"}])
def test_bad_scatter_groups_fail_before_creation(renderer, tmp_path, change):
    points = [{"x": [2.6e6], "y": [-1e-12], "color": "r", "symbol": "o", "size": 8} | change]
    path = tmp_path / "points.png"
    with pytest.raises(ValueError):
        renderer.export_spectrum_figure(path, points=points, **spectrum_arguments())
    assert not path.exists()


@pytest.mark.parametrize("extension", ["png", "svg"])
@pytest.mark.parametrize("inverted", [(False, False), (True, False), (False, True), (True, True)])
def test_logarithmic_raw_trace_data_and_view_inversion(renderer, tmp_path, inverted, extension):
    path = tmp_path / f"logarithmic.{extension}"
    x = np.array([1e6, 1e7, 1e8, 1e9])
    y = np.array([1e-6, 1e-6, 1e-6, 1e-6])
    info = renderer.export_spectrum_figure(
        path,
        traces=[{"name": "Raw", "x": x, "y": y, "color": "b", "width": 2}],
        view_ranges=[[6, 9], [-9, -3]],
        log_mode=(True, True),
        inverted_axes=inverted,
        x_label="Frequency",
        x_unit="Hz",
        y_label="Power",
        y_unit="W",
    )
    assert info["log_mode"] == [True, True]
    assert info["inverted_axes"] == list(inverted)
    np.testing.assert_array_equal(info["view_ranges"], [[6, 9], [-9, -3]])
    assert_rgb(pixel_at(figure_image(path), info, 7.2, -6), (0, 0, 255), tolerance=20)
    np.testing.assert_array_equal(x, [1e6, 1e7, 1e8, 1e9])
    np.testing.assert_array_equal(y, [1e-6] * 4)


def test_fractional_nice_tick_labels_do_not_round_signed_picowatts(renderer, tmp_path):
    path = tmp_path / "fractional.svg"
    renderer.export_spectrum_figure(path, **spectrum_arguments())
    texts = [
        "".join(node.itertext()).strip()
        for node in ET.parse(path).getroot().iter()
        if node.tag.endswith("}text")
    ]
    for label in ("-7.5", "-2.5", "2.5", "7.5"):
        assert label in texts
    assert "1" in texts and "5" in texts  # Keep the major X ticks at the view edges.
