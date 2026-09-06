"""Physical parameter extraction and analysis algorithms for MTJ and spintronic devices."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Sequence


@dataclass(frozen=True, slots=True)
class MtjFiguresOfMerit:
    """Standard spintronic and MTJ figures of merit computed from measurement traces."""

    curve_type: str  # "mr_loop", "iv_curve", "spectrum", "scalar_series", "unknown"
    r_min: float | None = None
    r_max: float | None = None
    r_p: float | None = None
    r_ap: float | None = None
    tmr_percent: float | None = None
    area_um2: float | None = None
    ra_product: float | None = None  # in Ohm * um^2
    h_coercive: float | None = None
    h_offset: float | None = None
    v_switching_pos: float | None = None
    v_switching_neg: float | None = None
    details: str = ""

    @property
    def rp(self) -> float | None:
        return self.r_p

    @property
    def rap(self) -> float | None:
        return self.r_ap

    @property
    def hc(self) -> float | None:
        return self.h_coercive

    @property
    def h_dipolar(self) -> float | None:
        return self.h_offset

    def summary_items(self) -> list[tuple[str, str]]:
        """Return human-readable label and formatted value pairs for UI presentation."""
        items: list[tuple[str, str]] = []
        if self.r_p is not None:
            items.append(("Rp (Parallel)", _format_resistance(self.r_p)))
        if self.r_ap is not None:
            items.append(("Rap (Antiparallel)", _format_resistance(self.r_ap)))
        if self.tmr_percent is not None:
            items.append(("TMR Ratio", f"{self.tmr_percent:.1f} %"))
        if self.area_um2 is not None:
            items.append(("Pillar Area", f"{self.area_um2:.4f} µm²"))
        if self.ra_product is not None:
            items.append(("RA Product", f"{self.ra_product:.2f} Ω·µm²"))
        if self.h_coercive is not None:
            items.append(("Coercivity (Hc)", f"{self.h_coercive:.2f} Oe"))
        if self.h_offset is not None:
            items.append(("Offset (H_dipolar)", f"{self.h_offset:.2f} Oe"))
        if self.r_min is not None and self.r_p is None:
            items.append(("R_min", _format_resistance(self.r_min)))
        if self.r_max is not None and self.r_ap is None:
            items.append(("R_max", _format_resistance(self.r_max)))
        return items


def _format_resistance(val: float) -> str:
    if math.isnan(val) or math.isinf(val):
        return "—"
    if abs(val) >= 1e6:
        return f"{val / 1e6:.3f} MΩ"
    if abs(val) >= 1e3:
        return f"{val / 1e3:.3f} kΩ"
    if abs(val) < 1:
        return f"{val * 1e3:.2f} mΩ"
    return f"{val:.2f} Ω"


def parse_dimension_area(label: str | None) -> float | None:
    """Parse a lithographic dimension string into area in square micrometers (µm²).

    Supported formats:
    - Circular pillars: "200 nm", "50nm", "1 µm", "1 um", "500 nm pillar"
    - Elliptical / Rectangular: "100x200 nm", "100 x 200 nm", "100nm x 300nm", "1x2 um"
    """
    if not label:
        return None
    text = label.strip().lower()

    # Match elliptical / rectangular: e.g. "100x200 nm", "100 x 200 nm"
    match_2d = re.search(
        r"([\d\.]+)\s*(nm|µm|um|mm)?\s*[x×*]\s*([\d\.]+)\s*(nm|µm|um|mm)?", text
    )
    if match_2d:
        w_val = float(match_2d.group(1))
        w_unit = match_2d.group(2) or match_2d.group(4) or "nm"
        h_val = float(match_2d.group(3))
        h_unit = match_2d.group(4) or w_unit or "nm"

        w_um = _to_micrometers(w_val, w_unit)
        h_um = _to_micrometers(h_val, h_unit)
        # Default to elliptical pillar area: pi * (w/2) * (h/2)
        return math.pi * (w_um / 2.0) * (h_um / 2.0)

    # Match circular pillar: e.g. "200 nm", "50 nm", "1.2 µm"
    match_1d = re.search(r"([\d\.]+)\s*(nm|µm|um|mm)", text)
    if match_1d:
        d_val = float(match_1d.group(1))
        unit = match_1d.group(2)
        d_um = _to_micrometers(d_val, unit)
        r_um = d_um / 2.0
        return math.pi * (r_um**2)

    return None


def _to_micrometers(val: float, unit: str) -> float:
    unit = unit.lower()
    if unit == "nm":
        return val / 1000.0
    if unit in ("µm", "um"):
        return val
    if unit == "mm":
        return val * 1000.0
    return val / 1000.0  # Default to nm if unspecified in nano regime


def calculate_mtj_metrics(
    x_values: Sequence[float],
    y_values: Sequence[float],
    x_name: str = "",
    y_name: str = "",
    *,
    dimension_label: str = "",
) -> MtjFiguresOfMerit:
    """Calculate spintronic MTJ parameters from measured X-Y points."""
    if len(x_values) == 0 or len(y_values) == 0 or len(x_values) != len(y_values):
        return MtjFiguresOfMerit(curve_type="unknown")

    # Filter out NaNs and Infs
    clean_pairs = [
        (float(x), float(y))
        for x, y in zip(x_values, y_values, strict=False)
        if not (math.isnan(x) or math.isnan(y) or math.isinf(x) or math.isinf(y))
    ]
    if len(clean_pairs) < 3:
        return MtjFiguresOfMerit(curve_type="unknown")

    xs = [p[0] for p in clean_pairs]
    ys = [p[1] for p in clean_pairs]

    x_lower = x_name.lower()
    y_lower = y_name.lower()

    area_um2 = parse_dimension_area(dimension_label)

    # Detect Curve Type
    is_mr = any(k in x_lower for k in ("field", "b_field", "h_field", "magnet", "oe", "tesla", "flux")) or any(
        k in y_lower for k in ("resistance", "r_dut", "r_mtj", "ohm", "tmr", "mr")
    )
    is_iv = any(k in x_lower for k in ("voltage", "v_dut", "v_source", "bias")) and any(
        k in y_lower for k in ("current", "i_dut", "i_meas", "amperes", "amp")
    )

    r_min = min(ys)
    r_max = max(ys)

    if is_mr or (len(ys) >= 10 and not is_iv):
        # Interpret as Magnetoresistance loop (R vs H)
        r_p = r_min
        r_ap = r_max

        tmr_pct = None
        if r_p > 0 and r_ap > r_p:
            tmr_pct = ((r_ap - r_p) / r_p) * 100.0

        ra_prod = None
        if area_um2 is not None and r_p > 0:
            ra_prod = r_p * area_um2

        h_c, h_off = _estimate_switching_fields(xs, ys)

        return MtjFiguresOfMerit(
            curve_type="mr_loop",
            r_min=r_min,
            r_max=r_max,
            r_p=r_p,
            r_ap=r_ap,
            tmr_percent=tmr_pct,
            area_um2=area_um2,
            ra_product=ra_prod,
            h_coercive=h_c,
            h_offset=h_off,
            details=f"MR loop with {len(clean_pairs)} points",
        )

    if is_iv:
        # IV Curve: estimate zero-bias resistance
        zero_pairs = sorted(clean_pairs, key=lambda p: abs(p[0]))
        r_zero = None
        if len(zero_pairs) >= 2 and abs(zero_pairs[1][0] - zero_pairs[0][0]) > 1e-9:
            dv = zero_pairs[1][0] - zero_pairs[0][0]
            di = zero_pairs[1][1] - zero_pairs[0][1]
            if abs(di) > 1e-12:
                r_zero = abs(dv / di)

        ra_prod = (r_zero * area_um2) if (r_zero is not None and area_um2 is not None) else None

        return MtjFiguresOfMerit(
            curve_type="iv_curve",
            r_min=r_min,
            r_max=r_max,
            r_p=r_zero,
            area_um2=area_um2,
            ra_product=ra_prod,
            details=f"I-V characteristic with {len(clean_pairs)} points",
        )

    return MtjFiguresOfMerit(
        curve_type="scalar_series",
        r_min=r_min,
        r_max=r_max,
        area_um2=area_um2,
        details=f"Series with {len(clean_pairs)} points",
    )


def _estimate_switching_fields(
    xs: Sequence[float], ys: Sequence[float]
) -> tuple[float | None, float | None]:
    """Estimate coercivity Hc and offset field H_offset from a hysteresis loop."""
    if len(xs) < 10:
        return None, None

    y_min, y_max = min(ys), max(ys)
    span = y_max - y_min
    if span <= 0:
        return None, None

    # Mid-height threshold
    y_mid = y_min + 0.5 * span

    # Find points crossing y_mid
    crossings: list[float] = []
    for i in range(len(ys) - 1):
        y1, y2 = ys[i], ys[i + 1]
        x1, x2 = xs[i], xs[i + 1]
        if (y1 <= y_mid <= y2) or (y2 <= y_mid <= y1):
            denom = y2 - y1
            if abs(denom) > 1e-12:
                frac = (y_mid - y1) / denom
                cross_x = x1 + frac * (x2 - x1)
                crossings.append(cross_x)

    if len(crossings) >= 2:
        pos_crossings = [c for c in crossings if c > 0]
        neg_crossings = [c for c in crossings if c < 0]
        if pos_crossings and neg_crossings:
            h_pos = min(pos_crossings, key=abs)
            h_neg = min(neg_crossings, key=abs)
            h_c = abs(h_pos - h_neg) / 2.0
            h_off = (h_pos + h_neg) / 2.0
            return h_c, h_off

        # Fallback if unipolar or shifted
        h_c = abs(max(crossings) - min(crossings)) / 2.0
        h_off = (max(crossings) + min(crossings)) / 2.0
        return h_c, h_off

    return None, None
