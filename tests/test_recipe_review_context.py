"""Recipe reviews must describe executable state, not inactive device-page drafts."""
import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog

from app.devices.keithley_2600.ui.page import (
    KeithleyConfigurationSnapshot,
    KeithleyNodeEditorDialog,
)
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.ui.design_system import apply_application_theme
from app.ui.recipes.page import RecipePage
from tests.helpers import simulation_settings
from tests.test_sweep_audit_contracts import audit_settings


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        font = Path("C:/Windows/Fonts") / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    application.setFont(QFont("Segoe UI", 10))
    return application


SOURCE = """schema_version: 1
name: Incoming range
root:
  id: root
  type: sequence
  children:
    - {id: baseline, type: configure_keithley, channel: A, mode: current, level: '0 mA', compliance: '20 mV', source_range: '10 mA', source_autorange: false}
    - id: selected
      type: sequence
      device_module: keithley
      operation: configure_selected_parameters
      channel: A
      source_mode: current
      output_policy: unchanged
      configuration: {channel: A, source_mode: current, source_level: '1 mA', compliance: '20 mV', source_range: '1 A'}
      parameter_actions:
        - {parameter_id: source.level, mode: set, value: '1 mA'}
"""


@pytest.mark.parametrize("range_action", [False, True])
def test_review_uses_preceding_recipe_range_and_preserve_shows_effective_value(app, tmp_path, range_action):
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "recipe.yml"))
    page.set_keithley_snapshot_provider(lambda *args: KeithleyConfigurationSnapshot(channel="A", source_range="1 A"))
    source = SOURCE
    if range_action:
        source += "        - {parameter_id: source.range, mode: set, value: '1 A'}\n"
    page._apply_builder_source(source, "Test")
    node = parse_recipe_text(source).root.children[1]

    def inspect(dialog):
        dialog.show()
        app.processEvents()
        row = next(row for row in dialog.comparison.rows if row.key == "source_range")
        assert row.current == "1 A"
        assert row.planned == ("1 A" if range_action else "10 mA")
        index = next(i for i, row in enumerate(dialog.comparison.rows) if row.key == "source_range")
        assert "baseline" in dialog.comparison.item(index, 4).text()
        effect = dialog.comparison.item(index, 3).text()
        assert ("Same" if range_action else "Changes") in effect
        if range_action:
            coupled = next(row for row in dialog.comparison.rows if row.key == "effective_measure_current_range")
            assert coupled.action == "Derived"
            assert "source range" in coupled.note
        assert dialog.comparison.isVisible()
        assert dialog.comparison.width() > 100
        panel = dialog.configuration_panel
        assert panel.advanced_ranges_form.isRowVisible(panel.source_range_field)
        assert panel.source_range_field.isEnabled() == range_action
        if not range_action:
            selector = dialog.parameter_selectors["source.range"]
            selector.setCurrentIndex(selector.findData("set"))
            assert panel.source_range.text() == "10 mA"
            selector.setCurrentIndex(selector.findData("unchanged"))
        assert dialog.grab().save(str(tmp_path / "recipe-review.png"))
        dialog.close()
        return QDialog.DialogCode.Accepted

    try:
        with patch.object(KeithleyNodeEditorDialog, "exec", inspect):
            page._edit_keithley_module_node(node)
        saved = parse_recipe_text(page.editor.toPlainText()).root.children[1]
        assert any(action["parameter_id"] == "source.range" for action in saved.data["parameter_actions"]) == range_action
        plan = RecipeCompiler(audit_settings(tmp_path)).compile(parse_recipe_text(page.editor.toPlainText()))
        requests = [action.payload["request"] for action in plan.actions if action.kind == "configure_keithley"]
        assert requests[-1].source_range_si == (1.0 if range_action else .01)
        assert ("source_range_si" in requests[-1].changed_fields) == range_action
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_first_literal_configuration_compares_with_device_tab(app, tmp_path):
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "recipe.yml"))
    page.set_keithley_snapshot_provider(lambda *args: KeithleyConfigurationSnapshot(channel="A", source_range="10 mA"))
    page._apply_builder_source(SOURCE, "Test")
    node = parse_recipe_text(SOURCE).root.children[0]

    def inspect(dialog):
        row = next(row for row in dialog.comparison.rows if row.key == "source_range")
        assert row.current == "10 mA"
        index = next(i for i, row in enumerate(dialog.comparison.rows) if row.key == "source_range")
        assert "Same" in dialog.comparison.item(index, 3).text()
        assert row.planned == "10 mA"
        return QDialog.DialogCode.Rejected

    try:
        with patch.object(KeithleyNodeEditorDialog, "exec", inspect):
            page._edit_legacy_keithley_configuration(node)
    finally:
        page._close_discard_confirmed = True
        page.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [900, 1280])
def test_recipe_range_summary_is_visible_without_scrolling(app, tmp_path, theme, width):
    from PySide6.QtCore import QPoint
    apply_application_theme(app, theme)
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "render.yml"))
    page._apply_builder_source(SOURCE, "Test")
    node = parse_recipe_text(SOURCE).root.children[1]

    def inspect(dialog):
        dialog.resize(width, 800)
        dialog.show()
        app.processEvents()
        assert "10 mA" in dialog.comparison_note.text()
        assert "baseline" in dialog.comparison_note.text()
        for widget in (dialog.comparison_note, dialog.comparison, dialog.apply_button):
            rect = widget.rect().translated(widget.mapTo(dialog, QPoint()))
            assert dialog.rect().contains(rect)
            assert widget.isVisibleTo(dialog)
            assert widget.width() > 50 and widget.height() > 10
        target = Path("artifacts/sweep-parameter-transparency")
        target.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(target / f"keithley-{theme}-{width}.png"))
        dialog.close()
        return QDialog.DialogCode.Rejected

    try:
        with patch.object(KeithleyNodeEditorDialog, "exec", inspect):
            page._edit_keithley_module_node(node)
    finally:
        page._close_discard_confirmed = True
        page.close()


@pytest.mark.parametrize("device", ["rigol", "anritsu"])
def test_other_device_reviews_use_recipe_instead_of_matching_page_drafts(app, tmp_path, device):
    from app.devices.anritsu_ms2830a.ui.recipe_dialog import AnritsuNodeEditorDialog
    from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog

    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "other-device.yml"))
    if device == "rigol":
        from app.devices.rigol_dg1000z.ui.page import RigolConfigurationSnapshot
        page.set_rigol_snapshot_provider(lambda *args: RigolConfigurationSnapshot(frequency="2 kHz"))
        source = """schema_version: 1
name: Rigol incoming
root:
  id: root
  type: sequence
  children:
    - {id: initial, type: configure_rigol, channel: 1, waveform: SIN, frequency: '1 kHz', high_level: '1 mV', low_level: '-1 mV'}
    - id: selected
      type: sequence
      device_module: rigol
      operation: configure_selected_parameters
      channel: 1
      configuration: {channel: 1, waveform: SIN, frequency: '2 kHz', high_level: '1 mV', low_level: '-1 mV'}
      parameter_actions: [{parameter_id: carrier.frequency, mode: set, value: '2 kHz'}]
      output_policy: unchanged
"""
        cls, key, before = RigolNodeEditorDialog, "frequency", "2 kHz"
        edit = page._edit_rigol_module_node
    else:
        from app.devices.anritsu_ms2830a.ui.page import AnritsuConfigurationSnapshot
        page.set_anritsu_snapshot_provider(lambda: AnritsuConfigurationSnapshot(
            start_hz=2e6, stop_hz=10e6, reference_level_dbm=0, points=101, instrument_mode="PLAN_EDIT"))
        source = """schema_version: 1
name: Anritsu incoming
root:
  id: root
  type: sequence
  children:
    - {id: initial, type: configure_anritsu, start_frequency: '1 MHz', stop_frequency: '10 MHz', reference_level: '0 dBm', points: 101}
    - id: selected
      type: sequence
      device_module: anritsu
      operation: configure_selected_parameters
      configuration: {start_frequency: '2 MHz', stop_frequency: '10 MHz', reference_level: '0 dBm', points: 101}
      parameter_actions: [{parameter_id: spectrum.start_frequency, mode: set, value: '2 MHz'}]
"""
        cls, key, before = AnritsuNodeEditorDialog, "spectrum.start_frequency", "2000000 Hz"
        edit = page._edit_anritsu_module_node
    page._apply_builder_source(source, "Test")
    node = parse_recipe_text(source).root.children[1]

    def inspect(dialog):
        dialog.show()
        app.processEvents()
        table = dialog.review.table
        index, row = next((i, row) for i, row in enumerate(table.rows) if row.key == key)
        assert row.current == before
        assert "Same" in table.item(index, 3).text()
        assert "initial" in table.item(index, 4).text()
        assert table.isVisible()
        dialog.close()
        return QDialog.DialogCode.Rejected

    try:
        with patch.object(cls, "exec", inspect):
            edit(node)
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_legacy_axis_review_exposes_inherited_range(app, tmp_path):
    from app.ui.recipes.sweep_editor import SweepGeneratorDialog
    source = SOURCE.split("    - id: selected")[0] + """    - id: axis
      type: sweep
      target: keithley.A.current
      start: 1 mA
      stop: 2 mA
      points: 2
      children: [{id: measure, type: measure_keithley, channel: A}]
"""
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "axis.yml"))
    page._apply_builder_source(source, "Test")
    node = parse_recipe_text(source).root.children[1]

    def inspect(dialog):
        row = next(row for row in dialog.review.table.rows if row.key == "source_range")
        assert row.planned == "10 mA" and row.source == "baseline"
        assert not any(row.key == "source_level" for row in dialog.review.table.rows)
        return QDialog.DialogCode.Rejected

    try:
        with patch.object(SweepGeneratorDialog, "exec", inspect):
            page._edit_selected_generator(node=node)
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_output_action_compares_recipe_state_instead_of_its_own_yaml(app, tmp_path):
    from app.ui.recipes.common_dialogs import ActionNodeEditorDialog
    source = SOURCE.split("    - id: selected")[0] + "    - {id: on, type: set_keithley_output, channel: A, enabled: true}\n"
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "output.yml"))
    page._apply_builder_source(source, "Test")
    node = parse_recipe_text(source).root.children[1]

    def inspect(dialog):
        table = dialog.review.table
        index, row = next((i, row) for i, row in enumerate(table.rows) if row.key == "enabled")
        assert row.current is None
        assert row.planned is True
        assert "Same" not in table.item(index, 3).text()
        return QDialog.DialogCode.Rejected

    try:
        with patch.object(ActionNodeEditorDialog, "exec", inspect):
            page._edit_action_node(node)
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_channel_switch_keeps_inactive_drafts_hidden(app, tmp_path):
    source = SOURCE.replace("    - id: selected", "    - {id: baseline-b, type: configure_keithley, channel: B, mode: current, level: '0 mA', compliance: '20 mV', source_range: '10 mA', source_autorange: false}\n    - id: selected")
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "channel.yml"))
    page.set_keithley_snapshot_provider(lambda channel="A", *args: KeithleyConfigurationSnapshot(channel=channel, source_range="1 A"))
    page._apply_builder_source(source, "Test")
    node = parse_recipe_text(source).root.children[-1]

    def inspect(dialog):
        dialog.channel.setCurrentText("B")
        app.processEvents()
        panel = dialog.configuration_panel
        assert panel.advanced_ranges_form.isRowVisible(panel.source_range_field)
        assert not panel.source_range_field.isEnabled()
        selector = dialog.parameter_selectors["source.range"]
        selector.setCurrentIndex(selector.findData("set"))
        assert panel.source_range.text() == "10 mA"
        return QDialog.DialogCode.Rejected

    try:
        with patch.object(KeithleyNodeEditorDialog, "exec", inspect):
            page._edit_keithley_module_node(node)
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_expression_channel_action_remains_editable(app, tmp_path):
    from app.ui.recipes.common_dialogs import ActionNodeEditorDialog
    source = SOURCE.split("    - id: selected")[0] + "    - {id: on, type: set_keithley_output, channel: '${channel}', enabled: true}\n"
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "expression.yml"))
    page._apply_builder_source(source, "Test")
    node = parse_recipe_text(source).root.children[-1]

    def inspect(dialog):
        row = next(row for row in dialog.review.table.rows if row.key == "enabled")
        assert row.current is None
        return QDialog.DialogCode.Rejected

    try:
        with patch.object(ActionNodeEditorDialog, "exec", inspect):
            page._edit_action_node(node)
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_rigol_continue_requirement_compares_boolean_output_state(app):
    from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog
    from app.recipes.configuration_context import ConfigurationValue
    dialog = RigolNodeEditorDialog(output_policy="continue")
    dialog.review.table.set_recipe_context(lambda: {"output_enabled": ConfigurationValue(True, "on")})
    row_index = next(i for i, row in enumerate(dialog.review.table.rows) if row.key == "output_enabled")
    try:
        assert "Requirement not met" not in dialog.review.table.item(row_index, 3).text()
        assert dialog.review.table.counts["blocked"] == 0
    finally:
        dialog.close()


def test_literal_editor_shows_station_autorange_policy_used_by_compiler(app, tmp_path):
    from app.settings.models import StationSettings
    raw = audit_settings(tmp_path).model_dump(mode="python")
    raw["devices"]["keithley"]["safety"]["channels"]["A"]["defaults"]["source_autorange"] = True
    settings = StationSettings.model_validate(raw)
    source = SOURCE.split("    - id: selected")[0].replace(", source_autorange: false", "")
    page = RecipePage(settings)
    page.path.setText(str(tmp_path / "auto.yml"))
    page._apply_builder_source(source, "Test")
    node = parse_recipe_text(source).root.children[0]
    request = RecipeCompiler(settings).compile(parse_recipe_text(source)).actions[0].payload["request"]
    assert request.source_autorange and request.source_range_si is None

    def inspect(dialog):
        assert dialog.configuration_snapshot().source_autorange is True
        row = next(row for row in dialog.comparison.rows if row.key == "source_range")
        assert row.planned == "AUTO"
        return QDialog.DialogCode.Rejected

    try:
        with patch.object(KeithleyNodeEditorDialog, "exec", inspect):
            page._edit_legacy_keithley_configuration(node)
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_selected_autorange_action_serializes_auto_instead_of_inactive_manual_range(app):
    dialog = KeithleyNodeEditorDialog(simulation_settings(), snapshot=KeithleyConfigurationSnapshot(
        channel="A", source_autorange=True, source_range="10 mA"))
    try:
        selector = dialog.parameter_selectors["source.range"]
        selector.setCurrentIndex(selector.findData("set"))
        action = next(action for action in dialog.planned_parameter_actions() if action["parameter_id"] == "source.range")
        assert action["value"] == "AUTO"
    finally:
        dialog.close()


def test_coupled_measurement_range_only_is_shown_as_derived(app):
    from app.recipes.configuration_context import ConfigurationValue
    dialog = KeithleyNodeEditorDialog(simulation_settings(), snapshot=KeithleyConfigurationSnapshot(
        channel="A", source_range="1 mA", measure_current_autorange=False, measure_current_range="1 A"))
    dialog.load_plan_actions([{"parameter_id": "measurement.current_range", "mode": "set", "value": "1 A"}], "unchanged")
    dialog.set_recipe_context(lambda: {"source_range": ConfigurationValue("1 mA", "initial")})
    dialog.show()
    app.processEvents()
    try:
        row = next(row for row in dialog.comparison.rows if row.key == "effective_measure_current_range")
        assert row.action == "Derived"
        assert "source range" in row.note
        assert row.planned == "1 mA"
        preference = next(row for row in dialog.comparison.rows if row.key == "measure_current_range")
        assert preference.planned == "1 A" and preference.action == "Set"
    finally:
        dialog.close()


def test_sg_unchanged_frequency_displays_inherited_value(app):
    from app.devices.anritsu_ms2830a import SignalGeneratorSnapshot
    from app.devices.anritsu_ms2830a.ui.recipe_dialog import AnritsuSignalGeneratorNodeEditorDialog
    from app.recipes.configuration_context import ConfigurationValue
    dialog = AnritsuSignalGeneratorNodeEditorDialog(
        current_snapshot=SignalGeneratorSnapshot(frequency_hz=10e6, power_dbm=-30, output_enabled=False, instrument_mode="SG"),
        frequency="999 MHz", power="-20 dBm",
        parameter_actions=[{"parameter_id": "sg.power", "mode": "set", "value": "-20 dBm"}],
    )
    dialog.review.table.set_recipe_context(lambda: {"frequency": ConfigurationValue("10 MHz", "initial")})
    try:
        row = next(row for row in dialog.review.table.rows if row.key == "sg.frequency")
        assert row.action == "Preserve"
        assert row.planned == "10 MHz"
    finally:
        dialog.close()


def test_preserved_range_hydrates_its_policy_before_selecting_set(app):
    from app.recipes.configuration_context import ConfigurationValue
    dialog = KeithleyNodeEditorDialog(simulation_settings(), snapshot=KeithleyConfigurationSnapshot(
        channel="A", source_range="1 A", source_autorange=True, sense_mode="4wire"))
    dialog.load_plan_actions([], "unchanged")
    inherited = {
        "source_range": ConfigurationValue("10 mA", "initial"),
        "source_autorange": ConfigurationValue(False, "initial"),
        "sense_mode": ConfigurationValue("2wire", "initial"),
    }
    dialog.set_recipe_context(lambda: inherited)
    dialog.show()
    app.processEvents()
    try:
        assert dialog.configuration_snapshot().source_autorange is False
        assert dialog.configuration_snapshot().sense_mode == "2wire"
        selector = dialog.parameter_selectors["source.range"]
        selector.setCurrentIndex(selector.findData("set"))
        action = next(action for action in dialog.planned_parameter_actions() if action["parameter_id"] == "source.range")
        assert action["value"] == "10 mA"
    finally:
        dialog.close()
        dialog.deleteLater()
        from PySide6.QtCore import QEvent
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
