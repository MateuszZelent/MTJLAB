"""Reference-aware resume from a persisted, output-confirmed boundary."""

import h5py

from app.bootstrap import StationComposition
from app.engine.recovery import RunRecoveryManager
from app.engine.runner import RecipeRunner
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.thatec_validator import ThatecCompatibilityValidator
from tests.test_sweep_audit_contracts import SETUP, audit_settings, compile_source


def test_reference_recovery_keeps_original_identity_and_processes_remaining_spectrum(tmp_path):
    settings = audit_settings(tmp_path)
    plan = compile_source(settings, SETUP.splitlines()[0] + "\n" + """    - {id: ref, type: acquire_reference, average_count: 1}
    - {id: first, type: acquire_spectrum, reference_operation: difference_db}
    - {id: second, type: acquire_spectrum, reference_operation: difference_db}
""")
    path = tmp_path / "reference-resume.h5"
    composition = StationComposition(settings, simulation=True)
    devices = {name: composition.create_adapter(name) for name in ("rigol", "keithley", "anritsu")}
    for adapter in devices.values():
        adapter.connect()
    writer = Hdf5RunWriter(path, recipe_source=plan.recipe_source,
        settings_source=settings.model_dump_json(), plan_hash=plan.sha256,
        expected_points=plan.total_points, device_idn={}, simulation_metadata={"enabled": True})
    runner = None

    def stop_at_boundary(name, payload):
        if name == "safe_resume_boundary" and payload["stored_points"] == 1:
            runner.request_stop()

    runner = RecipeRunner(**devices, writer=writer, on_event=stop_at_boundary)
    try:
        result = runner.run(plan)
        assert result.stored_points == 1
        checkpoint = RunRecoveryManager().inspect(path, plan)
        assert checkpoint.stored_points == 1
        assert checkpoint.reference is not None
        assert checkpoint.reference.index == 0
        writer = Hdf5RunWriter.resume(path, recipe_source=plan.recipe_source,
            settings_source=settings.model_dump_json(), plan_hash=plan.sha256,
            checkpoint_count=checkpoint.stored_points, expected_points=plan.total_points)
        result = RecipeRunner(**devices, writer=writer).run(plan,
            start_action_index=checkpoint.next_action_index, stored_points=checkpoint.stored_points,
            recovery_prelude=checkpoint.prelude_actions, recovery_reference=checkpoint.reference)
        assert result.error is None, result.error
        assert result.stored_points == 2
        with h5py.File(path) as file:
            assert len(file["references"]) == 1
            assert file["spectra/1"].attrs["reference_index"] == 0
            assert "processed_values" in file["spectra/1"]
            assert "reference_recovered" in file["events/name"].asstr()[:]
        assert ThatecCompatibilityValidator().validate(path, require_pythat=True).valid
    finally:
        for adapter in devices.values():
            adapter.disconnect()
