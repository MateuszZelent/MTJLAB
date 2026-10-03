"""Render the activated calibration alongside voltage control without energizing hardware."""

from dataclasses import replace

import pytest
from PySide6.QtTest import QTest

from app.storage.moke_calibration_store import MokeCalibrationRepository
from app.ui.design_system import apply_application_theme
from tests.test_fluent_moke_field_workflow import (
    application as application,
    workspace as workspace,
    wait_for,
)
from tests.test_moke_calibration import calibration_fixture
from app.devices.moke_box.calibration_runner import MokeCalibrationRunner


@pytest.mark.parametrize("theme,width", [("light", 1360), ("dark", 980)])
def test_active_field_column_is_visible_next_to_voltage_and_tracks_draft(
        workspace, application, theme, width):
    host, page, controllers, directory = workspace
    workflow = page.field_workflow
    # Produce a measured synthetic model with the same binding as the UI profile.
    adapter, reference, request = calibration_fixture()
    profile = workflow._profile
    adapter._config = replace(adapter._config, control_profile=profile)
    request = replace(request,
                      plan=replace(request.plan, profile_fingerprint=profile.fingerprint),
                      context=replace(request.context, profile_fingerprint=profile.fingerprint,
                                      binding_id=profile.binding_id))
    model = MokeCalibrationRunner(adapter, reference, page._settings.moke_box.calibration_directory).run(request, armed=True).model
    repository = MokeCalibrationRepository(page._settings.moke_box.calibration_directory)
    repository.save(model)
    workflow._list_saved_models()
    workflow._refresh_controls()
    page.views.setCurrentIndex(3)
    workflow.load_model_button.click()
    assert workflow._review_model is not None, workflow.calibration_status.text()
    assert workflow._active_model is None  # Saving/reviewing does not activate it.
    workflow.reviewed.setChecked(True)
    workflow.activate_button.click()
    assert workflow._active_model is not None, workflow.calibration_status.text()
    page.views.setCurrentIndex(2)
    apply_application_theme(application, theme)
    host.resize(width, 880)
    host.show()
    application.processEvents()
    QTest.qWait(250)
    assert host.size().width() == width
    assert host.size().height() == 880
    assert workflow.field_card.isVisibleTo(host)
    assert workflow.field_readout.isVisibleTo(host)
    assert workflow.field_readout.width() > 150
    left = workflow.target.mapTo(host, workflow.target.rect().center()).x()
    right = workflow.field_readout.mapTo(host, workflow.field_readout.rect().center()).x()
    assert right > left
    for voltage in (0, .1, -.2):
        workflow.target.setText(f"{voltage:g} V")
        application.processEvents()
        applied = workflow._manual_voltage_plan((voltage,)).applied_voltage(voltage)
        up = model.ascending.estimate(applied)
        down = model.descending.estimate(applied)
        assert f"{up * 1000:+.6g} mT" in workflow.field_readout.text()
        assert f"{down * 1000:+.6g} mT" in workflow.field_readout.text()
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0
    path = directory / f"moke-field-column-{theme}.png"
    assert host.grab().save(str(path))
    print(f"Field column screenshot: {path}")
    # Reconnection must restore the durable active pointer without relying on UI memory.
    controllers["moke_box"].call("disconnect")
    wait_for(application, lambda: not workflow._connected)
    workflow._active_model = None
    controllers["moke_box"].call("connect")
    wait_for(application, lambda: workflow._active_model is not None)
    assert workflow._active_model == model
    assert "B↑" in workflow.field_readout.text()
    workflow.configuration_panel.set_operator_limits("-1 V", "1 V")
    workflow.target.setText("0.8 V")
    assert "unavailable" in workflow.field_readout.text()
    workflow.target.setText("invalid")
    assert "valid voltage" in workflow.field_readout.text()
    assert "invalid" in workflow.field_basis.text()
    workflow.target.setText("0 V")
    workflow.channel_selector.setCurrentIndex(3)
    assert "no field prediction for this output" in workflow.field_readout.text()
    # A damaged old record must be rejected without hiding a healthy calibration.
    repository.save(replace(model, raw_run_id="a" * 32))
    workflow._list_saved_models()
    assert workflow.saved_models.count() == 1
    assert "Rejected 1" in workflow.calibration_status.text()
    apply_application_theme(application, "light")
