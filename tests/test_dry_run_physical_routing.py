"""Real-adapter routing with injected transports; never contacts hardware."""
from pathlib import Path
import json

import h5py
import pytest

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.keithley_2600 import KeithleyAdapter
from app.devices.rigol_dg1000z import RigolAdapter
from app.devices.simulators import AnritsuSimulator, KeithleySimulator, RigolSimulator, simulated_station_settings
from app.devices.visa import FakeVisaSessionFactory
from app.domain.errors import ExecutionError
from app.domain.models import DeviceState
from app.engine import ExecutionMode, RecipeCompiler, RecipeRunner
from app.engine.compiler import PlanAction
from app.recipes import parse_recipe_text
from app.storage.hdf5_reader import Hdf5RunReader
from app.ui.run_worker import RunWorker
from tests.helpers import loaded_settings
from tests.test_adapters_and_runner import DryRunOutputProbe, MemoryWriter


@pytest.mark.parametrize("state", [DeviceState.UNKNOWN, DeviceState.FAULT, DeviceState.DISCONNECTED, DeviceState.OUTPUT_ON])
def test_dry_run_requires_positive_off_state(state):
    probe = DryRunOutputProbe()
    probe.emergency_off = lambda: setattr(probe, "state", state)
    runner = RecipeRunner(rigol=probe, keithley=DryRunOutputProbe(), anritsu=DryRunOutputProbe(),
                          writer=MemoryWriter(), execution_mode=ExecutionMode.DRY_RUN)
    runner._required_devices = frozenset({"rigol"})
    with pytest.raises(ExecutionError, match="could not confirm all outputs OFF"):
        runner._confirm_dry_run_outputs_off()


def test_explicit_failed_off_confirmation_rejects_stale_safe_state():
    probe = DryRunOutputProbe()
    probe.emergency_off = lambda: False
    runner = RecipeRunner(rigol=probe, keithley=DryRunOutputProbe(), anritsu=DryRunOutputProbe(),
                          writer=MemoryWriter(), execution_mode=ExecutionMode.DRY_RUN)
    runner._required_devices = frozenset({"rigol"})
    with pytest.raises(ExecutionError, match="could not confirm all outputs OFF"):
        runner._confirm_dry_run_outputs_off()


@pytest.mark.parametrize("requested", [False, True])
@pytest.mark.parametrize("actual", [None, True, 0])
def test_dry_run_rejects_missing_or_on_readback_even_for_explicit_off(requested, actual):
    runner = RecipeRunner(rigol=DryRunOutputProbe(), keithley=DryRunOutputProbe(), anritsu=DryRunOutputProbe(),
                          writer=MemoryWriter(), execution_mode=ExecutionMode.DRY_RUN)
    action = PlanAction("off", "set_keithley_output", {"channel": "A", "enabled": requested}, {})
    with pytest.raises(ExecutionError, match="OFF was not confirmed"):
        runner._emit_dry_run_suppression(action, requested, actual)


@pytest.mark.parametrize("kind,payload", [
    ("update_keithley_level", {"channel": "A"}),
    ("update_keithley_compliance", {"channel": "B"}),
    ("update_rigol_frequency", {"channel": 1}),
    ("update_rigol_levels", {"channel": 1, "target": "rigol.1.high_level"}),
    ("configure_anritsu", {"target": "anritsu.spectrum.start_frequency"}),
    ("update_anritsu_sg", {"target": "anritsu.sg.frequency"}),
])
def test_planned_value_without_runtime_evidence_is_not_readback(kind, payload):
    runner = RecipeRunner(rigol=DryRunOutputProbe(), keithley=DryRunOutputProbe(),
                          anritsu=DryRunOutputProbe(), writer=MemoryWriter())
    action = PlanAction("set", kind, {**payload, "requested_si": 1.01, "applied_si": 1.0}, {}, semantic_id="set")
    assert runner._confirmed_semantic_value(action) == (1.0, None)


def test_non_simulated_worker_programs_adapters_and_archives_transport_spectrum(tmp_path, monkeypatch):
    from app.ui import run_worker
    settings = simulated_station_settings(loaded_settings())  # Qualified test profile only.
    source = Path("recipes/keithley_b_rigol_frequency_anritsu_reference_10x100.yml").read_text(encoding="utf-8")
    source = source.replace("      points: 10\n", "      points: 2\n").replace("          points: 100\n", "          points: 2\n")
    source = source.replace("150 mA", "1 mA")
    recipe = parse_recipe_text(source)
    plan = RecipeCompiler(settings, outputs_forced_off=True).compile(recipe)
    class TransportSpectrum(AnritsuSimulator):
        def _query(self, command):
            if command.startswith("TRAC? "):
                return ",".join(["-57.25"] * self.points)
            return super()._query(command)
    sessions = {"rigol": RigolSimulator(), "keithley": KeithleySimulator(), "anritsu": TransportSpectrum()}
    sessions["rigol"].output.update({1: True, 2: True})
    sessions["keithley"].output.update({"smua": True, "smub": True})
    constructed = []
    def factory(adapter, name):
        def create(profile, *, session_factory=None):
            assert session_factory is None, "Dry run unexpectedly selected a simulation factory"
            constructed.append(name)
            return adapter(profile, session_factory=FakeVisaSessionFactory(sessions[name]))
        return create
    monkeypatch.setattr(run_worker, "RigolAdapter", factory(RigolAdapter, "rigol"))
    monkeypatch.setattr(run_worker, "KeithleyAdapter", factory(KeithleyAdapter, "keithley"))
    monkeypatch.setattr(run_worker, "AnritsuAdapter", factory(AnritsuAdapter, "anritsu"))
    def forbidden(*args, **kwargs):
        raise AssertionError("Dry run must not activate simulation")
    monkeypatch.setattr(run_worker, "SimulatedVisaFactory", forbidden)
    worker = RunWorker(settings, tmp_path / "settings.yml", plan, simulation=False,
                       execution_mode="dry_run", output_dir_override=str(tmp_path), file_stem_override="physical-route")
    errors, results = [], []
    worker.failed.connect(errors.append)
    worker.finished.connect(results.append)
    worker.run()
    assert not errors, errors
    assert len(results) == 1
    assert set(constructed) == set(sessions)
    assert any(command.startswith(":SOUR1:FREQ ") for command in sessions["rigol"].commands)
    assert any("smub.source.leveli =" in command for command in sessions["keithley"].commands)
    assert not any(command.upper() in {":OUTP1 ON", ":OUTP2 ON"} for command in sessions["rigol"].commands)
    assert not any("OUTPUT_ON" in command for command in sessions["keithley"].commands)
    assert not any(command.upper() in {"OUTP 1", "OUTP ON"} for command in sessions["anritsu"].commands)
    assert not any(command.upper().startswith(("OUTP", "INST SG")) for command in sessions["anritsu"].commands)
    assert not any(sessions["rigol"].output.values())
    assert not any(sessions["keithley"].output.values())
    target, = tmp_path.glob("*.h5")
    detail = Hdf5RunReader.detail(target)
    assert detail.simulation_metadata["enabled"] is False
    assert detail.simulation_metadata["execution_mode"] == "dry_run"
    with h5py.File(target, "r") as file:
        confirmations = [
            json.loads(message)
            for name, message in zip(file["events/name"].asstr()[:], file["events/message"].asstr()[:])
            if name == "semantic_operation_applied"
        ]
        assert confirmations
        assert all(data["verification"] != "simulated_ack" for data in confirmations)
        setpoints = [data for data in confirmations if data.get("kind") in {"update_rigol_frequency", "update_keithley_level"}]
        assert setpoints and all(data["verification"] == "readback" for data in setpoints)
        assert len(file["spectra"]) == 4
        assert (file["reference/power_dbm"][:] == -57.25).all()
        for i in range(4):
            assert (file[f"spectra/{i}/power_dbm"][:] == -57.25).all()
