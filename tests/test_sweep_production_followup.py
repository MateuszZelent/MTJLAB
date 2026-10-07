"""Current analyzer configuration, RAM policy and Rigol command qualification."""
import os
from dataclasses import asdict
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtTest import QTest

from app.bootstrap import StationComposition
from app.domain.errors import ConfigurationError, DeviceError, ExecutionError
from app.engine.recovery import RunRecoveryManager
from app.settings.models import StationSettings
from app.storage import resource_budget
from tests.test_sweep_audit_contracts import SETUP, audit_settings, compile_source
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


@pytest.fixture
def rigol(tmp_path):
    raw = audit_settings(tmp_path).model_dump(mode="python")
    raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    for channel in ("1", "2"):
        raw["devices"]["rigol"]["safety"]["channels"][channel]["enabled"] = True
    settings = StationSettings.model_validate(raw)
    adapter = StationComposition(settings, simulation=True).create_adapter("rigol")
    adapter.connect()
    try:
        yield settings, adapter
    finally:
        adapter.disconnect()


@pytest.mark.parametrize("channel", [1, 2])
@pytest.mark.parametrize("waveform", ["SIN", "SQU", "RAMP", "PULS", "DC"])
def test_rigol_selected_update_and_enable_preserve_other_controls(rigol, channel, waveform):
    settings, adapter = rigol
    high, low = ("1 mV", "1 mV") if waveform == "DC" else ("1 mV", "-1 mV")
    plan = compile_source(settings, f"    - {{id: baseline, type: configure_rigol, channel: {channel}, waveform: {waveform}, frequency: '1 kHz', high_level: '{high}', low_level: '{low}'}}\n")
    adapter.configure_channel(plan.actions[0].payload["config"])
    session = adapter._require_session()
    session.write(f":OUTP{channel}:SYNC ON")
    session.commands.clear()
    assert adapter.set_output(channel, True)
    writes = [command for command in session.commands if "?" not in command]
    assert writes == [f":OUTP{channel} OFF", f":OUTP{channel} ON"]
    assert session.query(f":OUTP{channel}:SYNC?").strip() in {"1", "ON"}
    before = asdict(adapter.last_channel_config(channel))
    session.commands.clear()
    if waveform == "DC":
        assert adapter.update_levels(channel, high_level_v=.002, low_level_v=.002) == (.002, .002)
        expected = f":SOUR{channel}:VOLT:OFFS 0.002"
        changed = {"high_level_v", "low_level_v"}
    else:
        assert adapter.update_frequency(channel, 2000) == 2000
        expected = f":SOUR{channel}:FREQ 2000"
        changed = {"frequency_hz"}
    assert [command for command in session.commands if "?" not in command] == [expected]
    after = asdict(adapter.last_channel_config(channel))
    assert {key: value for key, value in before.items() if key not in changed} == {
        key: value for key, value in after.items() if key not in changed}


def test_rigol_detects_unselected_change_after_frequency_write(rigol, monkeypatch):
    settings, adapter = rigol
    plan = compile_source(settings, "    - {id: baseline, type: configure_rigol, channel: 1, waveform: SIN, frequency: '1 kHz', high_level: '1 mV', low_level: '-1 mV'}\n")
    adapter.configure_channel(plan.actions[0].payload["config"])
    adapter.set_output(1, True)
    session = adapter._require_session()
    write = session.write

    def side_effect(command):
        write(command)
        if command == ":SOUR1:FREQ 2000":
            write(":SOUR1:PHAS 13")

    monkeypatch.setattr(session, "write", side_effect)
    with pytest.raises(DeviceError, match="PHAS"):
        adapter.update_frequency(1, 2000)
    assert all(session.query(f":OUTP{channel}?").strip() in {"0", "OFF"} for channel in (1, 2))


@pytest.mark.parametrize("name,mode", [("rbw", "rbw_mode"), ("vbw", "vbw_mode"),
    ("attenuation", "attenuation_mode"), ("sweep_time", "sweep_time_mode")])
@pytest.mark.parametrize("selected_mode", [None, "auto"])
def test_advanced_values_cannot_disappear(tmp_path, name, mode, selected_mode):
    value = {"rbw": "10 kHz", "vbw": "30 kHz", "attenuation": "10 dB", "sweep_time": "1 s"}[name]
    mode_field = f", {mode}: {selected_mode}" if selected_mode else ""
    with pytest.raises(ConfigurationError, match="requires explicit"):
        compile_source(audit_settings(tmp_path), f"    - {{id: advanced, type: configure_anritsu_advanced, {name}: '{value}'{mode_field}}}\n")


def test_video_power_configuration_and_recovery(tmp_path):
    settings = audit_settings(tmp_path)
    source = SETUP.splitlines()[0].replace("points: 101", "points: 101, vbw_filter_mode: POW") + "\n"
    plan = compile_source(settings, source)
    adapter = StationComposition(settings, simulation=True).create_adapter("anritsu")
    adapter.connect()
    try:
        adapter.configure_spectrum(plan.actions[0].payload["config"])
        before = adapter.read_advanced_spectrum_configuration()
        assert before.vbw_filter_mode == "POW"
        prelude = RunRecoveryManager._configuration_prelude(plan, len(plan.actions), {
            "anritsu": {"advanced_spectrum": {"actual": asdict(before)}}})
        adapter._require_session().write("BAND:VID:MODE VID")
        for action in prelude:
            if action.kind == "configure_anritsu":
                adapter.configure_spectrum(action.payload["config"])
            else:
                adapter.configure_advanced_spectrum(action.payload["config"])
        assert adapter.read_advanced_spectrum_configuration() == before
    finally:
        adapter.disconnect()


def test_automatic_ram_budget_replaces_512_mib_cap(monkeypatch):
    monkeypatch.setattr(resource_budget, "available_physical_memory_bytes", lambda: 4 * 1024**3)
    assert resource_budget.resolve_import_memory_budget() == 2 * 1024**3
    resource_budget.require_public_import_capacity(922_378_048)
    with pytest.raises(ExecutionError, match="memory budget"):
        resource_budget.require_public_import_capacity(3 * 1024**3)
    with pytest.raises(ExecutionError, match="memory budget"):
        resource_budget.require_public_import_capacity(922_378_048, budget_bytes=512 * 1024**2)
    monkeypatch.setattr(resource_budget, "available_physical_memory_bytes", lambda: None)
    assert resource_budget.resolve_import_memory_budget() == 512 * 1024**2


@pytest.mark.parametrize("theme,width", [("light", 1180), ("dark", 1180), ("light", 780)])
def test_shown_editors_use_only_selected_parameters(tmp_path, theme, width, shell_qt_application):
    from app.devices.anritsu_ms2830a.ui.recipe_dialog import AnritsuNodeEditorDialog
    from app.devices.rigol_dg1000z.ui.recipe_dialog import RigolNodeEditorDialog
    from app.ui.design_system import apply_application_theme

    application = shell_qt_application
    apply_application_theme(application, theme)
    settings = audit_settings(tmp_path)
    directory = Path("docs/audits/2026-10-05-production-followup")
    directory.mkdir(parents=True, exist_ok=True)
    analyzer = AnritsuNodeEditorDialog(settings)
    rigol_dialog = RigolNodeEditorDialog(settings=settings)
    try:
        analyzer.resize(width, 850)
        analyzer.show()
        panel = analyzer.configuration_panel
        panel.rbw_mode.setCurrentIndex(panel.rbw_mode.findData("manual"))
        panel.rbw.setText("10 kHz")
        panel.vbw_mode.setCurrentIndex(panel.vbw_mode.findData("POW"))
        for name in ("advanced.rbw_mode", "advanced.rbw", "advanced.vbw_filter_mode"):
            selector = analyzer.parameter_selectors[name]
            selector.setCurrentIndex(selector.findData("set"))
        QTest.qWait(350)
        values = {item["parameter_id"]: item["value"] for item in analyzer.planned_parameter_actions()}
        assert values == {"advanced.rbw_mode": "manual", "advanced.rbw": "10 kHz", "advanced.vbw_filter_mode": "POW"}
        assert panel.rbw.isVisibleTo(analyzer) and panel.rbw.width() > 50
        assert analyzer.grab().save(str(directory / f"anritsu-{theme}-{width}.png"))
        analyzer.close()
        rigol_dialog.resize(width, 850)
        rigol_dialog.show()
        selector = rigol_dialog.parameter_selectors["carrier.frequency"]
        selector.setCurrentIndex(selector.findData("set"))
        rigol_dialog.frequency.setText("2 kHz")
        QTest.qWait(350)
        assert rigol_dialog.planned_parameter_actions() == [{"parameter_id": "carrier.frequency", "mode": "set", "value": "2 kHz"}]
        assert selector.isVisibleTo(rigol_dialog) and selector.width() > 50
        assert rigol_dialog.grab().save(str(directory / f"rigol-{theme}-{width}.png"))
        rigol_dialog.waveform.setCurrentText("DC")
        offset = rigol_dialog.parameter_selectors["carrier.offset"]
        assert offset.isEnabled() and rigol_dialog.actions_form.isRowVisible(offset)
    finally:
        analyzer.close()
        rigol_dialog.close()
        analyzer.deleteLater()
        rigol_dialog.deleteLater()


@pytest.mark.parametrize("channel", [1, 2])
@pytest.mark.parametrize("axis,start,stop,expected", [
    ("frequency", "1 kHz", "2 kHz", [1000., 1500., 2000.]),
    ("amplitude", "2 mV", "4 mV", [.002, .003, .004]),
    ("offset", "0 mV", "2 mV", [0., .001, .002]),
])
def test_rigol_sweep_archive_has_confirmed_setpoints(rigol, tmp_path, monkeypatch, channel, axis, start, stop, expected):
    from app.storage.hdf5_reader import Hdf5RunReader
    from app.storage.thatec_validator import ThatecCompatibilityValidator
    from tests.test_sweep_audit_contracts import execute

    settings, _adapter = rigol
    source = SETUP.splitlines()[0] + "\n" + f"""    - {{id: baseline, type: configure_rigol, channel: {channel}, waveform: SIN, frequency: '1 kHz', high_level: '1 mV', low_level: '-1 mV'}}
    - {{id: enable, type: set_rigol_output, channel: {channel}, enabled: true}}
    - id: axis
      type: sweep
      target: rigol.{channel}.{axis}
      start: '{start}'
      stop: '{stop}'
      points: 3
      children:
        - {{id: settle, type: wait, duration: '3 s'}}
        - {{id: acquire, type: acquire_spectrum, average_count: 1}}
"""
    plan = compile_source(settings, source)
    path = tmp_path / "rigol.h5"
    from app.engine.runner import RecipeRunner
    from tests.test_adapters_and_runner import MemoryWriter, ShutdownProbe
    probe = RecipeRunner(rigol=ShutdownProbe(), keithley=ShutdownProbe(),
        anritsu=ShutdownProbe(), writer=MemoryWriter())
    for action, value in zip([a for a in plan.actions if a.kind in {"update_rigol_frequency", "update_rigol_levels"}], expected):
        probe._active_safety_context[f"rigol.{channel}"] = {
            "frequency_hz": value if axis == "frequency" else 1000.,
            "high_level_v": value / 2 if axis == "amplitude" else value + .001 if axis == "offset" else .001,
            "low_level_v": -value / 2 if axis == "amplitude" else value - .001 if axis == "offset" else -.001,
        }
        assert probe._confirmed_semantic_value(action) == pytest.approx((value, value))
    result, waits = execute(settings, plan, path, monkeypatch)
    assert result.error is None, result.error
    assert result.stored_points == 3 and waits == [3.] * 3
    target = f"rigol.{channel}.{axis}"
    points = Hdf5RunReader.points(path)
    assert [p.setpoints[target] for p in points] == pytest.approx(expected)
    assert [p.metadata["setpoint_evidence_v1"][target]["readback_si"] for p in points] == pytest.approx(expected)
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid


def test_anritsu_filter_mode_survives_editor_recipe_roundtrip(tmp_path):
    import yaml
    from app.recipes import parse_recipe_text
    from app.ui.recipes.page import RecipePage
    from app.engine.compiler import RecipeCompiler

    recipe = parse_recipe_text("schema_version: 1\nname: mode\nroot:\n  id: analyzer\n  type: sequence\n  device_module: anritsu\n  children: []\n")
    node = RecipePage._configured_anritsu_node(recipe.root,
        parameter_actions=[{"parameter_id": "advanced.vbw_filter_mode", "mode": "set", "value": "POW"}],
        acquire_single=False, trace="TRAC1")
    source = yaml.safe_dump({"schema_version": 1, "name": "mode", "root": node})
    plan = RecipeCompiler(audit_settings(tmp_path)).compile(parse_recipe_text(source))
    configs = [a.payload["config"] for a in plan.actions if a.kind == "configure_anritsu_advanced"]
    assert len(configs) == 1 and configs[0].vbw_filter_mode == "POW"


def test_rigol_live_projection_preserves_full_confirmed_channel(tmp_path, shell_qt_application):
    from unittest.mock import Mock
    from app.devices.rigol_dg1000z.ui.page import RigolPage
    from app.domain.quantities import parse_quantity, DIMENSION_TIME

    controller = Mock()
    page = RigolPage(controller, audit_settings(tmp_path))
    try:
        page.resize(1180, 850)
        page.show()
        page.set_execution_controlled(True)
        actual = {"waveform": "PULS", "frequency_hz": 2000., "high_level_v": .002,
            "low_level_v": -.001, "output_load": "HIGHZ", "phase_deg": 37.,
            "pulse_width_s": 1e-4, "pulse_leading_s": 2e-8, "pulse_trailing_s": 3e-8}
        controller.reset_mock()
        page.apply_execution_event("action_finished", {}, {"channel_1": {"actual": actual}}, {"rigol.1": "on"})
        QTest.qWait(100)
        assert page.waveform.currentText() == "PULS" and page.phase.text() == "37"
        assert page.load.text() == "HIGHZ"
        assert parse_quantity(page.pulse_trailing.text(), DIMENSION_TIME).si_value == pytest.approx(3e-8)
        controller.call.assert_not_called()
        assert page.frequency.isVisibleTo(page) and page.frequency.width() > 20
    finally:
        page.close()
        page.deleteLater()


@pytest.mark.parametrize("response", ["timeout", "UNKNOWN"])
def test_missing_video_power_readback_fails_closed(tmp_path, monkeypatch, response):
    adapter = StationComposition(audit_settings(tmp_path), simulation=True).create_adapter("anritsu")
    adapter.connect()
    try:
        session = adapter._require_session()
        query = session.query
        def injected(command):
            if command == "BAND:VID:MODE?":
                if response == "timeout":
                    raise DeviceError("Injected timeout")
                return response
            return query(command)
        monkeypatch.setattr(session, "query", injected)
        with pytest.raises(DeviceError):
            adapter.read_full_configuration()
        with pytest.raises(DeviceError):
            adapter.read_advanced_spectrum_configuration()
    finally:
        adapter.disconnect()
