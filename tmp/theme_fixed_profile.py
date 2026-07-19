from __future__ import annotations

import os
import statistics
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.ui.design_system import fluent_theme
from app.ui.shell import MainWindow


app = QApplication.instance() or QApplication([])
window = MainWindow(".config/settings.yml", simulation=True)
window.resize(1360, 880)
window.show()
app.processEvents()
all_widgets = app.allWidgets()
print(
    "visibility",
    len(all_widgets),
    sum(widget.isVisible() for widget in all_widgets),
    sum(widget.window().isVisible() and widget.isVisibleTo(widget.window()) for widget in all_widgets),
    sum(not widget.visibleRegion().isNull() for widget in all_widgets),
)
if os.environ.get("THEME_PROFILE_VISIBILITY_ONLY") == "1":
    window.close()
    app.processEvents()
    raise SystemExit(0)

samples: dict[str, list[float]] = {}


def timed(name, function):
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            samples.setdefault(name, []).append((time.perf_counter() - started) * 1000)

    return wrapper


fluent_theme._supports_lazy_theme_update = lambda _application: True
fluent_theme.setTheme = timed("setTheme", fluent_theme.setTheme)
fluent_theme.setThemeColor = timed("setThemeColor", fluent_theme.setThemeColor)
fluent_theme._apply_station_control_styles = timed(
    "station control styles", fluent_theme._apply_station_control_styles
)
fluent_theme._settle_fluent_background_animations = timed(
    "settle animations", fluent_theme._settle_fluent_background_animations
)
fluent_theme._apply_application_palette = timed(
    "application palette", fluent_theme._apply_application_palette
)
app.setStyleSheet = timed("QApplication.setStyleSheet", app.setStyleSheet)

totals = []
for theme in ("light", "dark", "light", "dark"):
    started = time.perf_counter()
    window._set_theme_mode(theme, persist=False)
    totals.append((time.perf_counter() - started) * 1000)
    app.processEvents()

started = time.perf_counter()
window._set_theme_mode("dark", persist=False)
noop = (time.perf_counter() - started) * 1000

print(f"widgets={len(app.allWidgets())}")
print(f"switch_ms={totals} median={statistics.median(totals):.1f}")
print(f"same_theme_noop_ms={noop:.1f}")
for name, values in samples.items():
    print(f"{name}: calls={len(values)} median={statistics.median(values):.1f}")

window.close()
app.processEvents()
