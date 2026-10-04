"""Shown resume review, parent identity races and actual queued resume."""

from pathlib import Path
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtTest import QTest

from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.devices.anritsu_ms2830a.ui.finalization_resume_dialog import SpectrumFinalizationResumeDialog
from app.domain.spectrum_finalization import SpectrumFinalizationResumeRequest
from app.storage.finalized_spectrum_store import file_sha256, replay_finalized_artifact
from app.storage.spectrum_finalization_batch_store import replay_finalization_batch
from app.storage.spectrum_finalization_batch_store import finalize_spectrum_batch
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.shell_test_isolation import shell_qt_application as shell_qt_application
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_finalization_resume import interrupted_batch
from tests.test_spectrum_finalization_batch import batch
from tests.test_spectrum_finalization_adoption import closed_but_unjournaled


def inspect(app, dialog, previous):
    dialog.previous.setText(str(previous))
    dialog.inspect.click()
    assert dialog._busy and not dialog.start.isEnabled()
    wait_until(app, lambda: not dialog._busy)
    assert dialog._inspection is not None


@pytest.mark.parametrize("theme,size", [("light", (1000, 800)), ("dark", (760, 700))])
def test_shown_resume_requires_replacements_and_stopped_confirmation(shell_qt_application, tmp_path, monkeypatch, theme, size):
    app = shell_qt_application
    apply_application_theme(app, theme)
    original, _resume, paths = interrupted_batch(tmp_path, monkeypatch, partial=True)
    previous_hash = file_sha256(original.journal_path)
    hashes = [file_sha256(path) for path in paths]
    dialog = SpectrumFinalizationResumeDialog()
    selected = []
    dialog.selected.connect(selected.append)
    replacement = tmp_path / "retry.h5"
    monkeypatch.setattr("app.devices.anritsu_ms2830a.ui.finalization_resume_dialog.StationFileDialog.getSaveFileName",
                        lambda *_args: (str(replacement), ""))
    try:
        dialog.resize(*size)
        dialog.show()
        inspect(app, dialog, original.journal_path)
        assert dialog._inspection.completed_blocks == 1
        assert dialog.table.rowCount() == 2
        dialog.journal.setText(str(tmp_path / "new.jsonl"))
        assert not dialog.stopped.isChecked() and not dialog.start.isEnabled()
        dialog.stopped.setChecked(True)
        assert not dialog.start.isEnabled()  # Existing unfinished output requires a new destination.
        dialog.table.selectRow(0)
        assert not dialog.replace_output.isEnabled()
        dialog.table.selectRow(1)
        assert dialog.replace_output.isEnabled()
        dialog.replace_output.click()
        assert dialog.start.isEnabled()
        dialog.clear_output.click()
        assert not dialog.start.isEnabled()
        dialog.replace_output.click()
        dialog.journal.setText(str(original.journal_path))
        dialog.start.click()
        assert not selected and "distinct new" in dialog.status.text()
        dialog.journal.setText(str(tmp_path / "new.jsonl"))
        app.processEvents()
        for control in (dialog.table, dialog.adopt, dialog.journal, dialog.stopped, dialog.status, dialog.start, dialog.close_button):
            point = control.mapTo(dialog, QPoint(0, 0))
            assert control.isVisible() and control.height() >= control.fontMetrics().height()
            assert dialog.rect().contains(point)
            assert dialog.rect().contains(point + QPoint(control.width() - 1, control.height() - 1))
        directory = Path("artifacts/spectrum-finalization-ui")
        directory.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(directory / f"resume-{theme}-{size[0]}.png"))
        QTest.mouseClick(dialog.start, Qt.MouseButton.LeftButton)
        wait_until(app, lambda: bool(selected))
        assert type(selected[0]) is SpectrumFinalizationResumeRequest
        assert selected[0].replacement_destinations == ((1, replacement.resolve()),)
        assert selected[0].expected_parent_hash == previous_hash
        assert not dialog._controller._thread.isRunning()
        assert previous_hash == file_sha256(original.journal_path)
        assert hashes == [file_sha256(path) for path in paths]
        assert not selected[0].journal_path.exists()
    finally:
        assert dialog._controller.close(wait_ms=5000)
        dialog.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("parent_changed", [False, True])
def test_real_workspace_resume_preserves_files_and_rejects_changed_parent(shell_qt_application, tmp_path, monkeypatch, parent_changed):
    app = shell_qt_application
    original, _resume, paths = interrupted_batch(tmp_path, monkeypatch, partial=True)
    hashes = [file_sha256(path) for path in paths]
    first_hash = file_sha256(original.blocks[0].destination)
    partial_hash = file_sha256(original.blocks[1].destination)
    replacement = tmp_path / "new-output.h5"
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    commands, finished = [], []
    workspace.request_device.connect(lambda *args: commands.append(args))
    workspace._cpu.completed.connect(lambda operation, _result: finished.append(operation) if operation == "resume_batch" else None)
    workspace._cpu.failed.connect(lambda operation, _error: finished.append(operation) if operation == "resume_batch" else None)
    monkeypatch.setattr("app.devices.anritsu_ms2830a.ui.finalization_resume_dialog.StationFileDialog.getSaveFileName",
                        lambda *_args: (str(replacement), ""))
    try:
        workspace.resize(1100, 900)
        workspace.show()
        workspace.resume_batch_action.trigger()
        dialog = workspace._finalization_resume_dialog
        assert dialog is not None
        inspect(app, dialog, original.journal_path)
        dialog.table.selectRow(1)
        dialog.replace_output.click()
        dialog.journal.setText(str(tmp_path / "new-journal.jsonl"))
        dialog.stopped.setChecked(True)
        if parent_changed:
            with original.journal_path.open("ab") as stream:
                stream.write(b"external process update")
        parent_hash = file_sha256(original.journal_path)
        dialog.start.click()
        # A SHA mismatch can fail within one processEvents call. Observe the
        # terminal signal rather than requiring a transient busy state.
        wait_until(app, lambda: bool(finished), timeout=15)
        assert not workspace._finalization_busy
        wait_until(app, lambda: workspace._finalization_resume_dialog is None)
        assert not commands and not workspace._cpu._offline_busy
        assert first_hash == file_sha256(original.blocks[0].destination)
        assert partial_hash == file_sha256(original.blocks[1].destination)
        assert parent_hash == file_sha256(original.journal_path)
        assert hashes == [file_sha256(path) for path in paths]
        if parent_changed:
            assert "changed since inspection" in workspace.state_label.text()
            assert not replacement.exists() and not (tmp_path / "new-journal.jsonl").exists()
        else:
            assert "2 verified blocks" in workspace.recording_title.text()
            assert len(replay_finalization_batch(tmp_path / "new-journal.jsonl")) == 2
            block = replay_finalized_artifact(replacement)
            np.testing.assert_array_equal(block.result.values_w, workspace._latest_result.values_w)
            assert workspace._latest_result.final and workspace._latest_result.count == 2
    finally:
        if workspace._finalization_resume_dialog is not None:
            assert workspace._finalization_resume_dialog._controller.close(wait_ms=5000)
        assert workspace._cpu.close(wait_ms=5000)
        workspace.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_async_inspection_cannot_publish_a_changed_selection(shell_qt_application, tmp_path, monkeypatch):
    app = shell_qt_application
    original, _resume, _paths = interrupted_batch(tmp_path, monkeypatch)
    dialog = SpectrumFinalizationResumeDialog()
    entered, release = Event(), Event()
    execute = dialog._controller._worker._execute

    def paused(operation, payload):
        if operation == "inspect_batch_resume":
            entered.set()
            assert release.wait(10)
        return execute(operation, payload)

    monkeypatch.setattr(dialog._controller._worker, "_execute", paused)
    try:
        dialog.show()
        dialog.previous.setText(str(original.journal_path))
        dialog.inspect.click()
        wait_until(app, entered.is_set)
        dialog.previous.setText(str(tmp_path / "different.jsonl"))
        release.set()
        wait_until(app, lambda: not dialog._busy)
        assert dialog._inspection is None and not dialog.start.isEnabled()
        assert "Selection changed" in dialog.status.text()
        assert dialog.inspect.isEnabled()
    finally:
        release.set()
        assert dialog._controller.close(wait_ms=5000)
        dialog.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("state", ["missing", "completed"])
def test_inspection_empty_and_completed_states_cannot_start_resume(shell_qt_application, tmp_path, state):
    app = shell_qt_application
    dialog = SpectrumFinalizationResumeDialog()
    if state == "completed":
        request, _expected, _paths = batch(tmp_path)
        finalize_spectrum_batch(request)
        previous = request.journal_path
    else:
        previous = tmp_path / "missing.jsonl"
    selected = []
    dialog.selected.connect(selected.append)
    try:
        dialog.show()
        dialog.previous.setText(str(previous))
        dialog.inspect.click()
        wait_until(app, lambda: not dialog._busy)
        dialog.journal.setText(str(tmp_path / "new.jsonl"))
        dialog.stopped.setChecked(True)
        assert not dialog.start.isEnabled() and not selected
        assert "already completed" in dialog.status.text() if state == "completed" else "inspection failed" in dialog.status.text()
        assert dialog.inspect.isEnabled() and dialog.close_button.isEnabled()
    finally:
        assert dialog._controller.close(wait_ms=5000)
        dialog.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


@pytest.mark.parametrize("changed", [False, True])
def test_gui_adoption_is_explicit_and_pins_inspected_file(shell_qt_application, tmp_path, monkeypatch, changed):
    app = shell_qt_application
    original, _request, _paths = closed_but_unjournaled(tmp_path, monkeypatch)
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    finished, commands = [], []
    workspace._cpu.completed.connect(lambda operation, _result: finished.append(operation) if operation == "resume_batch" else None)
    workspace._cpu.failed.connect(lambda operation, _error: finished.append(operation) if operation == "resume_batch" else None)
    workspace.request_device.connect(lambda *args: commands.append(args))
    try:
        workspace.show()
        workspace.resume_batch_action.trigger()
        dialog = workspace._finalization_resume_dialog
        inspect(app, dialog, original.journal_path)
        assert dialog.adopt.isEnabled() and not dialog.adopt.isChecked()
        assert dialog._inspection.blocks[0].adoptable
        dialog.journal.setText(str(tmp_path / "adopted-gui.jsonl"))
        dialog.stopped.setChecked(True)
        assert not dialog.start.isEnabled()
        dialog.adopt.setChecked(True)
        assert dialog.start.isEnabled()
        if not changed:
            directory = Path("artifacts/spectrum-finalization-ui")
            directory.mkdir(parents=True, exist_ok=True)
            dialog.resize(900, 750)
            app.processEvents()
            assert dialog.grab().save(str(directory / "resume-adoption-selected.png"))
        if changed:
            with original.blocks[0].destination.open("ab") as stream:
                stream.write(b"changed after inspection")
        digest = file_sha256(original.blocks[0].destination)
        parent_hash = file_sha256(original.journal_path)
        dialog.start.click()
        wait_until(app, lambda: bool(finished), timeout=15)
        assert not commands and not workspace._finalization_busy
        assert digest == file_sha256(original.blocks[0].destination)
        assert parent_hash == file_sha256(original.journal_path)
        if changed:
            assert "closed output changed since inspection" in workspace.state_label.text()
            assert not (tmp_path / "adopted-gui.jsonl").exists()
        else:
            completed = replay_finalization_batch(tmp_path / "adopted-gui.jsonl")
            assert len(completed) == 2 and completed[0]["recovered_without_journal_commit"]
            assert workspace._latest_result.final
    finally:
        if workspace._finalization_resume_dialog is not None:
            assert workspace._finalization_resume_dialog._controller.close(wait_ms=5000)
        assert workspace._cpu.close(wait_ms=5000)
        workspace.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
