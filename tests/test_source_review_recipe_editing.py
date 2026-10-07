"""Editing and duplicating nodes must preserve authored execution semantics."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from PySide6.QtWidgets import QDialog

from app.devices.keithley_2600.ui import KeithleyConfigurationSnapshot
from app.recipes import RecipeNode, parse_recipe_text
from app.ui.recipes.page import AnritsuAcquisitionEditorDialog, RecipePage
from app.ui.recipes.common_dialogs import SweepGeneratorDialog
from tests.helpers import simulation_settings
from tests.shell_test_isolation import shell_qt_application as shell_qt_application


@pytest.mark.parametrize("channel", ["A", "B"])
@pytest.mark.parametrize("accepted", [False, True])
def test_legacy_roi_editor_changes_only_points(monkeypatch, shell_qt_application, channel, accepted):
    target = f"keithley.{channel}.current"
    children = [
        {"id": "config", "type": "configure_keithley", "channel": channel,
         "mode": "current", "level": "${" + target + "}", "compliance": "20 mV",
         "source_range": "10 mA", "nplc": 2, "settle_time": "250 ms", "sense_mode": "2wire"},
        {"id": "measurement", "type": "measure_keithley", "channel": channel},
        {"id": "user-wait", "type": "wait", "duration": "7 s"},
        {"id": "other-channel", "type": "configure_keithley", "channel": "B" if channel == "A" else "A",
         "mode": "current", "level": "0.1 mA", "compliance": "30 mV", "nplc": 3},
    ]
    source = yaml.safe_dump({"schema_version": 1, "name": "legacy-roi", "root": {
        "id": "axis", "type": "sweep", "target": target, "disabled": True,
        "start": "0.1 mA", "stop": "0.2 mA", "points": 2, "children": children}})
    node = parse_recipe_text(source).root
    page = RecipePage(simulation_settings())
    saved = []
    segments = [{"start": "0.2 mA", "stop": "0.4 mA", "points": 3}]

    def edit(dialog):
        assert type(dialog) is SweepGeneratorDialog
        assert dialog.definition["target"] == target
        dialog.resize(1180, 720)
        dialog.show()
        shell_qt_application.processEvents()
        assert dialog.isVisible() and dialog.segments.width() > 100
        assert not dialog.grab().isNull()
        monkeypatch.setattr(dialog, "segment_data", lambda: segments)
        dialog.close()
        return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

    monkeypatch.setattr(SweepGeneratorDialog, "exec", edit)
    def fail_warning(*args):
        pytest.fail(str(args[-1]))
    monkeypatch.setattr("app.ui.recipes.page.QMessageBox.warning", fail_warning)
    monkeypatch.setattr(page, "_builder_source", lambda: source)
    monkeypatch.setattr(page, "_apply_builder_source", lambda text, *args, **kwargs: saved.append(text))
    try:
        page._edit_selected_generator(node=node)
        assert len(saved) == int(accepted)
        if accepted:
            edited = parse_recipe_text(saved[0]).root
            assert edited.data["target"] == target
            assert edited.data["disabled"] is True
            assert [RecipePage._node_to_mapping(child) for child in edited.children] == children
            assert list(edited.data["segments"]) == segments
            assert not {"start", "stop", "points", "spacing"}.intersection(edited.data)
    finally:
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()


@pytest.mark.parametrize("module", ["keithley", "rigol", "anritsu", "anritsu_sg"])
def test_device_configuration_preserves_disabled(module):
    node = RecipeNode("disabled-node", "sequence", {"disabled": True}, ())
    if module == "keithley":
        result = RecipePage._configured_keithley_node(node, KeithleyConfigurationSnapshot())
    elif module == "rigol":
        result = RecipePage._configured_rigol_node(node, output_policy="unchanged")
    elif module == "anritsu":
        result = RecipePage._configured_anritsu_node(node, parameter_actions=[], acquire_single=False, trace="TRAC1")
    else:
        result = RecipePage._configured_anritsu_sg_node(node, frequency="1 GHz", power="-30 dBm", parameter_actions=[])
    assert result["disabled"] is True


def test_clone_rebinds_managed_acquisitions_in_both_branches():
    source = {"id": "root", "type": "sequence", "disabled": True,
              "managed_acquisition_id": "ref", "children": [{"id": "ref", "type": "acquire_reference"}],
              "else": [{"id": "nested", "type": "sequence", "managed_acquisition_id": "spectrum",
                        "children": [{"id": "spectrum", "type": "acquire_spectrum"}]}]}
    before = deepcopy(source)
    cloned = RecipePage._clone_node_mapping(SimpleNamespace(_new_node_id=RecipePage._new_node_id), source)
    assert cloned["managed_acquisition_id"] == cloned["children"][0]["id"] != "ref"
    nested = cloned["else"][0]
    assert nested["managed_acquisition_id"] == nested["children"][0]["id"] != "spectrum"
    assert cloned["disabled"] is True
    assert source == before


def test_editing_reference_to_zero_duration_removes_old_timing(monkeypatch, shell_qt_application):
    source = yaml.safe_dump({"schema_version": 1, "name": "edit-reference", "root": {
        "id": "reference", "type": "acquire_reference", "average_count": 4,
        "minimum_duration": "30 s", "inter_sweep_delay": "3 s", "purpose": "background", "disabled": True}})
    node = parse_recipe_text(source).root
    page = RecipePage(simulation_settings())
    saved = []

    def edit(dialog):
        dialog.minimum_duration.setText("0 s")
        dialog.inter_sweep_delay.setText("0 s")
        dialog.reference_purpose.setCurrentIndex(0)
        dialog.show()
        shell_qt_application.processEvents()
        assert dialog.isVisible() and dialog.width() >= 600
        dialog.close()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(AnritsuAcquisitionEditorDialog, "exec", edit)
    monkeypatch.setattr(page, "_builder_source", lambda: source)
    monkeypatch.setattr(page, "_apply_builder_source", lambda text, *args, **kwargs: saved.append(text))
    try:
        page._edit_anritsu_acquisition_node(node)
        assert len(saved) == 1
        edited = parse_recipe_text(saved[0]).root
        assert "minimum_duration" not in edited.data
        assert edited.data.get("inter_sweep_delay", "0 s") == "0 s"
        assert edited.data.get("purpose", "reference") == "reference"
        assert edited.data["disabled"] is True
    finally:
        page.close()
        page.deleteLater()


def test_changed_station_settings_reject_queued_preflight_result(shell_qt_application, monkeypatch):
    page = RecipePage(simulation_settings())
    try:
        page._preflight_source = page.editor.toPlainText()
        page._preflight_outputs_forced_off = page.execution_mode.currentData() == "dry_run"
        page._preflight_settings_generation = page._settings_generation
        thread = Mock()
        thread.isRunning.return_value = True
        page._preflight_thread = thread
        accept = Mock()
        monkeypatch.setattr(page, "_accept_preflight", accept)
        page.set_settings(simulation_settings())
        thread.requestInterruption.assert_called_once()
        page._preflight_succeeded(Mock(), Mock(), Mock())
        assert "stale result was discarded" in page.summary.text()
        accept.assert_not_called()
        assert page._plan is None and not page.run_button.isEnabled()
    finally:
        page._preflight_thread = None
        page.close()
        page.deleteLater()


def test_preflight_worker_owns_an_independent_settings_snapshot(shell_qt_application):
    from app.ui.workers import RecipePreflightWorker

    settings = simulation_settings()
    worker = RecipePreflightWorker(settings, "", "")
    try:
        old = worker._settings.model_dump()
        settings.keithley.safety.channels["A"].defaults["source_current"] = "1 mA"
        assert worker._settings.model_dump() == old
        assert worker._settings is not settings
    finally:
        worker.deleteLater()


def test_running_recipe_cannot_be_replaced_by_new_or_yaml_apply(shell_qt_application):
    page = RecipePage(simulation_settings())
    try:
        page._execution_controlled = True
        source, path, plan = page._tree_source, page.path.text(), page._plan
        page.new_recipe(confirm=False)
        assert not page.apply_yaml_to_tree(show_error=False)
        assert (page._tree_source, page.path.text(), page._plan) == (source, path, plan)
    finally:
        page._execution_controlled = False
        page.close()
        page.deleteLater()
