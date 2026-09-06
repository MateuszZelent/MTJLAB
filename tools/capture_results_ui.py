"""Capture screenshots of the modernized Results page."""

from __future__ import annotations

import os
from pathlib import Path
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication

from app.ui.shell import MainWindow

ARTIFACTS_DIR = Path(r"C:\Users\Shark\.gemini\antigravity\brain\159ab9a9-686f-4a9e-a10a-f0b433e21f5d")


def _settle(app: QApplication, count: int = 15) -> None:
    for _ in range(count):
        app.processEvents()
        time.sleep(0.02)


def main() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(".config/settings.yml", simulation=True)
    try:
        window.resize(1500, 900)
        window.show()
        window._navigate_to("results")
        _settle(app)

        page = window.results_page

        # Select a file from measurements/
        target_file = Path("measurements/20260903T123903.983672Z_Untitled_sweep.h5")
        if not target_file.exists():
            h5_files = sorted(list(Path("measurements").glob("*.h5")))
            if h5_files:
                target_file = h5_files[-1]
        if target_file.exists():
            from PySide6.QtCore import QThreadPool
            page.open_result_file(target_file)
            QThreadPool.globalInstance().waitForDone(5000)
            _settle(app, 30)

        # 1. Overview tab - Metadata
        _settle(app, 10)
        window.grab().save(str(ARTIFACTS_DIR / "results_overview_metadata.png"))

        # 2. Overview tab - Switch to Recipe subtab
        page.metadata_panel.tabs.setCurrentIndex(1)
        _settle(app, 10)
        window.grab().save(str(ARTIFACTS_DIR / "results_overview_recipe.png"))

        # 3. Switch to Sweep Tree tab
        page.result_tabs.setCurrentIndex(1)
        _settle(app, 10)
        window.grab().save(str(ARTIFACTS_DIR / "results_sweep_tree.png"))

        # 4. Switch to Spectrum tab
        page.result_tabs.setCurrentIndex(2)
        if page.points.topLevelItemCount() > 0:
            page.points.setCurrentItem(page.points.topLevelItem(0))
        _settle(app, 15)
        window.grab().save(str(ARTIFACTS_DIR / "results_spectrum.png"))

        # 5. Switch to Heatmaps tab and render data
        page.result_tabs.setCurrentIndex(3)
        _settle(app, 15)
        if page.heatmap_tab.load_button.isEnabled():
            page.heatmap_tab.load_button.click()
            from PySide6.QtCore import QThreadPool
            QThreadPool.globalInstance().waitForDone(8000)
            _settle(app, 30)
        window.grab().save(str(ARTIFACTS_DIR / "results_heatmap.png"))

        print("Captured all screenshots successfully!")
    finally:
        window.close()
        _settle(app, 5)


if __name__ == "__main__":
    main()
