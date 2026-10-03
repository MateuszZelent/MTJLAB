"""Explicit MOKE keyboard steps stay independent of notation and output limits."""

from decimal import Decimal
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QRect, QSettings, Qt
from PySide6.QtTest import QTest

from app.devices.moke_box.protocol import MokeCommandType
from app.domain.quantities import DIMENSION_VOLTAGE, parse_quantity
from app.domain.quick_controls import quantity_step_si, step_quantity_text
from app.ui.common.precision_stepper import install_precision_arrow_stepper
from app.ui.design_system import apply_application_theme
from app.ui.quick_controls import QuickControlsWindow
from app.ui.widgets.quick_quantity_slider import QuantitySliderMapping
from app.ui.widgets.quick_quantity_slider import QuickQuantitySlider
from app.recipes.parameter_registry import QUICK_CONTROLS_BY_TARGET
from app.safety.quick_controls import QuickControlSafetyBound
from tests.test_fluent_moke_field_workflow import application as application, workspace as workspace, wait_for
from tests.test_moke_startup_and_floating import existing_outputs as existing_outputs


@pytest.mark.parametrize("draft,expected", [
    ("0 mV", "1 mV"), ("0 V", "0.001 V"), ("0.00 V", "0.001 V"),
    ("0,00 V", "0,001 V"), ("1e-2 V", "1.1e-2 V"),
    ("0.00000 V", "0.00100 V"), ("-2 mV", "-1 mV"),
])
def test_explicit_step_preserves_unit_and_value(draft, expected):
    result, value_v = step_quantity_text(draft, DIMENSION_VOLTAGE, 1, step_text="1 mV")
    assert result == expected
    assert value_v == pytest.approx(parse_quantity(draft, DIMENSION_VOLTAGE).si_value + .001)
    assert quantity_step_si(draft, DIMENSION_VOLTAGE, step_text="1 mV") == .001


@pytest.mark.parametrize("invalid", ["0 mV", "-1 mV", "nan V", "inf V", "1 mA", "1"])
def test_invalid_explicit_steps_fail_closed(invalid):
    with pytest.raises(ValueError):
        step_quantity_text("0 V", DIMENSION_VOLTAGE, 1, step_text=invalid)


def test_partial_slider_endpoint_still_displays_exact_readback():
    mapping = QuantitySliderMapping(-.5, .750145, .001)
    assert mapping.value_for_position(mapping.position_for_value(mapping.maximum_si)) == mapping.maximum_si


def test_slider_arrows_use_exact_step_with_nondivisible_ranges(workspace, application):
    host, page, _, _ = workspace
    workflow = page.field_workflow
    page.views.setCurrentIndex(2)
    workflow.configuration_panel.set_operator_limits("-0.23 V", "0.37 V")
    workflow.target.setText("0 V")
    QTest.keyClick(workflow.voltage_slider, Qt.Key.Key_Right)
    assert workflow._voltage(workflow.target) == .001
    workflow.configuration_panel.voltage_step.combo.setCurrentText("5 mV")
    QTest.keyClick(workflow.voltage_slider, Qt.Key.Key_Right)
    assert workflow._voltage(workflow.target) == .006
    widget = QuickQuantitySlider(target="moke_box.vout2.voltage",
        descriptor=QUICK_CONTROLS_BY_TARGET["moke_box.vout2.voltage"], parent=host)
    widget.set_bounds(QuickControlSafetyBound(-.23, .37, "-0.23 V", "0.37 V"))
    widget.set_value_text("0 V")
    widget.set_step_text("1 mV")
    QTest.keyClick(widget.slider, Qt.Key.Key_Right)
    assert widget.value_si() == .001
    QTest.keyClick(widget.slider, Qt.Key.Key_Left)
    assert widget.value_si() == 0
    assert not workflow.busy


def test_steps_are_per_channel_and_shared_with_floating_controls(existing_outputs, application):
    _, page, controller, _, sent, _, coordinator = existing_outputs
    workflow = page.field_workflow
    controller.call("connect")
    wait_for(application, lambda: len(workflow._initialized_voltage_channels) == 8)
    install_precision_arrow_stepper(application)
    selector = workflow.configuration_panel.voltage_step
    assert selector.step_text() == "1 mV"
    workflow.target.setText("2 V")
    workflow.target.setFocus()
    selector.combo.setFocus()
    selector.combo.setCurrentText("5 mV")
    assert workflow.target.text() == "2 V"
    workflow.target.setText("0.00 V")
    assert workflow.target.text() == "0.00 V"
    assert coordinator.step_text("moke_box.vout0.voltage") == "5 mV"
    assert all(frame.record_type == MokeCommandType.READBACK_VOUT for frame in sent)
    workflow.channel_selector.setCurrentIndex(workflow.channel_selector.findData(2))
    assert selector.step_text() == "1 mV"
    selector.combo.setCurrentText("10 mV")
    workflow.channel_selector.setCurrentIndex(workflow.channel_selector.findData(0))
    assert selector.step_text() == "5 mV"
    workflow.open_floating_button.click()
    window = page._control_window
    window.show_history.setChecked(False)
    QTest.qWait(100)
    assert selector.isVisibleTo(window)
    workflow.target.setFocus()
    QTest.keyClick(workflow.target, Qt.Key.Key_Up)
    assert workflow.target.text() == "0.005 V"
    window.close()
    assert workflow.configuration_panel.voltage_step is selector
    assert selector.step_text() == "5 mV"
    workflow.target.setText("0.499 V")
    QTest.keyClick(workflow.target, Qt.Key.Key_Up)
    assert workflow._voltage(workflow.target) == .5
    QTest.qWait(550)
    assert all(frame.record_type == MokeCommandType.READBACK_VOUT for frame in sent)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_quick_step_selection_sync_and_focus_send_no_voltage(existing_outputs, application, theme):
    _, page, controller, _, sent, _, coordinator = existing_outputs
    workflow = page.field_workflow
    controller.call("connect")
    wait_for(application, lambda: len(workflow._initialized_voltage_channels) == 8)
    workflow.target.setText("0.00 V")
    settings = QSettings("LabControl", "LabControl")
    keys = ("quick_controls/targets", "quick_controls/outputs", "quick_controls/geometry")
    saved = {key: (settings.contains(key), settings.value(key)) for key in keys}
    apply_application_theme(application, theme)
    window = QuickControlsWindow(coordinator, page)
    try:
        window.set_output_targets(())
        window.set_targets(("moke_box.vout0.voltage", "moke_box.vout2.voltage"))
        window.resize(420, 760)
        window.show()
        QTest.qWait(100)
        row = window._rows["moke_box.vout0.voltage"]
        other = window._rows["moke_box.vout2.voltage"]
        assert row.voltage_step.step_text() == "1 mV"
        local_commits = []
        row.submit_requested.connect(lambda *args: local_commits.append(args))
        row.value.setFocus()
        row.voltage_step.combo.setFocus()
        row.voltage_step.combo.setCurrentText("5 mV")
        QTest.qWait(550)
        assert not local_commits and not workflow.busy
        assert all(frame.record_type == MokeCommandType.READBACK_VOUT for frame in sent)
        row.value.setFocus()
        row.value.setText("2 V")  # Invalid draft must not become a clamped output on focus transfer.
        row.voltage_step.combo.setFocus()
        row.voltage_step.combo.setCurrentText("10 mV")
        row.voltage_step.combo.setCurrentText("5 mV")
        QTest.qWait(550)
        assert row.value.text() == "2 V" and workflow.target.text() == "2 V"
        assert workflow.configuration_panel.voltage_step.step_text() == "5 mV"
        assert other.voltage_step.step_text() == "1 mV"
        assert not workflow.busy
        assert not local_commits
        assert all(frame.record_type == MokeCommandType.READBACK_VOUT for frame in sent)
        row.set_value_text("0.00 V")
        assert row.slider.step_si == .005
        for control in (row.value, row.voltage_step.combo, row.increase, row.decrease):
            assert control.isVisibleTo(window)
            assert window.rect().contains(QRect(control.mapTo(window, QPoint()), control.size()))
        assert row.value.width() >= 65
        assert window.controls_scroll.horizontalScrollBar().maximum() == 0
        folder = Path("artifacts/moke-voltage-steps")
        folder.mkdir(parents=True, exist_ok=True)
        assert window.grab().save(str(folder / f"quick-{theme}.png"))
        submitted = []
        row.submit_requested.disconnect()
        row.submit_requested.connect(lambda *args: submitted.append(args))
        row.value.setFocus()
        QTest.keyClick(row.value, Qt.Key.Key_Up)
        assert row.slider.value_si() == .005
        row.increase.click()
        assert row.slider.value_si() == .01
        row.step(-1, Decimal("0.1"))
        assert row.slider.value_si() == .0095
        row.set_value_text("0.499 V")
        row.increase.click()
        assert row.slider.value_si() == .5
        assert submitted and all(frame.record_type == MokeCommandType.READBACK_VOUT for frame in sent)
    finally:
        window.close()
        for key, (existed, value) in saved.items():
            settings.setValue(key, value) if existed else settings.remove(key)
        apply_application_theme(application, "light")
