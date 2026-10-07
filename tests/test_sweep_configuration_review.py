"""Visible recipe mutation review, snapshot fidelity, and modal isolation."""

import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from app.devices.keithley_2600.ui.page import (
    KeithleyConfigurationSnapshot, KeithleyNodeEditorDialog,
)
from app.ui.design_system import apply_application_theme
from app.ui.recipes.configuration_comparison import (
    ConfigurationComparison, ConfigurationComparisonRow, equivalent_setting,
)
from app.ui.recipes.page import RecipePage
from app.recipes import parse_recipe_text
from tests.helpers import simulation_settings


@pytest.fixture
def app():
    application = QApplication.instance() or QApplication([])
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        font = Path("C:/Windows/Fonts") / name
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    application.setFont(QFont("Segoe UI", 10))
    yield application


def snapshot(**kwargs):
    return replace(KeithleyConfigurationSnapshot(channel="A", source_range="10 mA"), **kwargs)


def effects(dialog):
    return {row.key: dialog.comparison.item(i, 3).text()
            for i, row in enumerate(dialog.comparison.rows)}


def test_summary_counts_actual_writes_separately_from_requirements_and_unknowns(app):
    table = ConfigurationComparison()
    row = ConfigurationComparisonRow
    table.set_rows([
        row("level", "Current", "1 mA", "2 mA", "Set"),
        row("delay", "Delay", "100 ms", "0.1 s", "Set"),
        row("nplc", "NPLC", "1", "8", "Preserve"),
        row("range", "Range", None, "10 mA", "Set"),
        row("axis", "Axis", "0 V", "1 V", "Sweep"),
        row("mode", "Mode", "voltage", "current", "Require"),
    ])
    assert table.counts == dict(changed=1, same=1, preserved=1, unknown=1, sweep=1, blocked=1)
    assert table.summary_text.startswith("1 parameter changed")
    assert table.item(5, 3).text() == "Require · Requirement not met"
    table.close()


@pytest.mark.parametrize("carrier_only", [False, True])
@pytest.mark.parametrize("policy", ["unchanged", "off", "on", "on_keep", "continue"])
def test_rigol_review_distinguishes_configuration_from_selected_updates(app, carrier_only, policy):
    from app.devices.rigol_dg1000z.ui.page import RigolConfigurationSnapshot
    from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog

    current = RigolConfigurationSnapshot(channel=1)
    dialog = RigolNodeEditorDialog(
        snapshot=current, current_snapshot_resolver=lambda channel: current,
        carrier_only=carrier_only, output_policy=policy,
        parameter_actions=[{"parameter_id": "carrier.frequency", "mode": "set", "value": "2 kHz"}],
    )
    try:
        dialog.resize(1120, 780)
        dialog.show()
        dialog.review.toggle.setChecked(True)
        app.processEvents()
        rows = {row.key: row for row in dialog.review.table.rows}
        assert rows["frequency"].action == "Set"
        assert rows["phase_deg"].action == ("Set" if carrier_only else "Preserve")
        assert rows["high_level"].action == ("Set" if carrier_only else "Preserve")
        assert dialog.waveform.isEnabled() == carrier_only
        assert dialog.phase.isEnabled() == carrier_only
        assert not dialog.sync_enabled.isEnabled()
        assert dialog.parameter_selectors["carrier.frequency"].isEnabled() != carrier_only
        output = rows["output_enabled"]
        if carrier_only:
            assert output.action == "Set" and output.planned == "OFF; no automatic ON"
            for key in ("modulation_enabled", "frequency_sweep_enabled", "burst_enabled",
                        "harmonics_enabled", "waveform_sum_enabled", "voltage_unit"):
                assert rows[key].action == "Require"
                assert rows[key].current is None
        else:
            assert "voltage_unit" not in rows
            assert output.action == ("Preserve" if policy == "unchanged" else
                                     "Require" if policy == "continue" else "Set")
            assert "during configuration" not in str(output.planned)
            assert dialog.output_policy.itemText(0) == "Leave OUTPUT unchanged"
        for widget in (dialog.review, dialog.content_splitter, dialog.apply_button):
            rect = widget.rect().translated(widget.mapTo(dialog, QPoint()))
            assert dialog.rect().contains(rect)
            assert widget.isVisibleTo(dialog)
        assert not dialog.grab().isNull()
    finally:
        dialog.close()


@pytest.mark.parametrize("mode", ["set", "sweep"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_rigol_retarget_preserves_authored_actions_and_refreshes_review(app, mode, theme):
    from app.devices.rigol_dg1000z.ui.page import RigolConfigurationSnapshot
    from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog

    apply_application_theme(app, theme)
    source = RigolConfigurationSnapshot(channel=1, frequency="1 kHz")
    destination = replace(source, channel=2, waveform="DC", frequency="900 kHz",
                          high_level="20 mV", low_level="10 mV", phase_deg="37")
    actions = [
        {"parameter_id": "carrier.frequency", "mode": mode, "value": "3 kHz",
         "segments": [{"start": "3 kHz", "stop": "5 kHz", "points": 3}]},
        {"parameter_id": "carrier.high_level", "mode": "set", "value": "2 mV"},
    ]
    dialog = RigolNodeEditorDialog(snapshot=source,
        snapshot_resolver=lambda channel: source if channel == 1 else destination,
        parameter_actions=actions, output_policy="continue")
    try:
        dialog.resize(1120, 780)
        dialog.show()
        dialog.review.toggle.setChecked(True)
        app.processEvents()
        dialog.frequency.setText("4 kHz")
        dialog._sync_period_from_frequency()
        before = dialog.configuration_snapshot()
        before_actions = dialog.planned_parameter_actions()
        dialog.channel.setCurrentIndex(dialog.channel.findData(2))
        app.processEvents()
        assert dialog.configuration_snapshot() == replace(before, channel=2)
        assert dialog.planned_parameter_actions() == before_actions
        assert dialog.selected_output_policy() == "continue"
        rows = {row.key: row for row in dialog.review.table.rows}
        assert rows["frequency"].current == "900 kHz"
        assert rows["frequency"].planned == "4 kHz"
        assert rows["frequency"].action == ("Sweep" if mode == "sweep" else "Set")
        for widget in (dialog.review, dialog.content_splitter, dialog.apply_button):
            rect = widget.rect().translated(widget.mapTo(dialog, QPoint()))
            assert dialog.rect().contains(rect)
            assert widget.isVisibleTo(dialog)
        if mode == "sweep":
            target = Path("docs/audits/2026-10-06-source-review") / f"rigol-retarget-{theme}.png"
            assert dialog.grab().save(str(target))
        dialog.channel.setCurrentIndex(dialog.channel.findData(1))
        app.processEvents()
        assert dialog.configuration_snapshot() == before
        assert dialog.planned_parameter_actions() == before_actions
    finally:
        dialog.close()


def test_rigol_period_serialization_does_not_depend_on_focus_event(app):
    from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog
    dialog = RigolNodeEditorDialog(parameter_actions=[
        {"parameter_id": "carrier.frequency", "mode": "set", "value": "1 kHz"}])
    try:
        dialog.time_mode.setCurrentText("Period")
        dialog.period.setText("250 us")
        assert dialog.frequency.text() == "1 kHz"  # editingFinished has not run.
        assert dialog.configuration_snapshot().frequency == "4000 Hz"
        assert dialog.planned_parameter_actions()[0]["value"] == "4000 Hz"
    finally:
        dialog.close()


@pytest.mark.parametrize("period", ["0 s", "-1 ms", "invalid", "1 V"])
def test_rigol_invalid_period_cannot_accept_stale_frequency(app, period):
    from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog
    dialog = RigolNodeEditorDialog(parameter_actions=[
        {"parameter_id": "carrier.frequency", "mode": "set", "value": "1 kHz"}])
    try:
        dialog.time_mode.setCurrentText("Period")
        dialog.period.setText(period)
        with patch("app.devices.rigol_dg1000z.ui.recipe_dialog.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        assert dialog.result() != QDialog.DialogCode.Accepted
    finally:
        dialog.close()


@pytest.mark.parametrize("field,value", [
    ("vpp", "invalid"), ("vpp", "1 A"), ("vpp", "-2 mV"),
    ("offset", "invalid"), ("offset", "2 Hz"),
])
def test_rigol_invalid_amplitude_offset_cannot_reuse_old_levels(app, field, value):
    from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog
    from app.domain.errors import ConfigurationError

    dialog = RigolNodeEditorDialog(carrier_only=True)
    try:
        dialog.level_mode.setCurrentText("Amplitude / Offset")
        original = (dialog.high_level.text(), dialog.low_level.text())
        getattr(dialog, field).setText(value)
        dialog._sync_levels_from_vpp_offset()  # Incomplete typing is allowed.
        assert (dialog.high_level.text(), dialog.low_level.text()) == original
        with pytest.raises((ConfigurationError, ValueError)):
            dialog.configuration_snapshot()
        with pytest.raises((ConfigurationError, ValueError)):
            dialog.planned_parameter_actions()
        with patch("app.devices.rigol_dg1000z.ui.recipe_dialog.QMessageBox.warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        assert dialog.result() != QDialog.DialogCode.Accepted
    finally:
        dialog.close()


def test_rigol_amplitude_offset_serializes_current_values_without_focus_event(app):
    from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog
    dialog = RigolNodeEditorDialog(carrier_only=True)
    try:
        dialog.level_mode.setCurrentText("Amplitude / Offset")
        dialog.vpp.setText("6 mV")
        dialog.offset.setText("1 mV")
        result = dialog.configuration_snapshot()
        assert result.high_level == "4 mV"
        assert result.low_level == "-2 mV"
    finally:
        dialog.close()


@pytest.mark.parametrize("channel", ["A", "B"])
def test_summary_updates_for_both_channels(app, channel):
    current = snapshot(channel=channel)
    dialog = KeithleyNodeEditorDialog(
        simulation_settings(), snapshot=current, snapshot_resolver=lambda *args: current,
        full_configuration=True,
    )
    try:
        assert dialog.comparison.counts["changed"] == 0
        dialog.level.setText("2 mA")
        assert dialog.comparison.counts["changed"] == 1
        assert dialog.change_summary.text().startswith("1 parameter changed")
        dialog.level.setText(current.source_level)
        assert dialog.comparison.counts["changed"] == 0
    finally:
        dialog.close()


@pytest.mark.parametrize("edit", [None, "phase_deg", "high_level", "channel"])
def test_rigol_literal_editor_preserves_omitted_fields(app, tmp_path, edit):
    from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog

    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "rigol-review.yml"))
    source = """schema_version: 1
name: Rigol explicit fields
root:
  id: root
  type: sequence
  children:
    - {id: carrier, type: configure_rigol, channel: 1, waveform: SIN, frequency: '1 kHz', high_level: '1 mV', low_level: '-1 mV'}
"""
    page._apply_builder_source(source, "Test")
    original = parse_recipe_text(source).root.children[0]

    def accept(dialog):
        rows = {row.key: row for row in dialog.review.table.rows}
        assert rows["output_load"].action == "Preserve"
        assert rows["phase_deg"].action == "Preserve"
        if edit == "phase_deg":
            dialog.phase.setText("25")
            assert dialog.programmed_configuration_fields() - set(original.data) == {"phase_deg"}
        elif edit == "high_level":
            dialog.high_level.setText("2 mV")
            dialog._sync_vpp_offset_from_levels()
        elif edit == "channel":
            from app.devices.rigol_dg1000z.ui.page import RigolConfigurationSnapshot
            dialog._review_baselines[2] = RigolConfigurationSnapshot(
                channel=2, phase_deg="37", output_load="50", frequency="900 kHz",
                high_level="10 mV", low_level="-5 mV",
            )
            dialog.channel.setCurrentIndex(dialog.channel.findData(2))
            assert dialog.phase.text() == "0"
            assert dialog.frequency.text() == "1 kHz"
            assert dialog.programmed_configuration_fields() == set(original.data)
        return QDialog.DialogCode.Accepted

    try:
        with patch.object(RigolNodeEditorDialog, "exec", accept):
            page._edit_legacy_rigol_configuration(original)
        data = parse_recipe_text(page.editor.toPlainText()).root.children[0].data
        expected = dict(original.data)
        if edit == "phase_deg":
            expected["phase_deg"] = 25.0
        elif edit == "high_level":
            expected["high_level"] = "2 mV"
        elif edit == "channel":
            expected["channel"] = 2
        assert data == expected
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_comparison_si_actions_unknown_and_live_edits(app):
    current = snapshot(source_level="0.001 A", nplc="8", sense_mode="4wire")
    dialog = KeithleyNodeEditorDialog(
        simulation_settings(), snapshot=snapshot(), snapshot_resolver=lambda *args: current,
    )
    try:
        dialog.load_plan_actions([{"parameter_id": "source.level", "mode": "set"}], "unchanged")
        assert effects(dialog)["source_level"] == "Set · Same"
        assert effects(dialog)["nplc"] == "Preserve · Not programmed"
        dialog.level.setText("2 mA")
        assert effects(dialog)["source_level"] == "Set · Changes"
        selector = dialog.parameter_selectors["source.level"]
        selector.setCurrentIndex(selector.findData("sweep"))
        assert "Changes over ROI" in effects(dialog)["source_level"]
        assert equivalent_setting("100 ms", "0.1 s")
        assert not equivalent_setting("1 mA", "1 A")
        assert not equivalent_setting("1 V", "1 A")
        assert not equivalent_setting(True, "True")
    finally:
        dialog.close()


def test_plan_snapshot_preserves_sense_and_rejected_autorange_instead_of_replacing(app):
    requested = snapshot(sense_mode="4wire", source_autorange=True, source_range="AUTO")
    dialog = KeithleyNodeEditorDialog(simulation_settings(), snapshot=requested, full_configuration=True)
    try:
        assert dialog.configuration_snapshot() == requested
        assert "PROHIBITED" in effects(dialog)["sense_mode"]
        assert dialog.configuration_panel.sense_mode.currentText() == "4wire"
        assert dialog.configuration_panel.source_autorange.isChecked()
        # Validation must reject the prohibited stored setting, not silently rewrite it.
        with patch("app.devices.keithley_2600.ui.page.QMessageBox.warning"):
            assert not dialog._validate()
    finally:
        dialog.close()


def test_literal_editor_apply_does_not_add_omitted_writes(app, tmp_path):
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "review.yml"))
    source = """schema_version: 1
name: preserve omitted fields
root:
  id: root
  type: sequence
  children:
    - {id: source, type: configure_keithley, channel: A, mode: current, level: '1 mA', compliance: '20 mV', source_range: '10 mA'}
"""
    page._apply_builder_source(source, "Test")
    page.set_keithley_snapshot_provider(lambda *args: snapshot(nplc="8", sense_mode="4wire"))
    node = parse_recipe_text(source).root.children[0]
    observed = []

    def accept(dialog):
        observed.append(effects(dialog))
        return QDialog.DialogCode.Accepted

    try:
        with patch.object(KeithleyNodeEditorDialog, "exec", accept):
            page._edit_legacy_keithley_configuration(node)
        data = parse_recipe_text(page.editor.toPlainText()).root.children[0].data
        assert "nplc" not in data and "sense_mode" not in data
        assert "measure_voltage_autorange" not in data
        assert observed[0]["nplc"] == "Preserve · Not programmed"
        assert data["level"] == "1 mA"
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_editing_omitted_field_is_explicit_and_only_adds_that_field(app):
    dialog = KeithleyNodeEditorDialog(
        simulation_settings(), snapshot=snapshot(), full_configuration=True,
        programmed_fields={"channel", "source_mode", "source_level", "compliance", "source_range"},
    )
    try:
        before = dialog.programmed_configuration_fields()
        dialog.nplc.setText("8")
        assert dialog.programmed_configuration_fields() - before == {"nplc"}
        assert effects(dialog)["nplc"].startswith("Set")
        assert effects(dialog)["sense_mode"].startswith("Preserve")
    finally:
        dialog.close()


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("use_provider", [False, True])
def test_omitted_settling_displays_channel_preference_and_stays_omitted(app, tmp_path, channel, use_provider):
    page = RecipePage(simulation_settings())
    page.path.setText(str(tmp_path / "settling-review.yml"))
    source = f"""schema_version: 1
name: preserved software settling
root:
  id: root
  type: sequence
  children:
    - {{id: bias, type: configure_keithley, channel: {channel}, mode: current, level: '1 mA', compliance: '20 mV', source_range: '10 mA'}}
"""
    page._apply_builder_source(source, "Test")
    if use_provider:
        def provider(requested_channel="B", mode=None):
            return snapshot(channel=requested_channel, settling_time="250 ms" if requested_channel == channel else "5 s")
        page.set_keithley_snapshot_provider(provider)
    node = parse_recipe_text(source).root.children[0]
    observed = []
    def accept(dialog):
        dialog.resize(1120, 780)
        dialog.show()
        app.processEvents()
        assert dialog.settle.text() == ("250 ms" if use_provider else "100 ms")
        assert dialog.settle.isVisibleTo(dialog)
        assert "settling_time" not in dialog.programmed_configuration_fields()
        assert effects(dialog)["settling_time"] == "Preserve · Not programmed"
        observed.append(dialog.configuration_snapshot())
        assert dialog.grab().save(str(tmp_path / f"settling-{channel}-{use_provider}.png"))
        return QDialog.DialogCode.Accepted
    try:
        with patch.object(KeithleyNodeEditorDialog, "exec", accept):
            page._edit_legacy_keithley_configuration(node)
        assert observed
        data = parse_recipe_text(page.editor.toPlainText()).root.children[0].data
        assert "settle_time" not in data and "settling_time" not in data
    finally:
        page._close_discard_confirmed = True
        page.close()


def test_main_keithley_page_exposes_default_software_settling(app, tmp_path):
    from unittest.mock import Mock
    from app.devices.keithley_2600.ui.page import KeithleyPage
    page = KeithleyPage(Mock(), simulation_settings())
    try:
        page.resize(1360, 900)
        page.show()
        app.processEvents()
        for channel in ("A", "B"):
            page.channel.setCurrentText(channel)
            app.processEvents()
            assert page.configuration_snapshot_for(channel).settling_time == "100 ms"
            assert page.settle.text() == "100 ms"
            assert page.settle.isVisibleTo(page)
            assert page.settle.width() > 0 and page.settle.height() > 0
        assert page.grab().save(str(tmp_path / "keithley-main-settling.png"))
    finally:
        page.close()


@pytest.mark.parametrize("width,height,theme", [(1120, 780, "light"), (1120, 780, "dark"), (760, 640, "light")])
@pytest.mark.parametrize("full", [True, False])
def test_rendered_fields_and_footer_are_inside_one_modal(app, width, height, theme, full):
    apply_application_theme(app, theme)
    current = snapshot(source_level="2 mA", nplc="8")
    dialog = KeithleyNodeEditorDialog(
        simulation_settings(), snapshot=snapshot(), full_configuration=full,
        snapshot_resolver=lambda *args: current,
    )
    try:
        dialog.resize(width, height)
        dialog.show()
        for _ in range(10):
            app.processEvents()
        for widget in (dialog.comparison, dialog.workspace.widget(0), dialog.apply_button):
            rect = widget.rect().translated(widget.mapTo(dialog, QPoint()))
            assert dialog.rect().contains(rect)
            assert widget.isVisibleTo(dialog) and widget.height() > 20
        dialog.workspace.widget(0).ensureWidgetVisible(dialog.level)
        app.processEvents()
        assert dialog.level.visibleRegion().boundingRect().height() > 10
        assert dialog.apply_button.visibleRegion().boundingRect().height() > 20
        assert dialog.workspace.height() > 100
        panel = dialog.configuration_panel
        scroll = dialog.workspace.widget(0)
        for widget in (panel.sense_mode, *panel._advanced_range_widgets):
            assert widget.isVisibleTo(dialog)
            assert not panel.advanced_ranges_dialog.isAncestorOf(widget)
            scroll.ensureWidgetVisible(widget)
            app.processEvents()
            assert widget.visibleRegion().boundingRect().height() > 10
        scroll.verticalScrollBar().setValue(0)
        app.processEvents()
        out = Path("docs/audits/2026-10-05-requested-sweep")
        out.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(out / f"configuration-review-{full}-{width}-{theme}.png"))
    finally:
        dialog.close()


def test_reentrant_modal_activation_does_not_stack_editors(app):
    parent = QWidget()
    first = KeithleyNodeEditorDialog(simulation_settings(), parent)
    second = KeithleyNodeEditorDialog(simulation_settings(), parent)
    results = []

    def duplicate_activation():
        results.append(second.exec())
        results.append(second.isVisible())
        first.reject()

    QTimer.singleShot(0, duplicate_activation)
    try:
        assert first.exec() == QDialog.DialogCode.Rejected
        assert results == [QDialog.DialogCode.Rejected, False]
        assert not first._active_editors
    finally:
        first.close()
        second.close()
        parent.close()
