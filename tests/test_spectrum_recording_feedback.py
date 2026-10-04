"""A real button click exposes missing inputs without starting acquisition."""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace, StationFileDialog
from app.ui.design_system import apply_application_theme
from tests.helpers import simulation_settings
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


@pytest.fixture
def workspace(shell_qt_application, monkeypatch, tmp_path):
    application = shell_qt_application
    widget = SpectrumCorrectionWorkspace(simulation_settings(), single_sweep_available=True)
    calls = {"files": [], "device": [], "path": tmp_path / "alternating.h5"}

    def choose(*args):
        calls["files"].append(args)
        return str(calls["path"]), ""

    monkeypatch.setattr(StationFileDialog, "getSaveFileName", choose)
    widget.request_device.connect(lambda *args: calls["device"].append(args))
    widget.set_available(True)
    widget.resize(1100, 900)
    widget.show()
    application.processEvents()
    try:
        yield application, widget, calls
    finally:
        if widget.running:
            widget.stop_acquisition()
        assert widget.shutdown()
        widget.close()
        widget.deleteLater()
        application.processEvents()


def click_record(application, widget):
    widget.controls_scroll.ensureWidgetVisible(widget.acquire_interleaved)
    application.processEvents()
    position = widget.acquire_interleaved.mapTo(widget.controls_scroll.viewport(), widget.acquire_interleaved.rect().center())
    assert widget.controls_scroll.viewport().rect().contains(position)
    QTest.mouseClick(widget.acquire_interleaved, Qt.MouseButton.LeftButton)
    application.processEvents()


@pytest.mark.parametrize("theme,size", [("light", (1100, 900)), ("dark", (760, 800))])
def test_missing_signal_is_visible_focused_and_not_a_silent_button(workspace, theme, size):
    application, widget, calls = workspace
    apply_application_theme(application, theme)
    widget.resize(*size)
    widget.reference_state.setText("operator REF state")
    assert not widget.signal_state.text()
    click_record(application, widget)
    assert not widget.running and not calls["files"] and not calls["device"] and not calls["path"].exists()
    assert QApplication.focusWidget() is widget.signal_state
    position = widget.signal_state.mapTo(widget.controls_scroll.viewport(), widget.signal_state.rect().center())
    assert widget.controls_scroll.viewport().rect().contains(position)
    assert "SIGNAL" in widget.state_label.text()
    assert widget._start_feedback._active_bar.isVisible()
    QTest.qWait(250)
    folder = Path("artifacts/spectrum-correction-layout")
    folder.mkdir(parents=True, exist_ok=True)
    assert widget.grab().save(str(folder / f"record-alternating-missing-signal-{theme}-{size[0]}.png"))


@pytest.mark.parametrize("field,value,expected", [
    ("reference_state", "", "reference state description"),
    ("interleaved_ref_duration", "1 Hz", "Alternating REF duration"),
    ("interleaved_signal_duration", "-1 s", "Alternating SIGNAL duration"),
    ("tau", "1 Hz", "Preview time constant"),
])
def test_invalid_input_brings_the_actual_field_into_view_before_file_selection(workspace, field, value, expected):
    application, widget, calls = workspace
    widget.reference_state.setText("operator REF state")
    widget.signal_state.setText("operator SIGNAL state")
    getattr(widget, field).setText(value)
    click_record(application, widget)
    editor = getattr(widget, field)
    assert QApplication.focusWidget() is editor
    position = editor.mapTo(widget.controls_scroll.viewport(), editor.rect().center())
    assert widget.controls_scroll.viewport().rect().contains(position)
    assert expected in widget.state_label.text()
    assert widget._start_feedback._active_bar.isVisible()
    assert not widget.running and not calls["files"] and not calls["device"] and not calls["path"].exists()


def test_valid_button_selects_one_archive_and_shows_the_required_confirmation(workspace):
    application, widget, calls = workspace
    widget.reference_state.setText("operator REF state")
    widget.signal_state.setText("operator SIGNAL state")
    # Manual REF duration does not belong to the alternating workflow.
    widget.duration.setText("not a time")
    click_record(application, widget)
    assert len(calls["files"]) == 1 and widget.running
    assert "prepare REFERENCE" in widget.recording_title.text()
    assert widget.confirm_interleaved_state.isVisible() and not widget.confirm_interleaved_state.isEnabled()
    assert str(calls["path"]) in widget.archive_label.text()
    assert not calls["device"] and not calls["path"].exists()


def test_canceling_archive_selection_has_explicit_feedback(workspace, monkeypatch):
    application, widget, calls = workspace
    widget.reference_state.setText("operator REF state")
    widget.signal_state.setText("operator SIGNAL state")
    monkeypatch.setattr(StationFileDialog, "getSaveFileName", lambda *args: ("", ""))
    click_record(application, widget)
    assert "canceled" in widget.recording_title.text()
    assert "not started" in widget.state_label.text()
    assert not widget.running and not calls["device"] and not calls["path"].exists()


def test_correcting_a_missing_input_clears_the_warning_and_starts_preparation(workspace):
    application, widget, calls = workspace
    widget.reference_state.setText("operator REF state")
    click_record(application, widget)
    assert widget._start_feedback.last_message and not calls["files"]
    widget.signal_state.setText("operator SIGNAL state")
    click_record(application, widget)
    assert widget.running and len(calls["files"]) == 1
    assert widget._start_feedback.last_message == "" and widget._start_feedback._active_bar is None
    assert "prepare REFERENCE" in widget.recording_title.text()
    assert not calls["device"] and not calls["path"].exists()


@pytest.mark.parametrize("state,expected", [
    ("unavailable", "another workflow"), ("unqualified", "single-sweep protocol"),
])
def test_unavailable_acquisition_has_a_reason_and_never_selects_an_archive(workspace, state, expected):
    application, widget, calls = workspace
    if state == "unavailable":
        widget.set_available(False)
    else:
        widget._single_sweep_available = False
        widget.set_available(True)
    assert not widget.acquire_interleaved.isEnabled() and widget.acquire_interleaved.toolTip()
    widget._start_dialog("interleaved")
    application.processEvents()
    assert expected in widget.state_label.text() and widget._start_feedback._active_bar.isVisible()
    assert not widget.running and not calls["files"] and not calls["device"] and not calls["path"].exists()
