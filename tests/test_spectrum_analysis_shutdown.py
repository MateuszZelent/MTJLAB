"""Offline workers remain owned until every analysis has drained."""

import time
from threading import Event

from PySide6.QtCore import QEvent
from shiboken6 import isValid

from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from tests.helpers import simulation_settings
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


def test_workspace_waits_for_all_offline_workers_without_orphaning_them(
    shell_qt_application, monkeypatch,
):
    app = shell_qt_application
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    dialogs, releases, controllers = [], [], []
    try:
        workspace.resize(1200, 900)
        workspace.show()
        for opener, attribute in (
            (workspace._open_reference_diagnostics, "_diagnostic_dialog"),
            (workspace._open_model_validation, "_validation_dialog"),
            (workspace._open_model_training, "_training_dialog"),
            (workspace._open_difference_analysis, "_difference_dialog"),
            (workspace._open_resonance_analysis, "_resonance_dialog"),
            (workspace._open_finalization_selection, "_finalization_selection_dialog"),
            (workspace._open_finalization_resume, "_finalization_resume_dialog"),
        ):
            opener()
            dialog = getattr(workspace, attribute)
            dialogs.append(dialog)
            controller = dialog._controller
            controllers.append(controller)
            entered, release = Event(), Event()
            releases.append(release)
            execute = controller._worker._execute

            def blocked(operation, payload, *, execute=execute, entered=entered, release=release):
                if operation == "test_checkpoint":
                    entered.set()
                    assert release.wait(15)
                    return None
                return execute(operation, payload)

            monkeypatch.setattr(controller._worker, "_execute", blocked)
            controller._submit("test_checkpoint")
            assert entered.wait(3)
        app.processEvents()
        assert all(dialog.isVisible() and dialog.width() > 500 for dialog in dialogs)
        started = time.monotonic()
        assert not workspace.shutdown()
        assert time.monotonic() - started < 0.3
        assert not workspace._cpu._thread.isRunning()
        assert all(controller._closing for controller in controllers)
        assert all(controller._processing_cancel.is_set() for controller in controllers)
        assert all(controller._thread.isRunning() for controller in controllers)
        for dialog, controller in zip(dialogs, controllers, strict=True):
            assert isValid(controller) and controller.parent() is dialog
        # Retrying while workers are blocked must use the same live owners.
        assert not workspace.shutdown()
        for release in releases:
            release.set()
        for controller in controllers:
            assert controller._thread.wait(3000)
        app.processEvents()
        assert workspace.shutdown()
        assert all(isValid(controller) for controller in controllers)
    finally:
        for release in releases:
            release.set()
        for controller in controllers:
            assert controller.close(wait_ms=5000)
        assert workspace._cpu.close(wait_ms=5000)
        workspace.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    assert all(not isValid(controller) for controller in controllers)
