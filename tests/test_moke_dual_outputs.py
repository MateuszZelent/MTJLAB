"""Coil VOUT0 and explicitly empty test VOUT2 stay independently controlled."""

from dataclasses import replace

import pytest
import yaml
from PySide6.QtWidgets import QWidget, QVBoxLayout

from app.bootstrap import StationComposition
from app.devices.moke_box.adapter import MokeBoxAdapter
from app.devices.moke_box.models import MokeBoxConfig
from app.devices.moke_box.module import create_simulated_moke_adapter
from app.devices.moke_box.protocol import MokeFrame, MokeCommandType
from app.devices.moke_box.simulator import SimulatedMokeBoxTransport
from app.devices.simulation import SimulationContext
from app.devices.simulators import simulated_station_settings
from app.devices.moke_box.ui.page import MokeBoxPage
from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.models import DeviceState
from app.safety.moke_box import control_profile_from_settings, additional_control_profiles_from_settings as _test_profiles
from app.settings.models import StationSettings
from app.settings import SettingsRepository
from app.ui.design_system import apply_application_theme
from app.ui.workers import DeviceController
from tests.helpers import loaded_settings
from tests.test_fluent_moke_field_workflow import application as application, wait_for
from tests.test_moke_quick_controls import coordinator_for
from tests.test_moke_voltage_control import plan_for


def dual_settings(directory):
    raw = simulated_station_settings(loaded_settings()).model_dump(mode="python")
    device = raw["devices"]["moke_box"]
    device.update(allowed_vout_channels=(0, 2), test_vout_channels=(2,),
                  calibration_directory=str(directory / "calibrations"))
    device["voltage_control"]["channel"] = 0
    test_profile = dict(device["voltage_control"], channel=2, binding_id="unconnected-test-VOUT2",
                        kepco_model="Unconnected DAC test output", kepco_mode="dac_test",
                        minimum="-300 mV", maximum="300 mV", maximum_step="10 mV",
                        maximum_slew="200 mV/s", minimum_settling_time="100 ms", ramp_timeout="12 s")
    device["channel_profiles"] = {"2": test_profile}
    return StationSettings.model_validate(raw)


class RecordingTransport(SimulatedMokeBoxTransport):
    def __init__(self, context):
        super().__init__(context, field_channel=0)
        self.writes = []

    def send(self, raw):
        frame = MokeFrame.decode(raw)
        if frame.record_type == MokeCommandType.SET_VOUT:
            self.writes.append(frame)
        super().send(raw)


def test_independent_outputs_selected_zero_and_emergency_shutdown(tmp_path):
    settings = dual_settings(tmp_path)
    primary = control_profile_from_settings(settings, simulation=True)
    test_profile = _test_profiles(settings, simulation=True)[0]
    assert primary.fingerprint == control_profile_from_settings(
        settings.model_copy(update={"devices": settings.devices.model_copy(update={
            "moke_box": settings.moke_box.model_copy(update={
                "allowed_vout_channels": (0,), "test_vout_channels": (),
            }),
        })}), simulation=True).fingerprint
    context = SimulationContext(seed=7)
    transport = RecordingTransport(context)
    adapter = MokeBoxAdapter(MokeBoxConfig(
        "SIM::MOKE::INSTR", allow_vout_control=True, allowed_vout_channels=(0, 2),
        control_profile=primary, additional_control_profiles=(test_profile,),
    ), transport)
    adapter.connect()
    for profile, voltage in ((primary, .2), (test_profile, -.3)):
        before = len(transport.writes)
        plan = plan_for(profile, (voltage,), minimum=profile.minimum_v, maximum=profile.maximum_v)
        adapter.configure_voltage_plan(plan)
        adapter.arm_voltage_plan(plan)
        assert len(transport.writes) == before
        adapter.ramp_vout(profile.channel, voltage)
        assert {frame.channel for frame in transport.writes[before:]} == {profile.channel}
    assert adapter.read_vouts()[0] == pytest.approx(.2, abs=.001)
    assert context.magnet.voltage_v == pytest.approx(.2, abs=.001)
    assert adapter.get_control_profile() == primary
    assert adapter.get_control_profile(2).kepco_mode == "dac_test"
    assert test_profile.maximum_v == .3 and primary.maximum_v == 1
    assert test_profile.maximum_step_v == .01 and primary.maximum_step_v == .05
    assert test_profile.maximum_slew_v_s == .2 and primary.maximum_slew_v_s == 1
    assert test_profile.ramp_timeout_s == 12 and primary.ramp_timeout_s == 30
    before = len(transport.writes)
    with pytest.raises(SafetyViolation):
        adapter.configure_voltage_plan(plan_for(test_profile, (.301,), minimum=-.3, maximum=.3))
    assert len(transport.writes) == before
    previous = 0
    from app.devices.moke_box.protocol import decode_voltage
    for frame in transport.writes:
        if frame.channel == 2:
            value = decode_voltage(frame.msb, frame.lsb)
            assert abs(value - previous) <= test_profile.maximum_step_v
            previous = value
    with pytest.raises(SafetyViolation):
        adapter.configure_voltage_plan(replace(plan_for(primary), channel=2))
    with pytest.raises(SafetyViolation):
        adapter.get_control_profile(1)
    adapter.stop_vout(2)
    assert adapter.read_vouts()[2] == 0
    assert adapter.read_vouts()[0] == pytest.approx(.2, abs=.001)
    assert not adapter.safe_target_confirmed
    transport.writes.clear()
    adapter.emergency_off()
    assert {frame.channel for frame in transport.writes} == {0}  # VOUT2 is freshly confirmed zero, without another SET.
    assert adapter.read_vouts()[0] == adapter.read_vouts()[2] == 0
    assert adapter.safe_target_confirmed


@pytest.mark.parametrize("tests,allowed", [((), (0, 2)), ((0,), (0,)), ((2, 2), (0, 2)), ((8,), (0, 8))])
def test_extra_outputs_require_explicit_distinct_test_bindings(tmp_path, tests, allowed):
    raw = dual_settings(tmp_path).model_dump(mode="python")
    raw["devices"]["moke_box"].update(test_vout_channels=tests, allowed_vout_channels=allowed)
    with pytest.raises(ValueError):
        StationSettings.model_validate(raw)


def test_channel_settings_roundtrip_and_profile_identity_are_independent(tmp_path):
    settings = dual_settings(tmp_path)
    original = control_profile_from_settings(settings, simulation=True)
    raw = settings.model_dump(mode="json")
    raw["devices"]["moke_box"]["channel_profiles"]["2"]["maximum"] = "200 mV"
    path = tmp_path / "settings.yml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    restored = SettingsRepository(path).load().settings
    assert set(restored.moke_box.channel_profiles) == set("1234567")
    assert all(not restored.moke_box.channel_profiles[str(channel)].approved
               for channel in (1, 3, 4, 5, 6, 7))
    assert control_profile_from_settings(restored, simulation=True) == original
    assert _test_profiles(restored, simulation=True)[0].maximum_v == .2
    assert _test_profiles(restored, simulation=True)[0].fingerprint != _test_profiles(settings, simulation=True)[0].fingerprint
    raw["devices"]["moke_box"]["channel_profiles"]["2"]["maximum"] = "200 ms"
    with pytest.raises(ValueError):
        StationSettings.model_validate(raw)


def test_emergency_shutdown_attempts_second_channel_after_first_failure(tmp_path, monkeypatch):
    adapter = create_simulated_moke_adapter(settings=dual_settings(tmp_path))
    adapter.connect()
    stop = adapter.stop_vout
    attempts = []

    def fail_primary(channel=None):
        attempts.append(channel)
        if channel == 0:
            raise RuntimeError("Injected failed primary shutdown")
        return stop(channel)

    monkeypatch.setattr(adapter, "stop_vout", fail_primary)
    adapter.emergency_off()
    assert attempts == [0, 2]
    assert adapter.state is DeviceState.UNKNOWN
    assert not adapter.connected
    assert not adapter.safe_target_confirmed


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_both_outputs_apply_and_quick_controls_with_primary_only_calibration(application, tmp_path, theme):
    settings = dual_settings(tmp_path)
    host = QWidget()
    layout = QVBoxLayout(host)
    controllers = StationComposition(settings, simulation=True).create_controllers(
        ("moke_box", "lakeshore_gaussmeter"), host)
    page = MokeBoxPage(controllers["moke_box"], settings, host)
    layout.addWidget(page)
    workflow = page.field_workflow
    workflow.bind_reference(controllers["lakeshore_gaussmeter"], simulation=True, authorize=None,
                            pause_live=lambda: page.stop_live("Paused"))
    apply_application_theme(application, theme)
    host.resize(1360, 880)
    host.show()
    page.views.setCurrentIndex(2)
    wait_for(application, lambda: workflow.control_page.isVisible())
    try:
        for controller in controllers.values():
            controller.call("connect")
        wait_for(application, lambda: workflow._profile is not None and workflow._reference_connected)
        coordinator = coordinator_for(host, workflow, settings)
        assert coordinator.bound("moke_box.vout0.voltage") is not None
        assert coordinator.bound("moke_box.vout2.voltage") is not None
        assert coordinator.bound("moke_box.vout1.voltage") is None
        adapter = controllers["moke_box"].adapter_for_run()
        assert workflow._selected_channel() == 0
        workflow.configuration_panel.set_operator_limits("-250 mV", "250 mV")
        workflow.manual_settling.setText("3 s")
        workflow.channel_selector.setCurrentIndex(workflow.channel_selector.findData(2))
        assert workflow.configuration_panel.profile.channel == 2
        assert workflow.manual_settling.text() == "0.1 s"
        assert coordinator.bound("moke_box.vout2.voltage").maximum_si == .3
        assert coordinator.bound("moke_box.vout0.voltage").maximum_si == .25
        workflow.configuration_panel.set_operator_limits("-150 mV", "150 mV")
        workflow.manual_settling.setText("500 ms")
        assert workflow._manual_voltage_plan((.1,)).settling_s == .5
        with pytest.raises(SafetyViolation):
            workflow.configuration_panel.set_operator_limits("-1 V", "1 V")
        for channel in (2, 0, 2, 0):
            workflow.channel_selector.setCurrentIndex(workflow.channel_selector.findData(channel))
            application.processEvents()
            assert workflow.set_button.isEnabled()
            assert workflow.zero_button.text() == ("Turn off field" if channel == 0 else "Turn off output")
            assert workflow.arm_calibration_button.isEnabled() == (channel == 0)
            assert workflow.manual_settling.text() == ("3 s" if channel == 0 else "500 ms")
            assert adapter.read_vouts()[0] == adapter.read_vouts()[2] == 0
        assert coordinator.bound("moke_box.vout2.voltage").maximum_si == .15
        coordinator.submit("moke_box.vout0.voltage", "200 mV")
        wait_for(application, lambda: not workflow.busy)
        coordinator.submit("moke_box.vout2.voltage", "-100 mV")
        wait_for(application, lambda: not workflow.busy)
        assert adapter.read_vouts()[0] == pytest.approx(.2, abs=.001)
        assert adapter.read_vouts()[2] == pytest.approx(-.1, abs=.001)
        with pytest.raises(ConfigurationError, match="coil VOUT0"):
            workflow._make_calibration_request()
        assert "test output" in workflow.profile_label.text()
        assert workflow.set_button.isVisible() and workflow.set_button.width() > 50
        assert workflow.voltage_readout.isVisible()
        application.processEvents()
        slider = workflow.voltage_slider
        expected_x = int((slider.value() - slider.minimum()) / (slider.maximum() - slider.minimum()) * slider.grooveLength)
        assert abs(slider.handle.x() - expected_x) <= 1
        artifacts = tmp_path / f"dual-{theme}.png"
        assert host.grab().save(str(artifacts))
        workflow._zero()
        wait_for(application, lambda: not workflow.busy)
        assert adapter.read_vouts()[2] == 0
        assert adapter.read_vouts()[0] == pytest.approx(.2, abs=.001)
    finally:
        if workflow.busy:
            workflow.stop()
            wait_for(application, lambda: not workflow.busy)
        assert DeviceController.close_all(controllers.values())
        host.close()
        application.processEvents()
