from types import SimpleNamespace
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

import pytest
import yaml
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtCore import QEventLoop, QTimer, Qt, QPoint
from PySide6.QtTest import QTest
from qfluentwidgets import RoundMenu
import h5py

from app.devices.keithley_2600 import KeithleyAdapter
from app.devices.rigol_dg1000z import RigolAdapter
from app.devices.simulators import KeithleySimulator, RigolSimulator, simulated_station_settings
from app.devices.visa import FakeVisaSessionFactory
from app.domain.models import ApplicationState, DeviceState
from app.engine import RecipeCompiler, RecipeRunner, ExecutionMode
from app.recipes import parse_recipe_text, RecipeNode
from app.recipes.semantic_tree import normalize_recipe_tree
from app.safety.moke_box import control_profile_from_settings
from app.settings.models import StationSettings
from app.ui.recipes.final_state_dialog import FinalStateDialog
from app.ui.recipes.page import RecipePage
from app.ui.run_worker import RunController
from app.ui.workers import DeviceController
from tests.test_adapters_and_runner import MemoryWriter
from tests.test_moke_smoke_sweep_limits import settings_for_channel_two
from tests.test_moke_voltage_control import controlled_adapter
from tests.test_moke_voltage_control import mutations
from app.devices.moke_box.protocol import decode_voltage


def recipe_for(device, *, output="hold", final=None, enabled=True):
    if device == "moke_box":
        children = [{"id": "source", "type": "set_moke_voltage", "channel": 2, "voltage": "5 mV"}]
        fields = {"voltage": "8 mV"}
        channel = 2
    elif device == "keithley":
        channel = "A"
        children = [{"id": "source", "type": "configure_keithley", "channel": channel,
                     "mode": "current", "level": "0.1 mA", "compliance": "20 mV", "sense_mode": "2wire", "source_range": "1 mA"}]
        fields = {"level": "0.2 mA"}
    else:
        channel = 1
        children = [{"id": "source", "type": "configure_rigol", "channel": channel,
                     "waveform": "SIN", "frequency": "1 kHz", "high_level": "1 mV", "low_level": "-1 mV", "output_load": "HIGHZ"}]
        fields = {"frequency": "2 kHz", "high_level": "2 mV", "low_level": "-2 mV"}
    if device != "moke_box" and enabled:
        children.append({"id": "on", "type": f"set_{device}_output", "channel": channel, "enabled": True})
    children.append({"id": "measurement", "type": "checkpoint"})
    final_node = {"id": "finish", "type": "final_state", "device": device, "channel": channel, "output": output,
                  **(fields if final is None else final)}
    if device == "moke_box" and output == "off" and final is None:
        final_node["voltage"] = "0 V"
    return parse_recipe_text(yaml.safe_dump({"schema_version": 1, "name": "final-state", "root":
        {"id": "main", "type": "sequence", "children": children}, "finally": [final_node]}))


def qualified_settings():
    raw = simulated_station_settings(settings_for_channel_two()).model_dump(mode="python")
    raw["devices"]["keithley"]["safety"]["allow_output_enable"] = True
    raw["devices"]["keithley"]["safety"]["channels"]["A"]["enabled"] = True
    raw["devices"]["rigol"]["safety"]["allow_output_enable"] = True
    return StationSettings.model_validate(raw)


@pytest.mark.parametrize("completion", ["automatic", "zero", "hold"])
def test_validation_preview_keeps_yaml_and_separates_engine_safeguards(tmp_path, completion):
    application = QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/arial.ttf")
    application.setFont(QFont("Arial", 10))
    page = RecipePage(qualified_settings())
    original = recipe_for("moke_box")
    finally_nodes = ([] if completion == "automatic" else
                     [{"id": "finish", "type": "final_state", "device": "moke_box", "channel": 2,
                       "output": "hold", "voltage": "0.2 V"}] if completion == "hold" else
                     [{"id": "zero", "type": "stop_moke_voltage", "channel": 2}])
    source = yaml.safe_dump({"schema_version": 1, "name": "preview", "root": page._node_to_mapping(original.root),
                            "finally": finally_nodes})
    try:
        page.resize(1280, 900)
        page.show()
        page._apply_builder_source(source, "Preview regression")
        accepted = page._builder_source()
        before = page.semantic_tree_snapshot()
        assert "__finally__.automatic_safeguards" in before.by_id
        before_ids = tuple(page.tree_model.tree.by_id)
        before_children = dict(page.tree_model.tree.children_by_id)
        plan = RecipeCompiler(qualified_settings()).compile(parse_recipe_text(accepted))
        for _ in range(3):
            tree = page.semantic_tree_snapshot(plan=plan)
            assert page._builder_source() == accepted
            assert len(parse_recipe_text(accepted).finally_nodes) == len(finally_nodes)
            assert tuple(page.tree_model.tree.by_id) == before_ids
            assert dict(page.tree_model.tree.children_by_id) == before_children
            assert page.tree_model.tree.require("__finally__.automatic_safeguards").children == ()
            safeguards = tree.by_id["__finally__.automatic_safeguards"]
            assert safeguards.children
            assert all(child.kind.value == "generated_safety" for child in safeguards.children)
            moke = tree.by_id["__finally__.moke_box_dac_zero_or_unknown"]
            assert ("hold after success" if completion == "hold" else "verify 0 V") in moke.label
            assert "VOUT 2" in moke.label
            assert "YAML unchanged" in safeguards.data["detail"]
            if completion == "hold":
                assert "0.2 V" in page.tree_model.tree.require("finish").data["detail"]
        page.measurement_tree.expandAll()
        page.measurement_tree.collapse(page.tree_model.index_for_semantic_id(original.root.id))
        safeguard_index = page.tree_model.index_for_semantic_id("__finally__.automatic_safeguards")
        page.measurement_tree.scrollTo(safeguard_index)
        application.processEvents()
        QTest.qWait(400)
        page.measurement_tree.verticalScrollBar().setValue(page.measurement_tree.verticalScrollBar().maximum())
        application.processEvents()
        assert page.measurement_tree.isVisible()
        assert page.measurement_tree.visualRect(safeguard_index).intersects(page.measurement_tree.viewport().rect())
        assert page.grab().save(str(tmp_path / f"preview-{completion}.png"))
    finally:
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("device,cleanup_type", [
    ("moke_box", "stop_moke_voltage"),
    ("keithley", "set_keithley_output"),
    ("rigol", "set_rigol_output"),
])
def test_selected_cleanup_edits_same_channel_and_saves_final_state(device, cleanup_type, tmp_path):
    application = QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/arial.ttf")
    application.setFont(QFont("Arial", 10))
    page = RecipePage(qualified_settings())
    original = recipe_for(device)
    final = original.finally_nodes[0]
    cleanup = {"id": "selected-cleanup", "type": cleanup_type, "channel": final.data["channel"]}
    if device != "moke_box":
        cleanup["enabled"] = False
    source = yaml.safe_dump({"schema_version": 1, "name": "selected-final", "root":
                            page._node_to_mapping(original.root), "finally": [cleanup]})
    dialogs = []

    def edit_and_save(dialog):
        dialogs.append(dialog)
        dialog.show()
        application.processEvents()
        assert dialog.channel.currentData() == final.data["channel"]
        assert dialog.device.currentData() == device
        assert dialog.output.currentData() == ("off" if device == "moke_box" and len(dialogs) == 1 else "hold")
        dialog.output.setCurrentIndex(dialog.output.findData("hold"))
        assert dialog.save.isVisible()
        if device == "moke_box":
            dialog.fields["voltage"].setText("10000 mV")
            QTest.mouseClick(dialog.save, Qt.MouseButton.LeftButton)
            assert dialog.result() == 0
            assert "outside" in dialog.status.text().lower()
            dialog.fields["voltage"].setText("8 mV")
        assert dialog.grab().save(str(tmp_path / f"final-state-{device}.png"))
        QTest.mouseClick(dialog.save, Qt.MouseButton.LeftButton)
        assert dialog.result() == 1, dialog.status.text()
        return dialog.result()

    try:
        page.resize(1280, 900)
        page.show()
        page._apply_builder_source(source, "Selected cleanup regression")
        page._select_source_node("selected-cleanup")
        application.processEvents()
        def choose_context_action(menu, *_args, **_kwargs):
            action = next(action for action in menu.actions() if action.text() == "Set final output state")
            action.trigger()

        with patch.object(FinalStateDialog, "exec", edit_and_save), patch.object(RoundMenu, "exec", choose_context_action):
            page._show_tree_context_menu(None, global_position=QPoint(100, 100))
        saved = parse_recipe_text(page._builder_source())
        assert len(saved.finally_nodes) == 1
        assert saved.finally_nodes[0].id == "selected-cleanup"
        assert saved.finally_nodes[0].type == "final_state"
        assert saved.finally_nodes[0].data["channel"] == final.data["channel"]
        assert saved.finally_nodes[0].data["output"] == "hold"
        endpoint = (f"moke_box.vout{final.data['channel']}" if device == "moke_box"
                    else f"{device}.{final.data['channel']}")
        assert RecipeCompiler(qualified_settings()).compile(saved).retained_outputs == {endpoint}
        # Reopening an existing final state also preserves its endpoint and avoids duplicates.
        with patch.object(FinalStateDialog, "exec", edit_and_save):
            page._edit_selected_final_state()
        assert len(parse_recipe_text(page._builder_source()).finally_nodes) == 1
    finally:
        for dialog in dialogs:
            dialog.close()
            dialog.deleteLater()
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        application.processEvents()


def rig(device):
    settings = qualified_settings()
    unused = SimpleNamespace(state=DeviceState.OUTPUT_OFF)
    if device == "moke_box":
        settings = settings.model_copy(update={"devices": settings.devices.model_copy(update={
            "moke_box": settings.moke_box.model_copy(update={"voltage_control":
                settings.moke_box.voltage_control.model_copy(update={"minimum_settling_time": "0 s"})})})})
        adapter, transport, _ = controlled_adapter(minimum_settling_s=0)
        adapter._active_profile = None
        adapter._config = adapter._config.__class__("SIM::MOKE::INSTR", allow_vout_control=True,
            allowed_vout_channels=(2,), control_profile=control_profile_from_settings(settings, simulation=True, channel=2))
        devices = {"rigol": unused, "keithley": unused, "anritsu": unused, "moke_box": adapter}
        return settings, adapter, transport, devices
    session = KeithleySimulator() if device == "keithley" else RigolSimulator()
    adapter = (KeithleyAdapter if device == "keithley" else RigolAdapter)(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    devices = {"rigol": unused, "keithley": unused, "anritsu": unused, device: adapter}
    return settings, adapter, session, devices


def moke_completion_recipe(completion):
    original = recipe_for("moke_box", final={"voltage": "0.2 V"})
    cleanup = [] if completion == "automatic" else [RecipePage._node_to_mapping(original.finally_nodes[0])]
    return parse_recipe_text(yaml.safe_dump({"schema_version": 1, "name": f"MOKE-{completion}",
        "root": RecipePage._node_to_mapping(original.root), "finally": cleanup}))


@pytest.mark.parametrize("entry", ["inspector", "tree", "context"])
@pytest.mark.parametrize("output", ["off", "hold"])
def test_moke_final_zero_uses_one_modal_for_all_entry_points(entry, output, tmp_path):
    application = QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/arial.ttf")
    application.setFont(QFont("Arial", 10))
    page = RecipePage(qualified_settings())
    original = recipe_for("moke_box")
    source = yaml.safe_dump({"schema_version": 1, "name": "one-modal",
        "root": page._node_to_mapping(original.root), "finally":
        [{"id": "finish", "type": "stop_moke_voltage", "channel": 2}]})
    dialogs = []
    def edit(dialog):
        dialogs.append(dialog)
        dialog.show()
        application.processEvents()
        assert dialog.channel.currentData() == 2
        assert dialog.output.currentData() == "off"
        assert "VOUT 2" in dialog.windowTitle()
        assert dialog.output.itemText(0) == "Ramp to 0 V and disarm"
        assert not dialog.fields["voltage"].isEnabled()
        dialog.output.setCurrentIndex(dialog.output.findData(output))
        if output == "hold":
            assert dialog.fields["voltage"].isEnabled()
            dialog.fields["voltage"].setText("0.2 V")
        assert dialog.grab().save(str(tmp_path / f"moke-final-{output}.png"))
        QTest.mouseClick(dialog.save, Qt.MouseButton.LeftButton)
        assert dialog.result() == 1, dialog.status.text()
        return dialog.result()
    try:
        page.resize(1280, 900)
        page.show()
        page._apply_builder_source(source, "Unified MOKE modal regression")
        page._select_source_node("finish")
        application.processEvents()
        with patch.object(FinalStateDialog, "exec", edit):
            if entry == "inspector":
                assert page.open_editor_button.isEnabled()
                page.open_editor_button.click()
            elif entry == "tree":
                page.measurement_tree.semantic_activated.emit("finish")
            else:
                page._edit_selected_final_state()
        assert len(dialogs) == 1
        saved = parse_recipe_text(page._builder_source())
        assert len(saved.finally_nodes) == 1
        node = saved.finally_nodes[0]
        assert node.id == "finish" and node.type == "final_state"
        assert node.data["channel"] == 2 and node.data["output"] == output
        assert node.data["voltage"] == ("0.2 V" if output == "hold" else "0 V")
        RecipeCompiler(qualified_settings()).compile(saved)
    finally:
        for dialog in dialogs:
            dialog.close()
            dialog.deleteLater()
        page._close_discard_confirmed = True
        page.close()
        page.deleteLater()
        application.processEvents()


@pytest.mark.parametrize("completion", ["hold", "automatic"])
def test_actual_smoke_recipe_preserves_ten_spectra_and_final_policy(completion):
    raw = yaml.safe_load(Path("recipes/anritsu_background_reference_smoke_test.yml").read_text(encoding="utf-8"))
    raw["finally"] = ([] if completion == "automatic" else
        [{"id": "finish", "type": "final_state", "block_type": "recipe.final_state",
          "device": "moke_box", "channel": 2, "output": "hold", "voltage": "0.2 V"}])
    source = yaml.safe_dump(raw)
    recipe = parse_recipe_text(source)
    first = RecipeCompiler(qualified_settings()).compile(recipe)
    second = RecipeCompiler(qualified_settings()).compile(parse_recipe_text(source))
    assert first.sha256 == second.sha256
    assert first.total_spectra == 10
    assert first.retained_outputs == (frozenset({"moke_box.vout2"}) if completion == "hold" else frozenset())
    completion_updates = [action for action in first.actions if action.completion_only and action.kind == "update_moke_voltage"]
    if completion == "hold":
        assert len(completion_updates) == 1
    else:
        assert not completion_updates


@pytest.mark.parametrize("completion", ["hold", "automatic"])
@pytest.mark.parametrize("outcome", ["success", "stop", "fault"])
def test_moke_point_two_volts_or_automatic_zero_commands(completion, outcome):
    settings, adapter, transport, devices = rig("moke_box")
    plan = RecipeCompiler(settings).compile(moke_completion_recipe(completion))
    runner = None
    def event(name, data):
        if name == "action_started" and data.get("node_id") == "measurement":
            if outcome == "stop":
                runner.request_stop()
            elif outcome == "fault":
                raise RuntimeError("injected measurement failure")
    runner = RecipeRunner(**devices, writer=MemoryWriter(), on_event=event)
    result = runner.run(plan)
    held = completion == "hold" and outcome == "success"
    assert adapter.read_vouts()[2] == pytest.approx(0.2 if held else 0, abs=0.00031)
    writes = mutations(transport)
    assert writes and all(frame.channel == 2 for frame in writes)
    values = [decode_voltage(frame.msb, frame.lsb) for frame in writes]
    assert values[-1] == pytest.approx(0.2 if held else 0, abs=0.00031)
    if held:
        assert result.state == ApplicationState.HOLDING
        assert all(value > 0 for value in values)
    else:
        assert result.state != ApplicationState.HOLDING


@pytest.mark.parametrize("completion", ["hold", "automatic"])
def test_worker_preserves_moke_point_two_or_disconnects_after_zero(completion, tmp_path):
    application = QApplication.instance() or QApplication([])
    settings, adapter, transport, _ = rig("moke_box")
    plan = RecipeCompiler(settings).compile(moke_completion_recipe(completion))
    device_controller = DeviceController(adapter)
    controller = RunController()
    loop = QEventLoop()
    successes, failures = [], []
    controller.finished.connect(lambda result: (successes.append(result), loop.quit()))
    controller.failed.connect(lambda error: (failures.append(error), loop.quit()))
    try:
        controller.start(settings, Path("app/resources/settings.template.yml"), plan, simulation=True,
                         output_dir_override=str(tmp_path), device_controllers={"moke_box": device_controller})
        QTimer.singleShot(20_000, loop.quit)
        loop.exec()
        assert not failures and len(successes) == 1
        assert not successes[0].get("cleanup_errors")
        assert adapter.connected is (completion == "hold")
        if completion == "hold":
            assert adapter.read_vouts()[2] == pytest.approx(0.2, abs=0.00031)
        values = [decode_voltage(frame.msb, frame.lsb) for frame in mutations(transport)]
        assert values[-1] == pytest.approx(0.2 if completion == "hold" else 0, abs=0.00031)
        with h5py.File(successes[0]["path"], "r") as file:
            assert file["run"].attrs["status"] == "completed"
    finally:
        controller.close()
        device_controller.close()
        application.processEvents()


@pytest.mark.parametrize("device", ["moke_box", "keithley", "rigol"])
@pytest.mark.parametrize("mode", ["success", "stop", "fault", "close_fault", "dry_run"])
def test_completion_holds_only_after_confirmed_success(device, mode):
    settings, adapter, session, devices = rig(device)
    recipe = recipe_for(device)
    plan = RecipeCompiler(settings, outputs_forced_off=mode == "dry_run").compile(recipe)
    writer = MemoryWriter()
    if mode == "close_fault":
        def close(_):
            raise OSError("storage close fault")
        writer.close = close
    runner = None
    def event(name, data):
        if name == "action_started" and data.get("node_id") == "measurement":
            if mode == "stop":
                runner.request_stop()
            elif mode == "fault":
                raise OSError("measurement fault")
    runner = RecipeRunner(**devices, writer=writer, on_event=event,
                         execution_mode=ExecutionMode.DRY_RUN if mode == "dry_run" else ExecutionMode.MEASUREMENT)
    result = runner.run(plan)
    assert result.state == (ApplicationState.HOLDING if mode == "success" else
                            ApplicationState.FAULT if mode in {"fault", "close_fault"} else
                            ApplicationState.UNKNOWN if device == "moke_box" and mode != "dry_run" else ApplicationState.SAFE)
    if device == "moke_box":
        assert adapter.read_vouts()[2] == pytest.approx(0.008 if mode == "success" else 0, abs=0.0003)
    elif device == "keithley":
        assert adapter.assert_output_state("A", expected_enabled=mode == "success") is (mode == "success")
        assert session.output["smub"] is False
        if mode == "success":
            assert session.level["smua"] == pytest.approx(0.0002)
    else:
        assert adapter.assert_output_state(1, expected_enabled=mode == "success") is (mode == "success")
        assert session.output[2] is False
        if mode == "success":
            assert session.frequency[1] == 2000


@pytest.mark.parametrize("device,final", [("moke_box", {"voltage": "10000 mV"}),
    ("moke_box", {"voltage": "1 mA"}), ("keithley", {"level": "100 A"}),
    ("keithley", {"level": "1 V"}), ("rigol", {"frequency": "1000 GHz"}),
    ("rigol", {"high_level": "1 V"})])
def test_final_limits_and_dimensions_cannot_bypass_preflight(device, final):
    with pytest.raises((RuntimeError, ValueError)):
        RecipeCompiler(qualified_settings()).compile(recipe_for(device, final=final))


@pytest.mark.parametrize("device", ["keithley", "rigol"])
def test_hold_requires_prior_on(device):
    with pytest.raises(RuntimeError, match="OUTPUT ON"):
        RecipeCompiler(qualified_settings()).compile(recipe_for(device, enabled=False))


def test_tree_names_exact_vout_and_both_completion_and_fault_behavior():
    recipe = recipe_for("moke_box")
    plan = RecipeCompiler(settings_for_channel_two()).compile(recipe)
    tree = normalize_recipe_tree(recipe, (), safe_shutdown_actions=plan.safe_shutdown_actions)
    assert "VOUT 2" in tree.by_id["finish"].label
    generated = tree.by_id["__finally__.moke_box_dac_zero_or_unknown"]
    assert "VOUT 2" in generated.label and "zero on fault/Stop" in generated.label


def test_final_state_modal_renders_all_rigol_fields_and_rejects_invalid_save(tmp_path):
    app = QApplication.instance() or QApplication([])
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/arial.ttf")
    app.setFont(QFont("Arial", 10))
    def validate(_):
        raise RuntimeError("outside station limits")
    dialog = FinalStateDialog(initial={"device": "rigol", "channel": 2, "output": "hold"}, validate=validate)
    try:
        dialog.show()
        app.processEvents()
        assert dialog.channel.currentData() == 2
        assert all(dialog.fields[key].isVisible() for key in ("frequency", "high_level", "low_level"))
        assert dialog.save.geometry().bottom() <= dialog.save.parentWidget().height()
        assert dialog.grab().save(str(Path(tmp_path) / "final-state.png"))
        dialog.accept()
        assert dialog.result() == 0 and "outside station limits" in dialog.status.text()
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("device", ["moke_box", "keithley", "rigol"])
def test_explicit_default_cleanup_is_fault_only_when_final_state_overrides_it(device):
    settings, adapter, session, devices = rig(device)
    recipe = recipe_for(device)
    cleanup = RecipeNode("old-cleanup", "stop_moke_voltage", {}) if device == "moke_box" else RecipeNode(
        "old-cleanup", f"set_{device}_output", {"channel": "A" if device == "keithley" else 1, "enabled": False})
    recipe = replace(recipe, finally_nodes=(cleanup, *recipe.finally_nodes))
    plan = RecipeCompiler(settings).compile(recipe)
    assert next(action for action in plan.actions if action.node_id == "old-cleanup").fault_only
    events = []
    runner = RecipeRunner(**devices, writer=MemoryWriter(), on_event=lambda name, data: events.append((name, data)))
    result = runner.run(plan)
    assert result.state == ApplicationState.HOLDING
    assert not any(name == "action_started" and data["node_id"] == "old-cleanup" for name, data in events)
    assert "Fault / Stop only" in normalize_recipe_tree(recipe, {}).by_id["old-cleanup"].label


@pytest.mark.parametrize("device", ["keithley", "rigol"])
def test_failed_retained_readback_falls_back_to_all_outputs_off(device, monkeypatch):
    settings, adapter, session, devices = rig(device)
    plan = RecipeCompiler(settings).compile(recipe_for(device))
    original = adapter.assert_output_state
    calls = 0
    def readback(channel, *, expected_enabled):
        nonlocal calls
        if expected_enabled:
            calls += 1
            if calls == 2:
                raise RuntimeError("retained readback lost")
        return original(channel, expected_enabled=expected_enabled)
    monkeypatch.setattr(adapter, "assert_output_state", readback)
    result = RecipeRunner(**devices, writer=MemoryWriter()).run(plan)
    assert result.state == ApplicationState.FAULT
    assert not any(session.output.values())


@pytest.mark.parametrize("device", ["moke_box", "keithley", "rigol"])
@pytest.mark.parametrize("release_error", [False, True])
def test_gui_worker_keeps_controller_connection_and_saves_completed_hdf5(device, tmp_path, monkeypatch, release_error):
    app = QApplication.instance() or QApplication([])
    settings, adapter, session, _ = rig(device)
    plan = RecipeCompiler(settings).compile(recipe_for(device))
    device_controller = DeviceController(adapter)
    if release_error:
        original_release = device_controller.release_run_lease
        def fail_release(owner):
            original_release(owner)
            raise RuntimeError("reservation release failed after releasing ownership")
        monkeypatch.setattr(device_controller, "release_run_lease", fail_release)
    controller = RunController()
    loop = QEventLoop()
    successes, failures = [], []
    controller.finished.connect(lambda result: (successes.append(result), loop.quit()))
    controller.failed.connect(lambda error: (failures.append(error), loop.quit()))
    try:
        controller.start(settings, Path("app/resources/settings.template.yml"), plan, simulation=True,
                         output_dir_override=str(tmp_path), device_controllers={device: device_controller})
        QTimer.singleShot(20_000, loop.quit)
        loop.exec()
        assert not failures and len(successes) == 1
        result = successes[0]
        assert result["result"].state == (ApplicationState.FAULT if release_error else ApplicationState.HOLDING)
        assert adapter.connected is (not release_error)
        if release_error:
            assert result["cleanup_errors"]
            if device != "moke_box":
                assert not any(session.output.values())
        elif device == "moke_box":
            assert adapter.read_vouts()[2] == pytest.approx(0.008, abs=0.0003)
        else:
            assert session.output["smua" if device == "keithley" else 1]
        with h5py.File(result["path"], "r") as file:
            assert file["run"].attrs["status"] == "completed"
        assert device_controller._run_access.owner is None
    finally:
        controller.close()
        device_controller.close()
        app.processEvents()


def test_moke_dialog_shows_zero_for_off_and_preserves_initial_hold_value():
    app = QApplication.instance() or QApplication([])
    dialog = FinalStateDialog(initial={"device": "moke_box", "channel": 2, "output": "hold", "voltage": "5 mV"})
    try:
        assert dialog.node_fields()["voltage"] == "5 mV"
        dialog.output.setCurrentIndex(0)
        assert dialog.fields["voltage"].text() == "0 V"
        assert dialog.node_fields()["voltage"] == "0 V"
        dialog.output.setCurrentIndex(dialog.output.findData("hold"))
        assert dialog.fields["voltage"].text() == "5 mV"
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_execution_completion_reports_holding_as_success(tmp_path):
    from app.ui.execution.page import RunMonitorPage
    from app.engine.runner import RunResult
    app = QApplication.instance() or QApplication([])
    page = RunMonitorPage()
    try:
        page.complete({"result": RunResult(ApplicationState.HOLDING, 4, 1), "path": str(tmp_path / "run.h5")})
        assert "completed" in page.completion_title.text()
        assert "held" in page.completion_title.text()
        assert "remain active" in page.completion_summary.text()
    finally:
        page.close()
        page.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("state,failed_shutdown,title", [
    (ApplicationState.FAULT, True, "safe state not confirmed"),
    (ApplicationState.UNKNOWN, False, "safe state not confirmed"),
    (ApplicationState.FAULT, False, "check shutdown status"),
    (ApplicationState.SAFE, False, "stopped safely"),
])
def test_fault_completion_does_not_claim_safe_shutdown_without_evidence(state, failed_shutdown, title, tmp_path):
    from app.ui.execution.page import RunMonitorPage
    from app.engine.runner import RunResult
    application = QApplication.instance() or QApplication([])
    page = RunMonitorPage()
    try:
        page._shutdown_failed = failed_shutdown
        page.complete({"result": RunResult(state, 0, 0, error="ramp failed"), "path": str(tmp_path / "run.h5")})
        assert title in page.completion_title.text()
        if state != ApplicationState.SAFE:
            assert "stopped safely" not in page.completion_title.text().lower()
    finally:
        page.close()
        page.deleteLater()
        application.processEvents()
