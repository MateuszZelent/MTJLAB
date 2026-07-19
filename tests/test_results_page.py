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
from app.storage.thatec_reader import ThatecRunReader
from app.ui.main_window import ResultsPage
from app.ui.recipes.page import RecipePage
from tests.helpers import ROOT, simulation_settings


REFERENCE_FILE = ROOT / "Elec_Det_20260606_RPTU0741_32P5_MTJcurrentSweep.h5"


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
                device_states={"rigol": {"channel_1": {"requested": {"frequency_hz": 1_000.0}}}},
            )
            writer.close("completed")

            page = ResultsPage(str(output_dir))
            try:
                self.assertEqual(page.runs.topLevelItemCount(), 1)
                page.runs.setCurrentItem(page.runs.topLevelItem(0))
                self.application.processEvents()
                self.assertIn("State: completed", page.metadata.toPlainText())
                self.assertFalse(page.resume_button.isEnabled())
                self.assertIn("browser-test", page.recipe_snapshot.toPlainText())
                self.assertEqual(page.details_tabs.tabText(3), "PyThat data")
                self.assertEqual(page.details_tabs.tabText(4), "Device state")
                self.assertIn("Checkpoint", page.pythat_data.toPlainText())
                self.assertEqual(page.points.topLevelItemCount(), 1)
                page.points.setCurrentItem(page.points.topLevelItem(0))
                self.application.processEvents()
                self.assertIn("frequency_hz", page.device_state.toPlainText())
                self.assertEqual(page.spectrum_plot.trace_point_count("Stored spectrum"), 3)
                self.assertIn("3 points", page.spectrum_info.text())
            finally:
                page.close()

    def test_resume_action_is_exposed_only_for_interrupted_selected_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            path = output_dir / "faulted.h5"
            writer = Hdf5RunWriter(
                path,
                recipe_source="schema_version: 1\nname: interrupted\n",
                settings_source="profile:\n  state: approved\n",
                plan_hash="b" * 64,
                device_idn={},
            )
            writer.close("faulted")

            page = ResultsPage(str(output_dir))
            requested: list[Path] = []
            page.resume_requested.connect(requested.append)
            try:
                page.runs.setCurrentItem(page.runs.topLevelItem(0))
                self.application.processEvents()
                self.assertTrue(page.resume_button.isEnabled())
                page.resume_button.click()
                self.application.processEvents()
                self.assertEqual(requested, [path])
            finally:
                page.close()

    def test_browses_real_thatec_tree_without_private_run_groups(self) -> None:
        reference = next(ROOT.glob("*.h5"))
        page = ResultsPage(str(reference.parent))
        try:
            run_item = next(
                page.runs.topLevelItem(index)
                for index in range(page.runs.topLevelItemCount())
                if page.runs.topLevelItem(index).text(0) == reference.name
            )
            page.runs.setCurrentItem(run_item)
            self.application.processEvents()

            self.assertEqual(run_item.text(1), "THATEC")
            self.assertEqual(page.experiment_tree.topLevelItem(0).text(0), "Measurements")
            self.assertEqual(page.experiment_tree.topLevelItem(1).text(0), "Devices")
            run = ThatecRunReader.describe(reference)
            recorded_item = page._find_tree_item(next(iter(run.rows)))
            self.assertIsNotNone(recorded_item)
            page.experiment_tree.setCurrentItem(recorded_item)
            self.application.processEvents()
            self.assertIn("definition", page.inspector.toPlainText())
        finally:
            page.close()

    def test_reference_thatec_result_can_rebuild_the_sweep_tree(self) -> None:
        """The public THATEC tree becomes the same complete historical Sweep tree."""
        reference = next(ROOT.glob("*.h5"))
        results = ResultsPage(str(reference.parent))
        sweeps = RecipePage(simulation_settings())
        try:
            results.open_sweep_requested.connect(sweeps.load_historical_thatec_sweep)
            result_item = next(
                results.runs.topLevelItem(index)
                for index in range(results.runs.topLevelItemCount())
                if results.runs.topLevelItem(index).text(0) == reference.name
            )
            results.runs.setCurrentItem(result_item)
            self.application.processEvents()
            results.open_sweep_button.click()
            self.application.processEvents()

            run = ThatecRunReader.describe(reference)
            self.assertTrue(sweeps.historical_sweep_active)
            self.assertFalse(sweeps.run_button.isEnabled())
            self.assertIn("Historical THATEC", sweeps.summary.text())
            self.assertEqual(sweeps.tree.topLevelItemCount(), 1)

            def recorded_ids(item):
                result = [item.data(1, 256)]
                for index in range(item.childCount()):
                    result.extend(recorded_ids(item.child(index)))
                return result

            self.assertEqual(
                {row_id for row_id in recorded_ids(sweeps.tree.topLevelItem(0)) if row_id},
                set(run.rows),
            )
            def find_row(item, row_id):
                if item.data(1, 256) == row_id:
                    return item
                for index in range(item.childCount()):
                    found = find_row(item.child(index), row_id)
                    if found is not None:
                        return found
                return None

            recorded_item = find_row(sweeps.tree.topLevelItem(0), next(iter(run.rows)))
            self.assertIsNotNone(recorded_item)
            sweeps.tree.setCurrentItem(recorded_item)
            self.application.processEvents()
            self.assertIn("THATEC row", sweeps.inspector.toPlainText())
            devices_item = next(
                sweeps.tree.topLevelItem(0).child(index)
                for index in range(sweeps.tree.topLevelItem(0).childCount())
                if sweeps.tree.topLevelItem(0).child(index).text(0) == "Recorded device configuration"
            )
            self.assertEqual(devices_item.childCount(), len(run.devices))
            for index, device in enumerate(run.devices):
                device_item = devices_item.child(index)
                self.assertEqual(device_item.data(0, 256), device)
                sweeps.tree.setCurrentItem(device_item)
                self.application.processEvents()
                for key, value in device.values:
                    self.assertIn(f"{key}: {value}", sweeps.inspector.toPlainText())
        finally:
            results.close()
            sweeps.close()

    def test_our_public_thatec_file_restores_an_editable_sweep_source(self) -> None:
        source = (
            "schema_version: 1\n"
            "name: restored sweep\n"
            "root:\n"
            "  id: sequence-main\n"
            "  type: sequence\n"
            "  children: []\n"
            "finally: []\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "restorable.h5"
            writer = Hdf5RunWriter(
                path, recipe_source=source, settings_source="schema_version: 1\n",
                plan_hash="restorable", device_idn={},
            )
            writer.close("completed")
            run = ThatecRunReader.describe(path)
            page = RecipePage(simulation_settings())
            try:
                page.load_reconstructed_thatec_sweep(run, ThatecRunReader.tree(path))
                self.assertFalse(page.historical_sweep_active)
                self.assertFalse(page.editor.isReadOnly())
                self.assertEqual(page.tree.topLevelItem(0).text(0), "Measurement sequence")
                self.assertIn("restored from public THATEC", page.summary.text())
            finally:
                page.close()

    def test_can_open_a_thatec_file_outside_the_results_directory(self) -> None:
        source = (
            "schema_version: 1\nname: imported\nroot:\n"
            "  id: sequence-main\n  type: sequence\n  children: []\nfinally: []\n"
        )
        with tempfile.TemporaryDirectory() as result_directory, tempfile.TemporaryDirectory() as external_directory:
            path = Path(external_directory) / "external.h5"
            writer = Hdf5RunWriter(
                path, recipe_source=source, settings_source="schema_version: 1\n",
                plan_hash="external", device_idn={},
            )
            writer.close("completed")
            page = ResultsPage(result_directory)
            try:
                self.assertEqual(page.runs.topLevelItemCount(), 0)
                page.open_result_file(path)
                self.application.processEvents()
                self.assertEqual(page.runs.topLevelItemCount(), 1)
                self.assertEqual(page.runs.currentItem().text(0), "external.h5")
                self.assertTrue(page.open_sweep_button.isEnabled())
            finally:
                page.close()


if __name__ == "__main__":
    unittest.main()
