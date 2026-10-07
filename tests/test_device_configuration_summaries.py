"""Offline mutation counts must reflect execution semantics and real baselines."""
import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QWidget, QFormLayout
from app.devices.rigol_dg1000z.ui.page import RigolConfigurationSnapshot
from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog
from app.devices.anritsu_ms2830a import SignalGeneratorSnapshot
from app.devices.anritsu_ms2830a.ui.recipe_dialog import AnritsuNodeEditorDialog, AnritsuSignalGeneratorNodeEditorDialog
from app.ui.recipes.common_dialogs import FixedValueDialog, ActionNodeEditorDialog, KeithleySweepBuilderDialog
from app.recipes import RecipeNode
from tests.helpers import simulation_settings


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_rigol_full_configuration_counts_hidden_action_defaults_and_channel_baselines(app):
    current = RigolConfigurationSnapshot()
    baselines = {1: current, 2: replace(current, channel=2, frequency="3 kHz")}
    dialog = RigolNodeEditorDialog(snapshot=current, snapshot_resolver=baselines.get)
    try:
        assert dialog.review.table.counts["changed"] == 0
        dialog.frequency.setText("0.002 MHz")
        assert dialog.review.table.counts["changed"] == 1
        # 'Unchanged' means use the full carrier snapshot, not omit its writes.
        assert dialog.parameter_selectors["carrier.frequency"].currentData() == "unchanged"
        dialog.channel.setCurrentIndex(dialog.channel.findData(2))
        assert dialog.review.table.counts["changed"] == 0
        dialog.vpp.setText("4 mV")
        assert dialog.review.table.counts["changed"] == 2  # high and low physical levels
        dialog.vpp.setText("invalid")
        assert dialog.review.table.counts["changed"] == 2
    finally:
        dialog.close()


def test_anritsu_only_selected_fields_count_and_missing_baseline_is_unknown(app):
    dialog = AnritsuNodeEditorDialog(simulation_settings(), current_values={"spectrum.start_frequency": "1 MHz"})
    try:
        dialog.configuration_panel.start.setText("2 MHz")
        assert dialog.review.table.counts["changed"] == 0
        selector = dialog.parameter_selectors["spectrum.start_frequency"]
        selector.setCurrentIndex(selector.findData("set"))
        assert dialog.review.table.counts["changed"] == 1
        unknown = dialog.parameter_selectors["advanced.detector"]
        unknown.setCurrentIndex(unknown.findData("set"))
        assert dialog.review.table.counts["unknown"] == 1
        selector.setCurrentIndex(selector.findData("sweep"))
        assert dialog.review.table.counts["changed"] == 0
        assert dialog.review.table.counts["sweep"] == 1
    finally:
        dialog.close()


def test_sg_writes_both_fields_and_never_substitutes_defaults_for_current(app):
    dialog = AnritsuSignalGeneratorNodeEditorDialog(current_snapshot=SignalGeneratorSnapshot(1e9, -30, False, "SPECT"))
    try:
        assert dialog.review.table.counts["same"] == 2
        dialog.power.setText("-20 dBm")
        assert dialog.review.table.counts["changed"] == 1
    finally:
        dialog.close()
    dialog = AnritsuSignalGeneratorNodeEditorDialog()
    assert dialog.review.table.counts["unknown"] == 2
    dialog.close()


def test_moke_fixed_value_uses_device_page_draft(app):
    parent = QWidget()
    parent.parameter_snapshot_provider = lambda: {"moke_box.vout0.voltage": "0 V"}
    dialog = FixedValueDialog(dict(label="MOKE voltage", dimension="voltage", target="moke_box.vout0.voltage"), parent)
    try:
        dialog.value.setText("0 V")
        assert dialog.review.table.counts["same"] == 1
        dialog.value.setText("20 mV")
        assert dialog.review.table.counts["changed"] == 1
    finally:
        dialog.close()
        parent.close()


def test_action_counter_is_explicitly_relative_to_saved_action(app):
    dialog = ActionNodeEditorDialog(RecipeNode("wait", "wait", {"duration": "1 s"}))
    try:
        dialog._editors["duration"][0].setText("1000 ms")
        assert dialog.review.table.counts["changed"] == 0
        dialog._editors["duration"][0].setText("2 s")
        assert dialog.review.table.counts["changed"] == 1
    finally:
        dialog.close()


def test_keithley_generator_settings_scroll_instead_of_overlapping(app):
    dialog = KeithleySweepBuilderDialog(simulation_settings())
    try:
        dialog.resize(900, 700)
        dialog.review.toggle.setChecked(True)
        dialog.show()
        app.processEvents()
        form = dialog.parameter_scroll.widget().findChild(QFormLayout)
        rectangles = [form.itemAt(i, QFormLayout.ItemRole.FieldRole).geometry()
                      for i in range(form.rowCount())]
        for index, rect in enumerate(rectangles):
            assert rect.height() >= 24
            assert all(not rect.intersects(other) for other in rectangles[:index])
        assert dialog.parameter_scroll.verticalScrollBar().maximum() > 0
        assert not dialog.plot.geometry().intersects(dialog.preview.geometry())
        assert not dialog.parameter_scroll.geometry().intersects(dialog.segments.geometry())
        assert {row.key for row in dialog.review.table.rows} == {
            "source_level", "source_mode", "compliance", "nplc", "settling_time", "sense_mode",
        }
    finally:
        dialog.close()
