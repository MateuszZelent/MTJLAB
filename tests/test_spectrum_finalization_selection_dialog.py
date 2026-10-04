"""Shown profile-history selection, invalidation and real worker finalization."""

from dataclasses import replace
from pathlib import Path
from threading import Event

import h5py
import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from shiboken6 import isValid

from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.devices.anritsu_ms2830a.ui.finalization_selection_dialog import SpectrumFinalizationSelectionDialog
from app.storage.finalized_spectrum_store import file_sha256, replay_finalized_artifact
from app.storage.spectrum_correction_codec import read_profile, write_profile
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.shell_test_isolation import shell_qt_application as shell_qt_application
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_finalized_store import archives


def multi_sources(tmp_path):
    context, expected, paths = archives(tmp_path)
    for path in paths:
        with h5py.File(path, "r+") as file:
            root = file["spectrum_processing_v1/profiles"]
            context, profile = read_profile(next(iter(root.values())))
            decoy = replace(profile, profile_id="000_decoy", mean_w=profile.mean_w * 2)
            group = root.create_group(decoy.profile_id)
            write_profile(group, context, decoy)
            group.attrs["complete"] = True
    return context, expected, paths


@pytest.mark.parametrize("theme,size", [("light", (1000, 850)), ("dark", (760, 650))])
def test_shown_selector_requires_explicit_profiles_and_invalidates_changed_sources(
    shell_qt_application, tmp_path, theme, size,
):
    app = shell_qt_application
    apply_application_theme(app, theme)
    _context, _expected, paths = multi_sources(tmp_path)
    hashes = [file_sha256(path) for path in paths]
    dialog = SpectrumFinalizationSelectionDialog()
    requests = []
    dialog.selected.connect(requests.append)
    try:
        dialog.resize(*size)
        dialog.show()
        for edit, path in zip(dialog.paths, paths, strict=True):
            edit.setText(str(path))
        dialog.output.setText(str(tmp_path / "selected.h5"))
        QTest.mouseClick(dialog.inspect, Qt.MouseButton.LeftButton)
        assert dialog._busy and not dialog.start.isEnabled()
        wait_until(app, lambda: not dialog._busy)
        assert dialog._loaded_sources is not None
        assert all(selector.currentData() is None for selector in dialog.profiles)
        assert not dialog.start.isEnabled()
        for selector, identity in zip(dialog.profiles, ("before", "before", "after"), strict=True):
            selector.setCurrentIndex(selector.findData(identity))
        assert dialog.start.isEnabled()
        dialog.indices.setText("2 1")
        dialog.start.click()
        assert "strictly ordered" in dialog.status.text() and not requests
        dialog.paths[0].setText(str(tmp_path / "changed.h5"))
        assert not dialog.start.isEnabled() and dialog._loaded_sources is None
        dialog.paths[0].setText(str(paths[0]))
        dialog.inspect.click()
        wait_until(app, lambda: not dialog._busy)
        for selector, identity in zip(dialog.profiles, ("before", "before", "after"), strict=True):
            selector.setCurrentIndex(selector.findData(identity))
        dialog.indices.setText("1, 2")
        app.processEvents()
        for control in (dialog.status, dialog.start, dialog.close_button):
            assert control.isVisible() and control.height() >= control.fontMetrics().height()
            origin = control.mapTo(dialog, QPoint(0, 0))
            assert dialog.rect().contains(origin)
            assert dialog.rect().contains(origin + QPoint(control.width() - 1, control.height() - 1))
        directory = Path("artifacts/spectrum-finalization-ui")
        directory.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(directory / f"selection-{theme}-{size[0]}.png"))
        dialog.start.click()
        wait_until(app, lambda: bool(requests))
        request = requests[0]
        assert request.point_indices == (1, 2)
        assert (request.signal_profile_id, request.before_profile_id, request.after_profile_id) == ("before", "before", "after")
        assert hashes == [file_sha256(path) for path in paths]
        assert not request.destination.exists()
        assert not dialog._controller._thread.isRunning()
    finally:
        dialog.shutdown()
        assert dialog._controller.close(wait_ms=5000)
        dialog.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        apply_application_theme(app, "light")


def test_workspace_history_selection_finalizes_without_device_commands(shell_qt_application, tmp_path):
    app = shell_qt_application
    _context, expected, paths = multi_sources(tmp_path)
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=False)
    commands = []
    workspace.request_device.connect(lambda *args: commands.append(args))
    destination = tmp_path / "selected-final.h5"
    try:
        workspace.resize(1100, 900)
        workspace.show()
        workspace.select_finalization_action.trigger()
        dialog = workspace._finalization_selection_dialog
        assert dialog is not None and dialog.isVisible()
        workspace.select_finalization_action.trigger()
        assert workspace._finalization_selection_dialog is dialog
        for edit, path in zip(dialog.paths, paths, strict=True):
            edit.setText(str(path))
        dialog.output.setText(str(destination))
        dialog.inspect.click()
        wait_until(app, lambda: not dialog._busy)
        for selector, identity in zip(dialog.profiles, ("before", "before", "after"), strict=True):
            selector.setCurrentIndex(selector.findData(identity))
        dialog.indices.setText("1 2")
        dialog.start.click()
        wait_until(app, lambda: workspace._latest_result is not None, timeout=15)
        assert workspace._finalization_selection_dialog is None
        assert not commands
        assert workspace._latest_result.final and workspace._latest_result.count == 2
        np.testing.assert_allclose(workspace._latest_result.values_w, expected, rtol=1e-12)
        replayed = replay_finalized_artifact(destination)
        np.testing.assert_array_equal(replayed.result.values_w, workspace._latest_result.values_w)
        assert workspace._latest_result.values_w[1] < 0
    finally:
        if workspace._finalization_selection_dialog is not None:
            worker = workspace._finalization_selection_dialog._controller
            assert worker.close(wait_ms=5000)
        assert workspace._cpu.close(wait_ms=5000)
        workspace.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    assert not isValid(workspace)


def test_sources_changed_during_inspection_cannot_publish_stale_choices(
    shell_qt_application, tmp_path, monkeypatch,
):
    app = shell_qt_application
    _context, _expected, paths = multi_sources(tmp_path)
    dialog = SpectrumFinalizationSelectionDialog()
    entered, release = Event(), Event()
    execute = dialog._controller._worker._execute

    def paused(operation, payload):
        if operation == "inspect_finalization":
            entered.set()
            assert release.wait(5)
        return execute(operation, payload)

    monkeypatch.setattr(dialog._controller._worker, "_execute", paused)
    try:
        dialog.show()
        for edit, path in zip(dialog.paths, paths, strict=True):
            edit.setText(str(path))
        dialog.inspect.click()
        assert entered.wait(2)
        # Exercise a programmatic update while normal user controls are disabled.
        dialog.paths[0].setText(str(tmp_path / "changed.h5"))
        release.set()
        wait_until(app, lambda: not dialog._busy)
        assert dialog._loaded_sources is None and not dialog.start.isEnabled()
        assert "Sources changed during inspection" in dialog.status.text()
        assert dialog.inspect.isEnabled()
    finally:
        release.set()
        dialog.shutdown()
        assert dialog._controller.close(wait_ms=5000)
        dialog.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_inspection_error_is_visible_and_retry_remains_available(shell_qt_application, tmp_path):
    app = shell_qt_application
    dialog = SpectrumFinalizationSelectionDialog()
    try:
        dialog.show()
        for edit in dialog.paths:
            edit.setText(str(tmp_path / "missing.h5"))
        dialog.inspect.click()
        wait_until(app, lambda: not dialog._busy)
        assert "inspection failed" in dialog.status.text()
        assert dialog.status.isVisible() and dialog.inspect.isEnabled()
        assert not dialog.start.isEnabled()
        dialog.reject()
        wait_until(app, lambda: not dialog.isVisible())
        assert not dialog._controller._thread.isRunning()
    finally:
        dialog.shutdown()
        assert dialog._controller.close(wait_ms=5000)
        dialog.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
