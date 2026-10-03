"""Quick Controls share the MOKE page's qualified, authorized ramp worker."""

import pytest

from app.ui.quick_controls import QuickControlCoordinator, QuickControlsWindow
from app.ui.design_system import apply_application_theme
from tests.test_fluent_moke_field_workflow import (
    application as application,
    workspace as workspace,
    wait_for,
)
from tests.test_quick_controls import _FakeController


TARGET = "moke_box.vout2.voltage"


def coordinator_for(host, workflow, settings):
    coordinator = QuickControlCoordinator(
        {"rigol": _FakeController(), "keithley": _FakeController()}, host,
        settings=settings,
    )
    coordinator.bind_moke_workflow(workflow)
    return coordinator


def test_quick_moke_draft_ramp_readback_and_zero(workspace, application):
    host, page, controllers, _ = workspace
    workflow = page.field_workflow
    coordinator = coordinator_for(host, workflow, page._settings)
    states = []
    approvals = []
    coordinator.state_changed.connect(lambda *args: states.append(args))
    workflow._authorize = lambda operation, request: approvals.append(operation)
    coordinator.publish_draft(TARGET, "200 mV", source="quick_controls")
    assert workflow.target.text() == "200 mV"
    assert not approvals
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0
    coordinator.submit(TARGET, "200 mV")
    assert workflow.busy
    wait_for(application, lambda: not workflow.busy)
    assert approvals == ["arm_voltage_plan", "ramp_vout"]
    actual = controllers["moke_box"].adapter_for_run().read_vouts()[2]
    assert actual == pytest.approx(.2, abs=.001)
    assert coordinator._confirmed_values[TARGET] == pytest.approx(actual)
    assert states[-1][1] == "ready"
    workflow.target.setText("100 mV")
    assert coordinator.draft_text(TARGET) == "100 mV"
    coordinator.stop_moke_voltage()
    assert workflow.busy, (states, workflow.manual_status.text())
    wait_for(application, lambda: not workflow.busy)
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0


def test_quick_moke_rejects_limits_channel_authorization_and_reservation(workspace, application):
    host, page, controllers, _ = workspace
    workflow = page.field_workflow
    coordinator = coordinator_for(host, workflow, page._settings)
    states = []
    coordinator.state_changed.connect(lambda *args: states.append(args))
    workflow.configuration_panel.set_operator_limits("-100 mV", "100 mV")
    assert coordinator.bound(TARGET).maximum_si == .1
    coordinator.submit(TARGET, "101 mV")
    assert states[-1][1] == "rejected"
    coordinator.submit("moke_box.vout3.voltage", "50 mV")
    assert states[-1][1] == "rejected"
    workflow.set_execution_controlled(True)
    coordinator.submit(TARGET, "50 mV")
    assert states[-1][1] == "rejected"
    workflow.set_execution_controlled(False)

    def deny(*args):
        from app.domain.errors import ConfigurationError
        raise ConfigurationError("Output authorization denied")

    workflow._authorize = deny
    coordinator.submit(TARGET, "50 mV")
    assert states[-1][1] == "rejected"
    assert not workflow.busy
    assert controllers["moke_box"].adapter_for_run().read_vouts()[2] == 0
    workflow._state_changed("disconnected")
    assert coordinator.bound(TARGET) is None
    coordinator.submit(TARGET, "50 mV")
    assert states[-1][1] == "rejected"


@pytest.mark.parametrize("theme,width", [("light", 550), ("dark", 420)])
def test_quick_moke_rendering(workspace, application, theme, width):
    host, page, _, directory = workspace
    apply_application_theme(application, theme)
    coordinator = coordinator_for(host, page.field_workflow, page._settings)
    window = QuickControlsWindow(coordinator, host)
    from PySide6.QtCore import QSettings
    settings = QSettings("LabControl", "LabControl")
    saved = {key: (settings.contains(key), settings.value(key))
             for key in ("quick_controls/targets", "quick_controls/outputs", "quick_controls/geometry")}
    try:
        window.set_output_targets(())
        window.set_targets((TARGET,))
        window.resize(width, 720)
        window.show()
        application.processEvents()
        row = window._rows[TARGET]
        assert row.isVisibleTo(window)
        assert row.width() > 200 and row.height() > 50
        assert row.slider.width() > 100
        assert window.controls_scroll.horizontalScrollBar().maximum() == 0
        path = directory / f"quick-moke-{theme}.png"
        assert window.grab().save(str(path))
        print(f"MOKE Quick Controls screenshot: {path}")
    finally:
        window.close()
        for key, (existed, value) in saved.items():
            settings.setValue(key, value) if existed else settings.remove(key)
        apply_application_theme(application, "light")
