"""Contract for replacing Keithley B with a verified Rigol DC voltage axis."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from app.domain.errors import ConfigurationError, SafetyViolation
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.settings import SettingsRepository

SOURCE = Path("recipes/example_keithley_a_10ma_b_0_40ma_fixed_moke.yml")
RECIPE = Path("recipes/example_keithley_a_10ma_rigol_dc_0_600mv_fixed_moke.yml")


@pytest.fixture
def settings():
    return SettingsRepository(Path(".config/settings.yml")).load().settings


def compile_mapping(settings, raw):
    return RecipeCompiler(settings).compile(parse_recipe_text(yaml.safe_dump(raw)))


def test_requested_recipe_preserves_existing_workflow_and_explicit_dc_axis(settings):
    original = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    raw = yaml.safe_load(RECIPE.read_text(encoding="utf-8"))
    old = {node["id"]: node for node in original["root"]["children"]}
    new = {node["id"]: node for node in raw["root"]["children"]}
    for key in old.keys() & new.keys():
        assert new[key] == old[key]
    assert raw["finally"][:2] == original["finally"][:2]
    assert raw["finally"][-1] == original["finally"][-1]
    sweep = new["rigol-dc-voltage-sweep"]
    assert sweep["children"] == [
        node for node in old["keithley-b-current-sweep"]["children"]
        if node["id"] != "measure-keithley-b"
    ]
    plan = compile_mapping(settings, raw)
    assert plan.total_spectra == plan.total_points == 41
    assert plan.retained_outputs == {"moke_box.vout0"}
    spectra = [a for a in plan.actions if a.kind == "acquire_spectrum"]
    assert [a.setpoints_si["rigol.1.offset"] for a in spectra] == pytest.approx(
        [i * .015 for i in range(41)]
    )
    assert all(a.setpoints_si["keithley.A.current"] == .01 for a in spectra)
    main = [a for a in plan.actions if not a.is_finally]
    assert not any(a.kind == "configure_anritsu" for a in main)
    assert not any(a.kind == "measure_keithley" and a.payload["channel"] == "B" for a in main)
    carrier = next(a for a in main if a.kind == "configure_rigol")
    assert carrier.payload["config"].waveform == "DC"
    assert carrier.payload["config"].high_level_v == carrier.payload["config"].low_level_v == 0
    assert carrier.payload["config"].output_load == "HIGHZ"
    output = [a.payload["enabled"] for a in main if a.kind == "set_rigol_output"]
    assert output == [False, True]
    references = [a for a in main if a.kind == "acquire_reference"]
    on_index = next(i for i, a in enumerate(main) if a.kind == "set_rigol_output" and a.payload["enabled"])
    assert all(main.index(a) < on_index for a in references)
    updates = [a for a in main if a.kind == "update_rigol_levels"]
    assert all(a.payload["high_level_v"] == a.payload["low_level_v"] for a in updates)
    finish = [a for a in plan.actions if a.source_node_id == "rigol-final-zero-off"]
    assert [a.kind for a in finish] == ["update_rigol_levels", "set_rigol_output"]
    assert finish[0].payload["high_level_v"] == finish[0].payload["low_level_v"] == 0
    assert finish[1].payload["enabled"] is False


@pytest.mark.parametrize("mutation", [
    "missing-baseline", "wrong-unit", "over-limit", "rounds-into-limit",
    "invalid-final", "dc-frequency", "dc-high-level", "output-path-before-carrier",
    "explicit-dc-frequency",
])
def test_dc_recipe_rejects_invalid_configuration_before_execution(settings, mutation):
    raw = yaml.safe_load(RECIPE.read_text(encoding="utf-8"))
    if mutation == "missing-baseline":
        raw["root"]["children"] = [n for n in raw["root"]["children"] if n["type"] != "configure_rigol"]
    elif mutation == "output-path-before-carrier":
        children = raw["root"]["children"]
        index = next(i for i, n in enumerate(children) if n["type"] == "configure_rigol")
        children[index], children[index + 1] = children[index + 1], children[index]
    elif mutation == "explicit-dc-frequency":
        raw["root"]["children"].append({"id": "invalid-frequency", "type": "update_rigol_frequency",
                                          "channel": 1, "frequency": "2 kHz"})
    elif mutation in {"wrong-unit", "over-limit", "rounds-into-limit", "dc-frequency", "dc-high-level"}:
        sweep = next(n for n in raw["root"]["children"] if n["type"] == "sweep")
        if mutation == "dc-frequency":
            sweep["target"] = "rigol.1.frequency"
            sweep["segments"] = [{"start": "1 kHz", "stop": "2 kHz", "points": 2}]
        elif mutation == "dc-high-level":
            sweep["target"] = "rigol.1.high_level"
        else:
            sweep["segments"][0]["stop"] = {
                "wrong-unit": "600 mA", "over-limit": "1001 mV", "rounds-into-limit": "1000.01 mV",
            }[mutation]
    else:
        final = next(n for n in raw["finally"] if n.get("device") == "rigol")
        final["high_level"] = final["low_level"] = "1001 mV"
    with pytest.raises((ConfigurationError, SafetyViolation, ValueError)):
        compile_mapping(settings, raw)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_rigol_initial_axis_and_final_settings_are_reviewable(settings, theme):
    from PySide6.QtWidgets import QApplication

    from app.recipes.parameter_overview import build_sweep_parameter_entries
    from app.ui.design_system import apply_application_theme
    from app.ui.recipes.parameter_overview_dialog import SweepParameterOverviewDialog

    app = QApplication.instance() or QApplication([])
    apply_application_theme(app, theme)
    recipe = parse_recipe_text(RECIPE.read_text(encoding="utf-8"))
    entries = build_sweep_parameter_entries(recipe, lambda device, channel: (
        settings.rigol.safety.channels[str(channel)].defaults if device == "rigol" else {}
    ))
    rows = {(entry.source_id, entry.parameter): entry for entry in entries}
    assert rows[("rigol-dc-initial", "waveform")].sweep_value == "DC"
    assert rows[("rigol-dc-initial", "high_level")].sweep_value == "0 mV"
    assert rows[("rigol-dc-initial", "low_level")].sweep_value == "0 mV"
    axis = rows[("rigol-dc-voltage-sweep", "offset")]
    assert axis.action == "sweep" and "41 points" in axis.detail
    assert rows[("rigol-final-zero-off", "output_enabled")].sweep_value is False
    dialog = SweepParameterOverviewDialog(recipe.name, entries)
    try:
        dialog.resize(1360, 900)
        dialog.show()
        dialog.device_filter.setCurrentIndex(dialog.device_filter.findData("rigol"))
        app.processEvents()
        assert dialog.tree.isVisible() and dialog.tree.width() > 600
        assert dialog.tree.height() > 200
        artifacts = Path("artifacts/rigol-dc-sweep-review")
        artifacts.mkdir(parents=True, exist_ok=True)
        assert dialog.grab().save(str(artifacts / f"rigol-review-{theme}.png"))
        for source_id in ("rigol-dc-initial", "rigol-dc-voltage-sweep", "rigol-final-zero-off"):
            dialog.search.setText(source_id)
            app.processEvents()
            assert any(not item.isHidden() and entry.source_id == source_id
                       for item, entry, _ in dialog._rows)
            assert dialog.grab().save(str(artifacts / f"{source_id}-{theme}.png"))
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_full_simulated_dc_sweep_and_storage(settings, tmp_path, monkeypatch):
    from app.devices.simulators import simulated_station_settings
    from tests.test_sweep_audit_contracts import execute

    simulated = simulated_station_settings(settings)
    raw = yaml.safe_load(RECIPE.read_text(encoding="utf-8"))
    # Only the simulated copy skips wall-clock reference collection; the
    # production source and its 30 s floor are covered by the equality test.
    for node in raw["root"]["children"]:
        if "minimum_duration" in node:
            node["minimum_duration"] = "0 s"
    plan = compile_mapping(simulated, deepcopy(raw))
    result, waits = execute(simulated, plan, tmp_path / "rigol-dc.h5", monkeypatch)
    assert result.error is None, result.error
    assert result.stored_points == 41
    assert waits.count(5.) == 42 and waits.count(1.) == 41
    from app.storage.hdf5_reader import Hdf5RunReader
    from app.storage.thatec_validator import ThatecCompatibilityValidator

    points = Hdf5RunReader.points(tmp_path / "rigol-dc.h5")
    assert len(points) == 41
    assert [point.setpoints["rigol.1.offset"] for point in points] == pytest.approx(
        [i * .015 for i in range(41)]
    )
    assert ThatecCompatibilityValidator().validate(tmp_path / "rigol-dc.h5", require_pythat=True).valid


@pytest.mark.parametrize("outcome", ["success", "stop", "fault"])
def test_dc_final_states_are_confirmed_and_faults_skip_moke_hold(settings, monkeypatch, outcome):
    from types import SimpleNamespace

    from app.bootstrap import StationComposition
    from app.devices.simulators import simulated_station_settings
    from app.domain.models import ApplicationState, DeviceState
    from app.engine.runner import RecipeRunner
    from tests.test_adapters_and_runner import MemoryWriter

    simulated = simulated_station_settings(settings)
    raw = yaml.safe_load(RECIPE.read_text(encoding="utf-8"))
    # A short electrical sequence uses the exact authored initial/final states
    # and voltage axis; the separate archive test exercises all acquisitions.
    raw["root"]["children"] = [
        n for n in raw["root"]["children"]
        if n["type"] not in {"acquire_reference", "wait"}
    ]
    sweep = next(n for n in raw["root"]["children"] if n["type"] == "sweep")
    sweep["children"] = [{"id": "probe", "type": "checkpoint"}]
    plan = compile_mapping(simulated, raw)
    composition = StationComposition(simulated, simulation=True)
    devices = {name: composition.create_adapter(name) for name in ("rigol", "keithley", "moke_box")}
    for device in devices.values():
        device.connect()
    probes = 0
    runner = None

    def event(name, data):
        nonlocal probes
        if name == "action_started" and data.get("node_id") == "probe":
            probes += 1
            if probes == 3:
                if outcome == "stop":
                    runner.request_stop()
                elif outcome == "fault":
                    raise OSError("Injected measurement failure")

    writer = MemoryWriter()
    runner = RecipeRunner(**devices, anritsu=SimpleNamespace(state=DeviceState.OUTPUT_OFF),
                          writer=writer, on_event=event)
    monkeypatch.setattr(runner, "_interruptible_wait", lambda _: None)
    try:
        result = runner.run(plan)
        assert probes >= 3, result.error
        for channel in (1, 2):
            assert devices["rigol"]._require_session().query(f":OUTP{channel}?") in {"0", "OFF"}
        assert devices["keithley"].read_output_states() == {"A": False, "B": False}
        if outcome == "success":
            assert result.error is None, result.error
            assert result.state is ApplicationState.HOLDING
            applied = devices["rigol"].last_channel_config(1)
            assert applied.high_level_v == applied.low_level_v == 0
            assert devices["moke_box"].read_vouts()[0] == pytest.approx(-.014, abs=.00031)
        else:
            assert writer.status == ("aborted" if outcome == "stop" else "faulted")
            assert result.stored_points < 41
            assert devices["moke_box"].read_vouts()[0] == pytest.approx(0, abs=.00031)
    finally:
        for device in devices.values():
            device.disconnect()
