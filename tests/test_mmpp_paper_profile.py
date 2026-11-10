"""MMPP's upstream style controls the physical publication figure."""

import hashlib
from importlib.resources import files

import numpy as np
import pytest
from PySide6.QtGui import QImage

from app.ui.widgets.figure_export import export_heatmap_figure
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414


def test_mmpp_style_is_the_original_licensed_resource():
    root = files("app").joinpath("resources", "plot_styles")
    source = root.joinpath("mmpp_paper.mplstyle").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha1(b"blob " + str(len(source)).encode() + b"\0" + source).hexdigest() == "0ae94010f0851237a030ab56a3f3ef5032437002"
    assert "Copyright (c) 2024 Mateusz Zelent" in root.joinpath("LICENSE.mmpp").read_text(encoding="utf-8")


def test_publication_uses_mmpp_five_inch_page_and_eleven_point_text(shell_qt_application, tmp_path):
    path = tmp_path / "mmpp.png"
    values = np.array([[-2e-12, 0], [1e-12, 3e-12]])
    info = export_heatmap_figure(path, values=values, x_edges=[1e9, 2e9, 3e9],
        y_edges=[.001, .002, .003], view_ranges=((1e9, 3e9), (.001, .003)),
        x_label="Frequency", x_unit="Hz", y_label="Current", y_unit="A",
        z_label="Power difference", z_unit="W", levels=(-2e-12, 3e-12), colormap="viridis")
    image = QImage(str(path))
    assert (image.width(), image.height()) == (1500, 1500)
    assert image.dotsPerMeterX() * .0254 == pytest.approx(300, abs=.1)
    assert info["size_inches"] == [5., 5.]
    assert set(info["font_sizes_pt"].values()) == {11}
    assert info["foreground"] == "#636363"
    assert info["axes"]["z"]["unit"] == "W"
    np.testing.assert_array_equal(values, [[-2e-12, 0], [1e-12, 3e-12]])


def test_style_values_are_typed_and_colour_cycle_is_not_executed():
    from app.ui.design_system.paper_plot_style import PaperPlotStyle

    style = PaperPlotStyle.from_text("font.size: 11 # points\nxtick.labelsize: medium\naxes.grid: True\ntext.color: '#636363'\n")
    assert style.font_size("xtick.labelsize") == 11
    assert style.boolean("axes.grid")
    assert style.colour("text.color") == "#636363"
    malformed = PaperPlotStyle.from_text("axes.grid: sometimes\nfont.size: nan\naxes.prop_cycle: cycler('color', [dangerous()])\n")
    with pytest.raises(ValueError):
        malformed.boolean("axes.grid")
    with pytest.raises(ValueError):
        malformed.number("font.size")
    with pytest.raises(ValueError):
        _ = malformed.colour_cycle
