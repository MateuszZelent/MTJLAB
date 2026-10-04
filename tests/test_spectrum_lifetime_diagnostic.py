"""The census detects destroyed wrappers without itself retaining them."""

import gc
import json
import weakref

import pytest

from PySide6.QtCore import QEvent, QObject
from shiboken6 import isValid

from tests.shell_test_isolation import shell_qt_application as shell_qt_application
from tools.diagnose_spectrum_qt_lifetimes import (
    bounded_reference_paths, diagnose_shell_lifetimes, qt_lifetime_census,
)


def test_census_distinguishes_native_lifetime_and_releases_its_object_list(shell_qt_application):
    application = shell_qt_application
    obj = QObject()
    reference = weakref.ref(obj)
    key = "PySide6.QtCore.QObject"
    live = qt_lifetime_census()
    assert live["qobject_valid_counts"].get(key, 0) >= 1
    obj.deleteLater()
    application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()
    assert not isValid(obj)
    destroyed = qt_lifetime_census()
    assert destroyed["qobject_invalid_counts"].get(key, 0) == live["qobject_invalid_counts"].get(key, 0) + 1
    del obj
    gc.collect()
    assert reference() is None
    assert qt_lifetime_census()["qobject_invalid_counts"].get(key, 0) == live["qobject_invalid_counts"].get(key, 0)


def test_reference_paths_describe_known_owner_without_retaining_objects(shell_qt_application):
    obj = QObject()
    reference = weakref.ref(obj)
    owner = {"__name__": "synthetic_test_owner", "owned": obj}
    report = bounded_reference_paths(obj)
    assert any(row["path"] and row["path"][0].get("keys") == ["owned"]
               and row["path"][0].get("module") == "synthetic_test_owner"
               for row in report["paths"])
    assert report["visited_nodes"] <= 300
    json.dumps(report, allow_nan=False)
    del obj, owner
    gc.collect()
    assert reference() is None


def test_failed_campaign_preserves_census_and_refuses_overwrite(
    shell_qt_application, tmp_path, monkeypatch,
):
    from tools import diagnose_spectrum_qt_lifetimes as diagnostic

    def failed_campaign(*_args, **_kwargs):
        diagnostic.stop_benchmark.process_memory()
        raise RuntimeError("injected failure after cleanup")

    monkeypatch.setattr(diagnostic.stop_benchmark, "benchmark_stop_response", failed_campaign)
    # This case tests the diagnostic journal, not a real shell benchmark.
    # Use a minimal observer to avoid relying on widgets from other tests.
    class EmptyApplication:
        def allWidgets(self):
            return []

    monkeypatch.setattr(diagnostic.QApplication, "instance", lambda: EmptyApplication())
    output = tmp_path / "census.json"
    with pytest.raises(RuntimeError, match="injected failure"):
        diagnose_shell_lifetimes(output, cycles=1, experimental_overrides=("test injection",))
    journal = output.with_suffix(".census.jsonl")
    original = journal.read_bytes()
    rows = [json.loads(line) for line in original.splitlines()]
    assert rows[0]["type"] == "incomplete_qt_lifetime_census"
    assert rows[0]["experimental_overrides"] == ["test injection"]
    assert rows[1]["cycle"] == 0 and "qobject_invalid_counts" in rows[1]["census"]
    assert not output.exists()
    with pytest.raises(FileExistsError, match="artifacts must be new"):
        diagnose_shell_lifetimes(output, cycles=1)
    assert journal.read_bytes() == original
