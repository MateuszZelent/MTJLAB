"""Alternating operator-confirmed blocks through Qt, simulated VISA and HDF5."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json

import h5py
import numpy as np
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.anritsu_ms2830a.module import MODULE
from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
from app.devices.simulators import SimulatedVisaFactory
from app.spectrum.interleaved_acquisition import InterleavedPhase
from app.spectrum.replay import replay_quantitative_session
from app.settings.models import StationSettings
from app.storage.finalized_spectrum_store import (finalize_spectrum_archives, replay_finalized_artifact,
    file_sha256, inspect_interleaved_blocks, inspect_finalization_sources)
from app.domain.errors import ExecutionError
from app.storage.spectrum_decision_store import iter_decisions
from app.storage.spectrum_correction_codec import read_profile, write_profile
from app.storage.thatec_validator import ThatecCompatibilityValidator
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.test_spectrum_correction_controller import wait_until
from tests.shell_test_isolation import shell_qt_application  # noqa: F401


@pytest.fixture
def acquisition(tmp_path):
    application = QApplication.instance() or QApplication([])
    settings = simulation_settings()
    payload = settings.model_dump()
    payload["devices"]["anritsu"]["spectrum_correction"]["reference_policy"]["maximum_reference_blocks"] = 3
    settings = StationSettings.model_validate(payload)
    adapter = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
    adapter.connect()
    workspace = SpectrumCorrectionWorkspace(settings, single_sweep_available=True, simulation_mode=True)
    workspace.reference_state.setText("synthetic REF; same RF path, no magnetic injection")
    workspace.signal_state.setText("synthetic SIGNAL with persistent overlapping weak resonance")
    workspace.interleaved_ref_duration.setText("250 ms")
    workspace.interleaved_signal_duration.setText("1.1 s")
    workspace.interleaved_minimum_sweeps.setValue(2)
    workspace.mode.setCurrentIndex(workspace.mode.findData("block"))
    workspace.set_available(True, device_idn="ANRITSU,SYNTHETIC")
    state = {"requests": [], "errors": [], "results": [], "frame": 0, "desired": None,
             "path": tmp_path / "alternating.h5", "change_settings": False, "stop_during_first_sweep": False}
    workspace.display_changed.connect(lambda _context, result, _description:
        state["results"].append(result) if result is not None else None)
    workspace._cpu.failed.connect(lambda *args: state["errors"].append(args))

    def request(operation, payload):
        def deliver():
            try:
                state["requests"].append(operation)
                result = MODULE.dispatch(adapter, operation, payload)
                if operation == "read_full_configuration" and state["change_settings"]:
                    result = replace(result, rbw_hz=result.rbw_hz * 2)
                if operation == "single_sweep":
                    size = len(result.frequencies_hz)
                    x = np.linspace(-1, 1, size)
                    background = 2e-11 + 1e-10 * np.exp(-((x + .35) / .02) ** 2)
                    desired = (1e-12 * np.exp(-((x + .35) / .035) ** 2)
                               - 5e-13 * np.exp(-((x - .3) / .04) ** 2))
                    state["desired"] = desired
                    cycle = workspace._interleaved
                    gain = (1, 1.3, .7)[cycle.reference_blocks if workspace._kind == "reference"
                                           else cycle.reference_blocks - 1]
                    powers = (background * gain * (.99 if cycle.count == 0 else 1.01)
                              if workspace._kind == "reference" else background * gain + desired)
                    started = datetime.fromtimestamp(1900000000 + state["frame"], timezone.utc)
                    completed = datetime.fromtimestamp(started.timestamp() + .5, timezone.utc)
                    result = replace(result, powers_dbm=tuple(10 * np.log10(powers) + 30),
                        acquired_at_utc=completed, acquisition_started_at_utc=started,
                        acquisition_completed_at_utc=completed)
                    state["frame"] += 1
                    if state["stop_during_first_sweep"]:
                        workspace.stop_acquisition()
                workspace.handle_result(operation, result)
            except Exception as exc:
                state["errors"].append((operation, str(exc)))
                workspace.handle_error(operation, str(exc))
        QTimer.singleShot(0, deliver)

    workspace.request_device.connect(request)
    try:
        yield application, workspace, state
    finally:
        assert workspace.shutdown()
        workspace.close()
        workspace.deleteLater()
        adapter.disconnect()
        application.processEvents()


def confirm_and_wait(application, workspace, state):
    workspace.stable_state.setChecked(True)
    workspace.confirm_interleaved_state.click()
    wait_until(application, lambda: (not workspace.running or bool(state["errors"])
               or workspace._interleaved.waiting_role is not None), timeout=30)
    assert not state["errors"]


@pytest.mark.parametrize("theme,size", [("light", (1100, 900)), ("dark", (760, 800))])
def test_alternating_refresh_preserves_overlapping_signal_archives_and_replays(acquisition, tmp_path, theme, size):
    application, workspace, state = acquisition
    apply_application_theme(application, theme)
    workspace.resize(*size)
    workspace.show()
    application.processEvents()
    workspace.start_acquisition("interleaved", state["path"])
    assert workspace.running and workspace._interleaved.phase == InterleavedPhase.WAIT_REFERENCE
    assert "prepare REFERENCE" in workspace.recording_title.text()
    assert workspace.confirm_interleaved_state.isVisible() and not workspace.confirm_interleaved_state.isEnabled()
    workspace.confirm_interleaved_state.click()
    application.processEvents()
    assert not state["requests"] and not state["path"].exists()
    for block in range(5):
        confirm_and_wait(application, workspace, state)
        if block == 1:
            assert workspace._interleaved.phase == InterleavedPhase.WAIT_REFERENCE
            assert workspace._latest_result.count == 2
            assert "Previous SIGNAL block" in workspace.frame_label.text()
            assert not workspace.new_signal_segment.isEnabled()
            for widget in (workspace.recording_status, workspace.confirm_interleaved_state,
                           workspace.corrected_plot, workspace.stop):
                assert widget.isVisible() and widget.width() > 50 and widget.height() > 10
            folder = Path("artifacts/spectrum-correction-layout")
            folder.mkdir(parents=True, exist_ok=True)
            assert workspace.grab().save(str(folder / f"interleaved-{theme}-{size[0]}.png"))
    assert not workspace.running and workspace._interleaved.reference_blocks == 3
    assert state["frame"] == 10
    assert set(state["requests"]) == {"read_full_configuration", "read_advanced_spectrum", "single_sweep"}
    assert state["requests"].count("read_full_configuration") == 5
    records = list(replay_quantitative_session(state["path"]))
    results = [record.result for record in records if record.result is not None]
    assert [result.count for result in results] == [1, 2, 1, 2]
    assert results[0].segment_id != results[2].segment_id
    assert results[0].profile_weights != results[2].profile_weights
    for result in results:
        np.testing.assert_allclose(result.values_w, state["desired"], rtol=1e-10, atol=1e-24)
        assert result.standard_uncertainty_w is None
        assert np.any(result.values_w < 0)
    with h5py.File(state["path"], "r") as file:
        assert file["run"].attrs["status"] == "completed"
        assert len(file["points"]) == 10 and not len(file["_pending"])
        assert len(file["spectrum_processing_v1/profiles"]) == 3
        decisions = list(iter_decisions(file))
        assert sum(record["operation"] == "finish_reference" for record in decisions) == 3
        reports = [json.loads(value) for name, value in zip(file["events/name"].asstr()[:],
            file["events/message"].asstr()[:]) if name == "spectrum_operator_state_confirmed"]
        assert len(reports) == 5
        for report in reports:
            payload = report.get("data", report)
            assert not payload["hardware_readback_verified"] and not payload["signal_free_qualified"]
        profiles = sorted((read_profile(group)[1] for group in
            file["spectrum_processing_v1/profiles"].values()), key=lambda row: row.started_at_s)
    report = ThatecCompatibilityValidator().validate(state["path"], require_pythat=True)
    assert report.valid, report.errors
    output = tmp_path / "bracketed.h5"
    block = finalize_spectrum_archives(state["path"], state["path"], state["path"], output,
        point_indices=(2, 3), before_profile_id=profiles[0].profile_id,
        after_profile_id=profiles[1].profile_id, signal_profile_id=profiles[0].profile_id)
    np.testing.assert_array_equal(replay_finalized_artifact(output).result.values_w, block.result.values_w)


def test_stop_before_confirmation_creates_no_archive_or_instrument_request(acquisition):
    application, workspace, state = acquisition
    workspace.start_acquisition("interleaved", state["path"])
    workspace.stop_acquisition()
    application.processEvents()
    assert not workspace.running and not state["path"].exists() and not state["requests"]
    assert "No archive was opened" in workspace.archive_label.text()


def test_changed_analyzer_settings_between_blocks_stop_before_a_new_sweep(acquisition):
    application, workspace, state = acquisition
    workspace.start_acquisition("interleaved", state["path"])
    confirm_and_wait(application, workspace, state)
    assert state["frame"] == 2
    state["change_settings"] = True
    workspace.stable_state.setChecked(True)
    workspace.confirm_interleaved_state.click()
    wait_until(application, lambda: not workspace.running, timeout=30)
    wait_until(application, lambda: not workspace._cpu._session_pending, timeout=30)
    assert "Analyzer settings changed" in workspace.state_label.text()
    assert state["frame"] == 2
    with h5py.File(state["path"], "r") as file:
        assert file["run"].attrs["status"] == "faulted" and len(file["points"]) == 2


def test_stop_during_ref_preserves_the_pending_raw_and_does_not_publish_partial_background(acquisition):
    application, workspace, state = acquisition
    state["stop_during_first_sweep"] = True
    workspace.start_acquisition("interleaved", state["path"])
    confirm_and_wait(application, workspace, state)
    assert not workspace.running and workspace._profile is None
    assert state["frame"] == 1
    with h5py.File(state["path"], "r") as file:
        assert len(file["points"]) == 1 and file["run"].attrs["status"] == "aborted"
        assert "spectrum_processing_v1/profiles" not in file
        assert not len(file["_pending"])
    records = list(replay_quantitative_session(state["path"]))
    assert len(records) == 1 and records[0].result is None


def closed_aborted_cycle(application, workspace, state):
    workspace.start_acquisition("interleaved", state["path"])
    for _ in range(3):  # REF / SIGNAL / REF, with one bracketed SIGNAL block.
        confirm_and_wait(application, workspace, state)
    workspace.stop_acquisition()
    wait_until(application, lambda: not workspace.running, timeout=30)
    with h5py.File(state["path"], "r") as file:
        assert file["run"].attrs["status"] == "aborted"


def test_completed_ref_blocks_in_operator_stopped_archive_can_be_finalized_read_only(acquisition, tmp_path):
    application, workspace, state = acquisition
    closed_aborted_cycle(application, workspace, state)
    identity = file_sha256(state["path"])
    path, blocks = inspect_interleaved_blocks(state["path"])
    assert path == state["path"].resolve() and len(blocks) == 1
    block = blocks[0]
    assert (block.start_point, block.stop_point) == (2, 3) and block.after_profile_id is not None
    assert (block.started_at_s, block.completed_at_s) == (1900000002, 1900000003.5)
    output = tmp_path / "stopped-final.h5"
    result = finalize_spectrum_archives(path, path, path, output,
        point_indices=tuple(range(block.start_point, block.stop_point + 1)),
        before_profile_id=block.before_profile_id, after_profile_id=block.after_profile_id,
        signal_profile_id=block.before_profile_id)
    assert result.result.final and result.result.standard_uncertainty_w is None
    assert file_sha256(state["path"]) == identity
    assert ThatecCompatibilityValidator().validate(output, require_pythat=True).valid


@pytest.mark.parametrize("corruption", ["raw", "history"])
def test_corrupted_ref_evidence_is_rejected_before_creating_a_final_artifact(acquisition, tmp_path, corruption):
    application, workspace, state = acquisition
    closed_aborted_cycle(application, workspace, state)
    _path, blocks = inspect_interleaved_blocks(state["path"])
    block = blocks[0]
    with h5py.File(state["path"], "r+") as file:
        if corruption == "raw":
            file["spectra/0/power_dbm"][0] += 1
        else:
            file["spectrum_processing_v1/decisions/records/00000001"].attrs["sha256"] = "0" * 64
    output = tmp_path / "invalid-final.h5"
    with pytest.raises(ExecutionError):
        finalize_spectrum_archives(state["path"], state["path"], state["path"], output,
            point_indices=(2, 3), before_profile_id=block.before_profile_id,
            after_profile_id=block.after_profile_id, signal_profile_id=block.before_profile_id)
    assert not output.exists()


def test_finalization_dialog_selects_recorded_block_and_profiles_from_one_archive(acquisition, tmp_path):
    from app.devices.anritsu_ms2830a.ui.finalization_selection_dialog import SpectrumFinalizationSelectionDialog

    application, workspace, state = acquisition
    closed_aborted_cycle(application, workspace, state)
    dialog = SpectrumFinalizationSelectionDialog(workspace, initial_source_path=state["path"])
    dialog.show()
    try:
        wait_until(application, lambda: dialog.interleaved_blocks.count() == 2 or "failed" in dialog.status.text(), timeout=30)
        assert dialog.interleaved_blocks.count() == 2, dialog.status.text()
        dialog.interleaved_blocks.setCurrentIndex(1)
        assert dialog.indices.text() == "2:4"
        dialog.output.setText(str(tmp_path / "selected-final.h5"))
        request = dialog._selected_block()
        assert request.point_indices == (2, 3)
        assert request.signal_path == request.before_path == request.after_path
        assert request.before_profile_id == request.signal_profile_id
        assert request.after_profile_id != request.before_profile_id
        assert dialog.start.isEnabled()
    finally:
        dialog.reject()
        wait_until(application, lambda: dialog._controller.close(wait_ms=0), timeout=30)
        application.processEvents()


def test_orphan_profile_is_not_selectable_or_finalizable_after_an_interrupted_commit(acquisition, tmp_path):
    application, workspace, state = acquisition
    closed_aborted_cycle(application, workspace, state)
    with h5py.File(state["path"], "r+") as file:
        root = file["spectrum_processing_v1/profiles"]
        context, profile = read_profile(next(iter(root.values())))
        orphan = replace(profile, reference_state="uncommitted REF completion")
        orphan = replace(orphan, profile_id=orphan.content_hash)
        group = root.create_group(orphan.profile_id)
        write_profile(group, context, orphan)
        group.attrs["complete"] = True
    identity = file_sha256(state["path"])
    summaries = inspect_finalization_sources((state["path"],) * 3)
    for _path, choices, _count in summaries:
        assert len(choices) == 2 and orphan.profile_id not in {choice[0] for choice in choices}
    _path, blocks = inspect_interleaved_blocks(state["path"])
    assert len(blocks) == 1 and blocks[0].after_profile_id != orphan.profile_id
    output = tmp_path / "orphan-final.h5"
    with pytest.raises(ExecutionError, match="completion decision"):
        finalize_spectrum_archives(state["path"], state["path"], state["path"], output,
            point_indices=(2, 3), before_profile_id=blocks[0].before_profile_id,
            after_profile_id=orphan.profile_id, signal_profile_id=blocks[0].before_profile_id)
    assert not output.exists() and file_sha256(state["path"]) == identity


def test_unbracketed_last_signal_is_visible_but_cannot_be_finalized(acquisition):
    from app.devices.anritsu_ms2830a.ui.finalization_selection_dialog import SpectrumFinalizationSelectionDialog

    application, workspace, state = acquisition
    workspace.start_acquisition("interleaved", state["path"])
    for _ in range(2):
        confirm_and_wait(application, workspace, state)
    workspace.stop_acquisition()
    wait_until(application, lambda: not workspace.running, timeout=30)
    _path, blocks = inspect_interleaved_blocks(state["path"])
    assert len(blocks) == 1 and blocks[0].after_profile_id is None
    dialog = SpectrumFinalizationSelectionDialog(workspace, initial_source_path=state["path"])
    dialog.show()
    try:
        wait_until(application, lambda: dialog.interleaved_blocks.count() == 2 or "failed" in dialog.status.text(), timeout=30)
        assert dialog.interleaved_blocks.count() == 2, dialog.status.text()
        dialog.interleaved_blocks.setCurrentIndex(1)
        assert "no completed REF after" in dialog.status.text()
        assert not dialog.start.isEnabled()
        with pytest.raises(ValueError, match="explicitly choose every profile"):
            dialog._selected_block()
        dialog._failed("inspect_interleaved_blocks", "test interrupted inspection")
        assert dialog.interleaved_blocks.count() == 0 and not dialog.interleaved_blocks.isEnabled()
    finally:
        dialog.reject()
        wait_until(application, lambda: dialog._controller.close(wait_ms=0), timeout=30)
        application.processEvents()


@pytest.mark.parametrize("start_action", ["single", "live"])
def test_main_background_action_uses_the_selected_interleaved_policy(tmp_path, monkeypatch, start_action):
    from unittest.mock import MagicMock

    from app.devices.anritsu_ms2830a.ui.correction_card import StationFileDialog
    from app.devices.anritsu_ms2830a.ui.page import AnritsuPage, AnritsuPageState

    application = QApplication.instance() or QApplication([])
    payload = simulation_settings().model_dump()
    payload["devices"]["anritsu"]["spectrum_correction"]["reference_policy"]["mode"] = "interleaved"
    settings = StationSettings.model_validate(payload)
    controller = MagicMock()
    controller.is_connected = True
    controller.visa_address = "SIM::ANRITSU"
    path = tmp_path / "main-alternating.h5"
    monkeypatch.setattr(StationFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    page = AnritsuPage(controller, settings, single_sweep_available=True)
    try:
        page.resize(1500, 900)
        page.show()
        application.processEvents()
        page._set_page_state(AnritsuPageState.IDLE)
        page.current_spectrum_view.setCurrentIndex(page.current_spectrum_view.findData("background"))
        workspace = page.correction_workspace
        workspace.reference_state.setText("REF sample state reported by operator")
        workspace.signal_state.setText("SIGNAL sample state reported by operator")
        assert "alternating REF / SIGNAL" in getattr(page, start_action).text()
        getattr(page, start_action).click()
        application.processEvents()
        assert page.analysis_tabs.currentIndex() == 2 and workspace.running
        assert workspace._interleaved.phase == InterleavedPhase.WAIT_REFERENCE
        assert workspace.confirm_interleaved_state.isVisible()
        assert workspace.confirm_interleaved_state.width() > 50
        assert workspace.confirm_interleaved_state.height() > 10
        assert "prepare REFERENCE" in workspace.recording_title.text()
        assert not path.exists()
        controller.call.assert_not_called()
        workspace.stop_acquisition()
        application.processEvents()
        assert not workspace.running and not path.exists()
    finally:
        assert page.correction_workspace.shutdown()
        page.close()
        page.deleteLater()
        application.processEvents()
