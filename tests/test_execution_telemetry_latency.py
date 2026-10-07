"""Regression cases for stale execution state despite a healthy runner."""

from unittest.mock import patch

import pytest

from PySide6.QtWidgets import QApplication, QTreeWidgetItem
from PySide6.QtCore import QPoint

from app.ui.execution import RunMonitorPage
from app.ui.run_worker import RunTelemetryCoalescer


def test_idle_stream_is_flushed_without_another_semantic_event():
    events = []
    with patch("app.ui.run_worker.time.monotonic", return_value=0.0) as clock:
        buffer = RunTelemetryCoalescer(lambda name, data: events.append((name, data)))
        buffer.submit("semantic_operation_applied", {"semantic_id": "set", "action_index": 0})
        buffer.submit("semantic_operation_started", {"semantic_id": "wait", "action_index": 1})
        assert len(events) == 1
        clock.return_value = 0.11
        buffer._flush_due()
        assert events[-1][1]["semantic_id"] == "wait"
        buffer._flush_due()
        assert len(events) == 2


def test_pull_buffer_does_not_enqueue_frames_when_gui_cannot_keep_up():
    events = []
    buffer = RunTelemetryCoalescer(lambda name, data: events.append((name, data)), pull_mode=True)
    with patch("app.ui.run_worker.time.monotonic", return_value=0.) as clock:
        for index in range(10000):
            clock.return_value = index * .1
            buffer.submit("spectrum_preview", {"point_index": index})
            buffer.submit("semantic_operation_applied", {"semantic_id": "axis", "action_index": index})
            buffer.submit("action_finished", {"semantic_id": "axis"})
    assert events == []
    assert len(buffer._pending) == 1
    assert len(buffer._pending_semantic) == 1
    buffer.flush()
    assert len(events) == 2
    assert events[0][1]["point_index"] == 9999
    assert events[1][1]["_coalesced_count"] == 10000
    buffer.submit("semantic_operation_started", {"semantic_id": "axis"})
    buffer.submit("watchdog_timeout", {"node_id": "axis"})
    assert events[-1][0] == "watchdog_timeout"
    buffer.flush()
    assert len(events) == 3


def test_coalesced_repeated_phases_keep_chronological_order():
    events = []
    with patch("app.ui.run_worker.time.monotonic", return_value=0.0):
        buffer = RunTelemetryCoalescer(lambda name, data: events.append((name, data)))
        for action in range(3):
            for phase in ("started", "applied"):
                buffer.submit(f"semantic_operation_{phase}", {"semantic_id": "loop", "action_index": action})
        buffer.flush()
    assert events[-1][0] == "semantic_operation_applied"
    assert events[-1][1]["action_index"] == 2


def test_narrow_workspace_reserves_space_for_both_tree_and_plot():
    app = QApplication.instance() or QApplication([])
    page = RunMonitorPage()
    try:
        page.resize(1360, 880)
        page.show()
        app.processEvents()
        page.resize(820, 700)
        app.processEvents()
        app.processEvents()
        activity = page.activity_splitter
        plot = page.spectrum_preview
        assert activity.height() >= page.measurement_tree.minimumHeight() + page.events.minimumHeight()
        assert plot.height() >= plot.minimumHeight()
        assert activity.mapTo(page, QPoint(0, activity.height())).y() <= plot.mapTo(page, QPoint()).y()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("completed_id", ["set", "wait"])
def test_batch_keeps_completed_progress_and_confirmed_output_while_wait_starts(completed_id):
    app = QApplication.instance() or QApplication([])
    page = RunMonitorPage()
    try:
        page.run_started(100)
        output = QTreeWidgetItem(["Keithley A", "UNKNOWN", "Not confirmed"])
        page._output_items["keithley.A"] = output
        page.queue_semantic_event("semantic_operation_applied", {
            "semantic_id": completed_id, "action_index": 39, "total_actions": 100,
            "kind": "update_keithley_level",
            "state_snapshot": {"output_status": {"keithley.A": "on"}},
        })
        page.queue_semantic_event("semantic_operation_started", {
            "semantic_id": "wait", "action_index": 40, "total_actions": 100,
            "kind": "wait", "duration_s": 5.0,
        })
        page.flush_semantic_states()
        assert page.progress.value() == 40
        assert output.text(1).upper() == "ON"
        assert page.current_operation_state.text() == "WAITING"
        page.append_event("point_stored", {"stored_points": 4})
        assert page.current_operation_state.text() == "WAITING"
        assert page.presentation_buffer.latest_device_snapshot is None
        page.queue_semantic_event("semantic_operation_started", {
            "semantic_id": "wait", "kind": "wait", "action_index": 41,
        })
        page.append_event("semantic_operation_failed", {
            "semantic_id": "wait", "kind": "wait", "action_index": 41,
        })
        page.flush_semantic_states()
        assert page.current_operation_state.text() == "FAILED"
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()
