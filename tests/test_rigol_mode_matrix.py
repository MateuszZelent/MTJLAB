"""DC/SIN semantics across card, offline editor, compiler and execution."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml
from PySide6.QtWidgets import QApplication

from app.devices.rigol_dg1000z.adapter import RigolAdapter
from app.devices.rigol_dg1000z.sweep_provider import PROVIDER
from app.devices.rigol_dg1000z.ui.page import RigolConfigurationSnapshot, RigolPage
from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog
from app.devices.simulators import RigolSimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import ConfigurationError, SafetyViolation
from app.domain.quantities import parse_quantity
from app.domain.quick_controls import QuickConfigureCommand
from app.engine.compiler import RecipeCompiler
from app.engine.runner import RecipeRunner
from app.recipes import parse_recipe_text
from tests.test_adapters_and_runner import MemoryWriter, ShutdownProbe
from tests.test_rigol_deep_programming_audit import settings_with_limit


@pytest.fixture(params=['card', 'editor'])
def voltage_ui(request):
    app = QApplication.instance() or QApplication([])
    settings = settings_with_limit()
    if request.param == 'card':
        widget = RigolPage(Mock(), settings)
        amp_mode, high_mode = widget.LEVEL_MODE_AMPLITUDE_OFFSET, widget.LEVEL_MODE_HIGH_LOW
    else:
        widget = RigolNodeEditorDialog(settings=settings, snapshot=RigolConfigurationSnapshot(), carrier_only=True)
        amp_mode, high_mode = 'Amplitude / Offset', 'High Level / Low Level'
    widget.waveform.setCurrentText('SIN')
    widget.level_mode.setCurrentText(amp_mode)
    yield widget, amp_mode, high_mode
    widget.close()
    widget.deleteLater()
    app.processEvents()


@pytest.mark.parametrize('offset', ['0 mV', '0.1 mV', '-0.1 mV'])
def test_ui_preserves_odd_vpp_steps_and_derived_half_step_endpoints(voltage_ui, offset):
    widget, _amp, _high = voltage_ui
    widget.vpp.setText('2.1 mV')
    widget.offset.setText(offset)
    widget._sync_levels_from_vpp_offset()
    snapshot = widget.configuration_snapshot()
    high, low = (parse_quantity(text, 'voltage').si_value for text in (snapshot.high_level, snapshot.low_level))
    center = parse_quantity(offset, 'voltage').si_value
    assert (high, low) == pytest.approx((center + .00105, center - .00105))
    if isinstance(widget, RigolPage):
        config = widget._channel_config_from_snapshot(snapshot)
        assert (config.high_level_v, config.low_level_v) == pytest.approx((high, low))


def test_changing_entry_mode_preserves_uncommitted_authored_voltage(voltage_ui):
    widget, amp, high = voltage_ui
    widget.vpp.setText('20 mV')
    widget.offset.setText('3 mV')
    widget.level_mode.setCurrentText(high)
    snapshot = widget.configuration_snapshot()
    assert parse_quantity(snapshot.high_level).si_value == pytest.approx(.013)
    assert parse_quantity(snapshot.low_level).si_value == pytest.approx(-.007)
    widget.high_level.setText('15 mV')
    widget.low_level.setText('-5 mV')
    widget.level_mode.setCurrentText(amp)
    assert parse_quantity(widget.vpp.text()).si_value == pytest.approx(.02)
    assert parse_quantity(widget.offset.text()).si_value == pytest.approx(.005)


def test_ui_does_not_round_away_an_out_of_limit_authored_request(voltage_ui):
    widget, _amp, _high = voltage_ui
    widget.vpp.setText('100.04 mV')
    widget.offset.setText('0 V')
    snapshot = widget.configuration_snapshot()
    with pytest.raises((SafetyViolation, ConfigurationError)):
        RecipeCompiler(settings_with_limit()).compile(parse_recipe_text(yaml.safe_dump({
            'schema_version': 1, 'name': 'raw limit', 'root': {'id': 'carrier', 'type': 'configure_rigol',
                'channel': 1, 'waveform': 'SIN', 'frequency': '1 kHz',
                'high_level': snapshot.high_level, 'low_level': snapshot.low_level}})))


def test_period_entry_is_normalized_before_snapshot_without_requiring_focus_loss(voltage_ui):
    widget, _amp, _high = voltage_ui
    widget.time_mode.setCurrentText('Period')
    widget.period.setText('100 us')
    snapshot = widget.configuration_snapshot()
    assert parse_quantity(snapshot.frequency, 'frequency').si_value == pytest.approx(10000.)


def test_editor_rejects_a_raw_valid_configuration_that_rounds_outside_the_limit():
    app = QApplication.instance() or QApplication([])
    dialog = RigolNodeEditorDialog(settings=settings_with_limit('100.05 mV'), snapshot=RigolConfigurationSnapshot(), carrier_only=True)
    try:
        dialog.level_mode.setCurrentText('Amplitude / Offset')
        dialog.vpp.setText('66.66 mV')
        dialog.offset.setText('33.38 mV')
        with patch('app.devices.rigol_dg1000z.ui.recipe_dialog.QMessageBox.warning') as warning:
            dialog.accept()
        assert warning.called
        assert dialog.result() == 0
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_live_dc_ignores_unused_frequency_and_phase_placeholders():
    app = QApplication.instance() or QApplication([])
    page = RigolPage(Mock(), settings_with_limit())
    try:
        page.waveform.setCurrentText('DC')
        page._confirmed_carrier_configs[1] = replace(page._visible_channel_config(), frequency_hz=1000., phase_deg=90.)
        page._device_state_changed('output_on')
        page._set_rigol_channel_output(1, True)
        operation, _command = page.quick_control_hardware_request('rigol.1.offset', '-10 mV', 'output_on')
        assert operation == 'quick_setpoint'
    finally:
        page.deleteLater()
        app.processEvents()


def test_card_caches_and_shows_applied_config_after_a_verified_off_grid_apply():
    app = QApplication.instance() or QApplication([])
    page = RigolPage(Mock(), settings_with_limit())
    try:
        page.level_mode.setCurrentText(page.LEVEL_MODE_HIGH_LOW)
        page.high_level.setText('10.04 mV')
        page.low_level.setText('-10 mV')
        config = page._visible_channel_config()
        page.quick_control_configuration_verified('rigol.1.high_level', QuickConfigureCommand('rigol.1.high_level', config))
        confirmed = page._confirmed_carrier_configs[1]
        assert (confirmed.high_level_v, confirmed.low_level_v) == pytest.approx((.01, -.01))
        assert parse_quantity(page.high_level.text()).si_value == pytest.approx(.01)
        assert parse_quantity(page.offset.text()).si_value == pytest.approx(0.)
    finally:
        page.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('frequency', ['1234.56789 Hz', '10000000.123456 Hz'])
@pytest.mark.parametrize('time_mode', ['Frequency', 'Period'])
def test_applied_frequency_prefix_formatting_does_not_block_an_offset_update(frequency, time_mode):
    app = QApplication.instance() or QApplication([])
    page = RigolPage(Mock(), settings_with_limit())
    try:
        page.frequency.setText(frequency)
        page._sync_period_from_frequency()
        page.time_mode.setCurrentText(time_mode)
        page._cache_applied_carrier(page._visible_channel_config())
        assert page._confirmed_carrier_configs[1].frequency_hz == pytest.approx(parse_quantity(frequency).si_value, abs=.5e-6, rel=0)
        page._device_state_changed('output_on')
        page._set_rigol_channel_output(1, True)
        operation, _command = page.quick_control_hardware_request('rigol.1.offset', '1 mV', 'output_on')
        assert operation == 'quick_setpoint'
    finally:
        page.deleteLater()
        app.processEvents()


def test_card_live_high_control_uses_semantic_rounding_at_the_limit():
    app = QApplication.instance() or QApplication([])
    page = RigolPage(Mock(), settings_with_limit('99.97 mV'))
    try:
        page.level_mode.setCurrentText(page.LEVEL_MODE_HIGH_LOW)
        page.high_level.setText('49.9 mV')
        page.low_level.setText('-49.9 mV')
        page._cache_applied_carrier(page._visible_channel_config())
        page._device_state_changed('output_on')
        page._set_rigol_channel_output(1, True)
        operation, _command = page.quick_control_hardware_request('rigol.1.high_level', '50 mV', 'output_on')
        assert operation == 'quick_setpoint'
    finally:
        page.deleteLater()
        app.processEvents()


def test_native_editor_defers_coupled_wire_validation_to_preflight():
    app = QApplication.instance() or QApplication([])
    dialog = RigolNodeEditorDialog(settings=settings_with_limit('99.97 mV'),
        snapshot=RigolConfigurationSnapshot(high_level='49.9 mV', low_level='-49.9 mV', level_mode='High Level / Low Level'),
        parameter_actions=[{'parameter_id': 'carrier.high_level', 'mode': 'set', 'value': '50 mV'}])
    try:
        with patch('app.devices.rigol_dg1000z.ui.recipe_dialog.QMessageBox.warning') as warning:
            dialog.accept()
        assert not warning.called
        assert dialog.result() == 1
    finally:
        dialog.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('field,text', [('amplitude', '20 mV'), ('high_level', '20 mV'),
                                      ('low_level', '20 mV'), ('frequency', '1 kHz')])
def test_dc_card_quick_controls_cannot_reinterpret_ac_controls_as_dc_level(field, text):
    app = QApplication.instance() or QApplication([])
    page = RigolPage(Mock(), settings_with_limit())
    try:
        page.waveform.setCurrentText('DC')
        with pytest.raises(SafetyViolation):
            page.quick_control_hardware_request(f'rigol.1.{field}', text, 'output_off')
    finally:
        page.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('draft', ['waveform', 'load', 'phase', 'other-voltage-control'])
def test_live_card_requires_confirmed_carrier_to_match_the_unswept_form(draft):
    app = QApplication.instance() or QApplication([])
    page = RigolPage(Mock(), settings_with_limit())
    try:
        page._confirmed_carrier_configs[1] = page._visible_channel_config()
        page._device_state_changed('output_on')
        page._set_rigol_channel_output(1, True)
        if draft == 'waveform':
            page.waveform.setCurrentText('DC')
        elif draft == 'load':
            page.load.setText('50')
        elif draft == 'phase':
            page.phase.setText('90')
        else:
            page.vpp.setText('20 mV')
        with pytest.raises(SafetyViolation):
            page.quick_control_hardware_request('rigol.1.offset', '2 mV', 'output_on')
    finally:
        page.deleteLater()
        app.processEvents()


def native_recipe(parameter_actions, waveform='SIN', *, enabled=False):
    high, low = ('10 mV', '10 mV') if waveform == 'DC' else ('20 mV', '-20 mV')
    children = [{'id': 'carrier', 'type': 'configure_rigol', 'channel': 1, 'waveform': waveform,
                 'frequency': '1 kHz', 'high_level': high, 'low_level': low}]
    if enabled:
        children.append({'id': 'on', 'type': 'set_rigol_output', 'channel': 1, 'enabled': True})
    children.append({'id': 'selected', 'type': 'sequence', 'device_module': 'rigol',
                     'operation': 'configure_selected_parameters', 'configuration': {'channel': 1},
                     'output_policy': 'continue' if enabled else 'unchanged',
                     'parameter_actions': parameter_actions, 'children': []})
    return parse_recipe_text(yaml.safe_dump({'schema_version': 1, 'name': 'mode matrix',
                                            'root': {'id': 'root', 'type': 'sequence', 'children': children}}))


@pytest.mark.parametrize('parameter,value', [('frequency', '1 kHz'), ('amplitude', '0 V'),
                                            ('high_level', '10 mV'), ('low_level', '10 mV')])
def test_dc_native_set_rejects_non_dc_controls_before_execution(parameter, value):
    recipe = native_recipe([{'parameter_id': f'carrier.{parameter}', 'mode': 'set', 'value': value}], 'DC')
    with pytest.raises((ConfigurationError, SafetyViolation)):
        RecipeCompiler(settings_with_limit()).compile(recipe)


@pytest.mark.parametrize('actions', [
    [{'parameter_id': 'carrier.high_level', 'mode': 'set', 'value': '25 mV'},
     {'parameter_id': 'carrier.offset', 'mode': 'set', 'value': '2 mV'}],
    [{'parameter_id': 'carrier.offset', 'mode': 'set', 'value': '2 mV'},
     {'parameter_id': 'carrier.offset', 'mode': 'set', 'value': '3 mV'}],
], ids=['mixed-representations', 'duplicate-control'])
def test_native_actions_reject_ambiguous_representations(actions):
    with pytest.raises(ConfigurationError):
        RecipeCompiler(settings_with_limit()).compile(native_recipe(actions))


def test_simultaneous_amplitude_and_offset_change_while_on_is_rejected_in_preflight():
    recipe = native_recipe([
        {'parameter_id': 'carrier.high_level', 'mode': 'set', 'value': '30 mV'},
        {'parameter_id': 'carrier.low_level', 'mode': 'set', 'value': '-5 mV'}], enabled=True)
    with pytest.raises((ConfigurationError, SafetyViolation)):
        RecipeCompiler(settings_with_limit()).compile(recipe)


def test_force_off_precedes_a_two_control_change():
    recipe = native_recipe([
        {'parameter_id': 'carrier.amplitude', 'mode': 'set', 'value': '30 mV'},
        {'parameter_id': 'carrier.offset', 'mode': 'set', 'value': '5 mV'}], enabled=True)
    selected = recipe.root.children[-1]
    selected.data['output_policy'] = 'off'
    plan = RecipeCompiler(settings_with_limit()).compile(recipe)
    assert [a.kind for a in plan.actions][-2:] == ['set_rigol_output', 'update_rigol_levels']
    assert plan.actions[-2].payload['enabled'] is False


@pytest.mark.parametrize('parameter', ['amplitude', 'offset'])
def test_voltage_roi_represents_the_executed_level_update(parameter):
    assert PROVIDER.axis_action_kinds(SimpleNamespace(parameter_id=f'carrier.{parameter}')) == frozenset({'update_rigol_levels'})


AXIS_CASES = [
    ('SIN', 'frequency', '1 kHz', '2 kHz', 'FREQ'),
    ('SIN', 'high_level', '10 mV', '20 mV', 'VOLT:HIGH'),
    ('SIN', 'low_level', '-10 mV', '-20 mV', 'VOLT:LOW'),
    ('SIN', 'amplitude', '20 mV', '30 mV', 'VOLT'),
    ('SIN', 'offset', '0 mV', '10 mV', 'VOLT:OFFS'),
    ('DC', 'offset', '-10 mV', '10 mV', 'VOLT:OFFS'),
    ('SIN', 'amplitude_off_grid', '20 mV', '21 mV', 'VOLT'),
    ('SIN', 'offset_off_grid', '1 mV', '2 mV', 'VOLT:OFFS'),
    ('SIN', 'high_level_boundary', '49.9 mV', '50 mV', 'VOLT:HIGH'),
    ('SIN', 'low_level_boundary', '-49.9 mV', '-50 mV', 'VOLT:LOW'),
]


@pytest.mark.parametrize('channel', [1, 2])
@pytest.mark.parametrize('enabled', [False, True], ids=['off', 'on'])
@pytest.mark.parametrize('native', [False, True], ids=['explicit-axis', 'native-roi'])
@pytest.mark.parametrize('waveform,parameter,start,stop,suffix', AXIS_CASES)
def test_full_compiler_to_execute_matrix(channel, enabled, native, waveform, parameter, start, stop, suffix):
    raw = settings_with_limit().model_dump(mode='python')
    for settings in raw['devices']['rigol']['safety']['channels'].values():
        settings['enabled'] = True
        settings['lab_limits']['combined_voltage_limit'] = '100 mV'
    settings = type(settings_with_limit()).model_validate(raw)
    high, low = ('-10 mV', '-10 mV') if waveform == 'DC' else ('10 mV', '-10 mV')
    if parameter.endswith('_off_grid'):
        high = '10.04 mV'
        parameter = parameter.removesuffix('_off_grid')
    if parameter.endswith('_boundary'):
        high, low = '49.9 mV', '-49.9 mV'
        parameter = parameter.removesuffix('_boundary')
        for channel_settings in raw['devices']['rigol']['safety']['channels'].values():
            channel_settings['lab_limits']['combined_voltage_limit'] = '99.97 mV'
        settings = type(settings).model_validate(raw)
    children = [{'id': 'carrier', 'type': 'configure_rigol', 'channel': channel, 'waveform': waveform,
                 'frequency': '1 kHz', 'high_level': high, 'low_level': low, 'output_load': 'HIGHZ'}]
    if enabled:
        children.append({'id': 'on', 'type': 'set_rigol_output', 'channel': channel, 'enabled': True})
    roi = [{'start': start, 'stop': stop, 'points': 2}]
    if native:
        axis = {'id': 'axis', 'type': 'sequence', 'device_module': 'rigol', 'operation': 'configure_selected_parameters',
                'configuration': {'channel': channel}, 'output_policy': 'continue' if enabled else 'unchanged',
                'parameter_actions': [{'parameter_id': f'carrier.{parameter}', 'mode': 'sweep', 'segments': roi}]}
    else:
        axis = {'id': 'axis', 'type': 'sweep', 'target': f'rigol.{channel}.{parameter}', 'segments': roi}
    axis['children'] = [{'id': 'point', 'type': 'checkpoint'}]
    children.append(axis)
    recipe = parse_recipe_text(yaml.safe_dump({'schema_version': 1, 'name': 'execute matrix',
                                              'root': {'id': 'root', 'type': 'sequence', 'children': children}}))
    plan = RecipeCompiler(settings).compile(recipe)
    session = RigolSimulator()

    class RecordingAdapter(RigolAdapter):
        def record(self, begin):
            config = self.last_channel_config(channel)
            captured.append((config, session.output[channel], [command for command in session.commands[begin:]
                            if command.startswith(f':SOUR{channel}:') and '?' not in command]))

        def update_levels(self, *args, **kwargs):
            begin = len(session.commands)
            result = super().update_levels(*args, **kwargs)
            self.record(begin)
            return result

        def update_frequency(self, *args, **kwargs):
            begin = len(session.commands)
            result = super().update_frequency(*args, **kwargs)
            self.record(begin)
            return result

    captured = []
    adapter = RecordingAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    writer = MemoryWriter()
    try:
        result = RecipeRunner(rigol=adapter, keithley=ShutdownProbe(), anritsu=ShutdownProbe(), writer=writer).run(plan)
        assert result.error is None, result.error
        assert len(captured) == 2
        requested = [parse_quantity(value).si_value for value in (start, stop)]
        for (config, output_on, commands), value in zip(captured, requested):
            amplitude = config.high_level_v - config.low_level_v
            offset = (config.high_level_v + config.low_level_v) / 2
            actual = {'frequency': config.frequency_hz, 'high_level': config.high_level_v,
                      'low_level': config.low_level_v, 'amplitude': amplitude, 'offset': offset}[parameter]
            assert actual == pytest.approx(value)
            assert output_on is enabled
            assert all(command.startswith(f':SOUR{channel}:{suffix} ') for command in commands)
            if parameter == 'high_level':
                assert config.low_level_v == pytest.approx(parse_quantity(low).si_value)
            elif parameter == 'low_level':
                assert config.high_level_v == pytest.approx(parse_quantity(high).si_value)
            elif parameter == 'amplitude':
                assert offset == pytest.approx(0.)
            elif parameter == 'offset':
                assert amplitude == pytest.approx(0. if waveform == 'DC' else .02)
        assert not any(session.output.values())
    finally:
        adapter.disconnect()


def test_invalid_source_draft_cannot_be_hidden_by_switching_representation(voltage_ui):
    widget, amp, high = voltage_ui
    widget.vpp.setText('20 mV')
    widget.offset.setText('3 mV')
    widget._sync_levels_from_vpp_offset()
    (widget.vpp.number if isinstance(widget, RigolPage) else widget.vpp).clear()
    widget.level_mode.setCurrentText(high)
    assert widget.level_mode.currentText() == amp
    with pytest.raises(ValueError):
        widget.configuration_snapshot()


def test_native_set_uses_single_endpoint_rounding_at_a_combined_limit():
    recipe = native_recipe([{'parameter_id': 'carrier.high_level', 'mode': 'set', 'value': '50 mV'}])
    recipe.root.children[0].data.update(high_level='49.9 mV', low_level='-49.9 mV')
    plan = RecipeCompiler(settings_with_limit('99.97 mV')).compile(recipe)
    assert plan.actions[-1].payload['high_level_v'] == pytest.approx(.05)


@pytest.mark.parametrize('control', ['amplitude', 'offset'])
def test_ac_single_control_keeps_non_grid_companion(control):
    from tests.test_rigol_deep_programming_audit import connected

    adapter, session = connected(enabled=True)
    try:
        adapter.update_amplitude_vpp(1, .0021)
        adapter.update_high_level(1, .0012)
        before = adapter.last_channel_config(1)
        snapshot = adapter.quick_control_snapshot()
        assert snapshot["rigol.1.amplitude"] == pytest.approx(before.high_level_v - before.low_level_v)
        assert snapshot["rigol.1.offset"] == pytest.approx((before.high_level_v + before.low_level_v) / 2)
        if control == 'amplitude':
            adapter.update_amplitude_vpp(1, .003)
            after = adapter.last_channel_config(1)
            assert after.high_level_v + after.low_level_v == pytest.approx(before.high_level_v + before.low_level_v)
        else:
            adapter.update_offset(1, .001)
            after = adapter.last_channel_config(1)
            assert after.high_level_v - after.low_level_v == pytest.approx(before.high_level_v - before.low_level_v)
        assert session.output[1]
    finally:
        adapter.disconnect()


@pytest.mark.parametrize('control', ['high_level', 'low_level', 'amplitude', 'offset'])
def test_reapplying_a_confirmed_voltage_view_is_a_verified_noop(control):
    from tests.test_rigol_deep_programming_audit import connected

    adapter, session = connected(enabled=True)
    try:
        adapter.update_amplitude_vpp(1, .0021)
        adapter.update_high_level(1, .0012)
        before = adapter.last_channel_config(1)
        value = {'high_level': before.high_level_v, 'low_level': before.low_level_v,
                 'amplitude': before.high_level_v - before.low_level_v,
                 'offset': (before.high_level_v + before.low_level_v) / 2}[control]
        session.commands.clear()
        method = 'update_amplitude_vpp' if control == 'amplitude' else f'update_{control}'
        actual = getattr(adapter, method)(1, value)
        after = adapter.last_channel_config(1)
        assert actual == pytest.approx(value)
        assert (after.high_level_v, after.low_level_v) == pytest.approx((before.high_level_v, before.low_level_v))
        assert not any(command.startswith(':SOUR1:') and '?' not in command for command in session.commands)
        assert session.output[1]
    finally:
        adapter.disconnect()
