"""Real process exit at HDF5 boundaries, without a destructor or clean close."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import subprocess
import sys

import h5py
import pytest

from app.domain.errors import ExecutionError
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.hdf5_reader import Hdf5RunReader
from app.storage.thatec_validator import ThatecCompatibilityValidator
from app.storage.thatec_reader import ThatecRunReader
from app.storage.pythat_bridge import open_measurement_tree
from tests.test_spectrum_correction_store import run_writer


CHILD = r'''
import os
import sys
from app.domain.models import MeasurementPoint
from tests.test_spectrum_correction_store import run_writer, fixture_profile, signal_fixture

path, phase = sys.argv[1:]
ctx, bg = fixture_profile()
raw, envelope, result = signal_fixture(ctx, bg)
writer = run_writer(path)
writer.store_background_profile(ctx, bg)
def append(index):
    writer.append(MeasurementPoint(index, {}, {}), raw,
                  acquisition_envelope=envelope, corrected_frame=result)
append(0)
if phase == "closed":
    writer.close("aborted")
    sys.exit(0)
if phase == "committed":
    os._exit(73)
if phase in {"public", "public_written"}:
    original_append = writer._thatec.append
    def crash_public(*args, **kwargs):
        if phase == "public_written":
            original_append(*args, **kwargs)
        writer._file.flush()
        os._exit(73)
    writer._thatec.append = crash_public
original_move = writer._file.move
def crash_move(source, destination):
    if source.startswith("_pending/"):
        if phase == "exposed":
            original_move(source, destination)
        if phase not in {"public", "public_written"}:
            writer._file.flush()
            os._exit(73)
    return original_move(source, destination)
writer._file.move = crash_move
append(1)
raise RuntimeError("Crash boundary was not reached")
'''


@pytest.mark.parametrize("phase", ["closed", "committed", "pending", "exposed", "public", "public_written"])
def test_real_exit_preserves_source_and_reports_openability(tmp_path, phase):
    path = tmp_path / f"{phase}.h5"
    process = subprocess.run(
        [sys.executable, "-c", CHILD, str(path), phase],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=45,
    )
    assert process.returncode == (0 if phase == "closed" else 73), process.stderr
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    observation = {"phase": phase, "exit_code": process.returncode,
                   "hdf5_version": h5py.version.hdf5_version, "source_sha256": before}
    try:
        with h5py.File(path, "r") as file:
            observation["openable"] = True
            assert bool(file["points/0"].attrs["complete"])
            assert "spectrum_processing_v1" in file
            if phase == "pending":
                assert "1" in file["_pending"] and "1" not in file["points"]
            if phase in {"exposed", "public", "public_written"}:
                assert not bool(file["points/1"].attrs["complete"])
            if phase == "closed":
                assert file["run"].attrs["status"] == "aborted"
    except OSError as exc:
        assert phase != "closed"
        observation.update(openable=False, error=str(exc))
        with pytest.raises(ExecutionError, match="Cannot open the run file") as failure:
            Hdf5RunWriter.resume(
                path, recipe_source="schema_version: 1\nname: Correction fixture\nsteps: []\n",
                settings_source="fixture: true\n", plan_hash="correction-fixture", checkpoint_count=1,
            )
        assert isinstance(failure.value.__cause__, OSError)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    if observation["openable"]:
        if phase != "closed":
            with pytest.raises(ExecutionError, match="closed archive with committed checkpoints"):
                open_measurement_tree(path)
            assert not path.with_suffix(".nc").exists()
        summary = Hdf5RunReader.summary(path)
        assert summary.point_count == summary.spectrum_count == 1
        assert [point.index for point in Hdf5RunReader.points(path)] == [0]
        assert Hdf5RunReader.spectrum_point_count(path, 0) == 3
        assert Hdf5RunReader.spectrum(path, 0) is not None
        if phase in {"public", "public_written"}:
            for read in (Hdf5RunReader.spectrum, Hdf5RunReader.spectrum_point_count,
                         Hdf5RunReader.spectrum_correction, Hdf5RunReader.spectrum_acquisition):
                with pytest.raises(ExecutionError, match="uncommitted checkpoint"):
                    read(path, 1)
        public_rows = ThatecRunReader.describe(path).rows
        measured = [row for row in public_rows.values()
                    if dict(row.definition).get("lab control role") == "spectrum"]
        assert len(measured) == 1
        assert measured[0].shape == (1, 3)
        assert ThatecRunReader.spectrum_slice(path, measured[0].id, 0) is not None
        with pytest.raises(ExecutionError, match="not committed"):
            ThatecRunReader.row_slice(path, measured[0].id, 1)
        values, times = ThatecRunReader.scalar_series(path, "row_00")
        assert len(values) == len(times) == 1
        recovery_path = tmp_path / f"{phase}-recovery.h5"
        shutil.copyfile(path, recovery_path)
        arguments = dict(
            recipe_source="schema_version: 1\nname: Correction fixture\nsteps: []\n",
            settings_source="fixture: true\n", plan_hash="correction-fixture",
        )
        if phase in {"exposed", "public", "public_written"}:
            with pytest.raises(ExecutionError, match="not marked complete"):
                Hdf5RunWriter.resume(recovery_path, checkpoint_count=2, **arguments)
        recovered = Hdf5RunWriter.resume(recovery_path, checkpoint_count=1, **arguments)
        assert recovered.point_count == 1
        assert not len(recovered._pending)
        assert list(recovered._points) == ["0"]
        recovered.close("aborted")
        assert Hdf5RunReader.spectrum_correction(recovery_path, 0).values_w[1] < 0
        report = ThatecCompatibilityValidator().validate(recovery_path, require_pythat=True)
        assert report.valid, report.errors
        observation["recovered_checkpoint_count"] = 1
        observation["recovered_pythat_valid"] = True
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    artifact = Path("artifacts/spectrum-process-crash")
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / f"{phase}.json").write_text(json.dumps(observation, indent=2), encoding="utf-8")


@pytest.mark.parametrize("private_contract", [False, True])
@pytest.mark.parametrize("role", [None, "axis", "measurement"])
def test_public_scalar_guard_preserves_external_and_axis_semantics(tmp_path, private_contract, role):
    path = tmp_path / "public.h5"
    with h5py.File(path, "x") as file:
        scan = file.create_group("scan_definition")
        definition = [["dimensions", "1"]]
        if role is not None:
            definition.append(["lab control role", role])
        scan.create_dataset("row_00", data=definition, dtype=h5py.string_dtype("utf-8"))
        row = file.create_group("measurement/row_00")
        row.create_dataset("data", data=[1.0, 2.0])
        row.create_dataset("timestamp", data=[10.0, 20.0])
        if private_contract:
            file.create_group("run")
            points = file.create_group("points")
            points.create_group("0").attrs["complete"] = True
            points.create_group("1").attrs["complete"] = False
    expected = 1 if private_contract and role == "measurement" else 2
    assert ThatecRunReader.row(path, "row_00").shape == (expected,)
    values, timestamps = ThatecRunReader.scalar_series(path, "row_00")
    assert len(values) == len(timestamps) == expected
    if expected == 1:
        with pytest.raises(ExecutionError, match="not committed"):
            ThatecRunReader.row_slice(path, "row_00", 1)
    else:
        assert ThatecRunReader.row_slice(path, "row_00", 1).values[0] == 2.0


def test_pythat_import_preserves_existing_sidecar(tmp_path):
    path = tmp_path / "closed.h5"
    writer = run_writer(path)
    writer.close("aborted")
    sidecar = path.with_suffix(".nc")
    sidecar.write_bytes(b"existing scientific data")
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    tree = open_measurement_tree(path)
    assert tree.path == path.resolve()
    assert not tree.save_path.exists()
    assert sidecar.read_bytes() == b"existing scientific data"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source_hash


def test_incomplete_close_does_not_report_successful_pythat_validation(tmp_path):
    path = tmp_path / "incomplete-close.h5"
    writer = run_writer(path)
    with pytest.raises(ExecutionError, match="Final HDF5 contract validation failed"):
        writer.close("incomplete")
    with h5py.File(path, "r") as file:
        assert file["run"].attrs["status"] == "faulted"


def test_parallel_pythat_processes_use_distinct_temporary_files(tmp_path):
    path = tmp_path / "parallel.h5"
    writer = run_writer(path)
    writer.close("aborted")
    sidecar = path.with_suffix(".nc")
    sidecar.write_bytes(b"preserve existing netCDF")
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    script = (
        "import json,sys; from app.storage.pythat_bridge import open_measurement_tree; "
        "tree=open_measurement_tree(sys.argv[1]); "
        "print(json.dumps({'temporary':str(tree.save_path),'sizes':dict(tree.dataset.sizes)}))"
    )
    processes = []
    try:
        for _ in range(2):
            processes.append(subprocess.Popen(
                [sys.executable, "-c", script, str(path)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=45)
            assert process.returncode == 0, stderr
            outputs.append(json.loads(stdout))
        assert outputs[0]["temporary"] != outputs[1]["temporary"]
        assert outputs[0]["sizes"] == outputs[1]["sizes"]
        assert all(not Path(output["temporary"]).parent.exists() for output in outputs)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
    assert sidecar.read_bytes() == b"preserve existing netCDF"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source_hash


def test_pythat_conversion_failure_closes_source_and_removes_only_temporary_data(tmp_path, monkeypatch):
    from PyThat import MeasurementTree

    path = tmp_path / "conversion-failure.h5"
    writer = run_writer(path)
    writer.close("aborted")
    sidecar = path.with_suffix(".nc")
    sidecar.write_bytes(b"existing netCDF")
    observed = []

    def fail_save(tree):
        observed.append((tree.path.parent, tree.f))
        raise RuntimeError("injected conversion failure")

    monkeypatch.setattr(MeasurementTree, "save_netcdf", fail_save)
    with pytest.raises(ExecutionError, match="injected conversion failure"):
        open_measurement_tree(path)
    assert len(observed) == 1
    directory, source_handle = observed[0]
    assert not source_handle.id.valid
    assert not directory.exists()
    assert sidecar.read_bytes() == b"existing netCDF"
    with h5py.File(path, "r+") as file:
        assert file["run"].attrs["status"] == "aborted"


def test_pythat_threads_restore_xarray_options(tmp_path):
    import xarray as xr

    path = tmp_path / "threads.h5"
    writer = run_writer(path)
    writer.close("aborted")
    previous_engines = tuple(xr.get_options()["netcdf_engine_order"])
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(open_measurement_tree, path) for _ in range(2)]
        trees = [future.result(timeout=45) for future in futures]
    assert tuple(xr.get_options()["netcdf_engine_order"]) == previous_engines
    assert trees[0].save_path != trees[1].save_path
    assert all(not tree.save_path.parent.exists() for tree in trees)
    assert not path.with_suffix(".nc").exists()
