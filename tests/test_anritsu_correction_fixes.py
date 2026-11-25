"""Regression tests for SpectrumCorrectionWorkspace & SpectrumCorrectionController fixes."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from qfluentwidgets import DropDownPushButton

from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.devices.anritsu_ms2830a.ui.correction_controller import (
    CorrectionSessionRequest,
    SpectrumCorrectionController,
)
from app.domain.spectrum_correction import CorrectionConfig, SpectrumAcquisitionContext
from tests.helpers import simulation_settings
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_correction_store import fixture_profile


@pytest.fixture
def application():
    return QApplication.instance() or QApplication([])


def test_correction_workspace_uses_horizontal_splitter_dropdown_and_idle_timer(application):
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    try:
        # PERF-M1: render timer must stay stopped while idle; PERF-M3: worker thread starts lazily.
        assert not workspace._render_timer.isActive()
        assert not workspace._cpu._thread.isRunning()

        workspace.resize(1000, 700)
        workspace.show()
        application.processEvents()

        assert workspace.workspace_splitter.orientation() == Qt.Orientation.Horizontal
        assert workspace.controls_scroll.height() >= 300
        assert workspace.raw_plot.height() >= 180
        assert workspace.corrected_plot.height() >= 200
        assert isinstance(workspace.offline_analysis_button, DropDownPushButton)
        assert not workspace._render_timer.isActive()
    finally:
        assert workspace.shutdown()
        workspace.close()
        application.processEvents()


def test_correction_worker_isolates_profile_io_failure_from_active_session(application, tmp_path):
    controller = SpectrumCorrectionController()
    completed, failed = [], []
    controller.completed.connect(lambda op, payload: completed.append((op, payload)))
    controller.failed.connect(lambda op, err: failed.append((op, err)))
    context = SpectrumAcquisitionContext([1e6, 2e6, 3e6], "isolation-fixture")
    archive = tmp_path / "active.h5"

    try:
        controller.start_session(
            CorrectionSessionRequest(
                context,
                CorrectionConfig(),
                archive,
                "fixture: true",
                "ANRITSU,SIM",
                reference_state="off resonance",
            )
        )
        wait_until(application, lambda: any(op == "start" for op, _ in completed) or bool(failed))
        assert not failed

        # Public load_profile rejects during an active session without touching the session.
        with pytest.raises(ValueError, match="Stop the active spectrum session"):
            controller.load_profile(tmp_path / "missing.h5")

        # Even if a failing profile I/O op reaches the worker directly, active writer/processor survive!
        controller._submit("import_profile", (tmp_path / "missing.h5", False))
        wait_until(application, lambda: any(op == "import_profile" for op, _ in failed))
        assert controller._worker._writer is not None
        assert controller._worker._processor is not None
        assert controller._session_pending

        controller.stop_session("completed")
        wait_until(application, lambda: any(op == "stop" for op, _ in completed))
        assert not controller._session_pending
    finally:
        assert controller.close(wait_ms=5000)
        controller.deleteLater()
        application.processEvents()


def test_save_profile_guards_busy_state_and_resets_flag_on_sync_error(application, monkeypatch, tmp_path):
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    context, profile = fixture_profile()
    workspace._context = context
    workspace._profile = profile
    try:
        monkeypatch.setattr(
            "app.devices.anritsu_ms2830a.ui.correction_card.StationFileDialog.getSaveFileName",
            lambda *_args, **_kwargs: (str(tmp_path / "out.h5"), "HDF5 (*.h5)"),
        )
        # Guard when already running: must not open file dialog or call save_profile.
        save_mock = MagicMock()
        workspace._cpu.save_profile = save_mock
        workspace._running = True
        workspace._save_profile()
        save_mock.assert_not_called()

        # Synchronous ValueError / BufferError must reset _profile_io_busy back to False.
        workspace._running = False
        workspace._cpu.save_profile = MagicMock(side_effect=BufferError("FIFO full"))
        workspace._save_profile()
        assert not workspace._profile_io_busy
        assert "failed" in workspace.recording_title.text().lower()
    finally:
        assert workspace.shutdown()
        workspace.close()
        application.processEvents()
