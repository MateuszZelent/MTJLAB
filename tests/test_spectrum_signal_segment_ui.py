"""Shown operator reset with real CPU queue and unchanged hardware requests."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import h5py
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.devices.anritsu_ms2830a.ui.correction_controller import CorrectionSessionRequest
from app.domain.spectrum_correction import CorrectionConfig, SweepEvidence, TemporalAverageMode
from app.spectrum.replay import replay_quantitative_session
from app.storage.spectrum_decision_store import iter_decisions
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.test_spectrum_correction_controller import wait_until
from tests.test_spectrum_correction_store import fixture_profile, signal_fixture
from tests.shell_test_isolation import shell_qt_application  # noqa: F401 - strong QApplication/font fixture


@pytest.mark.parametrize("theme,size", [("light", (1100, 850)), ("dark", (760, 700))])
def test_new_signal_segment_keeps_raw_profile_and_replays_without_mixing(tmp_path, theme, size):
    application = QApplication.instance() or QApplication([])
    apply_application_theme(application, theme)
    workspace = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    context, profile = fixture_profile()
    raw, _, _ = signal_fixture(context, profile)
    path = tmp_path / "operator-segments.h5"
    completed, failed = [], []
    workspace._cpu.completed.connect(lambda *args: completed.append(args))
    workspace._cpu.failed.connect(lambda *args: failed.append(args))
    workspace._request = MagicMock()
    workspace._kind, workspace._running = "signal", True
    workspace._context, workspace._profile, workspace._archive_path = context, profile, path
    config = CorrectionConfig(average_mode=TemporalAverageMode.BLOCK)
    workspace._processor_config = config
    workspace.mode.setCurrentIndex(workspace.mode.findData(TemporalAverageMode.BLOCK.value))
    try:
        workspace.resize(*size)
        workspace.show()
        workspace.set_available(False)
        assert not workspace.new_signal_segment.isEnabled()
        workspace._cpu.start_session(CorrectionSessionRequest(context, config, path,
            "fixture: true", "SYNTHETIC;NO_VISA", profile=profile, simulation_mode=True))
        wait_until(application, lambda: workspace._processor_active or bool(failed))
        assert not failed and workspace.new_signal_segment.isEnabled()
        for index in range(2):
            trace = replace(raw, acquired_at_utc=datetime.fromtimestamp(111 + index, timezone.utc),
                            sweep_evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP)
            workspace._ingest(trace)
            wait_until(application, lambda: workspace._committed_count == index + 1 or bool(failed))
        workspace._render()
        assert workspace._latest_result.count == 2
        old_segment = workspace._latest_result.segment_id
        profile_identity = workspace._profile
        device_requests = workspace._request.call_count
        workspace.freeze.setChecked(True)
        workspace.controls_scroll.ensureWidgetVisible(workspace.new_signal_segment)
        application.processEvents()
        center = workspace.new_signal_segment.mapTo(workspace.controls_scroll.viewport(),
                                                    workspace.new_signal_segment.rect().center())
        assert workspace.controls_scroll.viewport().rect().contains(center)
        assert workspace.new_signal_segment.isVisible()
        workspace.new_signal_segment.click()
        assert workspace._segment_reset_pending and not workspace.new_signal_segment.isEnabled()
        assert "Starting a new SIGNAL segment" in workspace.segment_status.text()
        workspace._start_signal_segment()  # A second request during the pending reset is ignored.
        wait_until(application, lambda: not workspace._segment_reset_pending or bool(failed))
        assert not failed and workspace._profile is profile_identity
        assert workspace._archive_path == path and workspace._running
        assert workspace._request.call_count == device_requests
        assert workspace._latest_result is None
        assert workspace.corrected_plot.trace_point_count("Signed residual") == 0
        trace = replace(raw, acquired_at_utc=datetime.fromtimestamp(113, timezone.utc),
                        sweep_evidence=SweepEvidence.QUALIFIED_SINGLE_SWEEP,
                        powers_dbm=tuple(10 * np.log10(profile.mean_w * .9) + 30))
        workspace._ingest(trace)
        wait_until(application, lambda: workspace._committed_count == 3 or bool(failed))
        assert not failed and workspace._latest_result.count == 1
        assert workspace._latest_result.segment_id != old_segment
        assert workspace._latest_result.segment_id.endswith("-segment-1")
        assert workspace.corrected_plot.trace_point_count("Signed residual") == 0  # Still frozen.
        workspace.freeze.setChecked(False)
        np.testing.assert_allclose(workspace.corrected_plot._traces["Signed residual"][1],
                                   -.1 * profile.mean_w, rtol=1e-12)
        directory = Path("artifacts/spectrum-correction-layout")
        directory.mkdir(parents=True, exist_ok=True)
        assert workspace.grab().save(str(directory / f"signal-segment-{theme}-{size[0]}.png"))
        workspace.stop_acquisition()
        assert not workspace.new_signal_segment.isEnabled()
        workspace._cpu.stop_session()
        wait_until(application, lambda: not workspace.running or bool(failed), timeout=30)
        assert not failed
        replayed = list(replay_quantitative_session(path))
        assert [record.result.count for record in replayed] == [1, 2, 1]
        assert replayed[2].envelope.segment_id == workspace._latest_result.segment_id
        with h5py.File(path, "r") as file:
            resets = [record for record in iter_decisions(file) if record["operation"] == "reset_segment"]
            assert len(resets) == 1 and resets[0]["before_point_index"] == 2
            assert len(file["points"]) == 3 and len(file["spectrum_processing_v1/profiles"]) == 1
    finally:
        assert workspace.shutdown()
        workspace.close()
        workspace.deleteLater()
        application.processEvents()
