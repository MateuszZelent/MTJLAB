"""Reproducible scientific demonstration and CPU-only map benchmark.

Run from the repository root: python docs/audits/2026-10-08-results-analysis-artifacts/generate_map_example.py
"""

import json
from pathlib import Path
import sys
from time import perf_counter
import tracemalloc

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from app.ui.results.map_processing import MapProcessing, analyse_map


def main():
    destination = Path(__file__).parent
    f = np.linspace(1e9, 3e9, 1001)
    coordinate = np.linspace(.002, .004, 41)
    power = (1e-11 + 5e-10 * np.exp(-((f - 1.08e9) / 1.5e6) ** 2)
             + 2e-10 * np.exp(-((f - 2e9) / 100e6) ** 2)
             + 2e-10 * np.exp(-((f[None, :] - np.linspace(1.2e9, 2.8e9, 41)[:, None]) / 30e6) ** 2)
             + 5e-13 * np.sin(np.arange(41))[:, None])
    dbm = 10 * np.log10(power / 1e-3)
    def derive(state):
        return analyse_map(dbm, unit="dBm", frequencies_hz=f,
                           coordinate_values=coordinate, frequency_axis=1, state=state)
    common = derive(MapProcessing(component="median_power", view="component"))
    difference = derive(MapProcessing(component="median_power", colour_range="symmetric"))
    masked = derive(MapProcessing(mask_lines=True))
    figure, axes = plt.subplots(2, 2, figsize=(13, 8), constrained_layout=True)
    plots = (("Recorded input", dbm, "dBm", "viridis", None),
             ("Common median component (also contains stationary physical signal)", common.values * 1e12, "pW", "viridis", None),
             ("Differential power: input minus common median", difference.values * 1e12, "pW", "coolwarm", tuple(value * 1e12 for value in difference.levels)),
             ("Stationary narrow lines masked as gaps", masked.values, "dBm", "viridis", None))
    for axis, (title, values, unit, cmap, limits) in zip(axes.flat, plots, strict=True):
        options = {"vmin": limits[0], "vmax": limits[1]} if limits else {}
        artist = axis.pcolormesh(f / 1e9, coordinate * 1e3, values, shading="nearest", cmap=cmap, **options)
        axis.set(title=title, xlabel="Frequency (GHz)", ylabel="Keithley A current (mA)")
        figure.colorbar(artist, ax=axis, label=unit)
    figure.suptitle("Synthetic data: a fixed narrow line, a stationary broad resonance, and a moving resonance", fontsize=13)
    figure.savefig(destination / "scientific-map-comparison.png", dpi=160)
    plt.close(figure)
    f = np.linspace(0, 6e9, 10001)
    coordinate = np.linspace(.002, .004, 405)
    dbm = (-82 + 10 * np.exp(-((f[None, :] - np.linspace(1e9, 3e9, 405)[:, None]) / 60e6) ** 2)
           + 20 * np.exp(-((f[None, :] - 600e6) / 1e6) ** 2))
    results = []
    for state in (MapProcessing(component="median_power"),
                  MapProcessing(component="median_power", mask_lines=True),
                  MapProcessing(component="median_db", colour_range="robust")):
        tracemalloc.start()
        started = perf_counter()
        result = analyse_map(dbm, unit="dBm", frequencies_hz=f, coordinate_values=coordinate,
                             frequency_axis=1, state=state)
        elapsed = perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        results.append({"component": state.component, "line_mask": state.mask_lines,
            "shape": list(dbm.shape), "elapsed_s": round(elapsed, 4),
            "additional_peak_mib": round(peak / 1024 ** 2, 2), "output_unit": result.unit,
            "masked_frequency_bins": len(result.masked_frequencies_hz)})
    (destination / "benchmark.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
