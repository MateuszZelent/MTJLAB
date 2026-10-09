"""Run-local storage isolation, committed baseline exports and Windows paths."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import h5py
import numpy as np
import pytest

from app.domain.errors import ExecutionError
from app.inventory.models import ActiveSampleTarget
from app.storage.hdf5_reader import Hdf5RunReader
from app.storage.run_bundle import RunBundle, bundle_directory
from app.storage.thatec_validator import ThatecCompatibilityValidator
from app.ui.run_worker import RunWorker, planned_run_paths
from tests.test_sweep_audit_contracts import audit_settings, compile_source


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
CHILDREN = """    - {id: analyzer, type: configure_anritsu, start_frequency: '1 MHz', stop_frequency: '2 MHz', reference_level: '0 dBm', points: 101}
    - {id: ref, type: acquire_reference, purpose: reference, average_count: 2}
    - {id: bg, type: acquire_reference, purpose: background, average_count: 3}
    - {id: signal, type: acquire_spectrum}
"""


@pytest.fixture(scope="module", autouse=True)
def qt_application():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont, QFontDatabase
    application = QApplication.instance() or QApplication([])
    font = Path("C:/Windows/Fonts/segoeui.ttf")
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
        application.setFont(QFont("Segoe UI", 10))
    yield application


def test_preview_creates_nothing_and_numbers_are_exclusive(tmp_path):
    root = tmp_path / "sample" / "measurements" / "sweeps"
    assert bundle_directory(root, "sweep", NOW).name == "1_20261007T120000Z_sweep"
    assert not root.exists()
    with ThreadPoolExecutor(max_workers=4) as executor:
        paths = list(executor.map(lambda _: bundle_directory(root, "sweep", NOW, reserve=True), range(8)))
    assert len(set(paths)) == 8
    assert {int(path.name.split("_")[0]) for path in paths} == set(range(1, 9))
    for path in paths:
        shutil.rmtree(path)
    assert bundle_directory(root, "sweep", NOW, reserve=True).name.startswith("9_")


def test_windows_long_name_is_shortened_and_explicit_template_resolved(tmp_path):
    settings = audit_settings(tmp_path)
    target = ActiveSampleTarget(sample_id="INL_MTJ_02.2026", row="20", col="4")
    data, _ = planned_run_paths(settings, "MOKE_VOUT2_" + "long_name_" * 40,
                               output_dir_override=tmp_path, timestamp=NOW, sample_target=target)
    assert data.name == "data.h5"
    assert len(str(data.parent / "references" / "ref0000_12345678.json")) <= 245
    assert not data.parent.exists()
    data, _ = planned_run_paths(settings, "test", output_dir_override=tmp_path, timestamp=NOW,
                               sample_target=target, file_stem_override="custom_{coord}")
    assert data.parent.name.endswith("custom_R20C4")


def test_initial_provenance_and_execution_attempts_are_durable(tmp_path):
    import hashlib
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, CHILDREN)
    directory = bundle_directory(tmp_path, plan.recipe_name, NOW, reserve=True)
    bundle = RunBundle(directory / "data.h5")
    source = settings.model_dump_json()
    bundle.initialize(plan=plan, settings_source=source, operator_context={}, sample_target=None,
                      simulation_metadata={"enabled": True}, display_name="Full display name")
    assert (directory / "settings.yml").read_bytes() == source.encode("utf-8")
    metadata = json.loads(bundle.manifest_path.read_text())
    assert metadata["provenance_ready"]
    assert metadata["settings_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    bundle.verify_identity(plan)
    bundle.mark_running()
    bundle.mark_running()
    metadata = json.loads(bundle.manifest_path.read_text())
    assert metadata["status"] == "running"
    assert metadata["execution_attempts"] == 2
    assert metadata["display_name"] == "Full display name"
    from app.ui.results.file_browser import CatalogueGroupItem
    assert CatalogueGroupItem("2_20261007T120000Z_run", 1) < CatalogueGroupItem("10_20261007T120000Z_run", 1)


def run_worker(settings, plan, root, target, monkeypatch, *, export_failure=False):
    if export_failure:
        monkeypatch.setattr(RunBundle, "finalize", lambda *a, **k: (_ for _ in ()).throw(OSError("export fault")))
    worker = RunWorker(settings, root / "settings.yml", plan, simulation=True,
                       simulation_seed=123, output_dir_override=root, sample_target=target)
    completed, failed = [], []
    worker.finished.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.run()
    return completed, failed


def test_worker_latches_dut_identity_before_execution(tmp_path):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, CHILDREN)
    target = {"sample_id": "wafer", "row": "20", "col": "4", "device_settings": {"device": "4"}}
    worker = RunWorker(settings, tmp_path / "settings.yml", plan, simulation=True, sample_target=target)
    target["col"] = "5"
    target["device_settings"]["device"] = "5"
    assert worker._sample_target["col"] == "4"
    assert worker._sample_target["device_settings"] == {"device": "4"}


def test_two_duts_have_independent_numbering_and_scientific_identity(tmp_path, monkeypatch):
    from app.inventory import InventoryStore, Sample
    store = InventoryStore(tmp_path / "inventory.db")
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, CHILDREN)
    try:
        sample = store.save_sample(Sample(sample_id="wafer", name="wafer", rows=("20",), cols=("4", "5")))
        for col in ("4", "5"):
            store.save_device_settings("wafer", "20", col, {"device": col})
            store.set_active_target(ActiveSampleTarget(sample_id="wafer", row="20", col=col, notes=f"DUT {col}"))
            target = store.get_active_target()
            root = store.measurement_directory_for("wafer", "sweeps", row=target.row, col=target.col)
            completed, failed = run_worker(settings, plan, root, target, monkeypatch)
            assert not failed, failed
            path = Path(completed[0]["path"])
            assert path.parent.name.startswith("1_")
            assert path.parent.parent.parent.parent.name == f"R20C{col}"
            manifest = json.loads((path.parent / "metadata.json").read_text())
            assert manifest["sample_target"]["col"] == col
            assert manifest["sample_target"]["device_settings"] == {"device": col}
            with h5py.File(path, "r") as data:
                assert data["run"].attrs["sample_row"] == "20"
                assert data["run"].attrs["sample_col"] == col
                assert data["run"].attrs["sample_cell_notes"] == f"DUT {col}"
                assert json.loads(data["run"].attrs["sample_device_settings"]) == {"device": col}
            for reference in manifest["references"]:
                with h5py.File(path.parent / reference["hdf5"], "r") as data:
                    assert data["run"].attrs["sample_col"] == col
            assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
        assert len(Hdf5RunReader.list_runs(store.sample_directory(sample.sample_id), recursive=True)) == 2
        from app.ui.results.file_browser import FileBrowserPanel
        from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
        browser = FileBrowserPanel(str(store.sample_directory(sample.sample_id)), catalogue_tree=True)
        try:
            browser.resize(1200, 800)
            browser.show()
            browser.refresh()
            QApplication.instance().processEvents()
            browser.runs.expandAll()
            QApplication.instance().processEvents()
            labels = []
            iterator = QTreeWidgetItemIterator(browser.runs)
            while iterator.value():
                labels.append(iterator.value().text(0))
                iterator += 1
            assert any("R20C4" in label for label in labels)
            assert any("R20C5" in label for label in labels)
            destination = Path("docs/audits/2026-10-07-sample-device-layout")
            destination.mkdir(parents=True, exist_ok=True)
            assert browser.grab().save(str(destination / "results-1200.png"))
        finally:
            browser.close()
            browser.deleteLater()
            QApplication.instance().processEvents()
    finally:
        store.close()


def test_two_simulated_runs_have_independent_metadata_and_baseline_files(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    application = QApplication.instance() or QApplication([])
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, CHILDREN)
    root = tmp_path / "active_sample" / "measurements" / "sweeps"
    target = ActiveSampleTarget(sample_id="active_sample", row="20", col="4", device_label="300 nm")
    paths = []
    for number in (1, 2):
        completed, failed = run_worker(settings, plan, root, target, monkeypatch)
        assert not failed, failed
        assert len(completed) == 1
        path = Path(completed[0]["path"])
        paths.append(path)
        assert path.parent.name.startswith(f"{number}_")
        manifest = json.loads((path.parent / "metadata.json").read_text(encoding="utf-8"))
        assert manifest["status"] == "completed"
        assert manifest["sample_target"]["sample_id"] == "active_sample"
        assert manifest["simulation"]["enabled"]
        assert {record["purpose"] for record in manifest["references"]} == {"background", "reference"}
        assert json.loads((path.parent / "plan.json").read_text())["sha256"] == plan.sha256
        assert (path.parent / "recipe.yml").read_text() == plan.recipe_source
        assert (path.parent / "settings.yml").is_file()
        assert (path.parent / "events.jsonl").stat().st_size > 0
        with h5py.File(path, "r") as source:
            for record in manifest["references"]:
                exported = path.parent / record["hdf5"]
                assert ThatecCompatibilityValidator().validate(exported, require_pythat=True).valid
                metadata = json.loads((path.parent / record["metadata"]).read_text())
                assert metadata["source_recipe_sweep_indices"]
                with h5py.File(exported, "r") as baseline:
                    np.testing.assert_array_equal(baseline["spectra/0/power_dbm"][:],
                                                  source[f'references/{record["index"]}/power_dbm'][:])
                    assert baseline["run"].attrs["sample_id"] == "active_sample"
        assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
        # Re-export is idempotent; never overwrites a baseline with other data.
        RunBundle(path).finalize(execution_state="completed", error=None)
    assert paths[0] != paths[1]
    assert len(Hdf5RunReader.list_runs(root, recursive=True)) == 2
    assert application is not None

    # Render the actual new structure and the active sample's path preview.
    from app.ui.results.file_browser import FileBrowserPanel
    from app.ui.recipes.page import RecipePage
    from PySide6.QtWidgets import QTreeWidgetItemIterator
    screenshots = Path("docs/audits/2026-10-07-sweep-run-bundles")
    screenshots.mkdir(parents=True, exist_ok=True)
    browser = FileBrowserPanel(str(tmp_path), catalogue_tree=True)
    page = RecipePage(settings)
    try:
        browser.resize(1200, 800)
        browser.show()
        browser.refresh()
        application.processEvents()
        browser.runs.expandAll()
        application.processEvents()
        iterator = QTreeWidgetItemIterator(browser.runs)
        labels = []
        while iterator.value():
            labels.append(iterator.value().text(0))
            iterator += 1
        assert "metadata.json" in labels
        assert "recipe.yml" in labels
        assert any(label.startswith("bg0001_") for label in labels)
        assert any(label.startswith("ref0000_") for label in labels)
        assert browser.runs.viewport().width() > 800
        assert browser.grab().save(str(screenshots / "results-1200.png"))
        page.set_active_sample_target(target, output_directory=root)
        page.resize(1280, 800)
        page.show()
        application.processEvents()
        assert page.width() == 1280
        assert str(root) in page.output_file_preview.text()
        assert "3_" in page.output_file_preview.text()
        assert page.output_directory.isReadOnly()
        assert not page.output_directory_button.isEnabled()
        assert not list(root.glob("3_*"))
        assert page.grab().save(str(screenshots / "preview-1280.png"))
        from app.ui.results import ResultsPage
        results = ResultsPage(str(tmp_path), catalogue_tree=True)
        try:
            metadata_path = paths[0].parent / "metadata.json"
            assert results.select_result_path(metadata_path)
            assert results._selected_artifact == metadata_path.resolve()
            assert results._result_task is None
        finally:
            results.close()
            results.deleteLater()
    finally:
        browser.close()
        browser.deleteLater()
        page.close()
        page.deleteLater()
        application.processEvents()


def test_worker_resume_reuses_bundle_and_baselines(tmp_path, monkeypatch):
    from app.engine.recovery import RunRecoveryManager
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, CHILDREN + "    - {id: second, type: acquire_spectrum}\n")
    root = tmp_path / "sweeps"
    worker = RunWorker(settings, tmp_path / "settings.yml", plan, simulation=True,
                       simulation_seed=123, output_dir_override=root)
    completed, failed = [], []
    worker.finished.connect(completed.append)
    worker.failed.connect(failed.append)
    worker.event.connect(lambda name, data: worker.request_stop()
                         if name == "safe_resume_boundary" and data["stored_points"] == 1 else None)
    worker.run()
    assert not failed, failed
    path = Path(completed[0]["path"])
    checkpoint = RunRecoveryManager().inspect(path, plan)
    assert checkpoint.stored_points == 1
    baseline_paths = list((path.parent / "references").glob("*.h5"))
    before = {p: p.read_bytes() for p in baseline_paths}
    manifest_before = json.loads((path.parent / "metadata.json").read_text())
    resume = RunWorker(settings, tmp_path / "settings.yml", plan, simulation=True,
                       simulation_seed=123, recovery=checkpoint)
    completed, failed = [], []
    resume.finished.connect(completed.append)
    resume.failed.connect(failed.append)
    resume.run()
    assert not failed, failed
    assert Path(completed[0]["path"]) == path
    assert completed[0]["result"].stored_points == 2
    assert {p: p.read_bytes() for p in baseline_paths} == before
    manifest = json.loads((path.parent / "metadata.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["references"] == manifest_before["references"]
    assert len(list(root.glob("[0-9]*_*"))) == 1


def test_export_failure_is_reported_and_bundle_marked_faulted(tmp_path, monkeypatch):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, CHILDREN)
    completed, failed = run_worker(settings, plan, tmp_path / "sweeps", None, monkeypatch,
                                  export_failure=True)
    assert not completed
    assert failed and "export fault" in failed[0]
    metadata = next((tmp_path / "sweeps").rglob("metadata.json"))
    assert json.loads(metadata.read_text())["status"] == "faulted"


def test_failed_baseline_file_creation_leaves_no_final_or_partial_export(tmp_path, monkeypatch):
    from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
    from app.domain.models import MeasurementPoint
    from app.storage.hdf5_writer import Hdf5RunWriter
    path = tmp_path / "data.h5"
    writer = Hdf5RunWriter(path, recipe_source="name: export-probe\n", settings_source="schema_version: 1\n",
                           plan_hash="probe", device_idn={})
    trace = SpectrumTrace((1., 2., 3.), (-70., -60., -65.), NOW, "TRAC1")
    writer.store_reference(trace)
    writer.append(MeasurementPoint(0, {}, {}), trace)
    writer.close("completed")
    original = path.read_bytes()

    def broken_writer(export_path, **kwargs):
        Path(export_path).write_bytes(b"partial creation")
        raise OSError("injected baseline file creation failure")

    monkeypatch.setattr("app.storage.hdf5_writer.Hdf5RunWriter", broken_writer)
    with h5py.File(path, "r") as source:
        with pytest.raises(OSError, match="injected baseline"):
            RunBundle(path)._export_reference(source, source["references/0"], 0)
    assert not list((tmp_path / "references").iterdir())
    assert path.read_bytes() == original


def test_failed_signal_preserves_this_runs_committed_background_and_reference(tmp_path, monkeypatch):
    from app.devices.anritsu_ms2830a.adapter import AnritsuAdapter
    from app.domain.errors import DeviceError
    original = AnritsuAdapter.acquire_single_sweep
    calls = 0

    def fail_signal(adapter, trace, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 5:
            raise DeviceError("injected signal failure")
        return original(adapter, trace, **kwargs)

    monkeypatch.setattr(AnritsuAdapter, "acquire_single_sweep", fail_signal)
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, CHILDREN)
    completed, failed = run_worker(settings, plan, tmp_path / "sweeps", None, monkeypatch)
    assert not failed, failed
    assert "injected signal failure" in completed[0]["result"].error
    path = Path(completed[0]["path"])
    manifest = json.loads((path.parent / "metadata.json").read_text())
    assert manifest["status"] == "faulted"
    assert manifest["point_count"] == 0
    assert len(manifest["references"]) == 2
    assert all((path.parent / record["hdf5"]).is_file() for record in manifest["references"])


def test_unreasonably_long_root_is_rejected_without_reservation(tmp_path):
    root = tmp_path / ("x" * 180)
    with pytest.raises(ExecutionError, match="too long"):
        bundle_directory(root, "test", NOW)
    assert not root.exists()
