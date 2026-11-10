"""Sweep targets are compared with the matching device tab, channel by channel."""
import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtWidgets import QApplication, QDialog
from qfluentwidgets import isDarkTheme

from app.devices.keithley_2600.ui.page import (
    KeithleyConfigurationSnapshot,
    KeithleyNodeEditorDialog,
)
from app.recipes import parse_recipe_text
from app.ui.design_system.tokens import tokens_for
from app.ui.recipes.page import RecipePage
from tests.helpers import simulation_settings


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("channel,card_range", [("A", "1 A"), ("B", "100 mA")])
def test_initial_configuration_compares_with_correct_device_tab(app, tmp_path, channel, card_range):
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "source.yml"))
    cards = {
        "A": KeithleyConfigurationSnapshot(channel="A", source_level="3 mA", source_range="1 A", nplc="8"),
        "B": KeithleyConfigurationSnapshot(channel="B", source_level="5 mA", source_range="100 mA", nplc="4"),
    }
    page.set_keithley_snapshot_provider(lambda requested="A", *_: cards[requested])
    source = f"""schema_version: 1
name: Compare device card
root:
  id: initial
  type: configure_keithley
  channel: {channel}
  mode: current
  level: 0 mA
  compliance: 20 mV
  source_range: 10 mA
  source_autorange: false
"""
    page._apply_builder_source(source, "Test")

    def inspect(dialog):
        dialog.show()
        app.processEvents()
        table = dialog.comparison
        i, row = next((i, r) for i, r in enumerate(table.rows) if r.key == "source_range")
        assert row.current == card_range
        assert row.planned == "10 mA"
        assert "Changes" in table.item(i, 3).text()
        tokens = tokens_for("dark" if isDarkTheme() else "light")
        assert table.item(i, 2).foreground().color().name() == tokens.caution.lower()
        assert "device tab" in table.horizontalHeaderItem(1).text().lower()
        assert "Not set earlier" not in [table.item(i, 1).text() for i in range(table.rowCount())]
        nplc = next(r for r in table.rows if r.key == "nplc")
        assert nplc.current == cards[channel].nplc and nplc.planned == cards[channel].nplc
        j = next(j for j, r in enumerate(table.rows) if r.key == "nplc")
        assert table.item(j, 2).foreground().color().name() == tokens.success.lower()
        assert dialog.nplc.text() == cards[channel].nplc
        dialog.close()
        return QDialog.DialogCode.Accepted

    try:
        with patch.object(KeithleyNodeEditorDialog, "exec", inspect):
            page._edit_legacy_keithley_configuration(parse_recipe_text(source).root)
        assert parse_recipe_text(page.editor.toPlainText()).root.data == parse_recipe_text(source).root.data
    finally:
        page._autosave_timer.stop()
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_earlier_recipe_change_is_visible_even_when_target_matches_tab(app, tmp_path):
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "ordered.yml"))
    card = KeithleyConfigurationSnapshot(channel="A", source_range="1 A")
    page.set_keithley_snapshot_provider(lambda *_: card)
    source = """schema_version: 1
name: Earlier range differs
root:
  id: root
  type: sequence
  children:
    - {id: earlier, type: configure_keithley, channel: A, mode: current, level: '1 mA', compliance: '20 mV', source_range: '10 mA', source_autorange: false}
    - id: target
      type: sequence
      device_module: keithley
      operation: configure_selected_parameters
      channel: A
      source_mode: current
      configuration: {channel: A, source_mode: current, source_range: '1 A'}
      parameter_actions: [{parameter_id: source.range, mode: set, value: '1 A'}]
      output_policy: unchanged
"""
    page._apply_builder_source(source, "Test")

    def inspect(dialog):
        table = dialog.comparison
        i, row = next((i, r) for i, r in enumerate(table.rows) if r.key == "source_range")
        assert row.current == "1 A" and row.planned == "1 A"
        assert "Same" in table.item(i, 3).text()
        assert "earlier" in table.item(i, 4).text()
        assert "recipe-order" in table.summary_text
        return QDialog.DialogCode.Rejected

    try:
        with patch.object(KeithleyNodeEditorDialog, "exec", inspect):
            page._edit_keithley_module_node(parse_recipe_text(source).root.children[1])
    finally:
        page._autosave_timer.stop()
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_main_action_opens_complete_read_only_overview_of_actual_recipe(app, tmp_path):
    from app.ui.recipes.parameter_overview_dialog import SweepParameterOverviewDialog

    source = (Path(__file__).parents[1] / "recipes/example_nested_keithley_ab_fixed_moke2.yml").read_text(encoding="utf-8")
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "inspection.yml"))
    page.set_keithley_snapshot_provider(lambda channel="A", *_: KeithleyConfigurationSnapshot(
        channel=channel, source_range="1 A" if channel == "A" else "100 mA"))
    page._apply_builder_source(source, "Test")
    requested_runs = []
    page.run_requested.connect(lambda *args: requested_runs.append(args))
    page.resize(1360, 900)
    page.show()
    app.processEvents()
    inspected = []
    errors = []

    def inspect():
        dialog = next((widget for widget in QApplication.topLevelWidgets()
                       if isinstance(widget, SweepParameterOverviewDialog) and widget.isVisible()), None)
        try:
            assert dialog is not None
            app.processEvents()
            assert dialog.tree.width() > 600 and dialog.tree.height() > 200
            assert len(dialog.entries) == len(dialog._rows) > 50
            assert all(not item.isHidden() and not item.parent().isHidden()
                       for item, _entry, _search in dialog._rows)
            assert not dialog.only_differences.isChecked()
            entries = dialog.entries
            assert {entry.channel for entry in entries if entry.device == "keithley"} == {"A", "B"}
            assert any(entry.action == "sweep" and entry.channel == "B" for entry in entries)
            assert any(entry.device == "moke_box" and entry.action == "final" for entry in entries)
            recipe = parse_recipe_text(source)
            expected_sources = set()

            def visit(node):
                if node.type != "sequence":
                    expected_sources.add(node.id)
                for child in (*node.children, *node.else_children):
                    visit(child)

            visit(recipe.root)
            for node in recipe.finally_nodes:
                visit(node)
            assert expected_sources <= {entry.source_id for entry in entries}
            range_rows = {entry.channel: entry.card_value for entry in entries
                          if entry.device == "keithley" and entry.parameter == "source_range"}
            assert range_rows == {"A": "1 A", "B": "100 mA"}
            inspected.append(True)
        except Exception as exc:  # noqa: BLE001 - report assertions outside the Qt callback
            errors.append(exc)
        finally:
            if dialog is not None:
                dialog.reject()

    try:
        QTimer.singleShot(0, inspect)
        page.all_device_settings_action.trigger()
        assert not errors, errors
        assert inspected and not requested_runs
        assert page.editor.toPlainText() == source
    finally:
        page._autosave_timer.stop()
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_advanced_anritsu_preserve_compares_earlier_value_with_typed_card(app):
    from app.recipes.configuration_context import ConfigurationValue
    from app.ui.recipes.configuration_comparison import (
        ConfigurationComparison,
        ConfigurationComparisonRow,
    )

    table = ConfigurationComparison()
    table.set_recipe_context(
        lambda: {"rbw": ConfigurationValue("10 kHz", "earlier")},
        lambda: {"advanced.rbw": "100 kHz", "advanced.preamplifier_enabled": False},
    )
    table.set_rows([
        ConfigurationComparisonRow("advanced.rbw", "RBW", None, "100 kHz", "Preserve"),
        ConfigurationComparisonRow("advanced.preamplifier_enabled", "Preamplifier", None, "false", "Set"),
    ])
    try:
        assert table.rows[0].current == "100 kHz" and table.rows[0].planned == "10 kHz"
        assert "Changes" in table.item(0, 3).text()
        assert table.rows[1].current is False and table.rows[1].planned is False
        assert "Same" in table.item(1, 3).text()
    finally:
        table.close()


def test_effective_coupled_range_is_distinct_from_retained_preference(app):
    dialog = KeithleyNodeEditorDialog(simulation_settings(), snapshot=KeithleyConfigurationSnapshot(
        channel="A", source_mode="current", source_range="10 mA", measure_current_range="1 A"))
    dialog.load_plan_actions([{"parameter_id": "source.range", "mode": "set", "value": "10 mA"}], "unchanged")
    dialog.set_recipe_context(dict, lambda: {
        "source_mode": "current", "source_range": "1 A", "source_autorange": False, "measure_current_range": "1 A"})
    try:
        preference = next(row for row in dialog.comparison.rows if row.key == "measure_current_range")
        assert preference.action == "Preserve" and preference.planned == "1 A"
        index, effective = next((i, row) for i, row in enumerate(dialog.comparison.rows)
                                if row.key == "effective_measure_current_range")
        assert effective.current == "1 A" and effective.planned == "10 mA"
        assert "Changes" in dialog.comparison.item(index, 3).text()
    finally:
        dialog.close()


def test_anritsu_card_provider_retains_types_for_overview(app):
    from app.devices.anritsu_ms2830a.adapter import AdvancedSpectrumSnapshot
    from app.devices.anritsu_ms2830a.ui.page import AnritsuConfigurationSnapshot
    from app.recipes.parameter_overview import build_sweep_parameter_entries, entry_comparison

    page = RecipePage(simulation_settings())
    page.set_anritsu_snapshot_provider(
        lambda: AnritsuConfigurationSnapshot(start_hz=1e6, stop_hz=2e6, reference_level_dbm=-30,
                                             points=501, instrument_mode="SA"),
        lambda: AdvancedSpectrumSnapshot(rbw_auto=False, rbw_hz=1e3, vbw_mode="manual", vbw_hz=1e3,
                                         detector="sample", attenuation_auto=False, attenuation_db=10,
                                         preamplifier_enabled=False, sweep_time_auto=True,
                                         sweep_time_s=1, instrument_mode="SA"),
    )
    try:
        values = page._device_tab_comparison_values("anritsu")
        assert type(values["spectrum.points"]) is int
        assert values["advanced.preamplifier_enabled"] is False
        recipe = parse_recipe_text("""schema_version: 1
name: Typed analyzer comparison
root:
  id: root
  type: sequence
  children:
    - {id: configure, type: configure_anritsu, points: 501}
    - id: advanced
      type: sequence
      device_module: anritsu
      operation: configure_selected_parameters
      parameter_actions: [{parameter_id: advanced.preamplifier_enabled, mode: set, value: false}]
""")
        rows = {entry.parameter: entry for entry in build_sweep_parameter_entries(recipe, page._device_tab_comparison_values)}
        assert entry_comparison(rows["points"]) == "same"
        assert entry_comparison(rows["preamplifier_enabled"]) == "same"
    finally:
        page._autosave_timer.stop()
        page._close_discard_confirmed = True
        page.close()
