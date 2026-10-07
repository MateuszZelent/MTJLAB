"""Manual archive operations serialize I/O without blocking Qt."""
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from tests.test_source_review_manual_provenance import app as app, page as page
from tests.test_main_window import wait_for_ui
from tests.test_source_review_manual_provenance import trace
from app.devices.anritsu_ms2830a.ui.manual_save import ManualSpectrumSaveOptions
from app.storage import ManualSpectrumSaveMode


@pytest.mark.parametrize("operation", ["save", "close"])
@pytest.mark.parametrize("fails", [False, True])
def test_archive_worker_keeps_gui_responsive_and_reports_after_finish(page, monkeypatch, operation, fails):
    entered, release = threading.Event(), threading.Event()
    gui_thread = threading.get_ident()
    archive = Mock(active_path=None)
    page._manual_archive = archive
    result = object()

    def io(**kwargs):
        assert threading.get_ident() != gui_thread
        entered.set()
        assert release.wait(10)
        if fails:
            raise OSError("injected storage failure")
        return result

    getattr(archive, operation).side_effect = io
    saved, closed = Mock(), Mock()
    monkeypatch.setattr(page, "_manual_save_completed", saved)
    monkeypatch.setattr(page, "_manual_close_completed", closed)
    context = {"options": None, "metadata_count": 0} if operation == "save" else {"path": "example.h5"}
    try:
        page._start_manual_archive_job(operation, {}, **context)
        assert wait_for_ui(entered.is_set)
        assert not page.prepare_manual_archive_shutdown()
        assert not page.save_manual_spectrum.isEnabled()
        assert not page.configure_manual_spectrum.isEnabled()
        assert not page.close_manual_archive.isEnabled()
        page._save_configured_manual_spectrum()
        page.close_manual_archive_session()
        assert getattr(archive, operation).call_count == 1
        saved.assert_not_called()
        closed.assert_not_called()
        release.set()
        assert wait_for_ui(lambda: page._manual_archive_thread is None)
        assert page.prepare_manual_archive_shutdown()
        if fails:
            assert "injected storage failure" in page.manual_save_status.text()
            saved.assert_not_called()
            closed.assert_not_called()
        elif operation == "save":
            saved.assert_called_once_with(result, **context)
        else:
            closed.assert_called_once_with("example.h5")
    finally:
        release.set()
        assert wait_for_ui(lambda: page._manual_archive_thread is None)


def test_async_save_uses_clicked_trace_and_context_snapshot(page, monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    original = trace()
    operator = {"session": {"name": "first"}}
    archive = Mock(active_path=None)
    payloads = []

    def save(**payload):
        entered.set()
        assert release.wait(10)
        payloads.append(payload)
        return object()

    archive.save.side_effect = save
    page._manual_archive = archive
    page.set_manual_archive_context(operator_context_provider=lambda: operator, simulation=False)
    page._apply_manual_save_options(ManualSpectrumSaveOptions(tmp_path / "snapshot.h5", ManualSpectrumSaveMode.APPEND, "none", (), "raw"))
    page._show_trace(original)
    monkeypatch.setattr(page, "_manual_save_completed", Mock())
    try:
        page._save_configured_manual_spectrum()
        assert wait_for_ui(entered.is_set)
        operator["session"]["name"] = "second"
        page._show_trace(replace(original, powers_dbm=(-10., -10., -10.)))
        release.set()
        assert wait_for_ui(lambda: page._manual_archive_thread is None)
        assert len(payloads) == 1
        assert payloads[0]["trace"] is original
        assert payloads[0]["capture_context"]["operator_context"] == {"session": {"name": "first"}}
    finally:
        release.set()
        assert wait_for_ui(lambda: page._manual_archive_thread is None)
