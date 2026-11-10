"""Read MMPP's licensed paper style as data for the Qt plotting backend."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType


def _style_value(line: str) -> str:
    quote = None
    for index, char in enumerate(line):
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif char == quote:
                quote = None
        elif char == "#" and quote is None:
            return line[:index].strip()
    return line.strip()


@dataclass(frozen=True, slots=True)
class PaperPlotStyle:
    values: Mapping[str, str]
    provenance: Mapping[str, object]

    @classmethod
    def from_text(cls, text: str, provenance=None):
        values = {}
        for line in text.splitlines():
            cleaned = _style_value(line)
            if cleaned and ":" in cleaned:
                key, value = cleaned.split(":", 1)
                values[key.strip()] = value.strip()
        return cls(MappingProxyType(values), MappingProxyType(dict(provenance or {})))

    def number(self, key: str) -> float:
        value = float(self.values[key])
        if not math.isfinite(value):
            raise ValueError(f"Non-finite paper style value: {key}")
        return value

    def boolean(self, key: str) -> bool:
        value = self.values[key].lower()
        if value not in {"true", "false"}:
            raise ValueError(f"Expected True/False for paper style {key}")
        return value == "true"

    def font_size(self, key: str) -> float:
        raw = self.values[key]
        factors = {"medium": 1., "small": .833, "large": 1.2}
        size = self.number("font.size") * factors[raw] if raw in factors else self.number(key)
        if size <= 0:
            raise ValueError(f"Font size must be positive: {key}")
        return int(size) if size.is_integer() else size

    @staticmethod
    def _colour(value: str) -> str:
        raw = value.strip().strip("'\"")
        if re.fullmatch(r"#?[0-9a-fA-F]{6}", raw):
            return "#" + raw.removeprefix("#").lower()
        named = {"w": "#ffffff", "white": "#ffffff", "k": "#000000", "black": "#000000"}
        if raw in named:
            return named[raw]
        try:
            gray = float(raw)
        except ValueError as exc:
            raise ValueError(f"Unsupported paper style colour: {value!r}") from exc
        if not math.isfinite(gray) or not 0 <= gray <= 1:
            raise ValueError(f"Invalid paper style gray: {value!r}")
        component = round(255 * gray)
        return f"#{component:02x}{component:02x}{component:02x}"

    def colour(self, key: str) -> str:
        value = self.values[key]
        if re.fullmatch(r"C[0-9]", value):
            return self.colour_cycle[int(value[1])]
        if value == "inherit":
            value = self.values["axes.facecolor"]
        if value == "auto":
            value = self.values["figure.facecolor"]
        return self._colour(value)

    @property
    def font_families(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.values["font.sans-serif"].split(",") if part.strip())

    @property
    def colour_cycle(self) -> tuple[str, ...]:
        expression = ast.parse(self.values["axes.prop_cycle"], mode="eval").body
        if not (isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name)
                and expression.func.id == "cycler" and len(expression.args) == 2
                and not expression.keywords and ast.literal_eval(expression.args[0]) == "color"):
            raise ValueError("Paper colour cycle must contain a literal colour list")
        colours = ast.literal_eval(expression.args[1])
        if not isinstance(colours, (list, tuple)) or not colours:
            raise ValueError("Paper colour cycle must not be empty")
        return tuple(self._colour(colour) for colour in colours)

    @property
    def figure_inches(self) -> tuple[float, float]:
        values = tuple(float(value.strip()) for value in self.values["figure.figsize"].split(","))
        if len(values) != 2 or not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("Paper figure size must contain two positive inch dimensions")
        return values

    @property
    def description(self) -> str:
        width, height = self.figure_inches
        return (f"MMPP paper: {self.font_families[0]} {self.font_size('font.size'):g} pt, "
                f"{width:g} × {height:g} in, {self.provenance['export_dpi']} dpi")


def _load_style() -> PaperPlotStyle:
    root = files("app").joinpath("resources", "plot_styles")
    source = root.joinpath("mmpp_paper.mplstyle").read_bytes().replace(b"\r\n", b"\n")
    provenance = json.loads(root.joinpath("mmpp_paper_source.json").read_text(encoding="utf-8"))
    provenance.update(name="mmpp-paper", sha256=hashlib.sha256(source).hexdigest())
    return PaperPlotStyle.from_text(source.decode("utf-8"), provenance)


PAPER_STYLE = _load_style()
