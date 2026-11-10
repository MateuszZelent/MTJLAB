"""Rendered Rigol editors separate waveform configuration from output wiring."""

from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QDialog

from app.devices.rigol_dg1000z.ui.page import RigolConfigurationSnapshot
from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog
from app.domain.quantities import parse_quantity
from app.recipes import RecipeNode, parse_recipe_text
from app.settings import SettingsRepository
from app.ui.design_system import apply_application_theme
from app.ui.recipes.page import RecipePage

RECIPE = Path("recipes/example_keithley_a_10ma_rigol_dc_0_600mv_fixed_moke.yml")
ARTIFACTS = Path("artifacts/rigol-recipe-modals")


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def contains_widget(host, widget):
    return host.rect().contains(widget.rect().translated(widget.mapTo(host, QPoint())))


@pytest.mark.parametrize("theme,width,height", [("light", 1040, 820), ("dark", 1040, 820), ("light", 760, 740), ("dark", 760, 740)])
def test_initial_dc_editor_has_one_form_and_accessible_comparison(app, theme, width, height):
    apply_application_theme(app, theme)
    snapshot = RigolConfigurationSnapshot(waveform="DC", high_level="0 mV", low_level="0 mV")
    dialog = RigolNodeEditorDialog(snapshot=snapshot, carrier_only=True)
    try:
        dialog.resize(width, height)
        dialog.show()
        dialog.review.toggle.setChecked(True)
        app.processEvents()
        assert "waveform" in dialog.windowTitle().lower()
        assert not dialog.open_roi_button.isVisibleTo(dialog)
        assert not dialog.output_policy.isVisibleTo(dialog)
        assert not dialog.output_polarity.isVisibleTo(dialog)
        assert dialog.offset.isVisibleTo(dialog) and dialog.offset.isEnabled()
        assert contains_widget(dialog.controls_scroll.viewport(), dialog.offset)
        assert contains_widget(dialog, dialog.apply_button)
        dialog.controls_scroll.ensureWidgetVisible(dialog.review.table)
        app.processEvents()
        assert dialog.review.table.height() >= 120
        assert contains_widget(dialog.controls_scroll.viewport(), dialog.review.table)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(ARTIFACTS / f"waveform-review-{theme}-{width}.png"))
        dialog.controls_scroll.verticalScrollBar().setValue(0)
        app.processEvents()
        assert dialog.grab().save(str(ARTIFACTS / f"waveform-{theme}-{width}.png"))
        dialog.offset.setText("15 mV")
        updated = dialog.configuration_snapshot()
        assert parse_quantity(updated.high_level).si_value == .015
        assert updated.high_level == updated.low_level
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("theme,width,height", [("light", 1040, 820), ("dark", 1040, 820), ("light", 760, 740), ("dark", 760, 740)])
def test_output_editor_renders_both_sections_and_preserves_recipe(app, tmp_path, theme, width, height):
    from app.devices.rigol_dg1000z.ui.output_recipe_dialog import RigolOutputNodeEditorDialog

    apply_application_theme(app, theme)
    settings = SettingsRepository(Path(".config/settings.yml")).load().settings
    source = RECIPE.read_text(encoding="utf-8")
    node = next(n for n in parse_recipe_text(source).root.children if n.type == "configure_rigol_output")
    page = RecipePage(settings)
    page.path.setText(str(tmp_path / "modal.yml"))
    page._apply_builder_source(source, "Render test")

    def inspect(dialog):
        dialog.resize(width, height)
        dialog.show()
        app.processEvents()
        assert "output" in dialog.windowTitle().lower()
        assert dialog.node_fields() == node.data
        assert dialog.review.toggle.isChecked()  # Actual recipe context is bound.
        assert contains_widget(dialog, dialog.apply_button)
        for key in ("output_load", "polarity", "mode", "sync_enabled", "sync_delay"):
            widget = dialog._editors[key][0]
            dialog.controls_scroll.ensureWidgetVisible(widget)
            app.processEvents()
            assert contains_widget(dialog.controls_scroll.viewport(), widget)
            assert widget.width() >= 100
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        for key in ("sync_delay", "output_load"):
            dialog.controls_scroll.ensureWidgetVisible(dialog._editors[key][0])
            app.processEvents()
            assert dialog.grab().save(str(ARTIFACTS / f"output-{key}-{theme}-{width}.png"))
        dialog._editors["sync_enabled"][0].setChecked(True)
        expected = {**node.data, "sync_enabled": True}
        assert dialog.node_fields() == expected
        return QDialog.DialogCode.Accepted

    try:
        with patch.object(RigolOutputNodeEditorDialog, "exec", inspect):
            page._edit_action_node(node)
        saved = parse_recipe_text(page._builder_source())
        updated = next(n for n in saved.root.children if n.id == node.id)
        assert updated.data == {**node.data, "sync_enabled": True}
        assert len(saved.root.children) == len(parse_recipe_text(source).root.children)
    finally:
        page._autosave_timer.stop()
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        app.processEvents()


def test_partial_output_editor_keeps_omitted_fields_and_validates_units(app):
    from app.devices.rigol_dg1000z.ui.output_recipe_dialog import RigolOutputNodeEditorDialog

    node = RecipeNode("sync-only", "configure_rigol_output", {"channel": 1, "sync_delay": "0 s"})
    dialog = RigolOutputNodeEditorDialog(node)
    try:
        assert dialog.node_fields() == node.data
        delay = dialog._editors["sync_delay"][0]
        delay.setText("2 mV")
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert dialog.validation_message.text()
        delay.setText("1 ms")
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.node_fields() == {"channel": 1, "sync_delay": "1 ms"}
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()
