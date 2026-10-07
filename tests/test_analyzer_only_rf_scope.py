"""Spectrum-only runs must never select SG or send RF-output commands."""
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import Mock

import h5py
import pytest

from app.devices.anritsu_ms2830a import AnritsuAdapter, SignalGeneratorConfig
from app.devices.simulators import AnritsuSimulator
from app.devices.visa import FakeVisaSession, FakeVisaSessionFactory
from app.domain.errors import DeviceError
from app.domain.models import DeviceState
from app.engine.compiler import RecipeCompiler, controlled_output_endpoints
from app.recipes import parse_recipe_text
from app.ui.run_worker import RunWorker
from tests.helpers import simulation_settings


class SpectrumOnlyTransport(AnritsuSimulator):
    def write(self, command):
        assert not command.upper().startswith(("OUTP", "INST SG")), command
        return super().write(command)

    def query(self, command):
        assert not command.upper().startswith("OUTP"), command
        return super().query(command)


@pytest.mark.parametrize("mode", ["measurement", "dry_run"])
@pytest.mark.parametrize("legacy_manifest", [False, True])
def test_spectrum_only_full_worker_without_rf_commands(tmp_path, mode, legacy_manifest, monkeypatch):
    settings = simulation_settings()
    recipe = parse_recipe_text("""schema_version: 1
name: analyzer only RF isolation
root:
  id: root
  type: sequence
  children:
    - {id: reference, type: acquire_reference, average_count: 2}
    - {id: spectrum, type: acquire_spectrum}
""")
    plan = RecipeCompiler(settings, outputs_forced_off=mode == "dry_run").compile(recipe)
    assert controlled_output_endpoints(plan.actions) == frozenset()
    assert plan.safe_shutdown_actions == ("storage.flush_checkpoint",)
    if legacy_manifest:
        plan = replace(plan, safe_shutdown_actions=("anritsu.rf_off_and_abort", "storage.flush_checkpoint"))
    session = SpectrumOnlyTransport()
    adapter = AnritsuAdapter(settings, session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    assert adapter.capabilities.supports("signal_generator")
    release = Mock()
    monkeypatch.setattr(adapter, "release", release, raising=False)
    worker = RunWorker(settings, tmp_path / "settings.yml", plan, simulation=False,
                       execution_mode=mode, output_dir_override=str(tmp_path),
                       device_controllers={"anritsu": SimpleNamespace(acquire_run_lease=lambda: adapter)})
    errors, finished = [], []
    worker.failed.connect(errors.append)
    worker.finished.connect(finished.append)
    worker.run()
    assert not errors, errors
    assert len(finished) == 1
    release.assert_called_once()
    assert "ABOR" not in session.commands
    assert "ABORT" not in session.commands
    assert not session.continuous_sweep
    assert adapter.state == DeviceState.DISCONNECTED
    path, = tmp_path.glob("*.h5")
    with h5py.File(path) as file:
        assert file["run"].attrs["status"] == "completed"
        assert len(file["spectra"]) == 1
        assert len(file["references"]) == 1


def test_abort_failure_does_not_report_verified():
    class BrokenAbort(SpectrumOnlyTransport):
        def write(self, command):
            if command == "ABOR":
                raise OSError("abort transport failed")
            return super().write(command)
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(BrokenAbort()))
    adapter.connect()
    with pytest.raises(OSError, match="abort transport failed"):
        adapter.abort_acquisition()
    assert adapter.state == DeviceState.UNKNOWN
    adapter.disconnect()


def test_explicit_sg_configuration_failure_still_requires_confirmed_rf_shutdown():
    settings = simulation_settings()
    raw = settings.model_dump(mode="python")
    raw["devices"]["anritsu"]["signal_generator"] = {
        "control_protocol": "basic_scpi",
        "frequency": {"min": "250 kHz", "max": "3.6 GHz"},
        "power": {"min": "-100 dBm", "max": "0 dBm"},
    }
    session = FakeVisaSession(responses={
        "*IDN?": "ANRITSU,MS2830A,123456,1.0", "SYST:HARD:OPT:CAT?": "1,020,ON,Option 020", "OUTP?": "1", "INIT:SWP?": "0",
    })
    adapter = AnritsuAdapter(type(settings).model_validate(raw), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    with pytest.raises(DeviceError, match="did not confirm RF OUTPUT OFF"):
        adapter.configure_signal_generator(SignalGeneratorConfig(1e9, -20.0))
    with pytest.raises(DeviceError, match="Cannot disconnect"):
        adapter.disconnect()
    assert adapter.connected
    assert adapter.state == DeviceState.UNKNOWN
    session.responses["OUTP?"] = "0"
    adapter.disconnect()
    assert adapter.state == DeviceState.DISCONNECTED


@pytest.mark.parametrize("response", ["1", "invalid"])
def test_abort_requires_fresh_stopped_readback(response):
    session = SpectrumOnlyTransport()
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    original_query = session.query
    session.query = lambda command: response if command == "INIT:SWP?" else original_query(command)
    with pytest.raises(DeviceError, match="did not confirm sweep stopped"):
        adapter.abort_acquisition()
    assert adapter.state == DeviceState.UNKNOWN
    assert session.commands[-1] == "ABOR"
    adapter.disconnect()


def test_explicit_abort_uses_documented_short_command_without_rf_control():
    session = SpectrumOnlyTransport()
    adapter = AnritsuAdapter(simulation_settings(), session_factory=FakeVisaSessionFactory(session))
    adapter.connect()
    assert adapter.abort_acquisition() is True
    assert session.commands[-2:] == ["ABOR", "INIT:SWP?"]
    assert adapter.state == DeviceState.VERIFIED
    adapter.disconnect()
