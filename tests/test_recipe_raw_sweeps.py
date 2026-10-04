"""Raw recipe blocks survive averaging and storage faults, using simulated VISA."""

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from app.devices.anritsu_ms2830a import AnritsuAdapter
from app.devices.keithley_2600 import KeithleyAdapter
from app.devices.rigol_dg1000z import RigolAdapter
from app.devices.simulators import SimulatedVisaFactory
from app.domain.errors import ExecutionError
from app.domain.spectrum_correction import SpectrumFrameRole, SweepEvidence
from app.engine import RecipeCompiler, RecipeRunner
from app.recipes import parse_recipe_text
from app.storage.hdf5_reader import iter_recipe_spectrum_sweeps
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.helpers import simulation_settings


SOURCE = """schema_version: 1
name: complete-source-blocks
root:
  id: root
  type: sequence
  children:
    - id: analyzer
      type: configure_anritsu
      start_frequency: "1 MHz"
      stop_frequency: "2 MHz"
      reference_level: "0 dBm"
      points: 101
    - id: reference-block
      type: acquire_reference
      average_count: 3
    - id: signal-block
      type: acquire_spectrum
      average_count: 2
      reference_operation: difference_db
      store_processed: true
"""


def execute(path, *, failure=False, monkeypatch=None, source=SOURCE, configuration_change=False,
            validation_failure=False, events=None):
    settings = simulation_settings()
    plan = RecipeCompiler(settings).compile(parse_recipe_text(source))
    anritsu = AnritsuAdapter(settings, session_factory=SimulatedVisaFactory("anritsu"))
    rigol = RigolAdapter(settings, session_factory=SimulatedVisaFactory("rigol"))
    keithley = KeithleyAdapter(settings, session_factory=SimulatedVisaFactory("keithley"))
    for adapter in (anritsu, rigol, keithley):
        adapter.connect()
    writer = Hdf5RunWriter(path, recipe_source=source, settings_source=settings.model_dump_json(),
                          plan_hash=plan.sha256, device_idn={"anritsu": "SIMULATED"})
    acquired = []
    original_acquire = anritsu.acquire_single_sweep

    def capture(name):
        trace = original_acquire(name)
        if configuration_change and len(acquired) == 1:
            trace = replace(trace, configuration_generation=trace.configuration_generation + 1)
        acquired.append(trace)
        return trace

    anritsu.acquire_single_sweep = capture
    if failure:
        original_store = writer.store_recipe_spectrum_sweep

        def fault(record):
            if len(acquired) == 2:
                raise ExecutionError("Injected raw recipe commit failure")
            return original_store(record)

        monkeypatch.setattr(writer, "store_recipe_spectrum_sweep", fault)
    if validation_failure:
        monkeypatch.setattr(ThatecCompatibilityValidator, "validate", lambda *args, **kwargs:
            SimpleNamespace(valid=False, errors=[SimpleNamespace(path="injected", message="validation fault")]))
    try:
        result = RecipeRunner(rigol=rigol, keithley=keithley, anritsu=anritsu, writer=writer,
                              on_event=None if events is None else lambda *args: events.append(args)).run(plan)
        return result, acquired
    finally:
        for adapter in (anritsu, rigol, keithley):
            adapter.disconnect()


def test_reference_and_signal_blocks_preserve_all_raw_and_public_means(tmp_path):
    path = tmp_path / "recipe.h5"
    result, acquired = execute(path)
    assert result.error is None and result.stored_points == 1
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = list(iter_recipe_spectrum_sweeps(path))
    assert len(rows) == len(acquired) == 5
    assert [record.role for _, _, record in rows] == [SpectrumFrameRole.REFERENCE] * 3 + [SpectrumFrameRole.SIGNAL] * 2
    assert [record.average_index for _, _, record in rows] == [0, 1, 2, 0, 1]
    assert [record.average_count for _, _, record in rows] == [3, 3, 3, 2, 2]
    assert len({record.execution_id for _, _, record in rows}) == 1
    for (ordinal, boundary, record), trace in zip(rows, acquired, strict=True):
        assert ordinal in range(5) and boundary == 0
        assert record.evidence == SweepEvidence.QUALIFIED_SINGLE_SWEEP
        assert record.acquired_at_s == trace.acquired_at_utc.timestamp()
        assert record.sweep_id == trace.sweep_id
        np.testing.assert_array_equal(record.frequencies_hz, trace.frequencies_hz)
        np.testing.assert_array_equal(record.powers_dbm, trace.powers_dbm)
        assert not record.powers_dbm.flags.writeable
    with h5py.File(path, "r") as file:
        assert len(file["points"]) == 1 and len(file["references"]) == 1
        assert file["references/0/source_recipe_sweep_indices"][:].tolist() == [0, 1, 2]
        assert len(file["_pending"]) == 0
        assert len(file["recipe_raw_grids_v1"]) == 1
        assert all(file[f"recipe_raw_sweeps_v1/{index}/frequency_hz"].id
                   == file["recipe_raw_sweeps_v1/0/frequency_hz"].id for index in range(5))
        for source, target in ((rows[:3], file["references/0/power_dbm"][:]),
                               (rows[3:], file["spectra/0/power_dbm"][:])):
            mean_w = np.mean([10 ** ((record.powers_dbm - 30) / 10) for _, _, record in source], axis=0)
            np.testing.assert_allclose(target, 10 * np.log10(mean_w) + 30, rtol=0, atol=1e-13)
        import json

        metadata = json.loads(file["points/0/metadata_json"].asstr()[()])
        assert metadata["raw_recipe_sweep_indices"] == [3, 4]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
    from app.engine.estimation import PlanEstimator

    settings = simulation_settings()
    plan = RecipeCompiler(settings).compile(parse_recipe_text(SOURCE))
    estimate = PlanEstimator(settings).estimate(plan)
    assert estimate.spectrum_values == 7 * 101  # Five raw sources plus raw/processed checkpoint.
    assert estimate.uncompressed_hdf5_bytes > path.stat().st_size


def test_storage_fault_stops_block_and_preserves_committed_prefix(tmp_path, monkeypatch):
    path = tmp_path / "fault.h5"
    result, acquired = execute(path, failure=True, monkeypatch=monkeypatch)
    assert "Injected raw recipe commit failure" in result.error
    assert "Archive close failed" in result.error
    assert len(acquired) == 2 and result.stored_points == 0
    rows = list(iter_recipe_spectrum_sweeps(path))
    assert len(rows) == 1 and rows[0][2].average_index == 0
    with h5py.File(path, "r") as file:
        assert file["run"].attrs["status"] == "faulted"
        assert "storage_validation_error" in file["run"].attrs
        assert len(file["points"]) == len(file["references"]) == len(file["_pending"]) == 0


@pytest.mark.parametrize("damage", ["schema", "hash", "unit", "ordinal", "pending"])
def test_reader_rejects_corrupted_committed_sources(tmp_path, damage):
    path = tmp_path / "damaged.h5"
    execute(path)
    with h5py.File(path, "r+") as file:
        root = file["recipe_raw_sweeps_v1"]
        if damage == "schema":
            root.attrs["schema"] = "future-schema"
        elif damage == "hash":
            root["0/power_dbm"][1] += 1
        elif damage == "unit":
            root["0/power_dbm"].attrs["unit"] = "W"
        elif damage == "ordinal":
            del root["1"]
        else:
            root["0"].attrs["complete"] = False
    with pytest.raises(ExecutionError):
        list(iter_recipe_spectrum_sweeps(path))


@pytest.mark.parametrize("value", ["true", "1.5", '"3"'])
def test_compiler_does_not_coerce_boolean_fraction_or_text_sweep_count(value):
    from app.domain.errors import ConfigurationError

    recipe = parse_recipe_text(SOURCE.replace("average_count: 3", f"average_count: {value}"))
    with pytest.raises(ConfigurationError, match="average_count must be an integer"):
        RecipeCompiler(simulation_settings()).compile(recipe)


def test_raw_history_survives_explicit_rollback_of_public_checkpoints(tmp_path):
    source = SOURCE + """    - id: another-signal
      type: acquire_spectrum
      average_count: 2
"""
    path = tmp_path / "rollback.h5"
    result, _ = execute(path, source=source)
    assert result.error is None and result.stored_points == 2
    original = [(i, point, record.powers_dbm.copy()) for i, point, record in iter_recipe_spectrum_sweeps(path)]
    assert original[-1][1] == 1
    # Simulate external confirmation of an earlier retained safe boundary.
    # The production recovery coordinator must still supply that approval.
    with h5py.File(path, "r+") as file:
        file["run"].attrs["status"] = "aborted"
    settings = simulation_settings()
    plan = RecipeCompiler(settings).compile(parse_recipe_text(source))
    writer = Hdf5RunWriter.resume(path, recipe_source=source, settings_source=settings.model_dump_json(),
                                 plan_hash=plan.sha256, checkpoint_count=0)
    try:
        writer.close("aborted")
    except ExecutionError as exc:
        assert "Final HDF5 contract validation failed" in str(exc)
    actual = list(iter_recipe_spectrum_sweeps(path))
    assert len(actual) == len(original) == 7
    for (index, point, powers), (new_index, new_point, record) in zip(original, actual, strict=True):
        assert (index, point) == (new_index, new_point)
        np.testing.assert_array_equal(powers, record.powers_dbm)
    with h5py.File(path, "r") as file:
        assert len(file["points"]) == 0


def test_configuration_change_keeps_raw_but_never_publishes_mixed_mean(tmp_path):
    path = tmp_path / "changed-configuration.h5"
    result, acquired = execute(path, configuration_change=True)
    assert "acquisition configuration changed" in result.error
    assert len(acquired) == 2 and result.stored_points == 0
    rows = list(iter_recipe_spectrum_sweeps(path))
    assert len(rows) == 2
    assert rows[0][2].configuration_generation != rows[1][2].configuration_generation


def test_ui_completion_requires_successful_archive_validation(tmp_path, monkeypatch):
    path = tmp_path / "validation-fault.h5"
    events = []
    result, acquired = execute(path, validation_failure=True, monkeypatch=monkeypatch, events=events)
    assert "validation fault" in result.error and len(acquired) == 5
    assert not any(name == "run_completed" for name, _ in events)
    with h5py.File(path, "r") as file:
        assert file["run"].attrs["status"] == "faulted"
    path = tmp_path / "validated.h5"
    monkeypatch.undo()
    events = []
    result, _ = execute(path, events=events)
    assert result.error is None
    terminal = [payload for name, payload in events if name == "run_completed"]
    assert len(terminal) == 1 and not terminal[0]["storage_validation_pending"]


def test_mismatched_cached_grid_is_rejected_before_reading_its_payload(tmp_path, monkeypatch):
    path = tmp_path / "bad-cache.h5"
    result, _ = execute(path)
    assert result.error is None
    record = list(iter_recipe_spectrum_sweeps(path))[-1][2]
    with h5py.File(path, "r+") as file:
        file["run"].attrs["status"] = "aborted"
        grids = file["recipe_raw_grids_v1"]
        identity = next(iter(grids))
        del grids[identity]  # Existing source hard links retain the original valid grid.
        grid = grids.create_dataset(identity, shape=(1024,), dtype="f8")
        grid.attrs.update(unit="Hz", sha256=identity, complete=True)
    settings = simulation_settings()
    plan = RecipeCompiler(settings).compile(parse_recipe_text(SOURCE))
    writer = Hdf5RunWriter.resume(path, recipe_source=SOURCE, settings_source=settings.model_dump_json(),
                                 plan_hash=plan.sha256, checkpoint_count=1)
    original_getitem = h5py.Dataset.__getitem__

    def guarded(dataset, key, *args, **kwargs):
        if dataset.name.startswith("/recipe_raw_grids_v1/") and dataset.shape == (1024,):
            raise AssertionError("Mismatched grid payload must not be allocated")
        return original_getitem(dataset, key, *args, **kwargs)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(h5py.Dataset, "__getitem__", guarded)
            with pytest.raises(ExecutionError, match="Committed recipe frequency grid differs"):
                writer.store_recipe_spectrum_sweep(record)
        assert len(writer._file["recipe_raw_sweeps_v1"]) == 5 and len(writer._pending) == 0
    finally:
        writer.close("aborted")
    assert len(list(iter_recipe_spectrum_sweeps(path))) == 5
