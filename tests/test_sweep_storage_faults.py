"""Fault-injection contracts for durable checkpoint accounting."""

import h5py
import pytest

from app.domain.errors import ExecutionError
from app.domain.models import MeasurementPoint
from app.storage.hdf5_writer import Hdf5RunWriter


@pytest.mark.parametrize("failed_flush", [1, 2, 3])
def test_raw_source_flush_failure_rolls_back_identity_and_retains_primary_error(tmp_path, monkeypatch, failed_flush):
    from app.domain.recipe_spectrum import RecipeSpectrumSweep
    from app.domain.spectrum_correction import SpectrumFrameRole, SweepEvidence
    from app.storage.hdf5_reader import iter_recipe_spectrum_sweeps

    writer = writer_for(tmp_path / f"raw-fault-{failed_flush}.h5")
    writer.append(MeasurementPoint(0, {}, {"lakeshore.field_t": .001}))
    record = RecipeSpectrumSweep((1., 2.), (-60., -61.), "signal", "attempt",
        SpectrumFrameRole.SIGNAL, 0, 1, 1., 0, SweepEvidence.QUALIFIED_SINGLE_SWEEP)
    try:
        with monkeypatch.context() as scoped:
            original = h5py.File.flush
            calls = 0
            def flush(file):
                nonlocal calls
                calls += 1
                if calls == failed_flush:
                    raise OSError("raw primary flush failure")
                return original(file)
            scoped.setattr(h5py.File, "flush", flush)
            with pytest.raises(ExecutionError, match="raw primary flush failure") as failure:
                writer.store_recipe_spectrum_sweep(record)
            assert isinstance(failure.value.__cause__, OSError)
        assert writer.point_count == 1 and not len(writer._file["recipe_raw_sweeps_v1"])
        assert writer.store_recipe_spectrum_sweep(record) == 0
    finally:
        writer.close("faulted")
    assert len(tuple(iter_recipe_spectrum_sweeps(writer.path))) == 1


def writer_for(path):
    return Hdf5RunWriter(path, recipe_source="", settings_source="", plan_hash="faults", device_idn={})


@pytest.mark.parametrize("failure", ["pending_flush", "commit_flush", "link_move", "public_append"])
def test_checkpoint_failure_reconciles_private_public_and_memory_counts(tmp_path, monkeypatch, failure):
    writer = writer_for(tmp_path / f"{failure}.h5")
    point = MeasurementPoint(index=0, setpoints={}, measurements={"lakeshore.field_t": .001})
    try:
        with monkeypatch.context() as scoped:
            if failure.endswith("flush"):
                original = h5py.File.flush
                calls = 0
                def flush(file):
                    nonlocal calls
                    calls += 1
                    if calls == (1 if failure == "pending_flush" else 2):
                        raise OSError(failure)
                    return original(file)
                scoped.setattr(h5py.File, "flush", flush)
            elif failure == "link_move":
                def move(*_args, **_kwargs):
                    raise OSError(failure)
                scoped.setattr(h5py.File, "move", move)
            else:
                original = writer._thatec.append
                def append(*args, **kwargs):
                    original(*args, **kwargs)
                    raise OSError(failure)
                scoped.setattr(writer._thatec, "append", append)
            with pytest.raises(ExecutionError, match=failure):
                writer.append(point)
        assert writer.point_count == writer._thatec._checkpoint_count == 0
        assert len(writer._points) == len(writer._spectra) == len(writer._pending) == 0
        # A fully rolled-back transient failure can retry the same identity.
        assert writer.append(point) == 0
        assert writer.point_count == writer._thatec._checkpoint_count == 1
    finally:
        writer.close("faulted")


def test_rollback_failure_preserves_origin_and_blocks_further_appends(tmp_path, monkeypatch):
    writer = writer_for(tmp_path / "rollback.h5")
    point = MeasurementPoint(index=0, setpoints={}, measurements={})
    try:
        with monkeypatch.context() as scoped:
            def primary(*_args, **_kwargs):
                raise OSError("original append fault")
            def secondary(*_args, **_kwargs):
                raise OSError("rollback fault")
            scoped.setattr(writer._thatec, "append", primary)
            scoped.setattr(writer._thatec, "rollback_last", secondary)
            with pytest.raises(ExecutionError, match="original append fault.*rollback fault") as caught:
                writer.append(point)
        assert isinstance(caught.value.__cause__, OSError)
        assert str(caught.value.__cause__) == "original append fault"
        assert writer.point_count == 0 and len(writer._points) == len(writer._pending) == 0
        with pytest.raises(ExecutionError, match="close and recover"):
            writer.append(point)
    finally:
        with pytest.raises(ExecutionError, match="rollback failed"):
            writer.close("faulted")
        assert not writer._file.id.valid


def test_csv_and_warning_log_failures_do_not_undo_a_committed_hdf5_point(tmp_path, monkeypatch):
    writer = writer_for(tmp_path / "csv.h5")
    try:
        def fault(*_args, **_kwargs):
            raise OSError("secondary export failure")
        monkeypatch.setattr(writer, "_append_csv_summary", fault)
        monkeypatch.setattr(writer, "append_event", fault)
        assert writer.append(MeasurementPoint(index=0, setpoints={}, measurements={})) == 0
        assert writer.point_count == 1 and writer._points["0"].attrs["complete"]
        assert writer._file["run"].attrs["csv_export_error"] == "secondary export failure"
    finally:
        writer.close("completed")


def test_runtime_disk_exhaustion_is_rejected_before_creating_pending_point(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.domain.errors import ConfigurationError

    writer = writer_for(tmp_path / "space.h5")
    try:
        monkeypatch.setattr("app.engine.estimation.shutil.disk_usage", lambda _path: SimpleNamespace(free=0))
        with pytest.raises(ConfigurationError, match="Insufficient free space"):
            writer.append(MeasurementPoint(index=0, setpoints={}, measurements={}))
        assert writer.point_count == 0 and len(writer._points) == len(writer._pending) == 0
    finally:
        writer.close("faulted")


def test_eager_pythat_import_rejects_large_logical_arrays_before_loading(tmp_path, monkeypatch):
    from app.storage.pythat_bridge import open_measurement_tree
    path = tmp_path / "sparse-large.h5"
    with h5py.File(path, "w") as file:
        file.create_dataset("measurement/row_00/data", shape=(100_000, 10_001), chunks=(1, 10_001), dtype="f8")
    monkeypatch.setattr("app.storage.pythat_bridge.version", lambda _name: pytest.fail("PyThat must not be loaded"))
    with pytest.raises(ExecutionError, match="memory budget"):
        open_measurement_tree(path)


def test_public_import_accounts_for_available_ram_and_exact_budget(monkeypatch):
    from app.storage.resource_budget import require_public_import_capacity
    monkeypatch.setattr("app.storage.resource_budget.available_physical_memory_bytes", lambda: 100)
    with pytest.raises(ExecutionError, match="memory budget"):
        require_public_import_capacity(51, budget_bytes=1000)
    require_public_import_capacity(50, budget_bytes=1000)
    with pytest.raises(ExecutionError, match="exact byte count"):
        require_public_import_capacity(1, budget_bytes=True)
