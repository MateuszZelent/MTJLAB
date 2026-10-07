"""Configuration evidence must survive the production owning-thread proxy."""
import threading
from types import SimpleNamespace

import pytest
import h5py
from PySide6.QtWidgets import QApplication

from app.devices.anritsu_ms2830a.adapter import AnritsuAdapter
from app.devices.simulators import SimulatedVisaFactory
from app.bootstrap import StationComposition
from app.engine.runner import RecipeRunner
from app.engine.compiler import RecipeCompiler
from app.recipes import parse_recipe_text
from app.storage.hdf5_writer import Hdf5RunWriter
from app.ui.workers import DeviceController
from tests.helpers import simulation_settings


@pytest.fixture
def analyzer_proxy():
    application = QApplication.instance() or QApplication([])
    adapter = AnritsuAdapter(simulation_settings(), session_factory=SimulatedVisaFactory("anritsu"))
    controller = DeviceController(adapter)
    proxy = controller.acquire_run_lease()
    proxy.connect()
    try:
        yield adapter, proxy
    finally:
        proxy.release()
        controller.close()
        application.processEvents()


def test_proxy_returns_fingerprint_and_reads_only_on_owner_thread(analyzer_proxy):
    adapter, proxy = analyzer_proxy
    threads = []
    read = adapter.read_full_configuration

    def recorded_read():
        threads.append(threading.get_ident())
        return read()

    adapter.read_full_configuration = recorded_read
    runner = SimpleNamespace(_anritsu=proxy)
    first = RecipeRunner._read_spectrum_identity(runner)
    second = RecipeRunner._read_spectrum_identity(runner)
    assert first is not None and first[3] == second[3]
    assert first[2] == proxy.identity.idn
    assert len(threads) == 2
    assert all(thread != threading.get_ident() for thread in threads)


@pytest.mark.parametrize("method", ["read_full_configuration", "read_acquisition_configuration"])
def test_proxy_readback_failure_is_not_an_unqualified_success(analyzer_proxy, method):
    adapter, proxy = analyzer_proxy

    def fail():
        raise RuntimeError("configuration readback failed")

    setattr(adapter, method, fail)
    with pytest.raises(RuntimeError, match="configuration readback failed"):
        RecipeRunner._read_spectrum_identity(SimpleNamespace(_anritsu=proxy))


def test_missing_readback_contract_fails_closed():
    with pytest.raises(AttributeError):
        RecipeRunner._read_spectrum_identity(SimpleNamespace(_anritsu=object()))


def test_proxy_acquisition_persists_qualified_configuration(analyzer_proxy, tmp_path):
    import json

    _adapter, proxy = analyzer_proxy
    recipe = parse_recipe_text("""schema_version: 1
name: proxy spectrum qualification
root:
  id: root
  type: sequence
  children:
    - {id: setup, type: configure_anritsu, start_frequency: '1 MHz', stop_frequency: '2 MHz', reference_level: '0 dBm', points: 101}
    - {id: spectrum, type: acquire_spectrum, average_count: 1}
""")
    plan = RecipeCompiler(simulation_settings()).compile(recipe)
    path = tmp_path / "proxy.h5"
    writer = Hdf5RunWriter(path, recipe_source=recipe.source_text,
                           settings_source="simulation: true", plan_hash=plan.sha256,
                           device_idn={"anritsu": proxy.identity.idn})
    station = StationComposition(simulation_settings(), simulation=True)
    result = RecipeRunner(rigol=station.create_adapter("rigol"),
                          keithley=station.create_adapter("keithley"),
                          anritsu=proxy, writer=writer).run(plan)
    assert result.error is None, result.error
    assert result.stored_points == 1
    with h5py.File(path) as file:
        states = json.loads(file["points/0/device_states_json"].asstr()[()])
        spectrum_state = states["anritsu"]
        assert spectrum_state["spectrum"]["actual"]["start_hz"] == 1e6
        assert spectrum_state["advanced_spectrum"]["actual"]["rbw_hz"] > 0
