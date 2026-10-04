"""Shown batch selection and actual queued finalization/cancellation."""

from pathlib import Path
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtTest import QTest

from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.devices.anritsu_ms2830a.ui.finalization_selection_dialog import SpectrumFinalizationSelectionDialog
from app.domain.spectrum_finalization import SpectrumFinalizationBatchRequest
from app.storage import spectrum_finalization_batch_store as store
from app.storage.finalized_spectrum_store import file_sha256
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.shell_test_isolation import shell_qt_application as shell_qt_application
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_finalization_selection_dialog import multi_sources


def choose_sources(app, dialog, paths):
    for edit, path in zip(dialog.paths, paths, strict=True):
        edit.setText(str(path))
    dialog.inspect.click()
    wait_until(app, lambda: not dialog._busy)
    assert dialog._loaded_sources is not None
    for selector, identity in zip(dialog.profiles, ("before", "before", "after"), strict=True):
        selector.setCurrentIndex(selector.findData(identity))


def add_blocks(dialog, tmp_path):
    for index, points in enumerate(("0", "1 2")):
        dialog.indices.setText(points)
        dialog.output.setText(str(tmp_path / f"selected-{index}.h5"))
        dialog.add_block.click()
    assert len(dialog._queued_blocks) == 2
    dialog.journal.setText(str(tmp_path / "batch.jsonl"))


@pytest.mark.parametrize("theme,size", [("light", (1000, 900)), ("dark", (760, 700))])
def test_shown_batch_queue_validation_removal_and_immutable_submission(shell_qt_application, tmp_path, theme, size):
    app = shell_qt_application
    apply_application_theme(app, theme)
    _context, _expected, paths = multi_sources(tmp_path)
    hashes = [file_sha256(path) for path in paths]
    dialog = SpectrumFinalizationSelectionDialog(batch_mode=True)
    selected = []
    dialog.selected.connect(selected.append)
    try:
        dialog.resize(*size)
        dialog.show()
        assert not dialog.start.isEnabled() and not dialog.add_block.isEnabled()
        choose_sources(app, dialog, paths)
        add_blocks(dialog, tmp_path)
        assert dialog.start.isEnabled()
        dialog.add_block.click()
        assert len(dialog._queued_blocks) == 2 and "distinct new output" in dialog.status.text()
        dialog.block_table.selectRow(1)
        assert dialog.remove_block.isEnabled()
        dialog.remove_block.click()
        assert len(dialog._queued_blocks) == dialog.block_table.rowCount() == 1
        dialog.add_block.click()
        assert len(dialog._queued_blocks) == 2
        dialog.journal.setText(str(paths[0]))
        dialog.start.click()
        assert not selected and "new journal" in dialog.status.text()
        dialog.journal.setText(str(tmp_path / "batch.jsonl"))
        # Editing the next source does not mutate previously queued selections.
        dialog.paths[0].setText(str(tmp_path / "other.h5"))
        assert dialog.start.isEnabled() and not dialog.add_block.isEnabled()
        assert dialog._queued_blocks[0].signal_path == paths[0].resolve()
        dialog.form_scroll.ensureWidgetVisible(dialog.block_table)
        app.processEvents()
        for control in (dialog.start, dialog.close_button, dialog.status, dialog.journal):
            origin = control.mapTo(dialog, QPoint(0, 0))
            assert control.isVisible() and control.height() >= control.fontMetrics().height()
            assert dialog.rect().contains(origin)
            assert dialog.rect().contains(origin + QPoint(control.width() - 1, control.height() - 1))
        assert dialog.block_table.viewport().width() > 400
        assert dialog.form_scroll.viewport().rect().intersects(dialog.block_table.rect().translated(
            dialog.block_table.mapTo(dialog.form_scroll.viewport(), QPoint(0, 0))))
        directory = Path("artifacts/spectrum-finalization-ui")
        directory.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(directory / f"batch-{theme}-{size[0]}.png"))
        QTest.mouseClick(dialog.start, Qt.MouseButton.LeftButton)
        wait_until(app, lambda: bool(selected))
        assert type(selected[0]) is SpectrumFinalizationBatchRequest
        assert [block.point_indices for block in selected[0].blocks] == [(0,), (1, 2)]
        assert not dialog._controller._thread.isRunning()
        assert hashes == [file_sha256(path) for path in paths]
        assert not selected[0].journal_path.exists()
    finally:
        assert dialog._controller.close(wait_ms=5000)
        dialog.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("cancel", [False, True])
def test_workspace_batch_worker_progress_and_cancel_preserve_completed_blocks(shell_qt_application, tmp_path, monkeypatch, cancel):
    app = shell_qt_application
    _context, expected, paths = multi_sources(tmp_path)
    hashes = [file_sha256(path) for path in paths]
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    commands, progress, errors = [], [], []
    workspace.request_device.connect(lambda *args: commands.append(args))
    workspace._cpu.processing_progress.connect(lambda *args: progress.append(args))
    workspace._cpu.failed.connect(lambda *args: errors.append(args))
    entered, release = Event(), Event()
    real_finalize = store.finalize_spectrum_archives
    calls = 0

    def paused(*args, **kwargs):
        nonlocal calls
        calls += 1
        if cancel and calls == 2:
            entered.set()
            assert release.wait(10)
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(store, "finalize_spectrum_archives", paused)
    try:
        workspace.resize(1100, 900)
        workspace.show()
        workspace.select_batch_action.trigger()
        dialog = workspace._finalization_selection_dialog
        assert dialog is not None and dialog._batch_mode
        choose_sources(app, dialog, paths)
        add_blocks(dialog, tmp_path)
        dialog.start.click()
        wait_until(app, lambda: workspace._finalization_busy)
        assert workspace.stop.isEnabled()
        if cancel:
            wait_until(app, entered.is_set)
            wait_until(app, lambda: bool(progress))
            assert "1/2" in workspace.recording_title.text()
            workspace.stop.click()
            assert "Canceling" in workspace.recording_title.text()
            release.set()
        wait_until(app, lambda: not workspace._finalization_busy, timeout=15)
        wait_until(app, lambda: workspace._finalization_selection_dialog is None)
        assert not commands and not errors
        assert not workspace._cpu._offline_busy
        assert hashes == [file_sha256(path) for path in paths]
        journal = tmp_path / "batch.jsonl"
        if cancel:
            assert "canceled" in workspace.recording_title.text()
            assert len(store.replay_finalization_batch(journal, require_completed=False)) == 1
            assert not (tmp_path / "selected-1.h5").exists()
            assert "batch.jsonl" in workspace.archive_label.text()
        else:
            assert "2 blocks saved" in workspace.recording_title.text()
            assert [item[1:] for item in progress] == [(1, 2), (2, 2)]
            assert len(store.replay_finalization_batch(journal)) == 2
            assert workspace._latest_result.final and workspace._latest_result.count == 2
            np.testing.assert_allclose(workspace._latest_result.values_w, expected, rtol=1e-12)
            assert "batch.jsonl" in workspace.archive_label.text()
    finally:
        release.set()
        if workspace._finalization_selection_dialog is not None:
            assert workspace._finalization_selection_dialog._controller.close(wait_ms=5000)
        assert workspace._cpu.close(wait_ms=5000)
        workspace.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
