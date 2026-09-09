"""B sequence entry is current-only, endpoint-inclusive and independent of hardware I/O."""
import pytest
from PySide6.QtWidgets import QApplication
from app.devices.keithley_2600.ui.field_series_panel import FieldSeriesPanel
from tests.test_keithley_field_series import config, make_runner
from tests.helpers import loaded_settings
from app.domain.errors import SafetyViolation


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    widget = FieldSeriesPanel()
    widget.enabled_box.setChecked(True)
    widget.resize(600, 700)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    app.processEvents()


def test_list_and_interval_share_values_and_preserve_drafts(panel, tmp_path):
    app = QApplication.instance()
    panel.currents.setText('-10 mA; -5 mA; 0 A; 5 mA; 10 mA')
    expected = panel.current_values()
    panel.input_mode.setCurrentIndex(1)
    panel.interval_start.setText('-10 mA')
    panel.interval_stop.setText('10 mA')
    panel.interval_points.setValue(5)
    app.processEvents()
    assert panel.current_values() == pytest.approx(expected)
    assert panel.current_values()[2] == 0
    assert not panel.currents.isVisible()
    assert panel.interval_points.isVisible()
    assert panel.interval_points.height() > 0
    assert not hasattr(panel, 'tolerance')
    assert panel.grab().save(str(tmp_path / 'b_interval.png'))
    saved = panel.draft_state()
    panel.input_mode.setCurrentIndex(0)
    assert panel.current_values() == expected
    panel.restore_draft_state(saved)
    assert panel.input_mode.currentIndex() == 1
    assert panel.current_values() == pytest.approx(expected)
    panel.interval_start.setText('10 mA')
    panel.interval_stop.setText('-10 mA')
    assert panel.current_values() == pytest.approx(tuple(reversed(expected)))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5, 10", (0.005, 0.010)),
        ("5 mA, 10 mA", (0.005, 0.010)),
        ("500 uA; 1 mA", (0.0005, 0.001)),
        ("5,5 mA; 10", (0.0055, 0.010)),
    ],
)
def test_current_list_accepts_clear_separators_and_milliamp_defaults(panel, text, expected):
    panel.currents.setText(text)
    assert panel.current_values() == pytest.approx(expected)


def test_safe_defaults_are_visible_after_loading_channel_b_limits(panel):
    runner, _, _, _ = make_runner()
    panel.refresh_bounds(runner.settings)
    defaults = loaded_settings()
    assert (
        defaults.keithley.safety.channels["A"].lab_limits.ramp_current_step_max
        == "100 uA"
    )
    assert (
        defaults.keithley.safety.channels["B"].lab_limits.ramp_current_step_max
        == "1 mA"
    )
    assert panel.currents.text() == "0 mA"
    assert panel.current_values() == (0.0,)
    assert panel.ramp_step.text() == "1 mA"
    assert panel.ramp_step.isReadOnly()
    assert panel.stabilization.text() == "1 s"
    assert panel.hold.text() == "120 s"
    assert panel.analysis_reference.value() == 0


def test_interval_bare_numbers_are_milliamperes(panel):
    panel.input_mode.setCurrentIndex(1)
    panel.interval_start.setText("-10")
    panel.interval_stop.setText("10")
    panel.interval_points.setValue(5)
    assert panel.current_values() == pytest.approx((-.01, -.005, 0, .005, .01))


def test_reference_is_automatic_when_fit_window_is_empty(panel):
    panel.analysis_reference.setValue(6)
    panel._sync_analysis_reference()
    assert panel.analysis_reference.value() == 0


def test_preflight_timeout_includes_the_a_sweep(panel):
    template = config()
    panel.currents.setText("0")
    panel.ramp_step.setText("5 uA")
    panel.stabilization.setText("0 s")
    panel.hold.setText("2 ms")
    cfg = panel.build_config(template.sweep, template.field_source)
    runner, device, _, _ = make_runner(cfg)
    with pytest.raises(SafetyViolation, match="enter at least"):
        runner.run()
    assert device.calls == []
    assert not panel.analysis_reference.isEnabled()
    panel.analysis_min.setText("-10 uA")
    panel.analysis_max.setText("10 uA")
    panel.currents.setText("0, 5, 10")
    assert panel.analysis_reference.isEnabled()
    panel.analysis_reference.setValue(3)
    assert panel.analysis_reference.value() == 3
    panel.analysis_max.clear()
    assert panel.analysis_reference.value() == 0


@pytest.mark.parametrize('value', ['10 mT', '10 mV', 'NaN A', 'inf A', ''])
def test_interval_rejects_wrong_units_and_nonfinite(panel, value):
    panel.input_mode.setCurrentIndex(1)
    panel.interval_start.setText(value)
    panel.interval_stop.setText('10 mA')
    with pytest.raises(ValueError):
        panel.current_values()


def test_simple_config_and_whole_interval_limit_check(panel):
    template = config()
    panel.input_mode.setCurrentIndex(1)
    panel.interval_start.setText('0 mA')
    panel.interval_stop.setText('11 mA')  # Test B limit is 10 mA.
    panel.ramp_step.setText('5 uA')
    panel.hold.setText('10 s')
    cfg = panel.build_config(template.sweep, template.field_source)
    assert not cfg.verify_current_stability
    runner, device, _, _ = make_runner(cfg)
    with pytest.raises(SafetyViolation):
        runner.run()
    assert device.calls == []


def test_interval_modal_uses_exact_generated_currents(panel):
    from app.devices.keithley_2600.characterization.field_scenario import build_field_scenario
    template = config()
    panel.input_mode.setCurrentIndex(1)
    panel.interval_start.setText('0 uA')
    panel.interval_stop.setText('10 uA')
    panel.ramp_step.setText('5 uA')
    panel.hold.setText('10 s')
    cfg = panel.build_config(template.sweep, template.field_source)
    runner, device, _, _ = make_runner(cfg)
    scenario = build_field_scenario(cfg, runner.settings, device.policies, device.policies)
    assert scenario.config.currents_a == pytest.approx((0, 2.5e-6, 5e-6, 7.5e-6, 10e-6))
    assert len(scenario.steps[1:6]) == 5
    assert all('No additional current-tolerance' in step.children[2].detail
               for step in scenario.steps[1:6])
    assert device.calls == []


def test_automatic_ramp_uses_b_limits_and_shows_timing(panel):
    runner, _, _, _ = make_runner()
    panel.refresh_bounds(runner.settings)
    panel.input_mode.setCurrentIndex(1)
    panel.interval_start.setText("-100 mA")
    panel.interval_stop.setText("100 mA")
    panel.interval_points.setValue(10)
    panel.ramp_step.setText("100 uA")  # Simulate a stale saved draft.
    cfg = panel.build_config(config().sweep, config().field_source)

    panel.show_ramp_summary(cfg, max_points=1000)

    assert cfg.ramp_step_a == pytest.approx(1e-3)
    assert cfg.ramp_settle_s == pytest.approx(1e-3)
    assert "407 B updates" in panel.ramp_summary.text()
    assert "407 ms" in panel.ramp_summary.text()
    assert "Device communication adds time" in panel.ramp_summary.text()


def test_legacy_list_draft_remains_readable(panel):
    saved = panel.draft_state()
    saved['text'].pop('interval_start')
    saved['text'].pop('interval_stop')
    saved['text'].update(currents='1 mA; 0 A; 1 mA', tolerance='10 nA', relative='1 %')
    saved.pop('input_mode')
    saved.pop('interval_points')
    saved['stable_readings'] = 3
    panel.restore_draft_state(saved)
    assert panel.input_mode.currentIndex() == 0
    assert panel.current_values() == (.001, 0, .001)
