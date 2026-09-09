"""Default fixed ranges and Settings-only AUTO exceptions at each boundary."""
import os
from dataclasses import replace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtWidgets import QApplication

from app.devices.keithley_2600.adapter import KeithleyAdapter
from app.devices.keithley_2600.characterization.models import CharacterizationSweepConfig
from app.devices.keithley_2600.characterization.runner import KeithleyCharacterizationRunner
from app.devices.keithley_2600.ui.page import (
    KeithleyConfigurationPanel,
    KeithleyConfigurationSnapshot,
)
from app.devices.simulators import KeithleySimulator
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import SafetyViolation
from app.engine.compiler import RecipeCompiler
from app.safety.keithley import KeithleySourceRequest, validate_keithley_source
from tests.helpers import simulation_settings as base_settings


def simulation_settings():
    settings = base_settings()
    settings.keithley.safety.channels["A"] = settings.keithley.safety.channels["A"].model_copy(update={"enabled": True})
    return settings



@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("mode,level,compliance,fixed", [
    ("current", 100e-6, .05, .001), ("voltage", .05, .001, .1),
])
def test_rejects_autorange_or_missing_range_before_io(channel, mode, level, compliance, fixed):
    settings = simulation_settings()
    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    for autorange, selected in [(True, None), (True, fixed), (False, None), (False, 0), (False, float("nan"))]:
        request = KeithleySourceRequest(channel, mode, level, compliance,
                                       source_autorange=autorange, source_range_si=selected)
        before = tuple(session.commands)
        with pytest.raises(SafetyViolation):
            adapter.configure_source(request)
        assert tuple(session.commands) == before


@pytest.mark.parametrize("channel,fixed", [("A", .01), ("B", 1.)])
def test_compiler_requires_explicit_range_and_never_emits_source_auto_on(channel, fixed):
    compiler = RecipeCompiler(simulation_settings())
    data = {"channel": channel, "mode": "current", "level": "100 uA", "compliance": "50 mV"}
    with pytest.raises(SafetyViolation, match="fixed"):
        compiler._compile_keithley(data, "test")
    with pytest.raises(SafetyViolation, match="autorange"):
        compiler._compile_keithley(dict(data, source_autorange=True), "test")
    request = compiler._compile_keithley(dict(data, source_range=f"{fixed} A"), "test")["request"]
    assert request.source_autorange is False
    assert request.source_range_si == fixed
    session = Mock()
    KeithleyAdapter._configure_ranges_and_sense(session, "smu" + channel.lower(), request)
    commands = [call.args[0] for call in session.write.call_args_list]
    assert any("source.autorangei" in command and "AUTORANGE_OFF" in command for command in commands)
    assert not any("source.autorange" in command and "AUTORANGE_ON" in command for command in commands)


@pytest.mark.parametrize("channel", ["A", "B"])
def test_fixed_range_boundary_and_characterization_preflight(channel):
    settings = simulation_settings()
    request = KeithleySourceRequest(channel, "current", .001, .05, source_range_si=.001)
    validate_keithley_source(settings.keithley.safety.channels[channel], request)
    validate_keithley_source(settings.keithley.safety.channels[channel], replace(request, level_si=.000999))
    with pytest.raises(SafetyViolation, match="whole sweep|outside"):
        validate_keithley_source(settings.keithley.safety.channels[channel], replace(request, level_si=.001001))
    config = CharacterizationSweepConfig(channel=channel, start_level_si=.0001,
                                        stop_level_si=.002, compliance_si=.05, source_range_si=.001)
    with pytest.raises(SafetyViolation):
        KeithleyCharacterizationRunner.validate_preflight(config, settings)
    device = Mock()
    with pytest.raises(SafetyViolation):
        KeithleyCharacterizationRunner.run_sweep(device, config)
    assert not device.mock_calls
    for bad in [replace(config, source_autorange=True), replace(config, source_range_si=None)]:
        with pytest.raises(SafetyViolation):
            KeithleyCharacterizationRunner.run_sweep(device, bad)
        assert not device.mock_calls


@pytest.mark.parametrize("channel", ["A", "B"])
def test_measure_only_has_no_source_range(channel):
    settings = simulation_settings()
    request = KeithleySourceRequest(channel, "measure_only", 0, 0)
    validate_keithley_source(settings.keithley.safety.channels[channel], request)
    assert not request.source_autorange


def test_source_selector_is_fixed_and_visible_at_desktop_size(tmp_path):
    app = QApplication.instance() or QApplication([])
    from pathlib import Path

    from PySide6.QtGui import QFont, QFontDatabase
    for filename in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        font = Path("C:/Windows/Fonts") / filename
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))
    app.setFont(QFont("Segoe UI", 10))
    panel = KeithleyConfigurationPanel(simulation_settings())
    panel.resize(1100, 760)
    panel.show()
    try:
        for channel, fixed in [("A", "10 mA"), ("B", "1 A")]:
            panel.load_snapshot(KeithleyConfigurationSnapshot(channel=channel, source_range=fixed))
            panel.set_advanced_ranges_expanded(False)
            app.processEvents()
            assert not panel.source_autorange.isChecked()
            assert not panel.source_autorange.isEnabled()
            assert panel.source_range.isVisible()
            assert panel.source_range.width() > 30
            assert panel.source_range.isEnabled()
            assert panel.snapshot().source_range == fixed
        assert panel.grab().save(str(tmp_path / "fixed-source-range.png"))
        settings = panel._settings
        settings.keithley.safety.channels["B"].defaults["source_autorange"] = True
        panel.set_settings(settings)
        panel.set_advanced_ranges_expanded(False)
        app.processEvents()
        assert panel.source_autorange.isVisible()
        assert panel.source_autorange.isChecked()
        assert not panel.source_autorange.isEnabled()
        assert not panel.source_range.isVisible()
        assert panel.grab().save(str(tmp_path / "settings-auto-indicator.png"))
    finally:
        panel.close()
        panel.deleteLater()
        app.processEvents()


def test_manual_channels_keep_independent_ranges_for_characterization():
    from app.devices.keithley_2600.ui.page import KeithleyPage
    settings = simulation_settings()
    for channel, fixed in [("A", "1 mA"), ("B", "100 mA")]:
        settings.keithley.safety.channels[channel].defaults.update(
            source_mode="current", source_current="100 uA", voltage_compliance="50 mV",
            source_autorange=False, source_range=fixed,
        )
    app = QApplication.instance() or QApplication([])
    page = KeithleyPage(Mock(), settings)
    try:
        for channel, fixed in [("A", "1 mA"), ("B", "100 mA"), ("A", "1 mA")]:
            page.channel.setCurrentText(channel)
            app.processEvents()
            assert page.source_range.text() == fixed
            assert not page.source_autorange.isChecked()
        request_a = page._characterization_source_request("A", "current", 100e-6)
        request_b = page._characterization_source_request("B", "current", 100e-6)
        assert request_a.source_range_si == .001
        assert request_b.source_range_si == .1
        assert not request_a.source_autorange and not request_b.source_autorange
    finally:
        page.deleteLater()
        app.processEvents()


def test_legacy_level_update_is_checked_against_range_before_run():
    from app.recipes import parse_recipe_text
    source = """
schema_version: 1
name: fixed-range-preflight
root:
  id: root
  type: sequence
  children:
    - id: config
      type: configure_keithley
      channel: B
      mode: current
      level: 100 uA
      compliance: 50 mV
      source_range: 1 mA
    - id: update
      type: update_keithley_level
      channel: B
      mode: current
      level: 2 mA
"""
    with pytest.raises(SafetyViolation, match="whole sweep"):
        RecipeCompiler(simulation_settings()).compile(parse_recipe_text(source))


def test_fixed_range_request_readback_and_ramp_never_enable_autorange():
    settings = simulation_settings()
    from app.settings.models import StationSettings
    raw = settings.model_dump(mode="python")
    raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
    settings = StationSettings.model_validate(raw)
    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    for channel, fixed in [("A", .001), ("B", .1)]:
        request = KeithleySourceRequest(channel, "current", .0001, .05, source_range_si=fixed)
        adapter.configure_source(request)
        adapter.set_output(channel, True)
        adapter.update_source_level(channel, mode="current", level_si=.0002)
        adapter.ramp_to_zero(channel)
    commands = session.commands
    assert not any("source.autorange" in command and "AUTORANGE_ON" in command for command in commands)


def test_measure_only_form_ignores_retained_source_range():
    from app.devices.keithley_2600.ui.page import KeithleyPage
    app = QApplication.instance() or QApplication([])
    page = KeithleyPage(Mock(), simulation_settings())
    try:
        request = page._source_request_from_snapshot(
            KeithleyConfigurationSnapshot(channel="A", source_mode="measure_only", source_range="10 mA")
        )
        assert request.source_range_si is None
        assert request.source_autorange is False
    finally:
        page.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("mode,level,compliance", [("current", "100 uA", "50 mV"), ("voltage", "50 mV", "1 mA")])
def test_settings_auto_exception_reaches_compiler_adapter_and_characterization(channel, mode, level, compliance):
    settings = simulation_settings()
    settings.keithley.safety.channels[channel].defaults["source_autorange"] = True
    compiler = RecipeCompiler(settings)
    # Old recipes with OFF cannot override the operator's Settings choice.
    request = compiler._compile_keithley({"channel": channel, "mode": mode,
        "level": level, "compliance": compliance, "source_autorange": False,
        "source_range": "10 mA"}, "settings-policy")["request"]
    assert request.source_autorange is True
    assert request.source_range_si is None
    session = KeithleySimulator()
    adapter = KeithleyAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    adapter.configure_source(request)
    suffix = "i" if mode == "current" else "v"
    assert f"smu{channel.lower()}.source.autorange{suffix} = smu{channel.lower()}.AUTORANGE_ON" in session.commands
    config = CharacterizationSweepConfig(channel=channel, mode=mode,
        start_level_si=request.level_si / 2, stop_level_si=request.level_si,
        compliance_si=request.compliance_si, source_autorange=True, source_range_si=None)
    KeithleyCharacterizationRunner.validate_preflight(config, settings)
    # Revoking the exception rejects the same request before further traffic.
    settings.keithley.safety.channels[channel].defaults["source_autorange"] = False
    before = tuple(session.commands)
    with pytest.raises(SafetyViolation, match="Settings"):
        adapter.configure_source(request)
    assert tuple(session.commands) == before


def test_main_ui_settings_policy_is_read_only_per_channel_and_updates_cached_drafts():
    from app.devices.keithley_2600.ui.page import KeithleyPage
    app = QApplication.instance() or QApplication([])
    settings = simulation_settings()
    for ch in ("A", "B"):
        settings.keithley.safety.channels[ch].defaults.update(source_mode="current",
            source_current="100 uA", voltage_compliance="50 mV", source_range="1 mA")
    settings.keithley.safety.channels["B"].defaults["source_autorange"] = True
    page = KeithleyPage(Mock(), settings)
    try:
        for channel, auto in [("A", False), ("B", True), ("A", False)]:
            page.channel.setCurrentText(channel)
            app.processEvents()
            assert page.source_autorange.isChecked() is auto
            assert not page.source_autorange.isEnabled()
            assert page.source_range.isEnabled() is (not auto)
            request = page._characterization_source_request(channel, "current", 100e-6)
            assert request.source_autorange is auto
            assert request.source_range_si == (None if auto else .001)
        settings.keithley.safety.channels["B"].defaults["source_autorange"] = False
        page.set_settings(settings)
        page.channel.setCurrentText("B")
        app.processEvents()
        assert not page.source_autorange.isChecked()
        assert page.source_range.isEnabled()
        assert page._characterization_source_request("B", "current", 100e-6).source_range_si == .001
    finally:
        page.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("channel", ["A", "B"])
def test_settings_auto_warning_cancel_and_accept_on_both_editors(channel):
    from unittest.mock import patch

    from app.settings.repository import SettingsRepository
    from app.ui.settings_page import QMessageBox, SettingsPage
    app = QApplication.instance() or QApplication([])
    page = SettingsPage(SettingsRepository("app/resources/settings.template.yml"))
    path = ("devices", "keithley", "safety", "channels", channel, "defaults", "source_autorange")
    try:
        editor = page._form_editors[path]
        with patch("app.ui.settings_page.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel) as warning:
            editor.setChecked(True)
            assert not editor.isChecked()
            assert not page._apply_tree_values()["devices"]["keithley"]["safety"]["channels"][channel]["defaults"]["source_autorange"]
            assert f"Channel {channel}" in warning.call_args.args[1]
        with patch("app.ui.settings_page.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes):
            editor.setChecked(True)
            assert page._apply_tree_values()["devices"]["keithley"]["safety"]["channels"][channel]["defaults"]["source_autorange"] is True
        editor.setChecked(False)
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem
        tree = QTreeWidget(page)
        tree.setColumnCount(2)
        item = QTreeWidgetItem(tree, ["Source autorange", "false"])
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        page._install_choice_editor(tree, item, path, (("ON", "true"), ("OFF", "false")))
        tree_editor = page._choice_editors[path]
        with patch("app.ui.settings_page.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            tree_editor.setCurrentIndex(tree_editor.findData("true"))
            assert tree_editor.currentData() == "false"
        with patch("app.ui.settings_page.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes):
            tree_editor.setCurrentIndex(tree_editor.findData("true"))
            assert tree_editor.currentData() == "true"
    finally:
        page.deleteLater()
        app.processEvents()


def test_staged_or_background_auto_enable_requires_confirmation_before_disk_write(tmp_path):
    from pathlib import Path
    from unittest.mock import patch

    from app.settings.models import StationSettings
    from app.settings.repository import SettingsRepository
    from app.ui.settings_page import QMessageBox, SettingsPage
    app = QApplication.instance() or QApplication([])
    filename = tmp_path / "settings.yml"
    filename.write_bytes(Path("app/resources/settings.template.yml").read_bytes())
    repository = SettingsRepository(filename)
    page = SettingsPage(repository)
    page._access = Mock()
    page._access.allows.return_value = True
    try:
        raw = repository.load().raw
        raw["devices"]["keithley"]["safety"]["channels"]["A"]["defaults"]["source_autorange"] = True
        page.stage_external_snapshot(StationSettings.model_validate(raw), raw)
        assert not page.save_draft(silent=True)
        assert repository.load().settings.keithley.safety.channels["A"].defaults["source_autorange"] is False
        with patch("app.ui.settings_page.QMessageBox.warning", return_value=QMessageBox.StandardButton.Cancel):
            assert not page.save_draft()
        with patch("app.ui.settings_page.QMessageBox.warning", return_value=QMessageBox.StandardButton.Yes):
            assert page.save_draft()
        assert repository.load().settings.keithley.safety.channels["A"].defaults["source_autorange"] is True
        def enable_b(draft):
            draft["devices"]["keithley"]["safety"]["channels"]["B"]["defaults"]["source_autorange"] = True
        assert not page.save_draft(silent=True, extra_transform=enable_b)
        assert repository.load().settings.keithley.safety.channels["B"].defaults["source_autorange"] is False
    finally:
        page.deleteLater()
        app.processEvents()
