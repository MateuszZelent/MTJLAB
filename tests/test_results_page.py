from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.devices.anritsu import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.storage import Hdf5RunWriter
from app.ui.main_window import ResultsPage


class ResultsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_browses_hdf5_metadata_checkpoint_and_spectrum_without_devices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            writer = Hdf5RunWriter(
                output_dir / "run.h5",
                recipe_source="name: browser-test\n",
                settings_source="profile:\n  state: approved\n",
                plan_hash="a" * 64,
                device_idn={"rigol": "RIGOL,DG1032Z"},
            )
            writer.append_event("run_started", {"timestamp_utc": "2026-01-01T00:00:00+00:00"})
            writer.append(
                MeasurementPoint(index=0, setpoints={"rigol.1.high_level": 0.001}, measurements={"keithley.B.current_a": 0.001}),
                SpectrumTrace(
                    frequencies_hz=(1e6, 1.5e6, 2e6),
                    powers_dbm=(-60.0, -40.0, -50.0),
                    acquired_at_utc=datetime.now(timezone.utc),
                    trace_name="TRAC1",
                ),
            )
            writer.close("completed")

            page = ResultsPage(str(output_dir))
            try:
                self.assertEqual(page.runs.topLevelItemCount(), 1)
                page.runs.setCurrentItem(page.runs.topLevelItem(0))
                self.application.processEvents()
                self.assertIn("State: completed", page.metadata.toPlainText())
                self.assertIn("browser-test", page.recipe_snapshot.toPlainText())
                self.assertEqual(page.points.topLevelItemCount(), 1)
                page.points.setCurrentItem(page.points.topLevelItem(0))
                self.application.processEvents()
                self.assertEqual(page.spectrum_plot.trace_point_count("Stored spectrum"), 3)
                self.assertIn("3 points", page.spectrum_info.text())
            finally:
                page.close()


if __name__ == "__main__":
    unittest.main()
