"""Persisted original target and idempotent inventory artifact registration."""

from pathlib import Path

import pytest

from app.devices.keithley_2600.characterization.field_catalogue import register_field_series
from app.inventory.models import Sample
from app.inventory.store import InventoryStore
from tests.test_keithley_field_worker import make_worker


def test_summary_links_require_membership_and_existing_files(saved_series):
    from dataclasses import replace
    from app.devices.keithley_2600.characterization.field_catalogue import summary_artifacts_for_run
    store, directory = saved_series
    record = register_field_series(store, directory)[0]
    assert summary_artifacts_for_run(record) == ()
    (directory / "field_series_report.pdf").write_bytes(b"test")
    (directory / "field_series_summary.csv").write_text("test", encoding="utf-8")
    links = summary_artifacts_for_run(record)
    assert len(links) == 2
    assert links[0][1] == str(directory / "field_series_report.pdf")
    unrelated = replace(record, csv_path=str(directory / "unrelated" / "characterization.csv"))
    assert summary_artifacts_for_run(unrelated) == ()


def test_measurements_menu_opens_both_summary_artifacts(saved_series, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator
    from app.ui.inventory.measurement_tree import MeasurementTreeWidget, RoundMenu
    app = QApplication.instance() or QApplication([])
    store, directory = saved_series
    records = register_field_series(store, directory)
    for filename in ("field_series_report.pdf", "field_series_summary.csv"):
        (directory / filename).write_text("test artifact", encoding="utf-8")
    widget = MeasurementTreeWidget()
    opened = []
    monkeypatch.setattr(widget, "_open_file", opened.append)
    def select_summary_actions(menu, *args):
        selected = [action for action in menu.actions() if action.text().startswith("Open series summary")]
        assert len(selected) == 2
        for action in selected:
            action.trigger()
    monkeypatch.setattr(RoundMenu, "exec", select_summary_actions)
    try:
        widget.resize(1000, 700)
        widget.set_runs(records)
        widget.show()
        widget.tree.expandAll()
        app.processEvents()
        iterator = QTreeWidgetItemIterator(widget.tree)
        while iterator.value() and iterator.value().data(0, Qt.ItemDataRole.UserRole) is None:
            iterator += 1
        item = iterator.value()
        assert item is not None
        rect = widget.tree.visualItemRect(item)
        assert rect.height() > 0 and widget.tree.isVisible()
        widget._on_context_menu(rect.center())
        assert opened == [str(directory / "field_series_report.pdf"), str(directory / "field_series_summary.csv")]
    finally:
        widget.close()
        app.processEvents()


@pytest.fixture
def saved_series(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.save_sample(Sample(sample_id="original", name="Original sample", rows=("1",), cols=("2",)))
    worker, _ = make_worker(tmp_path)
    worker.inventory_target = {"sample_id": "original", "sample_name": "Original sample",
                               "row": "1", "col": "2", "device_label": "MTJ"}
    worker.run()
    try:
        yield store, worker.directory
    finally:
        store.close()


def test_register_then_regenerate_does_not_duplicate_and_preserves_user_metadata(saved_series):
    store, directory = saved_series
    first = register_field_series(store, directory)
    assert len(first) == 3
    assert len(store.list_runs_for_cell("original", "1", "2")) == 3
    assert all(not record.report_path for record in first)
    store._connection.execute("UPDATE sample_runs SET notes = ?, elab_experiment_id = ? WHERE id = ?",
                              ("Operator note", 42, first[0].id))
    for record in first:
        (Path(record.csv_path).parent / "characterization_report.pdf").write_bytes(b"PDF fixture")
    second = register_field_series(store, directory)
    assert [record.id for record in second] == [record.id for record in first]
    assert all(record.report_path for record in second)
    assert second[0].notes == "Operator note"
    assert second[0].elab_experiment_id == 42
    assert "#1" in second[0].recipe_name
    assert "#3" in second[1].recipe_name  # The skipped field target is not a measured curve.


def test_changed_raw_csv_cannot_replace_registered_measurement(saved_series):
    store, directory = saved_series
    first = register_field_series(store, directory)
    with Path(first[0].csv_path).open("a", encoding="utf-8") as stream:
        stream.write("corrupted data\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        register_field_series(store, directory)
    assert len(store.list_runs_for_sample("original")) == 3


def test_manual_or_old_series_never_uses_current_inventory_selection(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    try:
        worker, _ = make_worker(tmp_path)
        worker.run()
        assert register_field_series(store, worker.directory) == ()
    finally:
        store.close()
