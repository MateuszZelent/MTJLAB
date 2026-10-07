"""Resume identity mismatches are rejected before any device connection."""
from types import SimpleNamespace
from unittest.mock import Mock
import hashlib

import h5py
import pytest

from app.ui import run_worker
from tests.helpers import simulation_settings
from tests.test_sweep_worker_initialization_faults import plan


@pytest.mark.parametrize("damage", ["settings_sha256", "recipe_source_sha256", "plan_sha256", "completed"])
def test_changed_recovery_identity_never_connects_analyzer(tmp_path, monkeypatch, damage):
    settings = simulation_settings()
    execution_plan = plan(settings)
    path = tmp_path / "resume.h5"
    worker = run_worker.RunWorker(settings, tmp_path / "settings.yml", execution_plan,
        simulation=True, recovery=SimpleNamespace(path=path))
    snapshot = worker._settings_snapshot()
    with h5py.File(path, "w") as file:
        group = file.create_group("run")
        group.attrs["settings_sha256"] = hashlib.sha256(snapshot.encode()).hexdigest()
        group.attrs["recipe_source_sha256"] = hashlib.sha256(execution_plan.recipe_source.encode()).hexdigest()
        group.attrs["plan_sha256"] = execution_plan.sha256
        group.attrs["status"] = "faulted"
        group.attrs["status" if damage == "completed" else damage] = "completed" if damage == "completed" else "changed"
    original = path.read_bytes()
    adapter = SimpleNamespace(connected=False, connect=Mock(side_effect=AssertionError("must not connect")),
                              disconnect=Mock())
    monkeypatch.setattr(run_worker, "AnritsuAdapter", lambda *a, **k: adapter)
    errors = []
    worker.failed.connect(errors.append)
    worker.run()
    assert errors and ("does not match" in errors[0] or "completed run" in errors[0])
    adapter.connect.assert_not_called()
    assert path.read_bytes() == original
