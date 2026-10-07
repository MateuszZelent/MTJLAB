"""Scalar least-squares oracle for numerical equivalence of batched fitting."""
import math
import numpy as np

def reference_fit(
    frequencies: np.ndarray,
    values_dbm: np.ndarray,
    index: int,
    measured_fwhm_hz: float | None,
) -> tuple[str, float | None, float | None, float | None]:
    spacing = float(np.median(np.diff(frequencies)))
    half_points = min(50, max(5, int(round((measured_fwhm_hz or spacing * 4) / spacing * 2))))
    start, stop = max(0, index - half_points), min(values_dbm.size, index + half_points + 1)
    x = frequencies[start:stop]
    y_mw = 10.0 ** (values_dbm[start:stop] / 10.0)
    if x.size < 7:
        return "none", None, None, None
    initial_width = max(measured_fwhm_hz or spacing * 3.0, spacing * 1.25)
    minimum_width = max(spacing, initial_width / 4.0)
    maximum_width = max(
        minimum_width,
        min(max(float(x[-1] - x[0]), spacing), initial_width * 4.0),
    )
    widths = np.geomspace(minimum_width, maximum_width, 20)
    center_span = max(
        spacing,
        min(initial_width / 2.0, float(x[-1] - x[0]) / 4.0),
    )
    centers = frequencies[index] + np.linspace(-center_span, center_span, 21)
    best: tuple[float, str, float, float] | None = None
    for model in ("Gaussian", "Lorentzian"):
        for center in centers:
            for width in widths:
                normalized = (x - center) / width
                shape = (
                    np.exp(-4.0 * math.log(2.0) * normalized**2)
                    if model == "Gaussian"
                    else 1.0 / (1.0 + 4.0 * normalized**2)
                )
                design = np.column_stack((np.ones(shape.size), shape))
                baseline, amplitude = np.linalg.lstsq(design, y_mw, rcond=None)[0]
                if baseline < 0 or amplitude <= 0:
                    continue
                predicted_mw = np.maximum(baseline + amplitude * shape, 1e-300)
                predicted_dbm = 10.0 * np.log10(predicted_mw)
                measured_dbm = 10.0 * np.log10(np.maximum(y_mw, 1e-300))
                rmse = float(np.sqrt(np.mean((predicted_dbm - measured_dbm) ** 2)))
                if best is None or rmse < best[0]:
                    best = (rmse, model, float(center), float(width))
    if best is None:
        return "none", None, None, None
    return best[1], best[2], best[3], best[0]
