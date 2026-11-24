"""A timeline must show one Rigol representation and its true voltage envelope."""

import math
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication
from ruamel.yaml import YAML

from app.devices.rigol_dg1000z.adapter import RigolChannelConfig, RigolOutputConfig
from app.devices.rigol_dg1000z.ui.page import RigolPage
from app.engine.compiler import PlanAction, RecipeCompiler
from app.recipes import parse_recipe_text
from app.settings.models import StationSettings
from app.ui.design_system import apply_application_theme, plot_theme, tokens_for
from app.ui.execution.plan_timeline import ExecutionPlanTimeline, planned_series
from app.ui.execution.rigol_projection import project_rigol
from tests.helpers import simulation_settings


def ac_actions():
    config = RigolChannelConfig(1, 'SIN', 1000., .3, -.3)
    return (
        PlanAction('initial', 'configure_rigol', {'config': config}, {}),
        PlanAction('on', 'set_rigol_output', {'channel': 1, 'enabled': True}, {}),
        PlanAction('offset', 'update_rigol_levels',
                   {'channel': 1, 'high_level_v': .9, 'low_level_v': .3},
                   {'rigol.1.offset': .6}),
    )


def dc_plan():
    settings = StationSettings.model_validate(YAML(typ='safe').load(Path('.config/settings.yml')))
    return RecipeCompiler(settings).compile(parse_recipe_text(Path(
        'recipes/example_keithley_a_10ma_rigol_dc_0_600mv_fixed_moke.yml'
    ).read_text(encoding='utf-8')))


def test_dc_recipe_has_one_voltage_lane_and_no_inapplicable_frequency():
    plan = dc_plan()
    rows = {row.target: row for row in planned_series(plan.actions)}
    assert rows['rigol.1.offset'].values == pytest.approx([i * .015 for i in range(41)] + [0.])
    assert not {'rigol.1.high_level', 'rigol.1.low_level', 'rigol.1.amplitude', 'rigol.1.frequency'} & rows.keys()
    assert rows['keithley.A.current'].values == [.01, 0.]


@pytest.mark.parametrize('mode,expected,hidden', [
    ('High Level / Low Level', {'high_level', 'low_level'}, {'amplitude', 'offset'}),
    ('Amplitude / Offset', {'amplitude', 'offset'}, {'high_level', 'low_level'}),
])
def test_ac_representation_matches_card_without_duplicate_derived_lanes(mode, expected, hidden):
    rows = {row.target: row for row in planned_series(ac_actions(), rigol_level_modes={1: mode})}
    assert {f'rigol.1.{key}' for key in expected} <= rows.keys()
    assert not {f'rigol.1.{key}' for key in hidden} & rows.keys()
    if 'amplitude' in expected:
        assert rows['rigol.1.amplitude'].values == pytest.approx([.6])
        assert rows['rigol.1.offset'].values == pytest.approx([0., .6])
    else:
        assert rows['rigol.1.high_level'].values == pytest.approx([.3, .9])
        assert rows['rigol.1.low_level'].values == pytest.approx([-.3, .3])


def test_dc_summary_says_600mv_and_ac_uses_half_vpp_not_full_vpp():
    app = QApplication.instance() or QApplication([])
    widget = ExecutionPlanTimeline()
    try:
        widget.set_plan(dc_plan().actions)
        assert '600 mV' in widget.rigol_voltage_summary.text()
        assert '1.2 V' not in widget.rigol_voltage_summary.text()
        assert 'DC' in widget.rigol_voltage_summary.text()
        assert 'HighL = LowL = offset' in widget.rigol_voltage_summary.text()
        widget.set_plan(ac_actions(), rigol_level_modes={1: 'Amplitude / Offset'})
        assert '900 mV' in widget.rigol_voltage_summary.text()
        assert 'Vpp/2' in widget.rigol_voltage_summary.text()
        widget.resize(1440, 620)
        widget.show()
        app.processEvents()
        assert widget.rigol_voltage_summary.isVisibleTo(widget)
        assert widget.plot.width() > 0
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()


def test_card_display_mode_can_be_read_even_with_invalid_numeric_drafts():
    app = QApplication.instance() or QApplication([])
    page = RigolPage(Mock(), simulation_settings())
    try:
        page.level_mode.setCurrentText(page.LEVEL_MODE_HIGH_LOW)
        page.frequency.setText('unfinished')
        page.offset.setText('unfinished')
        assert page.voltage_entry_modes()[1] == 'High Level / Low Level'
        assert page.voltage_entry_modes()[2] == 'Amplitude / Offset'
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


def test_switching_from_sine_to_dc_ends_the_ac_only_lanes():
    actions = (*ac_actions(), PlanAction('dc', 'configure_rigol',
                {'config': RigolChannelConfig(1, 'DC', 1000., .2, .2)}, {}))
    rows = {row.target: row for row in planned_series(actions, rigol_level_modes={1: 'High Level / Low Level'})}
    for field in ('frequency', 'high_level', 'low_level'):
        assert math.isnan(rows[f'rigol.1.{field}'].values[-1])
    assert rows['rigol.1.offset'].values == [.2]


def test_numeric_load_and_negative_endpoint_are_included_in_peak_summary():
    app = QApplication.instance() or QApplication([])
    config = replace(ac_actions()[0].payload['config'], output_load=50., high_level_v=.3, low_level_v=-.9)
    actions = (PlanAction('initial', 'configure_rigol', {'config': config}, {}), ac_actions()[1])
    widget = ExecutionPlanTimeline()
    try:
        widget.set_plan(actions)
        assert '900 mV' in widget.rigol_voltage_summary.text()
        assert '1.8 V' in widget.rigol_voltage_summary.text()
    finally:
        widget.deleteLater()
        app.processEvents()


def test_omitted_initial_load_is_unknown_until_explicitly_configured():
    config = RigolChannelConfig(1, 'DC', 1000., .6, .6, changed_fields=(
        'waveform', 'frequency_hz', 'high_level_v', 'low_level_v'))
    actions = (PlanAction('initial', 'configure_rigol', {'config': config}, {}), ac_actions()[1])
    summary = project_rigol(actions).summary()
    assert 'unconfirmed' in summary
    assert 'LOAD HIGHZ' not in summary
    explicit_load = PlanAction('path', 'configure_rigol_output',
                               {'config': RigolOutputConfig(1, output_load='HIGHZ')}, {})
    summary = project_rigol((actions[0], explicit_load, actions[1])).summary()
    assert 'LOAD HIGHZ' in summary and 'unconfirmed' not in summary


def test_polarity_only_output_patch_preserves_numeric_load():
    config = RigolChannelConfig(1, 'DC', 1000., .6, .6, output_load=50.)
    patch = RigolOutputConfig(1, polarity='INV', changed_fields=('polarity',))
    summary = project_rigol((PlanAction('initial', 'configure_rigol', {'config': config}, {}),
                            PlanAction('patch', 'configure_rigol_output', {'config': patch}, {}),
                            ac_actions()[1])).summary()
    assert '1.2 V' in summary
    assert 'LOAD HIGHZ' not in summary


def test_timeline_keeps_substep_views_derived_from_a_single_endpoint_update():
    actions = (
        PlanAction('initial', 'configure_rigol', {'config': RigolChannelConfig(1, 'SIN', 1000., .00105, -.00105)}, {}),
        PlanAction('high', 'update_rigol_levels', {'channel': 1, 'high_level_v': .0012, 'low_level_v': -.00105}, {}),
        PlanAction('amplitude', 'update_rigol_levels', {'channel': 1, 'high_level_v': .001575, 'low_level_v': -.001425}, {}),
    )
    rows = {row.target: row for row in planned_series(actions, rigol_level_modes={1: 'Amplitude / Offset'})}
    assert rows['rigol.1.offset'].values == pytest.approx([0., .000075])
    assert rows['rigol.1.amplitude'].values == pytest.approx([.0021, .00225, .003])


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_dc_timeline_is_readable_and_rendered_at_desktop_and_narrow_sizes(theme, tmp_path):
    app = QApplication.instance() or QApplication([])
    apply_application_theme(app, theme)
    widget = ExecutionPlanTimeline()
    try:
        widget.set_plan(dc_plan().actions)
        widget.show()
        for width in (1440, 900):
            widget.resize(width, 700)
            app.processEvents()
            assert widget.rigol_voltage_summary.isVisibleTo(widget)
            assert widget.plot.width() > 300 and widget.plot.height() > 200
            assert widget.plot.getAxis('left').textPen().color().name() == plot_theme(tokens_for(theme)).axes
            assert widget.grab().save(str(tmp_path / f'dc-{theme}-{width}.png'))
    finally:
        widget.close()
        widget.deleteLater()
        app.processEvents()
