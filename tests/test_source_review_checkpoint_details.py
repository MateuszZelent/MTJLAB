"""Catalogue records omit bulky details; explicit selections retain provenance."""
import json
import threading
from datetime import datetime, timezone

import h5py
import pytest
from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QTreeWidgetItem

from app.devices.anritsu_ms2830a import SpectrumTrace
from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.storage import Hdf5RunReader, Hdf5RunWriter, StoredPoint
from app.ui.results.page import ResultsPage
from app.ui.results.sweep_tree_panel import SweepTreePanel
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "details.h5"
    writer = Hdf5RunWriter(path, recipe_source="name: details\n", settings_source="schema_version: 1\n",
                           plan_hash="details", device_idn={})
    for index in range(3):
        writer.append(MeasurementPoint(index=index, setpoints={"current_a": index * .001}, measurements={},
                                      metadata={"large_provenance": "x" * 100000,
                                                "raw_recipe_sweep_indices": [index],
                                                "spectrum_processing_v1": {"configuration_fingerprint": "fingerprint"}}),
                      SpectrumTrace((1., 2., 3.), (-80., -40., -80.), datetime.now(timezone.utc), "TRAC1"),
                      device_states={"anritsu": {"configuration": "snapshot " + str(index)}})
    writer.close("completed")
    return path


def test_catalogue_never_reads_device_states_and_retains_processing_evidence(archive, monkeypatch):
    original = Hdf5RunReader._dataset_json
    def read(group, name):
        assert name != "device_states_json"
        return original(group, name)
    with monkeypatch.context() as patch:
        patch.setattr(Hdf5RunReader, "_dataset_json", read)
        points = Hdf5RunReader.points(archive, include_details=False)
    assert len(points) == 3
    for index, point in enumerate(points):
        assert not point.details_loaded and point.device_states == {}
        assert point.metadata == {"raw_recipe_sweep_indices": [index],
                                  "spectrum_processing_v1": {"configuration_fingerprint": "fingerprint"}}
    full = Hdf5RunReader.point(archive, 1)
    assert full.details_loaded
    assert len(full.metadata["large_provenance"]) == 100000
    assert full.device_states == {"anritsu": {"configuration": "snapshot 1"}}
    assert full == Hdf5RunReader.points(archive)[1]


def test_results_reads_full_selected_details_off_gui(archive, shell_qt_application, monkeypatch):
    app = shell_qt_application
    original = Hdf5RunReader.point
    indices = []
    def read(path, index):
        assert QThread.currentThread() != app.thread()
        indices.append(index)
        return original(path, index)
    monkeypatch.setattr(Hdf5RunReader, "point", read)
    page = ResultsPage(str(archive.parent))
    try:
        page.runs.setCurrentItem(page.runs.topLevelItem(0))
        wait_until(app, lambda: page._result_task is None)
        summaries = page.spectrum_tab._stored_points
        assert all(not point.details_loaded for point in summaries)
        page.spectrum_tab.show_stored_spectrum(1)
        wait_until(app, lambda: not page.spectrum_tab._read_tasks)
        assert page.spectrum_tab._selected_private_point.details_loaded
        assert "snapshot 1" in page.device_state.toPlainText()
        def find(parent):
            for ordinal in range(parent.childCount()):
                item = parent.child(ordinal)
                record = item.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(record, StoredPoint) and record.index == 2:
                    return item
                nested = find(item)
                if nested is not None:
                    return nested
        item = find(page.sweep_tree.tree.invisibleRootItem())
        assert item is not None
        page.sweep_tree.tree.setCurrentItem(item)
        wait_until(app, lambda: page.sweep_tree._detail_task is None)
        detail = json.loads(page.sweep_tree.inspector.toPlainText())
        assert detail["device_states"] == {"anritsu": {"configuration": "snapshot 2"}}
        assert len(detail["metadata"]["large_provenance"]) == 100000
        assert indices == [1, 2]
        assert all(not point.details_loaded and not point.device_states for point in summaries)
    finally:
        assert page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()


def test_point_details_cannot_cross_commit_gap(archive):
    with h5py.File(archive, "r+") as file:
        file["points/1"].attrs["complete"] = False
    with pytest.raises(ExecutionError, match="not committed"):
        Hdf5RunReader.point(archive, 2)


def test_late_inspector_details_cannot_replace_new_checkpoint(archive, shell_qt_application, monkeypatch):
    app = shell_qt_application
    entered, release = threading.Event(), threading.Event()
    original = Hdf5RunReader.point
    def read(path, index):
        if index == 0:
            entered.set()
            assert release.wait(5)
        return original(path, index)
    monkeypatch.setattr(Hdf5RunReader, "point", read)
    points = Hdf5RunReader.points(archive, include_details=False)
    panel = SweepTreePanel()
    panel._selected_path = archive
    items = []
    for point in points[:2]:
        item = QTreeWidgetItem([str(point.index)])
        item.setData(0, Qt.ItemDataRole.UserRole, point)
        items.append(item)
    try:
        panel._on_tree_selected(items[0], None)
        wait_until(app, entered.is_set)
        panel._on_tree_selected(items[1], items[0])
        release.set()
        wait_until(app, lambda: panel._detail_task is None)
        assert json.loads(panel.inspector.toPlainText())["checkpoint"] == 1
        assert panel._selected_stored_point.index == 1
    finally:
        release.set()
        panel.cancel_detail_read()
        assert panel._detail_pool.waitForDone(5000)
        panel.close()
        panel.deleteLater()
        app.processEvents()
