"""Scenario review uses the executor's grid and is an actual modal start gate."""

from dataclasses import replace
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from app.devices.keithley_2600.characterization.field_scenario import build_field_scenario
from app.devices.keithley_2600.ui.characterization_card import KeithleyCharacterizationCard
from app.devices.keithley_2600.ui.field_scenario_dialog import FieldScenarioDialog
from app.domain.errors import SafetyViolation
from tests.test_keithley_field_worker import make_worker
from tests.test_keithley_field_series import config


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_scenario_preserves_zero_repeats_and_executor_nonzero_grid(tmp_path):
    worker, device = make_worker(tmp_path)
    config = replace(worker.config, sweep=replace(worker.config.sweep, start_level_si=0))
    scenario = build_field_scenario(config, worker.settings, device.policies, device.policies)
    assert scenario.config is config
    assert scenario.sample_setpoints_si == (1.5e-6, 3e-6)
    targets = scenario.steps[1:5]
    assert len(targets) == 4
    assert "B = 0 A" in targets[0].title and "B = 0 A" in targets[2].title
    assert "configure B at 0 A while OFF" in targets[2].children[0].detail
    assert "2 actual nonzero points" in targets[0].children[4].detail
    assert "both outputs OFF" in scenario.steps[-2].children[0].detail
    device.policies["A"] = "stop"
    assert dict(scenario.initial_policies)["A"] == "warn_clamp"


def test_preview_rejects_invalid_last_target_without_device_calls(tmp_path):
    worker, device = make_worker(tmp_path)
    with pytest.raises(SafetyViolation):
        build_field_scenario(replace(worker.config, currents_a=(0, 100)), worker.settings,
                             device.policies, device.policies)
    assert device.calls == []


def test_worker_blocks_changed_reviewed_config(tmp_path):
    worker, device = make_worker(tmp_path)
    worker.reviewed_scenario = build_field_scenario(worker.config, worker.settings,
                                                    device.policies, device.policies)
    worker.config = replace(worker.config, currents_a=(0,))
    worker.run()
    assert worker.outcome.status == "fault"
    assert any("differs from the reviewed" in error for error in worker.outcome.errors)
    assert device.calls == []


def test_page_reservation_keeps_settings_locked_and_off_available(app, tmp_path):
    from app.devices.keithley_2600.ui.page import KeithleyPage
    worker, _ = make_worker(tmp_path)
    controller = Mock()
    page = KeithleyPage(controller, worker.settings)
    try:
        # Preserve a Live selection without starting actual polling.
        page.live_channel_a.blockSignals(True)
        page.live_channel_a.setChecked(True)
        page.live_channel_a.blockSignals(False)
        page._field_reservation_changed(True)
        controller.call.reset_mock()
        page._project_field_policies({"A": "stop", "B": "stop"})
        page._device_state_changed("VERIFIED")
        page._device_state_changed("OUTPUT_ON")
        page._request_live_measurement()
        assert not page.configuration_panel.isEnabled()
        assert not page.live_channel_a.isEnabled()
        assert not page.live_channel_a.isChecked()
        assert not page._live_timer.isActive()
        for ch in ("A", "B"):
            assert page._characterization_compliance_policy(ch) == "stop"
            assert not page.channel_cards[ch]["compliance_policy_combo"].isEnabled()
            assert not page.channel_cards[ch]["output_on_action"].isEnabled()
            assert page.channel_cards[ch]["output_off_action"].isEnabled()
        controller.call.assert_not_called()
        page._project_field_policies({"A": "warn_clamp", "B": "skip"})
        page._field_reservation_changed(False)
        assert page.configuration_panel.isEnabled()
        assert page.live_channel_a.isChecked()
        assert page._characterization_compliance_policy("A") == "warn_clamp"
        assert page._characterization_compliance_policy("B") == "skip"
    finally:
        page._live_timer.stop()
        page.close()


def test_modal_geometry_cancel_and_explicit_accept(app, tmp_path):
    worker, device = make_worker(tmp_path)
    scenario = build_field_scenario(worker.config, worker.settings, device.policies, device.policies)
    dialog = FieldScenarioDialog(scenario)
    try:
        dialog.show()
        app.processEvents()
        assert dialog.isModal()
        assert dialog.tree.isVisible() and dialog.tree.viewport().width() > 500
        assert dialog.tree.viewport().height() > 300
        assert dialog.tree.topLevelItemCount() == 7
        assert dialog.start_button.isVisible() and dialog.cancel_button.isVisible()
        assert dialog.grab().save(str(tmp_path / "field_scenario_desktop.png"))
        dialog.resize(660, 520)
        app.processEvents()
        assert dialog.tree.viewport().height() > 150
        assert dialog.start_button.mapTo(dialog, dialog.start_button.rect().bottomRight()).x() < dialog.width()
        assert dialog.grab().save(str(tmp_path / "field_scenario_narrow.png"))
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        app.processEvents()
        assert dialog.result() == QDialog.DialogCode.Rejected
        dialog.show()
        app.processEvents()
        dialog.start_button.click()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert device.calls == []
    finally:
        dialog.close()


def make_card(tmp_path):
    template, device = make_worker(tmp_path)
    controller = Mock()
    controller.adapter_for_run.return_value = device
    controller.acquire_run_lease.return_value = device
    device.release = Mock()
    card = KeithleyCharacterizationCard(controller, template.settings)
    card.set_source_request_provider(
        lambda ch, mode, level: replace(device.requests[ch], mode=mode, level_si=level),
        lambda ch: device.policies[ch],
    )
    card.start_level_edit.setText("1 uA")
    card.stop_level_edit.setText("3 uA")
    card.points_spin.setValue(3)
    panel = card.field_panel
    panel.enabled_box.setChecked(True)
    panel.currents.setText("0 A; 12 uA; 0 A; 5 uA")
    panel.ramp_step.setText("5 uA")
    panel.stabilization.setText("0 s")
    panel.hold.setText("10 s")
    card._automatic_run_directory = lambda dataset: tmp_path / "ui_series"
    return card, controller, device


def test_cancelled_modal_never_acquires_or_mutates(app, tmp_path, monkeypatch):
    card, controller, device = make_card(tmp_path)
    seen = []

    def cancel(dialog):
        seen.append(dialog.scenario)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(FieldScenarioDialog, "exec", cancel)
    try:
        card.resize(1440, 960)
        card.show()
        app.processEvents()
        assert card.field_panel.controls.isVisible()
        assert card.field_panel.currents.width() > 100
        assert card.grab().save(str(tmp_path / "field_series_card.png"))
        card._on_start_clicked()
        assert len(seen) == 1
        controller.acquire_run_lease.assert_not_called()
        assert device.calls == []
        assert not (tmp_path / "ui_series").exists()
    finally:
        card.close()


def test_direct_field_start_is_rejected_while_channel_b_is_selected(app, tmp_path):
    card, controller, device = make_card(tmp_path)
    try:
        card.channel_combo.setCurrentText("Channel B")
        app.processEvents()
        assert not card.field_panel.enabled_box.isEnabled()
        assert not card.field_panel.enabled_box.isChecked()
        card._start_field_series()
        controller.adapter_for_run.assert_not_called()
        controller.acquire_run_lease.assert_not_called()
        assert device.calls == []
    finally:
        card.close()


def test_live_validator_blocks_invalid_series_before_hardware_access(app, tmp_path):
    card, controller, device = make_card(tmp_path)
    try:
        app.processEvents()
        assert "Ready:" in card.field_panel.validation_status.text()
        card.field_panel.currents.setText("5 mV, 10 mV")
        app.processEvents()
        assert "Fix before start:" in card.field_panel.validation_status.text()
        assert "current" in card.field_panel.validation_status.text().lower()
        card._on_start_clicked()
        controller.adapter_for_run.assert_not_called()
        controller.acquire_run_lease.assert_not_called()
        assert device.calls == []
    finally:
        card.close()


def test_live_validator_rejects_optimistic_30_second_timeout(app, tmp_path):
    card, controller, device = make_card(tmp_path)
    try:
        card.points_spin.setValue(101)
        card.dwell_edit.setText("100 ms")
        card.field_panel.currents.setText("0 mA; 5 mA; 10 mA")
        card.field_panel.ramp_step.setText("100 uA")
        card.field_panel.stabilization.setText("1 s")
        card.field_panel.hold.setText("30 s")
        app.processEvents()
        assert "enter at least" in card.field_panel.validation_status.text()
        card._on_start_clicked()
        controller.adapter_for_run.assert_not_called()
        controller.acquire_run_lease.assert_not_called()
        assert device.calls == []
    finally:
        card.close()


def test_completion_modal_reports_curves_safety_and_missing_fit_window(
    app, tmp_path, monkeypatch
):
    from app.ui.dialogs import StationMessageBox

    card, _, _ = make_card(tmp_path)
    messages = []
    monkeypatch.setattr(
        StationMessageBox, "information",
        lambda parent, title, text, *args, **kwargs: messages.append((title, text)),
    )
    card._field_worker = SimpleNamespace(
        outcome=SimpleNamespace(
            status="completed", outputs_off=True, policies_restored=True,
            directory=tmp_path / "series",
        ),
        config=replace(config(), currents_a=(0.0, 5e-3, 10e-3),
                       analysis_current_window_a=None),
    )
    card._stored_field_series = SimpleNamespace(curves=(
        SimpleNamespace(status="completed"),
        SimpleNamespace(status="completed"),
        SimpleNamespace(status="sample_compliance"),
    ))
    try:
        card._show_field_completion_dialog([])
        assert len(messages) == 1
        title, text = messages[0]
        assert title == "Field-line characterization completed"
        assert "completed: 2" in text
        assert "A compliance: 1" in text
        assert "confirmed OFF" in text
        assert "no current window was selected" in text
    finally:
        card.close()


def test_confirmed_modal_executes_exact_reviewed_snapshot(app, tmp_path, monkeypatch):
    card, controller, device = make_card(tmp_path)
    from app.ui.dialogs import StationMessageBox
    monkeypatch.setattr(StationMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(StationMessageBox, "warning", lambda *args, **kwargs: None)
    from app.inventory.models import Sample
    from app.inventory.store import InventoryStore
    inventory = InventoryStore(tmp_path / "catalogue.db")
    inventory.save_sample(Sample(sample_id="SERIES-SAMPLE", name="Series sample", rows=("1",), cols=("1",)))
    card.set_inventory_store(inventory)
    seen = []
    from app.devices.keithley_2600.characterization.report_pdf import KeithleyPdfReportGenerator
    generate = KeithleyPdfReportGenerator.generate

    def generate_after_shutdown(*args):
        assert card._field_lease is None
        assert not any(device.outputs.values())
        assert device.policies == {"A": "warn_clamp", "B": "skip"}
        device.release.assert_called_once()
        return generate(*args)

    monkeypatch.setattr(KeithleyPdfReportGenerator, "generate", generate_after_shutdown)

    def accept(dialog):
        seen.append(dialog.scenario)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(FieldScenarioDialog, "exec", accept)
    try:
        card._on_start_clicked()
        deadline = time.monotonic() + 30
        while (card._field_lease is not None or (
            card._field_report_worker is not None and card._field_report_worker.isRunning()
        )) and time.monotonic() < deadline:
            app.processEvents()
            # Yield the GIL to the Python renderer, not only the Qt event loop.
            time.sleep(0.01)
        assert len(seen) == 1
        assert card._field_worker is not None
        assert card._field_worker.config is seen[0].config
        assert card._field_worker.outcome.status == "completed_with_skips"
        assert card._field_lease is None
        assert device.policies == {"A": "warn_clamp", "B": "skip"}
        device.release.assert_called_once()
        manifest = json.loads((tmp_path / "ui_series" / "series.json").read_text(encoding="utf-8"))
        assert manifest["provenance"]["reviewed_scenario"]["config"]["currents_a"] == pytest.approx([0, 12e-6, 0, 5e-6])
        assert card.csv_button.isEnabled()
        app.processEvents()
        assert card._field_report_worker.result is not None, card._field_report_worker.error
        assert not card._field_report_worker.result.errors
        assert card.pdf_button.isEnabled()
        records = inventory.list_runs_for_cell("SERIES-SAMPLE", "1", "1")
        assert len(records) == 3
        assert all(record.report_path and record.csv_path for record in records)
        assert all(record.sample_id == "SERIES-SAMPLE" for record in records)
        assert manifest["provenance"]["inventory_target"]["sample_id"] == "SERIES-SAMPLE"
    finally:
        if card._field_worker is not None and card._field_worker.isRunning():
            card._field_worker.request_stop()
            card._field_worker.wait(3000)
        app.processEvents()
        if card._field_report_worker is not None:
            card._field_report_worker.wait(30000)
        card.close()
        inventory.close()
