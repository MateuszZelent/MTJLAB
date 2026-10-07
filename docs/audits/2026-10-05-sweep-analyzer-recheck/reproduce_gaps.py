"""Audit probes: desired contracts that currently FAIL; simulator/offline only.

Run explicitly with pytest; deliberately excluded from normal test discovery.
"""
import os
from dataclasses import asdict

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from app.bootstrap import StationComposition
from app.devices.anritsu_ms2830a.ui.recipe_dialog import AnritsuNodeEditorDialog
from app.domain.errors import ConfigurationError, DeviceError, SafetyViolation
from app.engine.recovery import RunRecoveryManager
from tests.test_sweep_audit_contracts import SETUP, audit_settings, compile_source


@pytest.fixture(scope="module")
def application():
    instance = QApplication.instance() or QApplication([])
    yield instance


def test_visible_rbw_selection_reaches_recipe_actions(application, tmp_path):
    dialog = AnritsuNodeEditorDialog(audit_settings(tmp_path))
    try:
        panel = dialog.configuration_panel
        panel.rbw_mode.setCurrentIndex(panel.rbw_mode.findData("manual"))
        panel.rbw.setText("10 kHz")
        for parameter in ("advanced.rbw_mode", "advanced.rbw"):
            selector = dialog.parameter_selectors[parameter]
            selector.setCurrentIndex(selector.findData("set"))
        values = {item["parameter_id"]: item["value"]
                  for item in dialog.planned_parameter_actions()}
        assert values["advanced.rbw_mode"] == "manual", values
        assert values["advanced.rbw"] == "10 kHz", values
    finally:
        dialog.close()
        dialog.deleteLater()


def test_recipe_can_explicitly_author_current_video_power_mode(tmp_path):
    source = SETUP.splitlines()[0].replace("points: 101", "points: 101, vbw_mode: POW") + "\n"
    plan = compile_source(audit_settings(tmp_path), source)
    assert plan.actions[0].payload["config"].vbw_mode == "POW"


def test_advanced_value_without_mode_is_not_silently_discarded(tmp_path):
    with pytest.raises((ConfigurationError, SafetyViolation)):
        compile_source(audit_settings(tmp_path),
            "    - {id: bandwidth, type: configure_anritsu_advanced, rbw: '10 kHz'}\n")


def test_missing_video_power_readback_is_not_invented(tmp_path, monkeypatch):
    adapter = StationComposition(audit_settings(tmp_path), simulation=True).create_adapter("anritsu")
    adapter.connect()
    try:
        session = adapter._require_session()
        original = session.query

        def query(command):
            if command == "BAND:VID:MODE?":
                raise DeviceError("Injected readback timeout")
            return original(command)

        monkeypatch.setattr(session, "query", query)
        with pytest.raises(DeviceError):
            adapter.read_full_configuration()
    finally:
        adapter.disconnect()


def test_recovery_restores_confirmed_video_power_mode(tmp_path):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, SETUP.splitlines()[0] + "\n")
    adapter = StationComposition(settings, simulation=True).create_adapter("anritsu")
    adapter.connect()
    try:
        adapter.configure_spectrum(plan.actions[0].payload["config"])
        session = adapter._require_session()
        session.write("BAND:VID:MODE POW")
        full = adapter.read_full_configuration()
        advanced = adapter.read_advanced_spectrum_configuration()
        states = {"anritsu": {
            "spectrum": {"actual": asdict(full)},
            "advanced_spectrum": {"actual": asdict(advanced)},
        }}
        prelude = RunRecoveryManager._configuration_prelude(plan, len(plan.actions), states)
        session.write("BAND:VID:MODE VID")  # Different state after an interruption.
        for action in prelude:
            if action.kind == "configure_anritsu":
                adapter.configure_spectrum(action.payload["config"])
            elif action.kind == "configure_anritsu_advanced":
                adapter.configure_advanced_spectrum(action.payload["config"])
        assert adapter.read_full_configuration().vbw_mode == full.vbw_mode
    finally:
        adapter.disconnect()
